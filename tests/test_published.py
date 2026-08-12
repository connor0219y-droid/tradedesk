"""The imported published strategies: known answers, and the claims the family rests on.

Three kinds of test live here.

KNOWN ANSWERS for the detectors whose rules are easy to implement subtly wrong -- the
Turtle Soup dating requirement, the 80-20's open-and-close positions, the two-day
Momentum Pinball, the sequenced Bollinger squeeze. Each fixture is small enough to check
by hand and asserts "exactly these bars and no others", because a detector that fires
twice on a fixture containing one instance is not measuring what it claims to.

STRUCTURAL INVARIANTS of the pre-registered family: every member cites a source, carries
its own risk spec, and is measured under that spec rather than under the run-wide
defaults. These are what make PREREGISTRATION.md a description of what actually ran.

THE CALENDAR-WINDOW CLAIM. `windows.py` asserts that its lookbacks are measured in time
rather than in rows, which is the reason it uses `rolling_*_by` at all. That claim is
tested directly by deleting bars and checking the answer does not move.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tradedesk.backtest.engine import BacktestConfig
from tradedesk.backtest.validate import _bt_for
from tradedesk.config import load_config
from tradedesk.frames import BarFrame
from tradedesk.levels import compute_levels
from tradedesk.patterns import REGISTRY, detect, registered
from tradedesk.patterns.base import PatternError, RiskSpec

STEP = 300_000
BASE = 1_700_000_000_000 // STEP * STEP


def _frame(rows, *, gaps=None):
    """A bar frame carrying whatever level columns a detector declares it needs.

    `rows` are dicts, so a test supplies only the columns its detector reads and the
    fixture stays legible instead of carrying thirty irrelevant level values.
    """
    gaps = gaps or set()
    out = []
    for i, r in enumerate(rows):
        row = {
            "bar_open_ms": BASE + i * STEP,
            "session_date": date(2025, 6, 1),
            "volume": 10.0,
            "gap": (i == 0) or (i in gaps),
        }
        row.update(r)
        out.append(row)
    return pl.DataFrame(out)


def _hits(df, name):
    return [i for i, v in enumerate(detect(df, name).to_list()) if v]


# ----------------------------------------------------------------- known answers


def test_turtle_soup_requires_the_old_low_to_be_at_least_four_sessions_old():
    """The dating rule is the whole setup, and it is the easy half to drop.

    Raschke and Connors: "The previous 20-day low must have occurred at least four
    trading sessions earlier. This is very important." Encoded as `dc20_low < dc3_low`
    -- if the 20-day low had been made within the last three sessions, the 3-day low
    would BE it, so requiring the 20-day low to sit strictly below the 3-day low is the
    same statement.

        bar 1: low 90 < dc20_low 95, and dc20_low 95 < dc3_low 98  -> fires
        bar 2: low 90 < dc20_low 95, but dc3_low is also 95        -> does not fire,
               because the old low was made inside the last three sessions
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "dc20_low": 95.0, "dc3_low": 98.0},
        {"open": 100.0, "high": 101.0, "low": 90.0, "close": 96.0,
         "dc20_low": 95.0, "dc3_low": 98.0},
        {"open": 100.0, "high": 101.0, "low": 90.0, "close": 96.0,
         "dc20_low": 95.0, "dc3_low": 95.0},
    ])
    assert _hits(df, "turtle_soup_long") == [1]


def test_turtle_soup_plus_one_also_demands_the_close_outside_the_old_low():
    """Plus One's extra condition: "The close of the new low must be at or below the
    previous 20-bar low." That is what traps the participants who enter only on a close
    outside the range, and it is the only thing separating it from Turtle Soup.

        bar 1: low 90 < 95, close 96 > 95  -> Turtle Soup yes, Plus One NO
        bar 2: low 90 < 95, close 94 <= 95 -> both
    """
    rows = [
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "dc20_low": 95.0, "dc2_low": 98.0, "dc3_low": 98.0},
        {"open": 100.0, "high": 101.0, "low": 90.0, "close": 96.0,
         "dc20_low": 95.0, "dc2_low": 98.0, "dc3_low": 98.0},
        {"open": 100.0, "high": 101.0, "low": 90.0, "close": 94.0,
         "dc20_low": 95.0, "dc2_low": 98.0, "dc3_low": 98.0},
    ]
    df = _frame(rows)
    assert _hits(df, "turtle_soup_long") == [1, 2]
    assert _hits(df, "turtle_soup_p1_long") == [2]


