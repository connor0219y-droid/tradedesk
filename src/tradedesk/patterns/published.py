"""Eighteen strategies imported from published sources, as a pre-registered family.

READ `PREREGISTRATION.md` FIRST. It fixes this family, its decision rule and its Monte
Carlo resolution before any of it was run, which is the only thing that makes the
Benjamini-Hochberg correction downstream mean anything. This module is the
implementation of that document; where the two disagree, the document is the claim and
this file is the bug.

WHAT A DETECTOR HERE IS. Each one carries its source's own stop and target in a
`RiskSpec`, rather than inheriting whatever the command line passed. An imported
strategy is an entry AND its exits; validating Connors' RSI(2) entry under a Turtle stop
tests a strategy nobody published. It also removes the temptation to sweep: a strategy
whose stop lives in its spec has exactly one configuration.

THREE DEVIATIONS APPLY TO EVERY DETECTOR HERE, and they are properties of the engine
rather than choices made for this batch. They are stated once in PREREGISTRATION.md and
recalled here because they change what a negative result means:

  1. Entry is the NEXT BAR'S OPEN. Many of these enter on a stop order at a named price.
  2. Stops are k x ATR. Some sources specify a percentage or a price.
  3. There are no trailing stops and no indicator exits. Some sources use both.

Each docstring below names what its own source specified and what was substituted. A
strategy that fails here has not been refuted at its source -- it has failed this
implementation of it, on these three instruments, under these costs.

EVERY ENTRY IS A FRESH EVENT. Written as a standing condition, "the 12-month return is
positive" fires on every bar of a year-long trend and buries the sample in thousands of
copies of one idea. Each detector below therefore requires the condition to have been
FALSE on the previous bar -- the crossing, not the state.
"""

from __future__ import annotations

import polars as pl

from .base import RiskSpec, pattern

O, H, L, C = pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close")
PO, PH, PL_, PC = O.shift(1), H.shift(1), L.shift(1), C.shift(1)

FAMILY = "published"

#: The horizon each strategy is evaluated at, per PREREGISTRATION.md. Session-anchored
#: strategies run on 5m, where a thirty-minute window and a first hour actually exist;
#: swing strategies run on 4h and 1d. No strategy is evaluated at several horizons and
#: the best one reported -- these tuples are what stops that from happening by accident.
INTRADAY_TF = ("5m",)
SWING_TF = ("4h", "1d")

# ---------------------------------------------------------------- risk profiles
#
# One RiskSpec per source, shared by that source's long and short detector so the two
# cannot drift apart. `max_hold_days` is in days at every timeframe; see RiskSpec.

#: Trend-following swing risk: the Turtles' 2N stop, held for weeks.
TREND_RISK = RiskSpec(stop_atr=2.0, target_r=3.0, max_hold_days=20.0)
#: Moskowitz, Ooi & Pedersen hold one month.
TSMOM_RISK = RiskSpec(stop_atr=2.0, target_r=3.0, max_hold_days=30.0)
#: Liu & Tsyvinski's predictive horizon is one week.
WEEKLY_RISK = RiskSpec(stop_atr=2.0, target_r=3.0, max_hold_days=7.0)
#: Connors' RSI(2) exits on a 5-day MA cross, which the engine cannot express; a 1R
#: target and a five-day cap stand in for it.
RSI2_RISK = RiskSpec(stop_atr=2.0, target_r=1.0, max_hold_days=5.0)
#: Bollinger reversion: same shape as RSI(2), a touch more room.
REVERT_RISK = RiskSpec(stop_atr=2.0, target_r=1.0, max_hold_days=10.0)
#: Street Smarts' failed-breakout reversals: "a very tight risk point is predefined",
#: and partial profits are taken within two to six bars.
SOUP_RISK = RiskSpec(stop_atr=0.5, target_r=2.0, max_hold_days=6.0)
#: The 80-20 is a day trade in the source; one day is the cap at every timeframe.
DAY_RISK = RiskSpec(stop_atr=0.5, target_r=2.0, max_hold_days=1.0)
#: Crabel's volatility patterns, held a few days.
VOL_RISK = RiskSpec(stop_atr=1.0, target_r=2.0, max_hold_days=5.0)

