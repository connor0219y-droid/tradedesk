"""Per-bar shape features.

Every one of these divides by the bar's range, which is exactly zero on 19,852 SOL/USD
1m bars -- real minutes in which every trade printed at one price. The agreed answer is
null: the quantity is genuinely undefined, and this project does not fabricate values
anywhere else either (it will not forward-fill a missing bar).

These are per-bar, so nulling them does NOT cascade into ATR or any rolling level.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div, safe_ln

RANGE = pl.col("high") - pl.col("low")


@level(
    name="barshape",
    kind="per_bar",
    depth=1,
    outputs=("close_pos_in_range", "body_frac", "upper_wick_frac", "lower_wick_frac"),
)
def _barshape(ctx: LevelContext) -> pl.DataFrame:
    max_oc = pl.max_horizontal("open", "close")
    min_oc = pl.min_horizontal("open", "close")
    return ctx.df.with_columns(
        RANGE.alias("bar_range"),
        safe_div(pl.col("close") - pl.col("low"), RANGE, when_zero=None).alias(
            "close_pos_in_range"
        ),
        safe_div((pl.col("close") - pl.col("open")).abs(), RANGE, when_zero=None).alias(
            "body_frac"
        ),
        safe_div(pl.col("high") - max_oc, RANGE, when_zero=None).alias("upper_wick_frac"),
        safe_div(min_oc - pl.col("low"), RANGE, when_zero=None).alias("lower_wick_frac"),
    )


@level(name="bar_return", kind="rolling", depth=2, outputs=("bar_return",))
def _bar_return(ctx: LevelContext) -> pl.DataFrame:
    """Log return against the previous bar's close.

    A 2-bar feature, so the engine nulls it at every gap bar -- where the 'previous'
    close can be six hours stale. safe_ln additionally guards non-positive prices,
    which the ingest checks already flag as ERROR but which must not reach log().
    """
    return ctx.df.with_columns(
        safe_ln(
            safe_div(pl.col("close"), pl.col("close").shift(1), when_zero=None)
        ).alias("bar_return")
    )