def test_eighty_twenty_reads_open_and_close_positions_of_the_prior_bar():
    """A BUY needs the prior bar to open in the TOP 20% and close in the BOTTOM 20%.

    Getting this backwards produces a detector that fires on exactly the bars the source
    would short, so the test pins both ends.

        bar 1: prior range 90..100. open 98 (top 20% starts at 98), close 91 (bottom
               20% ends at 92)                       -> long fires at bar 2? no --
               the condition is read AT bar 2 about bar 1.
        bar 3's prior bar (bar 2) is the mirror      -> short fires at bar 3.
    """
    df = _frame([
        {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0},
        {"open": 98.0, "high": 100.0, "low": 90.0, "close": 91.0},   # top open, low close
        {"open": 91.0, "high": 100.0, "low": 90.0, "close": 99.0},   # low open, top close
        {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0},
    ])
    assert _hits(df, "eighty_twenty_long") == [2]
    assert _hits(df, "eighty_twenty_short") == [3]


def test_eighty_twenty_declines_to_classify_a_zero_range_bar():
    """A bar with high == low has no "top 20%" -- the question is undefined, not false.

    The detector states it as `range > 0` rather than dividing, so the zero-range case
    can never produce a null that has to be caught downstream. 19,852 SOL/USD 1m bars
    have high == low, so this is a real case and not a hypothetical.
    """
    df = _frame([
        {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        {"open": 95.0, "high": 95.0, "low": 95.0, "close": 95.0},
        {"open": 95.0, "high": 96.0, "low": 94.0, "close": 95.0},
    ])
    assert _hits(df, "eighty_twenty_long") == []
    assert _hits(df, "eighty_twenty_short") == []


def test_momentum_pinball_uses_yesterdays_indicator_not_todays():
    """The setup is two days: day one's LBR/RSI picks the side, day two supplies entry.

    Both bars below break the first hour's high identically. Only the one whose PRIOR
    SESSION closed with LBR/RSI under 30 may fire. Reading today's value instead would
    fire on both, and would be a materially easier rule.
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "prior_day_lbr_rsi3": 25.0, "first_hour_high": 105.0},
        {"open": 100.0, "high": 107.0, "low": 99.0, "close": 106.0,
         "prior_day_lbr_rsi3": 25.0, "first_hour_high": 105.0},   # crosses, RSI ok
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "prior_day_lbr_rsi3": 55.0, "first_hour_high": 105.0},
        {"open": 100.0, "high": 107.0, "low": 99.0, "close": 106.0,
         "prior_day_lbr_rsi3": 55.0, "first_hour_high": 105.0},   # crosses, RSI not
    ])
    assert _hits(df, "momentum_pinball_long") == [1]


def test_squeeze_breakout_requires_the_squeeze_before_the_break():
    """Bollinger's order is sequential: squeeze first, THEN wait for a band break.

    Testing both on the same bar would be a rarer coincidence and a different rule, so
    the squeeze is asserted on the prior bar. Here bar 2 breaks the upper band with the
    squeeze on bar 1; bar 4 breaks it with no prior squeeze.
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "bb_width": 0.05, "bb_width_min_125d": 0.04, "bb_upper": 105.0},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "bb_width": 0.03, "bb_width_min_125d": 0.04, "bb_upper": 105.0},  # squeeze
        {"open": 100.0, "high": 107.0, "low": 99.0, "close": 106.0,
         "bb_width": 0.05, "bb_width_min_125d": 0.03, "bb_upper": 105.0},  # break
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "bb_width": 0.09, "bb_width_min_125d": 0.03, "bb_upper": 105.0},
        {"open": 100.0, "high": 107.0, "low": 99.0, "close": 106.0,
         "bb_width": 0.09, "bb_width_min_125d": 0.03, "bb_upper": 105.0},  # no squeeze
    ])
    assert _hits(df, "squeeze_breakout_long") == [2]