#: Session-anchored intraday strategies: closed at the session boundary, never carried.
#: `hold_across_sessions=False` is what actually enforces the "exit at the close" rule
#: every one of these sources specifies; the bar cap is a backstop behind it.
PINBALL_RISK = RiskSpec(stop_atr=0.5, target_r=2.0, max_hold_days=2.0)
STRETCH_RISK = RiskSpec(
    stop_atr=1.0, target_r=2.0, max_hold_days=1.0, hold_across_sessions=False
)
LUNDSTROM_RISK = RiskSpec(
    stop_atr=0.6, target_r=2.0, max_hold_days=1.0, hold_across_sessions=False
)
#: Gao et al. and Zarattini both run unstopped to the close. The stop exists only
#: because the engine requires one, and is deliberately wide enough that the
#: session-close exit is what actually ends these trades.
GAO_RISK = RiskSpec(
    stop_atr=1.0, target_r=10.0, max_hold_days=1.0, hold_across_sessions=False
)
NOISE_RISK = RiskSpec(
    stop_atr=0.5, target_r=10.0, max_hold_days=1.0, hold_across_sessions=False
)


def _const(value: float) -> pl.Expr:
    """A constant threshold, broadcast to the frame's length.

    NOT `pl.lit(value)`. A literal is a one-element series, so `pl.lit(0.0).shift(1)` is
    a single null rather than a column of the constant -- and any crossing test written
    against it evaluates to null on every bar, which `fill_null(False)` in the engine
    then turns into a detector that never fires. Silent, and indistinguishable in the
    output from a strategy that legitimately produced no signals. `pl.repeat` gives a
    full-length column whose shift is the constant, which is what a fixed threshold
    means.
    """
    return pl.repeat(value, pl.len(), dtype=pl.Float64)


def _crosses_above(value: pl.Expr, level: pl.Expr) -> pl.Expr:
    """`value` is above `level` now and was not on the previous bar.

    Both sides are shifted, not just the value: a band that moves under a stationary
    price is a crossing too, and comparing this bar's price against last bar's price
    while holding the level fixed would miss it.
    """
    return (value > level) & (value.shift(1) <= level.shift(1))


def _crosses_below(value: pl.Expr, level: pl.Expr) -> pl.Expr:
    return (value < level) & (value.shift(1) >= level.shift(1))


# ================================================================ momentum and trend


@pattern(
    name="tsmom_12m_long", depth=2, direction="long", requires=("ret_12m",),
    risk=TSMOM_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Moskowitz, Ooi & Pedersen (2012), Journal of Financial Economics 104:228",
)
def _tsmom_12m_long() -> pl.Expr:
    """The trailing 12-month return turns positive.

    MOP's rule is `sign(r[t-12m,t])`, held one month, scaled to 40% annualised
    volatility. Only the SIGN is tested here: the 40%/sigma term is portfolio
    construction -- it sets how large the position is, not whether to take it -- and the
    engine measures outcomes in R, which is position-size independent by construction.

    Fired on the sign CHANGE. As a standing condition this is long for years at a time
    and would produce one trade per bar cap rather than one trade per signal.
    """
    return _crosses_above(pl.col("ret_12m"), _const(0.0))


@pattern(
    name="tsmom_12m_short", depth=2, direction="short", requires=("ret_12m",),
    risk=TSMOM_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Moskowitz, Ooi & Pedersen (2012), Journal of Financial Economics 104:228",
)
def _tsmom_12m_short() -> pl.Expr:
    """The trailing 12-month return turns negative."""
    return _crosses_below(pl.col("ret_12m"), _const(0.0))


