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
     usual form when RSI is a confirmation rather than a mean-reversion trigger. The
     other live candidates are a wider band (55/45) or an overbought veto (long only
     while RSI < 70), which are different strategies and would need their own runs.
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

_REQUIRES = ("ema_9", "ema_21", "ema_200", "rsi_14")


@pattern(name="ema_cross_trend_long", depth=2, direction="long", requires=_REQUIRES)
def _ema_cross_trend_long() -> pl.Expr:
    """EMA 9 crosses above EMA 21, above the 200 EMA, with RSI above the midline.

    The `<=` on the prior bar makes a cross out of an equality: two EMAs that touch
    exactly and then separate upward is a cross, and `<` would miss it.
    """
    return (
        (FAST > SLOW)
        & (FAST.shift(1) <= SLOW.shift(1))
        & (C > TREND)
        & (RSI > RSI_MIDLINE)
    )


@pattern(name="ema_cross_trend_short", depth=2, direction="short", requires=_REQUIRES)
def _ema_cross_trend_short() -> pl.Expr:
    """EMA 9 crosses below EMA 21, below the 200 EMA, with RSI below the midline."""
    return (
        (FAST < SLOW)
        & (FAST.shift(1) >= SLOW.shift(1))
        & (C < TREND)
        & (RSI < RSI_MIDLINE)
    )