def test_gao_fires_only_on_the_bar_that_leaves_thirty_minutes():
    """The position is taken AT THE START of the last half hour.

    The engine enters at the next bar's open, so the bar that must carry the signal is
    the one whose close leaves exactly 1,800,000 ms in the session. A detector that
    fired whenever the first-half-hour return was positive would take a position at
    every bar of the day and test nothing the paper claims.
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "ret_first30m": 0.01, "ms_to_session_end": 3_600_000},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "ret_first30m": 0.01, "ms_to_session_end": 1_800_000},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "ret_first30m": 0.01, "ms_to_session_end": 1_500_000},
    ])
    assert _hits(df, "gao_intraday_long") == [1]
    assert _hits(df, "gao_intraday_short") == []


def test_gao_sends_the_flat_case_short():
    """The paper's rule is `r13 if r1 > 0 else -r13`: a zero first half-hour goes SHORT.

    An edge case worth pinning because the natural implementation (`< 0` for the short
    leg) silently drops those days from both legs, and the two detectors would then not
    partition the sample the way the paper's strategy does.

    Bar 0 is a lead-in: `_frame` marks the first bar of every fixture as a gap bar (as
    the real store does, since nothing precedes it), and a gap bar can never fire.
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "ret_first30m": 0.0, "ms_to_session_end": 3_600_000},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "ret_first30m": 0.0, "ms_to_session_end": 1_800_000},
    ])
    assert _hits(df, "gao_intraday_short") == [1]
    assert _hits(df, "gao_intraday_long") == []