@pattern(
    name="turtle_s1_long", depth=2, direction="long", requires=("dc20_high",),
    risk=TREND_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Original Turtle Trading Rules (Dennis & Eckhardt, 1983), System 1",
)
def _turtle_s1_long() -> pl.Expr:
    """Price exceeds the high of the preceding 20 days.

    Tested on the bar's HIGH rather than its close, because the Turtles "always traded
    at the breakout when it was exceeded during the day" -- a close-only test is a
    different and much rarer signal.

    TWO PARTS OF SYSTEM 1 ARE NOT IMPLEMENTED, both because they are path-dependent
    state and a detector here is a pure boolean expression over bar `t`:

      * the filter that skips a breakout when the PREVIOUS breakout would have won
        (it requires simulating each prior breakout to a 2N stop or a 10-day exit);
      * the 55-day failsafe entry that catches a signal skipped by that filter.

    Their absence makes this the unfiltered 20-day Donchian breakout -- System 1's
    entry without its selectivity. That is a real difference and it is why this
    detector is named for the system rather than claiming to be it.
    """
    return _crosses_above(H, pl.col("dc20_high"))


@pattern(
    name="turtle_s1_short", depth=2, direction="short", requires=("dc20_low",),
    risk=TREND_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Original Turtle Trading Rules (Dennis & Eckhardt, 1983), System 1",
)
def _turtle_s1_short() -> pl.Expr:
    """Price drops below the low of the preceding 20 days."""
    return _crosses_below(L, pl.col("dc20_low"))


@pattern(
    name="turtle_s2_long", depth=2, direction="long", requires=("dc55_high",),
    risk=TREND_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Original Turtle Trading Rules (Dennis & Eckhardt, 1983), System 2",
)
def _turtle_s2_long() -> pl.Expr:
    """Price exceeds the high of the preceding 55 days.

    System 2 takes every breakout regardless of how the last one went, so unlike
    System 1 this detector is missing nothing but the exit rule.
    """
    return _crosses_above(H, pl.col("dc55_high"))


@pattern(
    name="turtle_s2_short", depth=2, direction="short", requires=("dc55_low",),
    risk=TREND_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Original Turtle Trading Rules (Dennis & Eckhardt, 1983), System 2",
)
def _turtle_s2_short() -> pl.Expr:
    """Price drops below the low of the preceding 55 days."""
    return _crosses_below(L, pl.col("dc55_low"))


@pattern(
    name="high_52w_long", depth=2, direction="long", requires=("hh_52w",),
    risk=TREND_RISK, family=FAMILY, timeframes=SWING_TF,
    source="George & Hwang (2004), Journal of Finance 59:2145",
)
def _high_52w_long() -> pl.Expr:
    """A new 52-week high.

    George & Hwang's strategy is CROSS-SECTIONAL: it buys the stocks nearest their own
    52-week high and sells those furthest from it. Three instruments cannot support that
    sort, so what is tested is the time-series residue of the idea -- that proximity to
    the 52-week high predicts continuation -- in its sharpest form, the moment the high
    is actually made. This is an adaptation and not a replication, and it is the largest
    interpretive step taken anywhere in this family.
    """
    return _crosses_above(H, pl.col("hh_52w"))


@pattern(
    name="high_52w_short", depth=2, direction="short", requires=("ll_52w",),
    risk=TREND_RISK, family=FAMILY, timeframes=SWING_TF,
    source="George & Hwang (2004), Journal of Finance 59:2145",
)
def _high_52w_short() -> pl.Expr:
    """A new 52-week low."""
    return _crosses_below(L, pl.col("ll_52w"))


@pattern(
    name="crypto_wk_mom_long", depth=2, direction="long", requires=("ret_1w",),
    risk=WEEKLY_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Liu & Tsyvinski (2021), Review of Financial Studies 34:2689",
)
def _crypto_wk_mom_long() -> pl.Expr:
    """The trailing one-week return turns positive.

    Liu & Tsyvinski report that this week's return predicts the next one to four weeks'
    for Bitcoin, and that the top QUINTILE of formation-week returns earns 11.22% the
    following week. The quintile version is not reproducible: the paper publishes each
    rank's mean formation return, not the breakpoints between ranks, so any threshold I
    chose would be my invention wearing their citation. The sign rule tested here is the
    weaker claim the paper also makes, and it is the one that can be stated without
    guessing.
    """
    return _crosses_above(pl.col("ret_1w"), _const(0.0))


