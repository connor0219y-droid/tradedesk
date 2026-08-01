"""Session VWAP and its sigma bands, anchored at 00:00 ET.

The numerics matter here more than anywhere else in the engine. The textbook
formulation

    var = sum(v * tp^2)/sum(v) - vwap^2

catastrophically cancels at crypto prices: with tp around 60,000, tp^2 is about 3.6e9,
and the two terms agree to roughly ten significant figures. What is left is mostly
floating-point noise, and it goes NEGATIVE -- at which point sqrt() returns NaN, which
then silently poisons every rolling window downstream.

The fix is the shifted-data trick. Variance is translation-invariant, so subtracting a
per-session constant before squaring is EXACT, not an approximation, and it drops the
magnitudes from ~6e4 to ~1e2. safe_sqrt then clamps any residual tiny negative to zero
-- correcting float error, not inventing data.

Everything is a cumulative sum over the session, so VWAP at bar t reflects only bars up
to t. It is a running value, never the session's final value back-applied.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div, safe_sqrt

TYPICAL = (pl.col("high") + pl.col("low") + pl.col("close")) / 3.0


@level(
    name="vwap",
    kind="session",
    depth=1,
    outputs=(
        "typical_price",
        "vwap",
        "vwap_sigma",
        "vwap_upper_1s",
        "vwap_lower_1s",
        "vwap_upper_2s",
        "vwap_lower_2s",
        "vwap_z",
    ),
)
def _vwap(ctx: LevelContext) -> pl.DataFrame:
    df = ctx.df.with_columns(TYPICAL.alias("typical_price"))

    # Per-session reference price. Any session constant works; the first typical price
    # is the natural one and is available causally at the session's first bar.
    df = df.with_columns(
        pl.col("typical_price").first().over("session_date").alias("_ref")
    )
    df = df.with_columns((pl.col("typical_price") - pl.col("_ref")).alias("_d"))

    df = df.with_columns(
        pl.col("volume").cum_sum().over("session_date").alias("_sw"),
        (pl.col("volume") * pl.col("_d")).cum_sum().over("session_date").alias("_swd"),
        (pl.col("volume") * pl.col("_d") * pl.col("_d"))
        .cum_sum()
        .over("session_date")
        .alias("_swdd"),
    )

    mean_d = safe_div(pl.col("_swd"), pl.col("_sw"), when_zero=None)
    df = df.with_columns(
        (pl.col("_ref") + mean_d).alias("vwap"),
        (safe_div(pl.col("_swdd"), pl.col("_sw"), when_zero=None) - mean_d.pow(2)).alias(
            "_var"
        ),
    )

    # A single observation has no dispersion. n == 1 is the session's first bar.
    df = df.with_columns(
        pl.when(pl.col("bar_idx_in_session") < 1)
        .then(None)
        .otherwise(safe_sqrt(pl.col("_var")))
        .alias("vwap_sigma")
    )

    return df.with_columns(
        (pl.col("vwap") + pl.col("vwap_sigma")).alias("vwap_upper_1s"),
        (pl.col("vwap") - pl.col("vwap_sigma")).alias("vwap_lower_1s"),
        (pl.col("vwap") + 2 * pl.col("vwap_sigma")).alias("vwap_upper_2s"),
        (pl.col("vwap") - 2 * pl.col("vwap_sigma")).alias("vwap_lower_2s"),
        safe_div(
            pl.col("close") - pl.col("vwap"), pl.col("vwap_sigma"), when_zero=None
        ).alias("vwap_z"),
    ).drop("_ref", "_d", "_sw", "_swd", "_swdd", "_var")
