"""DuckDB candle store.

Two decisions here carry the whole design:

1. `bar_open_ms BIGINT` is the canonical time column. DuckDB's TIMESTAMPTZ renders in
   the *session* timezone, which defaults to the host zone -- verified as
   `America/Detroit` on this machine. The same stored instant would print differently
   locally and in CI, and a developer eyeballing a report would "correct" a
   non-existent offset. An integer has no such ambiguity. `SET TimeZone='UTC'` is
   applied on every connection anyway, belt and braces.

2. Storage is SPARSE. We store exactly the bars the venue returned and never
   synthesise a row. Coinbase documents that it publishes no data for intervals with
   no ticks, so absence is meaningful data, not a defect to be filled.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import duckdb
import polars as pl

SCHEMA_VERSION = 1

_DDL = [
    """
    CREATE TABLE IF NOT EXISTS bars (
        venue            VARCHAR  NOT NULL,
        symbol           VARCHAR  NOT NULL,
        timeframe        VARCHAR  NOT NULL,
        bar_open_ms      BIGINT   NOT NULL,
        open             DOUBLE   NOT NULL,
        high             DOUBLE   NOT NULL,
        low              DOUBLE   NOT NULL,
        close            DOUBLE   NOT NULL,
        volume           DOUBLE   NOT NULL,
        session_date     DATE     NOT NULL,
        calendar_version SMALLINT NOT NULL,
        revision         INTEGER  NOT NULL DEFAULT 0,
        ingested_at_ms   BIGINT   NOT NULL,
        PRIMARY KEY (venue, symbol, timeframe, bar_open_ms)
    )
    """,
    # What was REQUESTED, not what came back. This table is what makes idempotency
    # possible at all: a missing timestamp is ambiguous between "no trades occurred"
    # and "we never fetched this window", and the bars table cannot tell them apart.
    # A fetcher that infers its resume point from missing timestamps re-fetches the
    # same holes on every run and never converges.
    """
    CREATE TABLE IF NOT EXISTS coverage (
        venue          VARCHAR NOT NULL,
        symbol         VARCHAR NOT NULL,
        timeframe      VARCHAR NOT NULL,
        range_start_ms BIGINT  NOT NULL,   -- inclusive
        range_end_ms   BIGINT  NOT NULL,   -- exclusive
        n_returned     INTEGER NOT NULL,
        fetched_at_ms  BIGINT  NOT NULL,
        PRIMARY KEY (venue, symbol, timeframe, range_start_ms)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS quality_issues (
        venue          VARCHAR NOT NULL,
        symbol         VARCHAR NOT NULL,
        timeframe      VARCHAR NOT NULL,
        bar_open_ms    BIGINT,             -- NULL for window-level findings
        check_name     VARCHAR NOT NULL,
        severity       VARCHAR NOT NULL,   -- INFO | WARN | ERROR
        detail         VARCHAR,
        causal         BOOLEAN NOT NULL,
        detected_at_ms BIGINT  NOT NULL
    )
    """,
    # Append-only. Exchanges do occasionally revise historical candles; silently
    # overwriting destroys backtest reproducibility, which is a cousin of lookahead --
    # an "out-of-sample" result you cannot reproduce is not a result.
    """
    CREATE TABLE IF NOT EXISTS bar_revisions (
        venue          VARCHAR NOT NULL,
        symbol         VARCHAR NOT NULL,
        timeframe      VARCHAR NOT NULL,
        bar_open_ms    BIGINT  NOT NULL,
        old_open       DOUBLE, old_high DOUBLE, old_low DOUBLE,
        old_close      DOUBLE, old_volume DOUBLE,
        new_open       DOUBLE, new_high DOUBLE, new_low DOUBLE,
        new_close      DOUBLE, new_volume DOUBLE,
        detected_at_ms BIGINT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key VARCHAR PRIMARY KEY,
        value VARCHAR NOT NULL
    )
    """,
    "CREATE VIEW IF NOT EXISTS bars_v AS "
    "SELECT *, make_timestamp(bar_open_ms * 1000) AS ts_utc FROM bars",
]

BAR_COLUMNS = [
    "venue",
    "symbol",
    "timeframe",
    "bar_open_ms",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "session_date",
    "calendar_version",
    "revision",
    "ingested_at_ms",
]


def connect(db_path: Path | str, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """Open the store with UTC pinned.

    DuckDB's default TimeZone is the host zone. Pinning UTC here means every
    connection -- interactive, CLI, or CI -- renders identically.
    """
    db_path = Path(db_path)
    if not read_only:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=read_only)
    con.execute("SET TimeZone='UTC'")
    return con


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    for stmt in _DDL:
        con.execute(stmt)
    con.execute(
        "INSERT INTO schema_meta VALUES ('schema_version', ?) ON CONFLICT DO NOTHING",
        [str(SCHEMA_VERSION)],
    )


@contextmanager
def transaction(con: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """All-or-nothing write.

    Bars and their coverage row must land together. A crash between them leaves
    coverage claiming bars that were never stored, which silently promotes UNKNOWN to
    ABSENT_NO_TRADES -- destroying the one distinction this architecture exists to
    make. Across a ~7,000-request backfill, partial failure is the normal case.
    """
    con.execute("BEGIN TRANSACTION")
    try:
        yield con
    except Exception:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def insert_bars(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> int:
    """Insert bars idempotently. Returns the number of rows newly stored.

    The caller must have already deduplicated `df`. Page overlap is the normal source
    of duplicates and dropping them here, in polars, keeps the behaviour explicit
    rather than dependent on DuckDB's within-statement conflict handling.
    """
    if df.is_empty():
        return 0
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"bar frame missing columns: {missing}")
    frame = df.select(BAR_COLUMNS)  # noqa: F841 -- referenced by DuckDB replacement scan

    before = con.execute("SELECT count(*) FROM bars").fetchone()[0]
    con.execute("INSERT INTO bars SELECT * FROM frame ON CONFLICT DO NOTHING")
    after = con.execute("SELECT count(*) FROM bars").fetchone()[0]
    return after - before


def record_quality_issues(con: duckdb.DuckDBPyConnection, df: pl.DataFrame) -> int:
    if df.is_empty():
        return 0
    frame = df.select(  # noqa: F841 -- referenced by DuckDB replacement scan
        [
            "venue",
            "symbol",
            "timeframe",
            "bar_open_ms",
            "check_name",
            "severity",
            "detail",
            "causal",
            "detected_at_ms",
        ]
    )
    con.execute("INSERT INTO quality_issues SELECT * FROM frame")
    return frame.height


def read_bars_raw(
    con: duckdb.DuckDBPyConnection,
    venue: str,
    symbol: str,
    timeframe: str,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> pl.DataFrame:
    """Bars as stored, ordered by time. No causality guard -- internal use only.

    Phases 2-6 must go through `tradedesk.frames.read_bars`, which requires an
    explicit `as_of`.
    """
    sql = [
        "SELECT * FROM bars WHERE venue = ? AND symbol = ? AND timeframe = ?",
    ]
    params: list[object] = [venue, symbol, timeframe]
    if start_ms is not None:
        sql.append("AND bar_open_ms >= ?")
        params.append(start_ms)
    if end_ms is not None:
        sql.append("AND bar_open_ms < ?")
        params.append(end_ms)
    sql.append("ORDER BY bar_open_ms")
    return con.execute(" ".join(sql), params).pl()


def stored_symbols(con: duckdb.DuckDBPyConnection) -> pl.DataFrame:
    return con.execute(
        """
        SELECT venue, symbol, timeframe,
               count(*)          AS n_bars,
               min(bar_open_ms)  AS first_ms,
               max(bar_open_ms)  AS last_ms
        FROM bars
        GROUP BY 1, 2, 3
        ORDER BY 1, 2, 3
        """
    ).pl()