@pattern(
    name="crypto_wk_mom_short", depth=2, direction="short", requires=("ret_1w",),
    risk=WEEKLY_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Liu & Tsyvinski (2021), Review of Financial Studies 34:2689",
)
def _crypto_wk_mom_short() -> pl.Expr:
    """The trailing one-week return turns negative."""
    return _crosses_below(pl.col("ret_1w"), _const(0.0))


#: Thirty minutes of session left after this bar closes -- so the engine's next-bar-open
#: entry lands exactly at the start of the last half hour, which is when Gao et al. take
#: the position.
_LAST_HALF_HOUR = pl.col("ms_to_session_end") == 1_800_000


@pattern(
    name="gao_intraday_long", depth=2, direction="long",
    requires=("ret_first30m", "ms_to_session_end"),
    risk=GAO_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Gao, Han, Li & Zhou (2018), Journal of Financial Economics 129:394",
)
def _gao_intraday_long() -> pl.Expr:
    """First half-hour return positive; take the last half hour long.

    The paper's rule is exactly `eta(r1) = r13 if r1 > 0 else -r13` -- position taken at
    the start of the last half-hour, closed at the market close, always in the market on
    one side or the other. Registered as two detectors so the long and short legs do not
    average into a single number that hides which side carried it.

    On crypto the "session" is the ET calendar day, which has no opening auction and no
    close. The pattern the paper found is a market-microstructure story about
    institutional rebalancing around a real open and close; there is no reason to expect
    it here, and testing it is the point.
    """
    return (pl.col("ret_first30m") > 0) & _LAST_HALF_HOUR


@pattern(
    name="gao_intraday_short", depth=2, direction="short",
    requires=("ret_first30m", "ms_to_session_end"),
    risk=GAO_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Gao, Han, Li & Zhou (2018), Journal of Financial Economics 129:394",
)
def _gao_intraday_short() -> pl.Expr:
    """First half-hour return negative or flat; take the last half hour short.

    `<= 0` rather than `< 0`, because the paper's rule sends the zero case short.
    """
    return (pl.col("ret_first30m") <= 0) & _LAST_HALF_HOUR


#: Zarattini checks the boundary only on the clock half-hour. Since a session is a whole
#: number of half-hours, "this bar closes on a half-hour boundary" is exactly
#: `ms_to_session_end` being divisible by thirty minutes.
_ON_HALF_HOUR = (pl.col("ms_to_session_end") % 1_800_000) == 0


@pattern(
    name="noise_breakout_long", depth=2, direction="long",
    requires=("noise_upper", "ms_to_session_end"),
    risk=NOISE_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Zarattini, Aziz & Barbon (2024), SSRN 4824172",
)
def _noise_breakout_long() -> pl.Expr:
    """Price leaves the noise band upward, checked on the half-hour.

    The band is the session open times one plus the average absolute move from the open
    seen at this time of day over the last 14 sessions (see `levels/intraday.py`).
    Inside it, supply and demand are taken to be balanced; leaving it is read as a
    demand imbalance worth following.

    The paper's dynamic trailing stop is replaced by a fixed 0.5x daily ATR stop and a
    10R target, per deviation 3. This matters more here than elsewhere: the trailing
    stop is what the authors credit for the return profile, so what is tested is the
    ENTRY, on its own.
    """
    return _crosses_above(C, pl.col("noise_upper")) & _ON_HALF_HOUR


@pattern(
    name="noise_breakout_short", depth=2, direction="short",
    requires=("noise_lower", "ms_to_session_end"),
    risk=NOISE_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Zarattini, Aziz & Barbon (2024), SSRN 4824172",
)
def _noise_breakout_short() -> pl.Expr:
    """Price leaves the noise band downward, checked on the half-hour."""
    return _crosses_below(C, pl.col("noise_lower")) & _ON_HALF_HOUR


