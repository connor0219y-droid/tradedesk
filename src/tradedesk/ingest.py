"""The ingest pipeline: plan -> fetch -> guard -> validate -> write.

Idempotent and resumable by construction. The full backfill is ~7,000 requests per
symbol at 1m, so interruption is the normal case rather than an edge case; every
window either lands completely, with its coverage row, in a single transaction, or
does not land at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dtime

import duckdb
import polars as pl

from .config import Config
from .coverage import clamp_coverage_end, plan_fetch, record_coverage
from .quality import checks
from .store import insert_bars, record_quality_issues, transaction
from .timeutil import (
    ET_NAME,
    UTC,
    is_bar_final,
    now_ms,
    readable_watermark,
    tf_ms,
    to_ms,
)
from .venues.base import Venue


@dataclass
class IngestStats:
    symbol: str
    timeframe: str
    windows_planned: int = 0
    windows_fetched: int = 0
    bars_returned: int = 0
    bars_inserted: int = 0
    bars_dropped_unsettled: int = 0
    bars_dropped_duplicate: int = 0
    issues: dict[str, int] = field(default_factory=dict)
    requests: int = 0

    def merge_issue_counts(self, counts: dict[str, int]) -> None:
        for name, n in counts.items():
            self.issues[name] = self.issues.get(name, 0) + n


def rows_to_frame(
    rows: list[list[float]],
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    calendar_version: int,
    ingested_at_ms: int,
) -> pl.DataFrame:
    """Venue rows -> a store-shaped frame, with the ET session date attached.

    The session label is computed once, at ingest, from the UTC instant. Going
    UTC -> ET is always unambiguous, so no strict localisation is needed here; it is
    building grids in the other direction that is dangerous.
    """
    if not rows:
        return empty_bar_frame()

    df = pl.DataFrame(
        {
            "bar_open_ms": [int(r[0]) for r in rows],
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        }
    )
    return df.with_columns(
        pl.lit(venue).alias("venue"),
        pl.lit(symbol).alias("symbol"),
        pl.lit(timeframe).alias("timeframe"),
        pl.from_epoch("bar_open_ms", time_unit="ms")
        .dt.replace_time_zone("UTC")
        .dt.convert_time_zone(ET_NAME)
        .dt.date()
        .alias("session_date"),
        pl.lit(calendar_version, dtype=pl.Int16).alias("calendar_version"),
        pl.lit(0, dtype=pl.Int32).alias("revision"),
        pl.lit(ingested_at_ms, dtype=pl.Int64).alias("ingested_at_ms"),
    )


def empty_bar_frame() -> pl.DataFrame:
    return pl.DataFrame(
        schema={
            "bar_open_ms": pl.Int64,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "volume": pl.Float64,
            "venue": pl.String,
            "symbol": pl.String,
            "timeframe": pl.String,
            "session_date": pl.Date,
            "calendar_version": pl.Int16,
            "revision": pl.Int32,
            "ingested_at_ms": pl.Int64,
        }
    )


def ingest(
    con: duckdb.DuckDBPyConnection,
    venue: Venue,
    cfg: Config,
    symbol: str,
    timeframe: str,
    *,
    target_start_ms: int,
    target_end_ms: int | None = None,
    now: int | None = None,
    on_progress=None,
) -> IngestStats:
    """Fetch whatever is still missing for one symbol/timeframe."""
    stats = IngestStats(symbol=symbol, timeframe=timeframe)
    now = now if now is not None else now_ms()
    settle_ms = cfg.venue.settle_ms
    step = tf_ms(timeframe)

    # Never request past the settled watermark. Anything newer is either still
    # forming or not yet published; claiming coverage over it would freeze a false
    # gap that no later run would revisit, because coverage would say we had looked.
    watermark = readable_watermark(timeframe, now, settle_ms)
    end = min(target_end_ms, watermark) if target_end_ms is not None else watermark
    if end <= target_start_ms:
        return stats

    windows = plan_fetch(
        con,
        venue.name,
        symbol,
        timeframe,
        target_start_ms=target_start_ms,
        target_end_ms=end,
        max_bars=venue.max_bars_per_request,
    )
    stats.windows_planned = len(windows)

    for window in windows:
        limit = min(window.n_bars, venue.max_bars_per_request)
        rows = venue.fetch_ohlcv(
            symbol, timeframe, since_ms=window.start_ms, limit=limit
        )
        stats.windows_fetched += 1
        stats.bars_returned += len(rows)

        # Drop anything not yet closed and settled. Both venues return the forming
        # bucket; storing it puts a mutable, wrong bar in the database permanently --
        # and it is exactly the bar a live signal would fire on.
        settled = [r for r in rows if is_bar_final(int(r[0]), timeframe, now, settle_ms)]
        stats.bars_dropped_unsettled += len(rows) - len(settled)

        ingested_at = now_ms()
        frame = rows_to_frame(
            settled,
            venue=venue.name,
            symbol=symbol,
            timeframe=timeframe,
            calendar_version=cfg.session.calendar_version,
            ingested_at_ms=ingested_at,
        )
        # Adjacent pages can overlap by one bar depending on boundary conventions.
        # Deduplicate here, in polars, so the behaviour is explicit rather than
        # dependent on how a particular DuckDB version handles within-statement
        # conflicts.
        before = frame.height
        frame = frame.unique(subset=["bar_open_ms"], keep="first").sort("bar_open_ms")
        stats.bars_dropped_duplicate += before - frame.height

        issues = checks.run_causal_checks(
            frame,
            venue=venue.name,
            symbol=symbol,
            timeframe=timeframe,
            detected_at_ms=ingested_at,
            cfg=cfg,
        )

        last_returned = int(settled[-1][0]) if settled else None
        # If the response came back full it may have been truncated by the row cap;
        # anything past the last bar we actually saw is unverified. Claiming it would
        # mint a permanent, invisible "no trades" assertion over data we never got.
        coverage_end = clamp_coverage_end(
            window.end_ms, last_returned, len(rows), limit, timeframe
        )
        # Never claim coverage over unsettled territory either.
        coverage_end = min(coverage_end, watermark)

        with transaction(con):
            stats.bars_inserted += insert_bars(con, frame)
            if not issues.is_empty():
                record_quality_issues(con, issues)
            record_coverage(
                con,
                venue.name,
                symbol,
                timeframe,
                range_start_ms=window.start_ms,
                range_end_ms=coverage_end,
                n_returned=len(settled),
                fetched_at_ms=ingested_at,
            )

        if not issues.is_empty():
            counts = (
                issues.group_by("check_name").len().rows_by_key("check_name", unique=True)
            )
            stats.merge_issue_counts({k: v[0] for k, v in counts.items()})

        if on_progress is not None:
            on_progress(stats, window)

    stats.requests = getattr(venue, "request_count", 0)
    return stats


def target_range_ms(cfg: Config, timeframe: str, now: int | None = None) -> tuple[int, int]:
    """The full range the config asks us to hold for a timeframe."""
    now = now if now is not None else now_ms()
    start = to_ms(datetime.combine(cfg.data.history_start, dtime(0, 0), tzinfo=UTC))
    return start, readable_watermark(timeframe, now, cfg.venue.settle_ms)