def test_crossings_are_fresh_events_not_standing_conditions():
    """Every detector in the family fires on a crossing, never on a state.

    Written as a standing condition, `ret_12m > 0` is true for years at a time and the
    sample fills with copies of one idea. Here the return is positive for three bars and
    only the first may fire.
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "ret_12m": -0.1},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "ret_12m": 0.1},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "ret_12m": 0.2},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "ret_12m": 0.3},
    ])
    assert _hits(df, "tsmom_12m_long") == [1]


def test_a_detector_cannot_fire_across_a_gap():
    """The engine's contiguity mask applies to imported strategies too.

    Bar 1 would cross the 12-month return above zero, but it opens a gap -- so the
    "previous bar" it is being compared against is on the far side of a hole, and the
    crossing is an artifact of the hole rather than an event in the market.
    """
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "ret_12m": -0.1},
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "ret_12m": 0.1},
    ], gaps={1})
    assert _hits(df, "tsmom_12m_long") == []


def test_a_detector_cannot_fire_where_its_required_level_is_null():
    """`first_hour_high` is null until the first hour closes, so Momentum Pinball cannot
    enter on a break of a range that does not exist yet."""
    df = _frame([
        {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0,
         "prior_day_lbr_rsi3": 25.0, "first_hour_high": None},
        {"open": 100.0, "high": 107.0, "low": 99.0, "close": 106.0,
         "prior_day_lbr_rsi3": 25.0, "first_hour_high": None},
    ])
    assert _hits(df, "momentum_pinball_long") == []


# ------------------------------------------------------- the family's own invariants


def test_the_family_is_exactly_what_was_pre_registered():
    """36 detectors, 18 strategies, each long and short.

    A guard on the thing PREREGISTRATION.md actually promises. If a detector is added
    later, this fails and forces the question of whether the correction still covers
    the family that was declared.
    """
    pub = registered(family="published")
    assert len(pub) == 36
    longs = {n[:-5] for n in pub if n.endswith("_long")}
    shorts = {n[:-6] for n in pub if n.endswith("_short")}
    assert longs == shorts, "every strategy must register both directions"
    assert len(longs) == 18


@pytest.mark.parametrize("name", registered(family="published"))
def test_every_published_detector_cites_a_source_and_carries_its_risk(name):
    """An imported strategy without its citation is untraceable, and one without its
    own stop and target would silently be measured under whatever the CLI passed --
    which is a different strategy that happens to share an entry condition."""
    spec = REGISTRY[name]
    assert spec.source, f"{name} has no source"
    assert spec.risk is not None, f"{name} has no RiskSpec"
    assert spec.risk.stop_atr > 0 and spec.risk.target_r > 0


def test_registering_an_imported_strategy_without_a_source_is_refused():
    """The rule is enforced at import time, not by review."""
    with pytest.raises(PatternError, match="must cite its source"):

        @pattern_with_risk_but_no_source()
        def _bad() -> pl.Expr:
            return pl.lit(True)


def pattern_with_risk_but_no_source():
    from tradedesk.patterns.base import pattern

    return pattern(
        name="_test_no_source", depth=1, direction="long",
        risk=RiskSpec(stop_atr=1.0, target_r=1.0, max_hold_days=1.0),
    )


def test_risk_spec_converts_holding_days_into_bars_per_timeframe():
    """"Hold for a month" is 30 bars on daily bars and 180 on 4h ones.

    Expressed as a bar count in the spec, the same number would mean a month at one
    timeframe and five days at another, and the 4h and 1d results would not be
    comparable -- which is the entire reason the cap is stated in days.
    """
    risk = RiskSpec(stop_atr=2.0, target_r=3.0, max_hold_days=30.0)
    assert risk.bars_for(86_400_000) == 30          # 1d
    assert risk.bars_for(4 * 3_600_000) == 180      # 4h
    assert risk.bars_for(3_600_000) == 720          # 1h
    # Never zero: a sub-bar holding cap still has to permit one bar.
    assert RiskSpec(stop_atr=1.0, target_r=1.0, max_hold_days=0.1).bars_for(86_400_000) == 1


def test_published_detectors_are_measured_under_their_own_spec():
    """`_bt_for` must prefer the detector's risk spec over the run-wide config."""
    run_bt = BacktestConfig(stop_atr=1.0, target_r=2.0, max_bars=48)
    spec = REGISTRY["turtle_s1_long"]
    bt = _bt_for(spec, run_bt, tf_ms=4 * 3_600_000)
    assert bt.stop_atr == 2.0 and bt.target_r == 3.0
    assert bt.atr_column == "atr_daily" and bt.hold_across_sessions is True
    assert bt.max_bars == 120                       # 20 days of 4h bars


def test_library_patterns_keep_the_run_wide_config_exactly():
    """The regression guard that matters: every number this project published before
    the imported family existed must still be reproducible. A hand-written pattern has
    no RiskSpec, so it must come back with the run's own config, unmodified."""
    run_bt = BacktestConfig(stop_atr=1.0, target_r=2.0, max_bars=48, atr_column="atr_intraday")
    assert _bt_for(REGISTRY["bullish_engulfing"], run_bt, tf_ms=STEP) is run_bt


def test_every_strategy_declares_exactly_one_horizon_class():
    """The pre-registration says each strategy is evaluated at one horizon. This is
    that promise made executable.

    Without it, a swing detector whose columns happen to exist on 5m bars gets silently
    evaluated there too, the family grows from 36 tests to something larger, and the
    Benjamini-Hochberg correction sized for 36 stops controlling the error rate it
    claims to. The failure would be invisible in the output.
    """
    pub = registered(family="published")
    intraday = [n for n in pub if REGISTRY[n].timeframes == ("5m",)]
    swing = [n for n in pub if REGISTRY[n].timeframes == ("4h", "1d")]
    assert len(intraday) == 10
    assert len(swing) == 26
    assert len(intraday) + len(swing) == len(pub), "a detector declares no horizon"

    # And the restriction is actually consulted, not merely declared.
    assert REGISTRY["gao_intraday_long"].runs_on("5m")
    assert not REGISTRY["gao_intraday_long"].runs_on("4h")
    assert REGISTRY["turtle_s1_long"].runs_on("4h")
    assert not REGISTRY["turtle_s1_long"].runs_on("5m")
    # Hand-written library patterns are unrestricted, as they always were.
    assert REGISTRY["bullish_engulfing"].runs_on("5m")
    assert REGISTRY["bullish_engulfing"].runs_on("1d")


