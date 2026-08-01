"""Store round-tripping and coverage arithmetic."""

from __future__ import annotations

import polars as pl
import pytest

from tradedesk import store
from tradedesk.coverage import (
    clamp_coverage_end,
    covered_intervals,
    merge_intervals,
    plan_windows,
    record_coverage,
    subtract,
)
from tradedesk.frames import TS_DTYPE
from tradedesk.timeutil import tf_ms

STEP = tf_ms("5m")
BASE = 1_700_000_000_000 // STEP * STEP


def test_connection_pins_utc(con):
    """DuckDB defaults to the host timezone -- verified as America/Detroit here.

    Unpinned, the same stored instant prints differently locally and in CI, and
    someone eventually "fixes" a four-hour offset that was never there.
    """
    assert con.execute("SELECT current_setting('TimeZone')").fetchone()[0] == "UTC"


def test_bigint_round_trips_to_utc_tagged_datetime(con):
    con.execute(
        "INSERT INTO bars VALUES "
        "('coinbase','BTC/USD','5m', ?, 1,2,0.5,1.5, 10, DATE '2025-01-01', 1, 0, 0)",
        [BASE],
    )
    df = store.read_bars_raw(con, "coinbase", "BTC/USD", "5m")
    ts = df.with_columns(
        pl.from_epoch("bar_open_ms", time_unit="ms")
        .dt.replace_time_zone("UTC")
        .alias("ts_utc")
    )
    assert ts.schema["ts_utc"] == TS_DTYPE
    assert int(ts["bar_open_ms"][0]) == BASE


def test_bars_view_exposes_readable_timestamp(con):
    con.execute(
        "INSERT INTO bars VALUES "
        "('coinbase','BTC/USD','5m', ?, 1,2,0.5,1.5, 10, DATE '2025-01-01', 1, 0, 0)",
        [BASE],
    )
    got = con.execute("SELECT ts_utc FROM bars_v").fetchone()[0]
    assert got.year >= 2023


def test_insert_bars_is_idempotent(con):
    frame = pl.DataFrame(
        {
            "venue": ["coinbase"], "symbol": ["BTC/USD"], "timeframe": ["5m"],
            "bar_open_ms": [BASE], "open": [1.0], "high": [2.0], "low": [0.5],
            "close": [1.5], "volume": [10.0],
            "session_date": [pl.Series([1], dtype=pl.Int32).cast(pl.Date)[0]],
            "calendar_version": pl.Series([1], dtype=pl.Int16),
            "revision": pl.Series([0], dtype=pl.Int32),
            "ingested_at_ms": pl.Series([0], dtype=pl.Int64),
        }
    )
    assert store.insert_bars(con, frame) == 1
    assert store.insert_bars(con, frame) == 0
    assert con.execute("SELECT count(*) FROM bars").fetchone()[0] == 1


def test_merge_intervals_coalesces_touching_ranges():
    assert merge_intervals([(0, 10), (10, 20), (30, 40)]) == [(0, 20), (30, 40)]
    assert merge_intervals([(0, 10), (5, 20)]) == [(0, 20)]
    assert merge_intervals([]) == []


def test_subtract_finds_uncovered_gaps():
    assert subtract((0, 100), []) == [(0, 100)]
    assert subtract((0, 100), [(0, 100)]) == []
    assert subtract((0, 100), [(20, 40)]) == [(0, 20), (40, 100)]
    assert subtract((0, 100), [(0, 30), (70, 100)]) == [(30, 70)]
    # Coverage extending beyond the target must not produce negative ranges.
    assert subtract((10, 20), [(0, 100)]) == []


def test_plan_windows_never_exceeds_the_venue_cap():
    """Coinbase rejects an oversized window outright rather than truncating it."""
    windows = plan_windows([(BASE, BASE + 1000 * STEP)], "5m", 300)
    assert [w.n_bars for w in windows] == [300, 300, 300, 100]
    assert all(w.n_bars <= 300 for w in windows)
    # Windows must tile the gap exactly, with no overlap and no hole.
    assert windows[0].start_ms == BASE
    for a, b in zip(windows, windows[1:]):
        assert a.end_ms == b.start_ms
    assert windows[-1].end_ms == BASE + 1000 * STEP


def test_clamp_coverage_end_refuses_to_over_claim():
    """A full response may have been truncated by the row cap.

    Claiming the whole requested window anyway converts a fetch truncation into a
    permanent, invisible "no trades occurred" assertion -- destroying the only
    distinction the coverage table exists to make.
    """
    requested_end = BASE + 300 * STEP
    last_seen = BASE + 120 * STEP

    # Response hit the cap: trust only as far as the last bar actually seen.
    assert clamp_coverage_end(requested_end, last_seen, 300, 300, "5m") == last_seen + STEP
    # Short response: the venue had nothing more, so the full window is honest.
    assert clamp_coverage_end(requested_end, last_seen, 120, 300, "5m") == requested_end
    # Empty response: nothing to clamp to.
    assert clamp_coverage_end(requested_end, None, 0, 300, "5m") == requested_end


def test_record_coverage_merges_on_reread(con):
    record_coverage(con, "coinbase", "BTC/USD", "5m", range_start_ms=0,
                    range_end_ms=100, n_returned=1, fetched_at_ms=0)
    record_coverage(con, "coinbase", "BTC/USD", "5m", range_start_ms=100,
                    range_end_ms=200, n_returned=1, fetched_at_ms=0)
    assert covered_intervals(con, "coinbase", "BTC/USD", "5m") == [(0, 200)]

    # Re-recording the same start with a shorter end must never shrink coverage.
    record_coverage(con, "coinbase", "BTC/USD", "5m", range_start_ms=0,
                    range_end_ms=50, n_returned=1, fetched_at_ms=1)
    assert covered_intervals(con, "coinbase", "BTC/USD", "5m") == [(0, 200)]
