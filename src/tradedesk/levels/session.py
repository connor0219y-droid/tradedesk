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

from ..calendars import CalendarError, MarketCalendar
from ..timeutil import tf_ms as _tf_ms


def add_session_anchors(
    df: pl.DataFrame, *, calendar: MarketCalendar, timeframe: str
) -> pl.DataFrame:
    """Attach the session's real open and close, and each bar's place inside it.

    THIS IS THE SEAM BETWEEN INSTRUMENT CLASSES. Before equities, every level measured
    time as "milliseconds since ET midnight", which is correct for a 24-hour crypto day
    and wrong for a 6.5-hour equity session in three separate ways: the anchor is seven
    and a half hours early, the session is a quarter as long, and 17 days in the sample
    end at 13:00. Levels now read `ms_since_open` and `ms_to_session_end` from here
    instead of recomputing an anchor, so there is one definition to be right about.

    `ms_since_open` is NEGATIVE in the premarket, deliberately. It is the honest answer
    -- those bars are before the open -- and it makes the RTH test `ms_since_open >= 0`
    rather than a separate lookup. An opening range that forgot the sign would treat
    every premarket bar as inside the first thirty minutes.

    For crypto this reduces exactly to the old ET-midnight arithmetic: the session opens
    at 00:00, runs 23, 24 or 25 hours, and every bar is in the single segment. That
    equality is what keeps findings 1-8 reproducible, and it is asserted in the tests.
    """
    if df.is_empty():
        return df.with_columns(
            pl.lit(None, dtype=pl.Int64).alias("session_open_ms"),
            pl.lit(None, dtype=pl.Int64).alias("session_close_ms"),
            pl.lit(None, dtype=pl.Int64).alias("ms_since_open"),
            pl.lit(None, dtype=pl.Int64).alias("ms_to_session_end"),
            pl.lit(None, dtype=pl.String).alias("session_segment"),
            pl.lit(None, dtype=pl.Boolean).alias("early_close"),
        )

    opens: dict[object, int | None] = {}
    closes: dict[object, int | None] = {}
    pre: dict[object, int | None] = {}
    early: dict[object, bool | None] = {}
    for day in df["session_date"].unique().to_list():
        try:
            w = calendar.window(day)
        except CalendarError:
            # A bar on a day the market was shut. Nulled rather than guessed, so the
            # quality layer sees it as unanchorable instead of silently placing it in a
            # session that did not happen.
            opens[day] = closes[day] = pre[day] = None
            early[day] = None
            continue
        opens[day], closes[day] = w.open_ms, w.close_ms
        pre[day], early[day] = w.premarket_open_ms, w.early_close

    step = _tf_ms(timeframe)
    df = df.with_columns(
        pl.col("session_date").replace_strict(opens, return_dtype=pl.Int64)
        .alias("session_open_ms"),
        pl.col("session_date").replace_strict(closes, return_dtype=pl.Int64)
        .alias("session_close_ms"),
        pl.col("session_date").replace_strict(pre, return_dtype=pl.Int64)
        .alias("_premarket_open_ms"),
        pl.col("session_date").replace_strict(early, return_dtype=pl.Boolean)
        .alias("early_close"),
    )

    return df.with_columns(
        (pl.col("bar_open_ms") - pl.col("session_open_ms")).alias("ms_since_open"),
        # Measured from this bar's CLOSE, so a rule phrased "at the start of the last
        # half hour" can name the bar that must carry the signal.
        (pl.col("session_close_ms") - (pl.col("bar_open_ms") + step))
        .alias("ms_to_session_end"),
        pl.when(pl.col("session_open_ms").is_null())
        .then(None)
        .when(pl.col("bar_open_ms") >= pl.col("session_open_ms"))
        .then(pl.lit("rth"))
        .when(
            pl.col("_premarket_open_ms").is_not_null()
            & (pl.col("bar_open_ms") >= pl.col("_premarket_open_ms"))
        )
        .then(pl.lit("premarket"))
        .otherwise(None)
        .alias("session_segment"),
    ).drop("_premarket_open_ms")


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
