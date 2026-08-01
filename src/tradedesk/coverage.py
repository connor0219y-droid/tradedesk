"""Coverage bookkeeping: what has been *requested*, as opposed to what came back.

On a venue that omits no-trade buckets, a missing bar is ambiguous. Only by recording
the requested ranges separately can we tell:

    covered   + no bar  ->  ABSENT_NO_TRADES   (real market information)
    uncovered + no bar  ->  UNKNOWN            (we simply have not looked)

That distinction is what makes incremental fetch converge, and what stops the quality
report from confusing an outage with a quiet market.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb

from .timeutil import align_down, align_up, tf_ms

Interval = tuple[int, int]  # [start, end) in UTC ms


@dataclass(frozen=True)
class FetchWindow:
    """One planned request: [start_ms, end_ms), at most `max_bars` buckets wide."""

    start_ms: int
    end_ms: int
    n_bars: int


def merge_intervals(intervals: list[Interval]) -> list[Interval]:
    """Coalesce overlapping and touching intervals."""
    if not intervals:
        return []
    ordered = sorted(intervals)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:  # touching counts as contiguous
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def subtract(target: Interval, covered: list[Interval]) -> list[Interval]:
    """The parts of `target` not already covered."""
    start, end = target
    gaps: list[Interval] = []
    cursor = start
    for c_start, c_end in merge_intervals(covered):
        if c_end <= cursor:
            continue
        if c_start >= end:
            break
        if c_start > cursor:
            gaps.append((cursor, min(c_start, end)))
        cursor = max(cursor, c_end)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return [(s, e) for s, e in gaps if e > s]


def covered_intervals(
    con: duckdb.DuckDBPyConnection, venue: str, symbol: str, timeframe: str
) -> list[Interval]:
    rows = con.execute(
        """
        SELECT range_start_ms, range_end_ms FROM coverage
        WHERE venue = ? AND symbol = ? AND timeframe = ?
        ORDER BY range_start_ms
        """,
        [venue, symbol, timeframe],
    ).fetchall()
    return merge_intervals([(int(a), int(b)) for a, b in rows])


def plan_windows(
    gaps: list[Interval], timeframe: str, max_bars: int
) -> list[FetchWindow]:
    """Split uncovered ranges into requests no larger than the venue's cap.

    Coinbase *rejects* an oversized window (HTTP 400) rather than truncating it, so
    the cap is a hard constraint rather than a performance hint.
    """
    step = tf_ms(timeframe)
    span = step * max_bars
    windows: list[FetchWindow] = []
    for gap_start, gap_end in gaps:
        cursor = align_down(gap_start, timeframe)
        end = align_up(gap_end, timeframe)
        while cursor < end:
            stop = min(cursor + span, end)
            windows.append(FetchWindow(cursor, stop, (stop - cursor) // step))
            cursor = stop
    return windows


def plan_fetch(
    con: duckdb.DuckDBPyConnection,
    venue: str,
    symbol: str,
    timeframe: str,
    *,
    target_start_ms: int,
    target_end_ms: int,
    max_bars: int,
) -> list[FetchWindow]:
    """Everything still needed to cover [target_start_ms, target_end_ms)."""
    if target_end_ms <= target_start_ms:
        return []
    already = covered_intervals(con, venue, symbol, timeframe)
    gaps = subtract((target_start_ms, target_end_ms), already)
    return plan_windows(gaps, timeframe, max_bars)


def record_coverage(
    con: duckdb.DuckDBPyConnection,
    venue: str,
    symbol: str,
    timeframe: str,
    *,
    range_start_ms: int,
    range_end_ms: int,
    n_returned: int,
    fetched_at_ms: int,
) -> None:
    """Claim a range as fetched.

    Callers must clamp `range_end_ms` when a response hit the row cap -- see
    `clamp_coverage_end`. Over-claiming here is the most corrupting bug available in
    this design: it converts a truncated fetch into a permanent, invisible
    "no trades occurred" assertion.
    """
    if range_end_ms <= range_start_ms:
        return
    con.execute(
        """
        INSERT INTO coverage
            (venue, symbol, timeframe, range_start_ms, range_end_ms,
             n_returned, fetched_at_ms)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (venue, symbol, timeframe, range_start_ms) DO UPDATE SET
            range_end_ms  = greatest(coverage.range_end_ms, excluded.range_end_ms),
            n_returned    = excluded.n_returned,
            fetched_at_ms = excluded.fetched_at_ms
        """,
        [
            venue,
            symbol,
            timeframe,
            range_start_ms,
            range_end_ms,
            n_returned,
            fetched_at_ms,
        ],
    )


def clamp_coverage_end(
    requested_end_ms: int,
    last_returned_ms: int | None,
    n_returned: int,
    limit: int,
    timeframe: str,
) -> int:
    """How much of a requested window we may honestly claim as covered.

    If the response came back full, it may have been truncated by the row cap, and
    everything past the last bar we actually saw is unverified. Claiming it anyway
    would mint a permanent false ABSENT_NO_TRADES over data we never received.
    """
    if n_returned >= limit and last_returned_ms is not None:
        return min(requested_end_ms, last_returned_ms + tf_ms(timeframe))
    return requested_end_ms


def coverage_summary(
    con: duckdb.DuckDBPyConnection, venue: str, symbol: str, timeframe: str
) -> tuple[int, int, int]:
    """(n_ranges, first_covered_ms, last_covered_ms) after merging."""
    intervals = covered_intervals(con, venue, symbol, timeframe)
    if not intervals:
        return 0, 0, 0
    return len(intervals), intervals[0][0], intervals[-1][1]
