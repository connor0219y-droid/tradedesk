"""Momentum indicators: the EMA stack and Wilder's RSI.

These exist to reproduce one specific TradingView strategy's settings -- EMA 9, EMA 21,
a 200 EMA trend filter and RSI 14 -- closely enough that a disagreement between this
backtest and the chart is a real disagreement about the strategy rather than an artifact
of how the averages were seeded. Both run through `seeded_average`, which matches
ta.ema/ta.rma's SMA seeding; see the note in scale.py on why the older `ema_fast` /
`ema_slow` level does not.

PERIODS ARE IN THE COLUMN NAMES ON PURPOSE. `ema_200` cannot quietly become a 150 EMA
during a config edit, and a reader comparing this against a chart knows exactly which
average they are looking at without going and finding the constant.

Everything here is reset at contiguity breaks for the same reason ATR is: an EMA(200)
spanning a six-hour venue outage is averaging prices from either side of a hole as
though they were adjacent, and RSI's average gain/loss inherits the same infinite
memory from Wilder's smoothing.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div
from .scale import seeded_average

#: (period, column). Changing a period means changing the name with it.
EMA_PERIODS: tuple[tuple[int, str], ...] = ((9, "ema_9"), (21, "ema_21"), (200, "ema_200"))

RSI_PERIOD = 14
RSI_COLUMN = "rsi_14"


@level(name="ema_stack", kind="rolling", depth=2, outputs=("ema_9", "ema_21", "ema_200"))
def _ema_stack(ctx: LevelContext) -> pl.DataFrame:
    """EMA 9 / 21 / 200 on the close, SMA-seeded and reset per run.

    Declared depth is 2; the real requirement -- `period` contiguous bars before a value
    exists -- is enforced per-run inside `seeded_average`, because the warm-up follows
    the run rather than a fixed window. On BTC 5m that costs about 200 bars after each
    of the 11 contiguity breaks in four years, which is why `ema_200` is usable at all
    on a 24/7 venue.
    """
    df = ctx.df
    for period, col in EMA_PERIODS:
        df = seeded_average(
            df, "close", period=period, alpha=2.0 / (period + 1), out=col
        )
    return df


@level(name="rsi", kind="rolling", depth=2, outputs=(RSI_COLUMN,))
def _rsi(ctx: LevelContext) -> pl.DataFrame:
    """Wilder's RSI(14), reset at every contiguity break.

    Written as `100 * avg_gain / (avg_gain + avg_loss)` rather than the textbook
    `100 - 100/(1 + avg_gain/avg_loss)`. The two are algebraically identical, but the
    textbook form divides by avg_loss, which is genuinely zero on any 14-bar stretch
    that never ticked down -- polars would emit inf there and `assert_total` would
    (correctly) refuse the frame. This form only degenerates when the window is
    perfectly flat, where the answer is undefined and `safe_div` returns null.
    TradingView returns na in that same case.

    The bar that opens a gap gets a null change: its "previous close" can be hours
    stale, so the difference is measuring the hole rather than the bar.
    """
    change = pl.col("close") - pl.col("close").shift(1)
    df = ctx.df.with_columns(
        pl.when(pl.col("gap")).then(None).otherwise(change).alias("_chg")
    )
    # Null must survive the split into gains and losses. A bare `when(_chg > 0)` sends
    # nulls to `otherwise` and turns an unknown change into a zero gain, which would
    # seed the average off a bar that never happened.
    df = df.with_columns(
        pl.when(pl.col("_chg").is_null())
        .then(None)
        .when(pl.col("_chg") > 0)
        .then(pl.col("_chg"))
        .otherwise(pl.lit(0.0))
        .alias("_gain"),
        pl.when(pl.col("_chg").is_null())
        .then(None)
        .when(pl.col("_chg") < 0)
        .then(-pl.col("_chg"))
        .otherwise(pl.lit(0.0))
        .alias("_loss"),
    )
    df = seeded_average(
        df, "_gain", period=RSI_PERIOD, alpha=1.0 / RSI_PERIOD, out="_avg_gain"
    )
    df = seeded_average(
        df, "_loss", period=RSI_PERIOD, alpha=1.0 / RSI_PERIOD, out="_avg_loss"
    )
    return df.with_columns(
        safe_div(
            pl.col("_avg_gain") * 100.0,
            pl.col("_avg_gain") + pl.col("_avg_loss"),
            when_zero=None,
        ).alias(RSI_COLUMN)
    ).drop("_chg", "_gain", "_loss", "_avg_gain", "_avg_loss")