# ================================================================== mean reversion


@pattern(
    name="connors_rsi2_long", depth=2, direction="long",
    requires=("rsi_2", "sma_200d"),
    risk=RSI2_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Connors & Alvarez (2008), Short Term Trading Strategies That Work",
)
def _connors_rsi2_long() -> pl.Expr:
    """Above the 200-day average, RSI(2) drops below 5.

    The full rule is: price above the 200-day SMA (buy dips only in an uptrend), RSI(2)
    below 5, buy the close, exit when price closes above the 5-day SMA. The exit is an
    indicator cross the engine cannot express, so a 1R target and a five-day cap stand
    in -- and Connors specifies no stop at all, so the 2x daily ATR stop is an addition
    that can only hurt the strategy's measured performance relative to its source.

    RSI(2) is a BAR count, so this is Connors' rule literally on 1d bars and its
    faster analogue on 4h. Both are reported; neither is presented as the other.
    """
    return (C > pl.col("sma_200d")) & _crosses_below(pl.col("rsi_2"), _const(5.0))


@pattern(
    name="connors_rsi2_short", depth=2, direction="short",
    requires=("rsi_2", "sma_200d"),
    risk=RSI2_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Connors & Alvarez (2008), Short Term Trading Strategies That Work",
)
def _connors_rsi2_short() -> pl.Expr:
    """Below the 200-day average, RSI(2) rises above 95."""
    return (C < pl.col("sma_200d")) & _crosses_above(pl.col("rsi_2"), _const(95.0))


@pattern(
    name="turtle_soup_long", depth=2, direction="long",
    requires=("dc20_low", "dc3_low"),
    risk=SOUP_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 4",
)
def _turtle_soup_long() -> pl.Expr:
    """A new 20-day low whose predecessor is at least four sessions old.

    The dating rule -- "the previous 20-day low must have occurred at least four trading
    sessions earlier. This is very important." -- is encoded exactly rather than
    approximately. If the previous 20-day low had been made within the last three
    sessions, the 3-day low would equal the 20-day low; requiring the 20-day low to be
    strictly LOWER than the 3-day low is therefore the same statement, and needs no
    index arithmetic to get wrong.

    The source enters on a buy stop 5-10 ticks above the previous 20-day low and stops
    out one tick under today's low. The engine enters at the next bar's open and stops
    at 0.5x daily ATR (deviations 1 and 2). For a reversal entry the next open is not
    systematically worse than the stop order, but it is not the same trade.
    """
    return (L < pl.col("dc20_low")) & (pl.col("dc20_low") < pl.col("dc3_low"))


@pattern(
    name="turtle_soup_short", depth=2, direction="short",
    requires=("dc20_high", "dc3_high"),
    risk=SOUP_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 4",
)
def _turtle_soup_short() -> pl.Expr:
    """A new 20-day high whose predecessor is at least four sessions old."""
    return (H > pl.col("dc20_high")) & (pl.col("dc20_high") > pl.col("dc3_high"))


@pattern(
    name="turtle_soup_p1_long", depth=2, direction="long",
    requires=("dc20_low", "dc2_low"),
    risk=SOUP_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 5",
)
def _turtle_soup_p1_long() -> pl.Expr:
    """Plus One: the new 20-day low must also CLOSE at or below the old one.

    Two differences from Turtle Soup, both from the source: the dating requirement is
    three sessions rather than four (hence the 2-day low here, not the 3-day), and the
    close of the new low must be at or below the previous 20-day low -- which is what
    traps the participants who enter only on a close outside the range.

    The entry is placed the NEXT day, which is exactly what the engine's next-bar-open
    entry does. Of everything in this family, this detector's timing is the closest fit
    to its source.
    """
    return (
        (L < pl.col("dc20_low"))
        & (C <= pl.col("dc20_low"))
        & (pl.col("dc20_low") < pl.col("dc2_low"))
    )


