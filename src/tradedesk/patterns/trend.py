"""The EMA-cross-with-trend-filter setup, reconstructed from a TradingView strategy.

WHAT WAS GIVEN AND WHAT WAS INFERRED. The chart's settings panel supplied the numbers
(EMA 9 / 21, EMA 200 trend filter, RSI 14, ATR 14, 2xATR stop, 3xATR target, 6 bars
minimum between entries). The entry CONDITION was not visible, so it is reconstructed
below in its standard form. Every choice that could reasonably have gone another way is
listed here rather than buried in the expression, because an unstated choice makes the
result untestable -- if this disagrees with the chart, the disagreement should be
locatable in this list:

  1. FRESH CROSS, not a standing condition. The signal is the bar on which EMA 9 goes
     from at-or-below EMA 21 to above it. The alternative reading -- "long whenever
     fast > slow" -- fires on every bar of a trend and would produce tens of thousands
     of signals rather than the ~120-per-two-months the chart reports.
  2. TREND FILTER ON THE CLOSE. Long requires close > EMA 200. The common alternatives
     are EMA 21 > EMA 200 or EMA 9 > EMA 200; "price side of the 200 EMA" was the
     stated reading, so the close it is.
  3. RSI CONFIRMS AT THE MIDLINE. Long requires RSI(14) > 50, short < 50. This is the
     usual form when RSI is a confirmation rather than a mean-reversion trigger.
     Because it was the least-constrained inference, the two other live readings -- a
     55/45 band and a midline-plus-overbought-veto -- are REGISTERED AS THEIR OWN
     DETECTORS rather than swapped in behind a flag. That is deliberate: running three
     thresholds and quoting the best is the garden of forking paths, so all three sit
     in the same registry and take the same Benjamini-Hochberg correction across the
     family. Note they are nested and highly correlated, which makes BH conservative
     here rather than lenient.
  4. BOTH DIRECTIONS, registered separately. Long and short are two detectors, so their
     expectancies never cancel into a meaningless combined number.
  5. Everything is evaluated at the signal bar's CLOSE, and the engine enters at the
     next bar's open. TradingView's default `process_orders_on_close = false` does the
     same thing.

Not encoded here, because they belong to the backtest rather than the detector: the
2xATR stop and 3xATR target (stop_atr=2.0 with target_r=1.5, since 3 ATR is 1.5x a
2-ATR risk unit) and the 6-bar minimum spacing (`min_bars_between_entries`).
"""

from __future__ import annotations

import polars as pl

from .base import pattern

FAST, SLOW, TREND = pl.col("ema_9"), pl.col("ema_21"), pl.col("ema_200")
C = pl.col("close")
RSI = pl.col("rsi_14")

#: RSI is a confirmation filter here, so it splits at the midline rather than at an
#: overbought/oversold boundary. Assumption 3 above.
RSI_MIDLINE = 50.0
#: The wider-band reading: demand more momentum before believing the cross.
RSI_BAND = 55.0
#: The exhaustion reading: momentum confirmed at the midline, but not already spent.
RSI_OVERBOUGHT, RSI_OVERSOLD = 70.0, 30.0

_REQUIRES = ("ema_9", "ema_21", "ema_200", "rsi_14")


def _long(rsi_ok: pl.Expr) -> pl.Expr:
    """Cross up, above the trend filter, plus whichever RSI reading is being tested.

    The cross and trend halves live here rather than in each variant so a sensitivity
    test over the RSI threshold varies ONLY the RSI threshold. Six copy-pasted bodies
    that drift apart would produce a comparison table measuring the drift.

    The `<=` on the prior bar makes a cross out of an equality: two EMAs that touch
    exactly and then separate upward is a cross, and `<` would miss it.
    """
    return (FAST > SLOW) & (FAST.shift(1) <= SLOW.shift(1)) & (C > TREND) & rsi_ok


def _short(rsi_ok: pl.Expr) -> pl.Expr:
    """Mirror of `_long`."""
    return (FAST < SLOW) & (FAST.shift(1) >= SLOW.shift(1)) & (C < TREND) & rsi_ok


# ---- assumption 3 as stated: RSI confirms at the midline


@pattern(name="ema_cross_trend_long", depth=2, direction="long", requires=_REQUIRES)
def _ema_cross_trend_long() -> pl.Expr:
    """EMA 9 crosses above EMA 21, above the 200 EMA, RSI above the midline."""
    return _long(RSI > RSI_MIDLINE)


@pattern(name="ema_cross_trend_short", depth=2, direction="short", requires=_REQUIRES)
def _ema_cross_trend_short() -> pl.Expr:
    """EMA 9 crosses below EMA 21, below the 200 EMA, RSI below the midline."""
    return _short(RSI < RSI_MIDLINE)


# ---- the 55/45 band: the same idea, demanding more momentum


@pattern(name="ema_cross_trend_rsi55_long", depth=2, direction="long", requires=_REQUIRES)
def _ema_cross_trend_rsi55_long() -> pl.Expr:
    """As above, but RSI must clear 55 rather than 50."""
    return _long(RSI > RSI_BAND)


@pattern(name="ema_cross_trend_rsi55_short", depth=2, direction="short", requires=_REQUIRES)
def _ema_cross_trend_rsi55_short() -> pl.Expr:
    """As above, but RSI must be below 45 rather than 50."""
    return _short(RSI < 100.0 - RSI_BAND)


# ---- the overbought veto: confirmed, but not already exhausted


@pattern(name="ema_cross_trend_veto_long", depth=2, direction="long", requires=_REQUIRES)
def _ema_cross_trend_veto_long() -> pl.Expr:
    """Midline confirmation AND an overbought veto: 50 < RSI < 70.

    Read as "momentum agrees but the move is not already spent". The other reading of
    an overbought veto -- RSI < 70 with no midline requirement -- is not tested here
    because it admits RSI-35 longs, which is not a filter on a trend-following entry
    so much as the absence of one.
    """
    return _long((RSI > RSI_MIDLINE) & (RSI < RSI_OVERBOUGHT))


@pattern(name="ema_cross_trend_veto_short", depth=2, direction="short", requires=_REQUIRES)
def _ema_cross_trend_veto_short() -> pl.Expr:
    """Mirror: 30 < RSI < 50."""
    return _short((RSI < RSI_MIDLINE) & (RSI > RSI_OVERSOLD))
