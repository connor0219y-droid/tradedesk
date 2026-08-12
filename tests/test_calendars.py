"""Market calendars: the facts every equity coverage number inherits.

These are known-answer tests against the real NYSE calendar. The numbers below are
properties of the exchange, not of the implementation, so a reader can check them
without reading any code: a regular session is 6.5 hours, an early close is 3.5, and
the market was shut on these specific days for these specific reasons.
"""

from __future__ import annotations

from datetime import date

import pytest

from tradedesk.calendars import (
    CalendarError,
    CryptoCalendar,
    EquityCalendar,
    classify,
    expected_bars,
    for_instrument,
)

# Weekdays adjacent to a DST transition. The transition itself falls on a Sunday, so
# the session that matters is the Monday after -- when the UTC offset has changed but
# the session length has not.
DST_ADJACENT = [
    date(2024, 3, 11), date(2024, 11, 4),
    date(2025, 3, 10), date(2025, 11, 3),
    date(2026, 3, 9), date(2026, 11, 2),
]


@pytest.fixture(scope="module")
def cal():
    return EquityCalendar()


@pytest.mark.parametrize("day", DST_ADJACENT)
def test_rth_is_65_hours_on_both_sides_of_a_dst_transition(cal, day):
    """The UTC offset moves; the session length does not.

    If this ever returns 72 or 84 bars, sessions are being built by adding a fixed
    offset to local midnight instead of using real instants -- the same bug class
    `timeutil` exists to prevent, one level up.
    """
    w = cal.window(day)
    assert w.length_ms == int(6.5 * 3600 * 1000)
    assert expected_bars(w, "5m", segment="rth") == 78
    assert expected_bars(w, "5m", segment="premarket") == 66
    assert not w.early_close


def test_early_closes_are_flagged_and_counted_as_35_hours(cal):
    """A 13:00 close is a scheduled event, not missing data.

    Counted as a normal session it looks like a 2.5-hour outage, `session_broken` fires,
    and the day is discarded. Counted correctly it is 42 five-minute bars.
    """
    for day in (date(2024, 7, 3), date(2024, 11, 29), date(2024, 12, 24),
                date(2025, 7, 3), date(2025, 11, 28), date(2025, 12, 24)):
        w = cal.window(day)
        assert w.early_close, f"{day} should be an early close"
        assert w.length_ms == int(3.5 * 3600 * 1000)
        assert expected_bars(w, "5m", segment="rth") == 42


def test_the_one_off_closures_are_known(cal):
    """Two days in this sample follow no holiday rule at all.

    The NYSE shut for President Bush's funeral on 2018-12-05 and President Carter's on
    2025-01-09. A hand-written holiday table generates neither, and would report both
    as a whole trading day of missing data.
    """
    assert not cal.is_session(date(2018, 12, 5))
    assert not cal.is_session(date(2025, 1, 9))
    # The surrounding days did trade, so this is a closure and not a bad date range.
    assert cal.is_session(date(2018, 12, 4))
    assert cal.is_session(date(2018, 12, 6))
    assert cal.is_session(date(2025, 1, 8))
    assert cal.is_session(date(2025, 1, 10))


def test_juneteenth_became_a_holiday_in_2022(cal):
    """A holiday that did not exist for part of the sample.

    Treating the current holiday list as if it always applied would discard three real
    trading days at the start of the window and keep three closed ones at the end.
    """
    assert cal.is_session(date(2021, 6, 18))
    assert not cal.is_session(date(2022, 6, 20))
    assert not cal.is_session(date(2024, 6, 19))


def test_weekends_and_fixed_holidays_are_not_sessions(cal):
    assert not cal.is_session(date(2025, 6, 7))    # Saturday
    assert not cal.is_session(date(2025, 6, 8))    # Sunday
    assert not cal.is_session(date(2025, 12, 25))  # Christmas
    assert not cal.is_session(date(2025, 1, 1))    # New Year's Day
    assert not cal.is_session(date(2025, 4, 18))   # Good Friday


def test_session_count_over_the_backfill_window(cal):
    """~252 sessions a year. A wildly different number means the range is wrong."""
    days = cal.sessions(date(2018, 8, 1), date(2026, 8, 1))
    assert len(days) == 2010
    assert 249 < len(days) / 8 < 253


def test_segment_of_classifies_bars_against_the_session(cal):
    """Premarket, RTH, and outside -- the three answers everything downstream needs."""
    w = cal.window(date(2025, 6, 2))
    assert w.segment_of(w.premarket_open_ms) == "premarket"
    assert w.segment_of(w.open_ms - 1) == "premarket"
    assert w.segment_of(w.open_ms) == "rth"
    assert w.segment_of(w.close_ms - 1) == "rth"
    # 16:00 itself belongs to no session: bars are half-open [t, t+tf).
    assert w.segment_of(w.close_ms) is None
    assert w.segment_of(w.premarket_open_ms - 1) is None


def test_asking_about_a_closed_day_raises_rather_than_inventing_one(cal):
    """A silently fabricated session on Christmas would produce a full day of bars
    that never traded, and every statistic computed over it would look normal."""
    with pytest.raises(CalendarError, match="not an XNYS trading day"):
        cal.window(date(2025, 12, 25))


# ---------------------------------------------------------------- crypto

def test_crypto_trades_every_day_including_holidays():
    c = CryptoCalendar()
    assert c.is_session(date(2025, 12, 25))
    assert c.is_session(date(2025, 6, 8))
    assert len(c.sessions(date(2025, 1, 1), date(2025, 12, 31))) == 365


