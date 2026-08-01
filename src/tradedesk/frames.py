"""The read boundary, and the causality guardrail.

Everything in phases 2-6 reads bars through `read_bars`. Its `as_of` parameter is
keyword-only with no default, so you cannot accidentally read the future: you cannot
call the function at all without stating what time you are pretending it is.

`BarFrame` then carries the contiguity information that sparse storage makes
necessary. On a 24/7 venue an absent bar means no trades occurred, and rows
`t-2, t-1, t` are not necessarily adjacent in time. A three-bar pattern detector
handed such a triple is detecting an artifact, so detectors take their windows from
`window_mask`, which refuses to mark a window valid unless the bars really are
consecutive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb
import polars as pl

from .timeutil import ET_NAME, tf_ms, to_ms

TS_DTYPE = pl.Datetime(time_unit="us", time_zone="UTC")


class MixedCalendarError(ValueError):
    """Bars carrying more than one session-calendar definition."""


@dataclass(frozen=True)
class BarFrame:
    """Bars plus the metadata needed to use them safely."""

    df: pl.DataFrame
    venue: str
    symbol: str
    timeframe: str
    calendar_version: int
    as_of_ms: int

    def __len__(self) -> int:
        return self.df.height

    @property
    def is_empty(self) -> bool:
        return self.df.is_empty()

    def to_polars(self) -> pl.DataFrame:
        """The underlying frame. Cheap -- polars frames are copy-on-write."""
        return self.df

    def with_local_time(self, tz: str = ET_NAME) -> pl.DataFrame:
        """Add a display column in local time. Never used for computation."""
        return self.df.with_columns(
            pl.col("ts_utc").dt.convert_time_zone(tz).alias("ts_local")
        )

    def gap_mask(self) -> pl.Series:
        """True where this bar is NOT adjacent to its predecessor."""
        step = tf_ms(self.timeframe)
        if self.df.is_empty():
            return pl.Series("gap", [], dtype=pl.Boolean)
        diffs = self.df["bar_open_ms"].diff()
        return (diffs != step).fill_null(True).alias("gap")

    def window_mask(self, n: int) -> pl.Series:
        """True at row i when rows i-n+1 .. i are genuinely consecutive in time.

        This is the guard that makes multi-bar pattern detection honest on a sparse
        series. Without it, a detector silently reaches across a no-trade hole and
        compares bars that are minutes or hours apart as though they were adjacent.
        """
        if n < 1:
            raise ValueError("window size must be >= 1")
        gaps = self.gap_mask()
        if self.df.is_empty():
            return pl.Series("window_ok", [], dtype=pl.Boolean)
        if n == 1:
            return pl.Series("window_ok", [True] * self.df.height)
        # A window is valid when none of its bars after the first opened a gap.
        breaks = (
            gaps.to_frame("gap")
            .with_columns(
                pl.col("gap")
                .cast(pl.Int32)
                .rolling_sum(window_size=n - 1, min_samples=n - 1)
                .alias("breaks")
            )["breaks"]
            .shift(0)
        )
        ok = (breaks == 0).fill_null(False)
        # The first n-1 rows have no complete history behind them.
        idx = pl.int_range(0, self.df.height, eager=True)
        return (ok & (idx >= n - 1)).alias("window_ok")

    def contiguous_windows(self, n: int) -> pl.DataFrame:
        """Only the rows whose n-bar lookback window is fully contiguous."""
        return self.df.filter(self.window_mask(n))


def _attach_ts(df: pl.DataFrame) -> pl.DataFrame:
    return df.with_columns(
        pl.from_epoch("bar_open_ms", time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("ts_utc")
    )


def read_bars(
    con: duckdb.DuckDBPyConnection,
    symbol: str,
    timeframe: str,
    *,
    as_of: datetime,
    venue: str = "coinbase",
    start: datetime | None = None,
    fill_gaps: str = "never",
) -> BarFrame:
    """Bars that had closed at or before `as_of`.

    `as_of` is required and keyword-only by design. The causality rule is applied
    here, once, rather than being re-derived (and eventually mis-derived) by every
    caller:

        a bar is visible at `as_of` only if  bar_open + timeframe <= as_of

    `fill_gaps` accepts only "never" in phase 1. Reindexing a sparse series onto a
    dense grid invites `fill_null(strategy="forward")`, which fabricates prices that
    never traded and would poison every statistic downstream. When a flagged variant
    is added it will insert explicit nulls, never carried-forward values.
    """
    if fill_gaps != "never":
        raise NotImplementedError(
            "only fill_gaps='never' is supported; gap filling would fabricate prices"
        )
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")

    as_of_ms = to_ms(as_of)
    visible_before = as_of_ms - tf_ms(timeframe) + 1  # bar_open + tf <= as_of

    params: list[object] = [venue, symbol, timeframe, visible_before]
    sql = (
        "SELECT * FROM bars "
        "WHERE venue = ? AND symbol = ? AND timeframe = ? AND bar_open_ms < ?"
    )
    if start is not None:
        sql += " AND bar_open_ms >= ?"
        params.append(to_ms(start))
    sql += " ORDER BY bar_open_ms"

    df = con.execute(sql, params).pl()

    versions = set()
    if not df.is_empty():
        versions = set(df["calendar_version"].unique().to_list())
        if len(versions) > 1:
            raise MixedCalendarError(
                f"{symbol} {timeframe} spans calendar versions {sorted(versions)}; "
                "refusing to blend incompatible session definitions"
            )
        df = _attach_ts(df)
        if df.schema["ts_utc"] != TS_DTYPE:
            raise TypeError(f"ts_utc dtype drifted: {df.schema['ts_utc']}")

    return BarFrame(
        df=df,
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        calendar_version=next(iter(versions)) if versions else 0,
        as_of_ms=as_of_ms,
    )
