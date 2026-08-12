"""Market calendars: which days trade, and between which instants.

WHY THIS MODULE HAS TO EXIST BEFORE ANY EQUITY DATA IS FETCHED. Crypto trades every
day, so "was this bar missing" and "was the market shut" are the same question, and the
whole project so far has been able to treat absence as information about liquidity. On
equities they are completely different questions. Without a calendar:

  * every weekend and every holiday reads as a venue outage, so `session_broken` fires
    on the first bar of most weeks and the quality gate blocks the entire store;
  * `expected_bar_count` over a date range is wrong by ~30%, so coverage percentages are
    meaningless and the ingest loop re-fetches closed days forever without converging;
  * the 17 early closes between 2018 and 2026 look like 3.5-hour outages, and a
    strategy exiting "at the close" exits into a session that ended two hours earlier.

The one-off closures are the reason this is not a hand-written holiday table. Between
2018 and 2026 the NYSE also shut for President Bush's funeral (2018-12-05) and
President Carter's (2025-01-09). Both are real trading-day gaps that no rule generates,
and a table written from the usual holiday list would classify them as missing data.
`exchange_calendars` carries them; that is what it is for.

THE ABSTRACTION. Crypto and equities differ in three ways that everything downstream
cares about -- which days exist, when the session starts, and how long it lasts -- so
those are exactly the three things a calendar answers. The rest of the codebase asks
the calendar rather than assuming a 24-hour ET day, which is what it assumed when
crypto was the only instrument class.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal, Protocol

from .timeutil import ET, MS, UTC, align_up, localize_strict, tf_ms, to_ms

InstrumentClass = Literal["crypto", "equity"]

#: A bar belongs to exactly one segment of its session. Equities have two tradable ones
#: plus an after-hours window this project does not ingest; crypto has a single
#: continuous segment, named "rth" so that downstream code has one column to group on
#: rather than a special case per instrument class.
Segment = Literal["premarket", "rth"]

MS_PER_DAY = 86_400_000


class CalendarError(Exception):
    """A calendar was asked about a day it does not cover."""


@dataclass(frozen=True)
class SessionWindow:
    """One tradable session, as real UTC instants.

    Instants, not wall-clock offsets. An ET session is a fixed number of hours but a
    varying number of milliseconds after midnight UTC, and a grid built by adding
    offsets to a local midnight is wrong twice a year -- the failure `timeutil` exists
    to prevent, reproduced here for sessions rather than days.
    """

    day: date
    open_ms: int
    close_ms: int
    premarket_open_ms: int | None
    early_close: bool

    @property
    def length_ms(self) -> int:
        return self.close_ms - self.open_ms

    def segment_of(self, bar_open_ms: int) -> Segment | None:
        """Which segment a bar belongs to, or None if it is outside the session."""
        if self.premarket_open_ms is not None and (
            self.premarket_open_ms <= bar_open_ms < self.open_ms
        ):
            return "premarket"
        if self.open_ms <= bar_open_ms < self.close_ms:
            return "rth"
        return None


class MarketCalendar(Protocol):
    instrument_class: InstrumentClass

    def is_session(self, day: date) -> bool: ...

    def sessions(self, start: date, end: date) -> list[date]:
        """Trading days in [start, end], inclusive of both."""
        ...

    def window(self, day: date) -> SessionWindow: ...


@dataclass(frozen=True)
class CryptoCalendar:
    """Every calendar day trades, anchored at 00:00 ET.

    The anchor is a modelling choice rather than a market fact -- crypto has no opening
    bell -- and it is the same one findings 1-8 were measured under. It is restated as a
    calendar so that code downstream can ask one question of both instrument classes
    instead of branching on which one it has.
    """

    instrument_class: InstrumentClass = "crypto"

    def is_session(self, day: date) -> bool:
        return True

    def sessions(self, start: date, end: date) -> list[date]:
        out, d = [], start
        while d <= end:
            out.append(d)
            d += timedelta(days=1)
        return out

    def window(self, day: date) -> SessionWindow:
        start = to_ms(localize_strict(datetime.combine(day, time(0, 0))))
        end = to_ms(localize_strict(datetime.combine(day + timedelta(days=1), time(0, 0))))
        # No premarket: crypto trades continuously, and inventing one would fabricate a
        # segment that does not exist. `prior.py` already refuses to do this.
        return SessionWindow(
            day=day, open_ms=start, close_ms=end, premarket_open_ms=None,
            early_close=False,
        )


@dataclass(frozen=True)
class EquityCalendar:
    """NYSE sessions: RTH 09:30-16:00 ET, premarket from 04:00, early closes at 13:00.

    Backed by `exchange_calendars`' XNYS calendar, which is the part worth not writing
    by hand -- Good Friday moves, Juneteenth was added in 2022, and the two funeral
    closures follow no rule at all.

    The premarket window is a project convention, not an exchange one: US equities trade
    from 04:00 ET on most venues, and Alpaca serves those bars. It is fixed at 04:00
    rather than read from the calendar because `exchange_calendars` models the auction
    session, not the ECN session.
    """

    premarket_start: str = "04:00"
    instrument_class: InstrumentClass = "equity"
    _calendar_name: str = "XNYS"

    @functools.cached_property
    def _schedule(self):
        import exchange_calendars as xcals

        cal = xcals.get_calendar(self._calendar_name)
        return cal.schedule

    @functools.cached_property
    def _by_day(self) -> dict[date, tuple[int, int]]:
        """{trading day: (open_ms, close_ms)} straight from the exchange calendar.

        Both instants come from the calendar rather than from a 09:30/16:00 assumption,
        because that assumption is wrong on 17 days in this sample and the wrongness is
        silent -- an early close looks exactly like a session that lost two hours to an
        outage.
        """
        sched = self._schedule
        opens = sched["open"].dt.tz_convert(ET)
        closes = sched["close"].dt.tz_convert(ET)
        out: dict[date, tuple[int, int]] = {}
        for ts, o, c in zip(sched.index, opens, closes):
            out[ts.date()] = (
                int(o.timestamp() * MS + 0.5),
                int(c.timestamp() * MS + 0.5),
            )
        return out

    def is_session(self, day: date) -> bool:
        return day in self._by_day

    def sessions(self, start: date, end: date) -> list[date]:
        return [d for d in sorted(self._by_day) if start <= d <= end]

    def window(self, day: date) -> SessionWindow:
        pair = self._by_day.get(day)
        if pair is None:
            raise CalendarError(f"{day} is not an XNYS trading day")
        open_ms, close_ms = pair
        pm_h, pm_m = (int(x) for x in self.premarket_start.split(":"))
        pm_ms = to_ms(localize_strict(datetime.combine(day, time(pm_h, pm_m))))
        # 6.5 hours is the full session. Anything shorter is a scheduled early close,
        # flagged so that coverage does not read it as missing data.
        return SessionWindow(
            day=day, open_ms=open_ms, close_ms=close_ms, premarket_open_ms=pm_ms,
            early_close=(close_ms - open_ms) < int(6.5 * 3600 * MS),
        )


def for_instrument(instrument_class: InstrumentClass) -> MarketCalendar:
    if instrument_class == "crypto":
        return CryptoCalendar()
    if instrument_class == "equity":
        return EquityCalendar()
    raise CalendarError(f"unknown instrument class {instrument_class!r}")


def classify(symbol: str) -> InstrumentClass:
    """Crypto pairs carry a slash; equity tickers do not.

    Deliberately syntactic and deliberately narrow. The alternative -- a lookup table
    of every ticker -- has to be maintained, and a symbol missing from it would silently
    get the wrong session model rather than raising.
    """
    return "crypto" if "/" in symbol else "equity"


def expected_bars(
    window: SessionWindow, timeframe: str, *, segment: Segment | None = None
) -> int:
    """How many bars a session should contain, per segment.

    This is what makes a coverage percentage mean anything on equities. A normal RTH
    day is 78 five-minute bars and an early close is 42; counting either as the other
    produces a coverage figure that is confidently wrong.
    """
    step = tf_ms(timeframe)

    def count(lo: int, hi: int) -> int:
        first = align_up(lo, timeframe)
        return 0 if hi <= first else (hi - first + step - 1) // step

    if segment == "rth" or segment is None and window.premarket_open_ms is None:
        return count(window.open_ms, window.close_ms)
    if segment == "premarket":
        if window.premarket_open_ms is None:
            return 0
        return count(window.premarket_open_ms, window.open_ms)
    # Both segments.
    total = count(window.open_ms, window.close_ms)
    if window.premarket_open_ms is not None:
        total += count(window.premarket_open_ms, window.open_ms)
    return total