@pytest.mark.parametrize(
    "day,bars", [(date(2025, 3, 9), 276), (date(2025, 11, 2), 300), (date(2025, 6, 1), 288)]
)
def test_crypto_days_are_23_24_or_25_hours(day, bars):
    """The invariant findings 1-8 were measured under, restated as a calendar.

    An ET day is 23 or 25 hours across a transition. This is what stops the crypto
    results changing when the session model became pluggable.
    """
    assert expected_bars(CryptoCalendar().window(day), "5m") == bars


def test_crypto_has_no_premarket():
    """Inventing one would fabricate a segment that does not exist -- the same refusal
    `prior.py` already makes for premarket_high/low."""
    w = CryptoCalendar().window(date(2025, 6, 1))
    assert w.premarket_open_ms is None
    assert expected_bars(w, "5m", segment="premarket") == 0
    assert w.segment_of(w.open_ms) == "rth"


# ------------------------------------------------- session anchors on real frames


def _bars(day: date, calendar, timeframe: str = "5m", *, segment_span=("premarket", "rth")):
    """One frame of bars covering the requested segments of a single session."""
    import polars as pl

    from tradedesk.calendars import expected_bars
    from tradedesk.timeutil import tf_ms

    w = calendar.window(day)
    step = tf_ms(timeframe)
    starts: list[int] = []
    if "premarket" in segment_span and w.premarket_open_ms is not None:
        starts += list(range(w.premarket_open_ms, w.open_ms, step))
    if "rth" in segment_span:
        starts += list(range(w.open_ms, w.close_ms, step))
    return pl.DataFrame(
        {
            "bar_open_ms": starts,
            "session_date": [day] * len(starts),
            "open": [100.0] * len(starts),
            "high": [101.0] * len(starts),
            "low": [99.0] * len(starts),
            "close": [100.5] * len(starts),
            "volume": [10.0] * len(starts),
        }
    ), w


@pytest.mark.parametrize(
    "day,hours", [(date(2025, 3, 9), 23), (date(2025, 11, 2), 25), (date(2025, 6, 1), 24)]
)
def test_ms_to_session_end_uses_the_real_day_length_on_crypto(day, hours):
    """The bug this replaced: a hardcoded 86,400,000 as the length of every day.

    An ET day is 23 or 25 hours across a DST transition, so on those days the old
    arithmetic put "thirty minutes before the close" at the wrong bar -- or at no bar at
    all. Measured on the real BTC store that was 8 sessions out of 1,461, and it moved
    the Gao intraday detector's signal count from 1,457 to 1,461.

    The verdict did not change (no demonstrated edge either way), but the detector was
    firing at the wrong instant on those days.
    """
    from tradedesk.levels.session import add_session_anchors

    cal = CryptoCalendar()
    df, w = _bars(day, cal)
    out = add_session_anchors(df, calendar=cal, timeframe="5m")

    assert w.length_ms == hours * 3600 * 1000
    # The last bar of the session leaves exactly zero milliseconds.
    assert out["ms_to_session_end"].to_list()[-1] == 0
    # Exactly one bar leaves exactly thirty minutes, on every day length.
    assert (out["ms_to_session_end"] == 1_800_000).sum() == 1


def test_equity_premarket_bars_sit_before_the_open(cal):
    """`ms_since_open` is negative in the premarket, and that is the honest answer.

    It is also what makes the RTH test `ms_since_open >= 0` rather than a lookup -- and
    what stops an equity opening range from swallowing all 66 premarket bars.
    """
    from tradedesk.levels.session import add_session_anchors

    df, w = _bars(date(2025, 6, 2), cal)
    out = add_session_anchors(df, calendar=cal, timeframe="5m")

    pre = out.filter(out["session_segment"] == "premarket")
    rth = out.filter(out["session_segment"] == "rth")
    assert pre.height == 66 and rth.height == 78
    assert pre["ms_since_open"].max() < 0
    assert rth["ms_since_open"].min() == 0
    # 04:00 is 5.5 hours before the 09:30 open.
    assert pre["ms_since_open"].min() == -int(5.5 * 3600 * 1000)
    assert rth["ms_to_session_end"].to_list()[-1] == 0


def test_equity_early_close_shortens_the_session_not_the_data(cal):
    """A 13:00 close must produce 42 RTH bars whose last one leaves zero time.

    Read against a 16:00 assumption, the session looks like it lost its final 2.5
    hours, and a strategy exiting "at the close" would be holding into a market that
    shut hours earlier.
    """
    from tradedesk.levels.session import add_session_anchors

    day = date(2025, 11, 28)
    df, w = _bars(day, cal)
    out = add_session_anchors(df, calendar=cal, timeframe="5m")

    rth = out.filter(out["session_segment"] == "rth")
    assert w.early_close
    assert rth.height == 42
    assert rth["ms_to_session_end"].to_list()[-1] == 0
    assert out["early_close"].unique().to_list() == [True]


def test_instrument_class_is_inferred_from_the_symbol():
    assert classify("BTC/USD") == "crypto"
    assert classify("ETH/USD") == "crypto"
    assert classify("AAPL") == "equity"
    assert classify("BRK.B") == "equity"
    assert for_instrument("crypto").instrument_class == "crypto"
    assert for_instrument("equity").instrument_class == "equity"
    with pytest.raises(CalendarError, match="unknown instrument class"):
        for_instrument("futures")  # type: ignore[arg-type]