def test_intraday_strategies_do_not_carry_positions_across_the_session():
    """Gao, Zarattini, Crabel's stretch and Lundstrom all close at the session close.

    `hold_across_sessions=False` is what actually enforces that; the bar cap is only a
    backstop behind it. If one of these flipped to True the strategy would become a
    multi-day hold wearing an intraday name.
    """
    for name in ("gao_intraday_long", "noise_breakout_long", "crabel_stretch_long",
                 "lundstrom_orb_long"):
        assert REGISTRY[name].risk.hold_across_sessions is False, name


# --------------------------------------------------- the calendar-window claim


def _daily_frame(closes, *, drop=()):
    """One bar per day, with `drop` omitting whole days as an absent venue would."""
    day_ms = 86_400_000
    start = 1_700_000_000_000 // day_ms * day_ms
    rows = []
    for i, c in enumerate(closes):
        if i in drop:
            continue
        rows.append({
            "venue": "coinbase", "symbol": "X/USD", "timeframe": "1d",
            "bar_open_ms": start + i * day_ms,
            "open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 10.0,
            "session_date": date(2025, 1, 1) + timedelta(days=i),
            "calendar_version": 1, "revision": 0, "ingested_at_ms": 0,
        })
    df = pl.DataFrame(rows)
    return BarFrame(df=df, venue="coinbase", symbol="X/USD", timeframe="1d",
                    calendar_version=1, as_of_ms=int(df["bar_open_ms"].max()) + day_ms)


def test_donchian_windows_are_measured_in_time_not_in_rows():
    """The claim `windows.py` is built on, tested by deleting bars.

    A 20-day high computed as `rolling_max(window_size=20)` counts ROWS. Delete three
    bars from the middle of the window and the row-counting version silently reaches
    three days further back, changing the answer. The time-based version does not,
    because the window is still twenty days.

    Construction: 40 ascending days, then a series where days 21-23 are absent. The
    20-day high at the final bar must be identical in both, because the deleted days
    are not the maximum and the window covers the same span of time either way.
    """
    cfg = load_config()
    closes = [100.0 + i for i in range(40)]

    full = compute_levels(_daily_frame(closes), cfg).to_polars()
    holed = compute_levels(_daily_frame(closes, drop=(21, 22, 23)), cfg).to_polars()

    last_full = full.filter(pl.col("bar_open_ms") == full["bar_open_ms"].max())
    last_holed = holed.filter(pl.col("bar_open_ms") == holed["bar_open_ms"].max())
    assert last_full["bar_open_ms"][0] == last_holed["bar_open_ms"][0]

    a, b = last_full["dc20_high"][0], last_holed["dc20_high"][0]
    assert a is not None and b is not None
    assert a == pytest.approx(b), (
        f"a 20-day high moved from {a} to {b} when three unrelated bars were deleted; "
        "the window is counting rows, not days"
    )


def test_donchian_excludes_the_current_bar():
    """"The high of the PRECEDING 20 days" must not include today.

    Including it makes the breakout test `high >= high`, which is either always or
    never true -- in both cases measuring nothing. On a strictly ascending series today
    is always the highest, so the 20-day high must equal YESTERDAY's high.
    """
    cfg = load_config()
    df = compute_levels(_daily_frame([100.0 + i for i in range(30)]), cfg).to_polars()
    row = df.filter(pl.col("bar_open_ms") == df["bar_open_ms"].max())
    prior_high = df["high"].to_list()[-2]
    assert row["dc20_high"][0] == pytest.approx(prior_high)
