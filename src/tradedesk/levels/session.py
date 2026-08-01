"""ET session segmentation, contiguity runs, and session integrity.

Three derived columns that everything else builds on:

  run_id         -- contiguous-bar run index. Increments at every gap, so grouping by
                    it confines any rolling computation to bars that really are
                    adjacent in time.
  session_broken -- whether a hole of at least `outage_minutes` has occurred AT OR
                    BEFORE this bar, within this session.
  bar_idx_in_session -- 0-based position, used for "is this the first bar" guards.

The `at or before` in session_broken is the interesting part, and it falls straight out
of causality. A six-hour hole at 14:00 does NOT invalidate the 10:00 VWAP -- at 10:00
that hole has not happened yet, and the level used only complete data. So session
levels are nulled from the hole onward, not for the whole session. That is both
strictly causal and preserves more data than invalidating the session wholesale.
"""

from __future__ import annotations

import polars as pl

from ..timeutil import tf_ms as _tf_ms


def add_session_columns(
    df: pl.DataFrame, *, timeframe: str, outage_minutes: float
) -> pl.DataFrame:
    """Attach run_id, gap, session_broken, bar_idx_in_session, session_start_ms.

    `df` must be sorted by bar_open_ms and carry `session_date` (written at ingest as
    the ET calendar day, so DST is already handled and never recomputed here).
    """
    if df.is_empty():
        return df.with_columns(
            pl.lit(None, dtype=pl.Boolean).alias("gap"),
            pl.lit(None, dtype=pl.UInt32).alias("run_id"),
            pl.lit(None, dtype=pl.Boolean).alias("session_broken"),
            pl.lit(None, dtype=pl.UInt32).alias("bar_idx_in_session"),
            pl.lit(None, dtype=pl.Int64).alias("session_start_ms"),
            pl.lit(None, dtype=pl.Int64).alias("hole_ms"),
        )

    step = _tf_ms(timeframe)
    outage_ms = int(outage_minutes * 60_000)

    df = df.with_columns(
        (pl.col("bar_open_ms").diff() != step).fill_null(True).alias("gap"),
        (pl.col("session_date") == pl.col("session_date").shift(1))
        .fill_null(False)
        .alias("same_session"),
    )

    # Missing time immediately before this bar. Only meaningful within a session -- a
    # diff across a session boundary is a boundary, not a hole.
    df = df.with_columns(
        pl.when(pl.col("same_session"))
        .then(pl.col("bar_open_ms").diff() - step)
        .otherwise(pl.lit(0, dtype=pl.Int64))
        .fill_null(0)
        .alias("hole_ms")
    )

    return df.with_columns(
        pl.col("gap").cum_sum().cast(pl.UInt32).alias("run_id"),
        ((pl.col("hole_ms") >= outage_ms).cum_sum().over("session_date") > 0).alias(
            "session_broken"
        ),
        pl.int_range(pl.len()).over("session_date").cast(pl.UInt32).alias(
            "bar_idx_in_session"
        ),
        pl.col("bar_open_ms").min().over("session_date").alias("session_start_ms"),
    ).drop("same_session")


def session_valid(df: pl.DataFrame) -> pl.DataFrame:
    """One row per session with whether it ever broke. Used for cross-session levels.

    A prior day's high/low/close is only usable if that whole session was intact --
    unlike an intra-session level, a completed session is judged as a whole because
    downstream consumers read it as a single summary number.
    """
    return (
        df.group_by("session_date")
        .agg(
            pl.col("session_broken").last().alias("broken"),
            pl.len().alias("n_bars"),
            pl.col("high").max().alias("s_high"),
            pl.col("low").min().alias("s_low"),
            pl.col("close").last().alias("s_close"),
            pl.col("open").first().alias("s_open"),
            pl.col("bar_open_ms").min().alias("s_start_ms"),
        )
        .sort("session_date")
        .with_columns((~pl.col("broken")).alias("valid"))
    )