@pattern(
    name="turtle_soup_p1_short", depth=2, direction="short",
    requires=("dc20_high", "dc2_high"),
    risk=SOUP_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 5",
)
def _turtle_soup_p1_short() -> pl.Expr:
    """Plus One, short: the new 20-day high must also close at or above the old one."""
    return (
        (H > pl.col("dc20_high"))
        & (C >= pl.col("dc20_high"))
        & (pl.col("dc20_high") > pl.col("dc2_high"))
    )


#: The prior bar's range, and where its open and close sat inside it. Written as
#: multiplications against the range rather than as two divisions, so a zero-range bar
#: is excluded by `_PRIOR_RANGE > 0` instead of producing a null that has to be caught
#: downstream.
_PRIOR_RANGE = PH - PL_
_OPEN_POS = PO - PL_
_CLOSE_POS = PC - PL_


@pattern(
    name="eighty_twenty_long", depth=2, direction="long",
    risk=DAY_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 6",
)
def _eighty_twenty_long() -> pl.Expr:
    """The prior bar opened in the top 20% of its range and closed in the bottom 20%.

    Moore's finding behind it: a bar closing at the extreme of its range follows through
    the next morning 80-90% of the time but closes that way only half the time, so the
    reversal is the tradable half. Gipson's addition, which the source adopts, is that
    the reversal is likelier still when the bar OPENED at the opposite extreme.

    Reads the prior BAR, so this is the published daily rule on 1d bars and its
    bar-scale analogue elsewhere. The source also requires price to trade 5-15 ticks
    through yesterday's low before entering, a discretionary trigger ("the exact amount
    is left to your discretion") that cannot be pinned down without inventing it, so it
    is omitted -- which makes this detector fire more often than the source's rule.
    """
    return (
        (_PRIOR_RANGE > 0)
        & (_OPEN_POS >= 0.8 * _PRIOR_RANGE)
        & (_CLOSE_POS <= 0.2 * _PRIOR_RANGE)
    )


@pattern(
    name="eighty_twenty_short", depth=2, direction="short",
    risk=DAY_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 6",
)
def _eighty_twenty_short() -> pl.Expr:
    """The prior bar opened in the bottom 20% of its range and closed in the top 20%."""
    return (
        (_PRIOR_RANGE > 0)
        & (_OPEN_POS <= 0.2 * _PRIOR_RANGE)
        & (_CLOSE_POS >= 0.8 * _PRIOR_RANGE)
    )


@pattern(
    name="momentum_pinball_long", depth=2, direction="long",
    requires=("prior_day_lbr_rsi3", "first_hour_high"),
    risk=PINBALL_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 7",
)
def _momentum_pinball_long() -> pl.Expr:
    """Yesterday's LBR/RSI below 30, then today's price breaks the first hour's high.

    A genuinely two-day setup: day one's closing LBR/RSI -- a 3-period RSI of the
    1-period rate of change -- picks the side, and day two's break of the opening hour
    supplies the entry. Reading today's LBR/RSI instead would collapse it into a
    same-bar rule that is both easier to satisfy and not what was published.

    `first_hour_high` is null until the first hour closes, so the crossing test cannot
    fire inside the hour that defines it.
    """
    return (pl.col("prior_day_lbr_rsi3") < 30.0) & _crosses_above(
        C, pl.col("first_hour_high")
    )


@pattern(
    name="momentum_pinball_short", depth=2, direction="short",
    requires=("prior_day_lbr_rsi3", "first_hour_low"),
    risk=PINBALL_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Raschke & Connors (1995), Street Smarts, ch. 7",
)
def _momentum_pinball_short() -> pl.Expr:
    """Yesterday's LBR/RSI above 70, then price breaks the first hour's low."""
    return (pl.col("prior_day_lbr_rsi3") > 70.0) & _crosses_below(
        C, pl.col("first_hour_low")
    )


