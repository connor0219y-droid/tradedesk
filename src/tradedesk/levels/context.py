"""Phase 2B's causal inputs: relative volume and ATR percentile.

Phase 2B proper (instrument profile, catalyst calendar, in-play score) is deferred until
Phase 3 can validate it -- the brief is explicit that the in-play score must earn its
place by showing measurably different expectancy, and that machinery does not exist yet.

But these two inputs ship now, because Phase 3's context slicing needs them to answer
"does this setup work better on high relative-volume days".

RELATIVE VOLUME IS COMPUTED AT THE SAME TIME OF DAY, not against a full-day average.
The brief calls the full-day shortcut out by name as "common and badly wrong", and it is:
crypto volume has a pronounced intraday shape, so comparing a 03:00 bar against the
day's mean flags every quiet hour as unusual and every busy hour as normal.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div

RVOL_SESSIONS = 20


@level(
    name="rvol_tod",
    kind="cross_session",
    depth=RVOL_SESSIONS,
    outputs=("rvol_tod",),
)
def _rvol_tod(ctx: LevelContext) -> pl.DataFrame:
    """Volume relative to the same time of day over the previous 20 sessions.

    The comparison window is the last 20 same-time-of-day observations, ALL of which
    must come from unbroken sessions -- volume from a session containing a venue outage
    is not a fair baseline. Fewer than 20 qualifying observations yields null rather
    than a thin-sample ratio.
    """
    from .opening_range import add_et_midnight

    df = add_et_midnight(ctx.df)
    df = df.with_columns(
        (pl.col("bar_open_ms") - pl.col("et_midnight_ms")).alias("tod_ms"),
        # Broken sessions contribute no baseline.
        pl.when(pl.col("session_broken"))
        .then(None)
        .otherwise(pl.col("volume"))
        .alias("_vol_ok"),
    )

    # Rolling median of PRIOR sessions at the same time of day. shift(1) within the
    # time-of-day group is what makes it strictly prior -- without it the bar's own
    # volume sits in its own baseline.
    df = df.sort(["tod_ms", "bar_open_ms"])
    df = df.with_columns(
        pl.col("_vol_ok")
        .shift(1)
        .rolling_median(window_size=RVOL_SESSIONS, min_samples=RVOL_SESSIONS)
        .over("tod_ms")
        .alias("_median_tod")
    )
    df = df.sort("bar_open_ms")

    return df.with_columns(
        safe_div(pl.col("volume"), pl.col("_median_tod"), when_zero=None).alias("rvol_tod")
    ).drop("et_midnight_ms", "_vol_ok", "_median_tod")