@pattern(
    name="bollinger_revert_long", depth=2, direction="long", requires=("bb_lower",),
    risk=REVERT_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Bollinger (2001), Bollinger on Bollinger Bands",
)
def _bollinger_revert_long() -> pl.Expr:
    """Close crosses below the lower band (20 periods, 2 sigma).

    The mean-reversion reading of a band tag. Bollinger himself is explicit that a tag
    is not by itself a signal -- it is a relative-price statement -- so this detector is
    the popular reading of his indicator rather than his own recommendation, and it is
    included on those terms.
    """
    return _crosses_below(C, pl.col("bb_lower"))


@pattern(
    name="bollinger_revert_short", depth=2, direction="short", requires=("bb_upper",),
    risk=REVERT_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Bollinger (2001), Bollinger on Bollinger Bands",
)
def _bollinger_revert_short() -> pl.Expr:
    """Close crosses above the upper band."""
    return _crosses_above(C, pl.col("bb_upper"))


# ====================================================================== volatility


@pattern(
    name="crabel_nr7_long", depth=8, direction="long", requires=("nr7",),
    risk=VOL_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Crabel (1990), Day Trading with Short Term Price Patterns and ORB",
)
def _crabel_nr7_long() -> pl.Expr:
    """The prior bar had the narrowest range of seven; price breaks above it.

    The contraction-expansion premise: a range contraction precedes a range expansion,
    and the direction of the break picks the side. Depth is 8 -- seven bars for the
    narrow-range comparison plus the breakout bar -- so the engine will not let this
    fire where the seven-bar window spans a venue outage, which would be comparing six
    bars against a hole and calling the result narrow.
    """
    return (pl.col("nr7").shift(1) == 1.0) & (C > PH)


@pattern(
    name="crabel_nr7_short", depth=8, direction="short", requires=("nr7",),
    risk=VOL_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Crabel (1990), Day Trading with Short Term Price Patterns and ORB",
)
def _crabel_nr7_short() -> pl.Expr:
    """The prior bar had the narrowest range of seven; price breaks below it."""
    return (pl.col("nr7").shift(1) == 1.0) & (C < PL_)


@pattern(
    name="crabel_id_nr4_long", depth=5, direction="long",
    requires=("nr4", "inside_bar"),
    risk=VOL_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Crabel (1990), Day Trading with Short Term Price Patterns and ORB",
)
def _crabel_id_nr4_long() -> pl.Expr:
    """The prior bar was both an inside bar and the narrowest of four; break above it.

    Crabel treats the two conditions together as a stronger contraction than either
    alone: an inside bar says the range shrank relative to its immediate predecessor,
    NR4 says it shrank relative to the recent past. Requiring both is his combination,
    not a filter added here to improve a result.
    """
    return (
        (pl.col("inside_bar").shift(1) == 1.0)
        & (pl.col("nr4").shift(1) == 1.0)
        & (C > PH)
    )


@pattern(
    name="crabel_id_nr4_short", depth=5, direction="short",
    requires=("nr4", "inside_bar"),
    risk=VOL_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Crabel (1990), Day Trading with Short Term Price Patterns and ORB",
)
def _crabel_id_nr4_short() -> pl.Expr:
    """Inside bar and NR4 on the prior bar; break below it."""
    return (
        (pl.col("inside_bar").shift(1) == 1.0)
        & (pl.col("nr4").shift(1) == 1.0)
        & (C < PL_)
    )


@pattern(
    name="crabel_stretch_long", depth=2, direction="long",
    requires=("stretch", "session_open"),
    risk=STRETCH_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Crabel (1990), Day Trading with Short Term Price Patterns and ORB",
)
def _crabel_stretch_long() -> pl.Expr:
    """Price crosses the session open plus the stretch.

    The stretch is the 10-session average of the smaller of `high - open` and
    `open - low` -- how far this instrument routinely travels on the quiet side of its
    open. Crabel's trigger is therefore self-calibrating: it asks for a move larger than
    the instrument's own recent noise, rather than a fixed percentage.
    """
    return _crosses_above(C, pl.col("session_open") + pl.col("stretch"))


@pattern(
    name="crabel_stretch_short", depth=2, direction="short",
    requires=("stretch", "session_open"),
    risk=STRETCH_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Crabel (1990), Day Trading with Short Term Price Patterns and ORB",
)
def _crabel_stretch_short() -> pl.Expr:
    """Price crosses the session open minus the stretch."""
    return _crosses_below(C, pl.col("session_open") - pl.col("stretch"))


#: Lundstrom reports rho in {0.5, 1.0, 1.5, 2.0}%. ONE value is fixed in advance --
#: testing all four and quoting the best is precisely the failure PREREGISTRATION.md
#: exists to prevent, and the four are nested enough that BH would not save it.
LUNDSTROM_RHO = 0.010


@pattern(
    name="lundstrom_orb_long", depth=2, direction="long", requires=("session_open",),
    risk=LUNDSTROM_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Lundstrom (2013), Umea Economic Studies 861",
)
def _lundstrom_orb_long() -> pl.Expr:
    """Price crosses 1.0% above the session open.

    Lundstrom's thresholds are `open x (1 +/- rho)`, with the OPPOSITE threshold serving
    as the stop -- so the published risk is exactly 2 x rho = 2% of price, and the
    position is closed at the session close. The engine cannot place a stop at a fixed
    percentage, so 0.6x daily ATR stands in: BTC's daily ATR is about 3.5% of price, so
    0.6x is roughly the 2% the source intends. On ETH and SOL, whose daily ATR differs,
    that correspondence is looser, and the stop is then the nearest ATR equivalent
    rather than the source's number.
    """
    return _crosses_above(C, pl.col("session_open") * (1.0 + LUNDSTROM_RHO))


@pattern(
    name="lundstrom_orb_short", depth=2, direction="short", requires=("session_open",),
    risk=LUNDSTROM_RISK, family=FAMILY, timeframes=INTRADAY_TF,
    source="Lundstrom (2013), Umea Economic Studies 861",
)
def _lundstrom_orb_short() -> pl.Expr:
    """Price crosses 1.0% below the session open."""
    return _crosses_below(C, pl.col("session_open") * (1.0 - LUNDSTROM_RHO))


@pattern(
    name="squeeze_breakout_long", depth=2, direction="long",
    requires=("bb_width", "bb_width_min_125d", "bb_upper"),
    risk=VOL_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Bollinger (2001), Bollinger on Bollinger Bands, 'The Squeeze'",
)
def _squeeze_breakout_long() -> pl.Expr:
    """Band width at a 125-day low, then a break of the upper band.

    Bollinger's simple form of the Squeeze is "the lowest volatility in six months",
    which on a daily chart is 125 bars; here it is 125 days at any timeframe. The
    squeeze is required on the PRIOR bar and the break on this one, because the two are
    sequential in the source -- "wait for a band break to signal the start of a new
    move" -- and a simultaneous test would be a different, rarer coincidence.

    `bb_width_min_125d` excludes the current bar, so the squeeze condition is a genuine
    new low in width rather than a comparison of a value against a window containing it.
    """
    return (
        (pl.col("bb_width").shift(1) <= pl.col("bb_width_min_125d").shift(1))
        & _crosses_above(C, pl.col("bb_upper"))
    )


@pattern(
    name="squeeze_breakout_short", depth=2, direction="short",
    requires=("bb_width", "bb_width_min_125d", "bb_lower"),
    risk=VOL_RISK, family=FAMILY, timeframes=SWING_TF,
    source="Bollinger (2001), Bollinger on Bollinger Bands, 'The Squeeze'",
)
def _squeeze_breakout_short() -> pl.Expr:
    """Band width at a 125-day low, then a break of the lower band."""
    return (
        (pl.col("bb_width").shift(1) <= pl.col("bb_width_min_125d").shift(1))
        & _crosses_below(C, pl.col("bb_lower"))
    )
