"""The quality report.

The verdict goes on the first line. A report that buries "this data is not usable"
under three tables of statistics has failed at its only job.
"""

from __future__ import annotations

from dataclasses import dataclass

import duckdb
import polars as pl
from rich.console import Console
from rich.table import Table
from rich.text import Text

from ..config import Config
from ..coverage import covered_intervals
from ..store import read_bars_raw
from ..timeutil import ET_NAME, expected_bar_count, from_ms
from ..timeutil import tf_ms as _tf_ms
from .checks import absent_runs, classify_absent_bars


def outage_threshold_bars(timeframe: str, outage_minutes: float) -> int:
    """How many consecutive absent bars constitute venue downtime at this timeframe.

    Expressed as a duration rather than a bar count so the same real outage is
    classified identically at 1m, 5m and 15m. A fixed bar count cannot do that: three
    absent bars is 3 minutes at 1m (an ordinary quiet stretch on SOL, which has 3,434
    of them) and 45 minutes at 15m (unambiguously downtime).
    """
    step_minutes = _tf_ms(timeframe) / 60_000
    return max(1, int(-(-outage_minutes // step_minutes)))  # ceil

_SPARK = "▁▂▃▄▅▆▇█"

USABLE = "USABLE"
PROVISIONAL = "PROVISIONAL"
BLOCKED = "BLOCKED"
NO_DATA = "NO DATA"

_VERDICT_STYLE = {
    USABLE: "bold green",
    PROVISIONAL: "bold yellow",
    BLOCKED: "bold red",
    NO_DATA: "bold red",
}


@dataclass
class SymbolQuality:
    symbol: str
    timeframe: str
    expected: int
    present: int
    absent_no_trades: int
    unknown: int
    coverage_pct: float
    absent_pct: float
    first_ms: int
    last_ms: int
    issues: pl.DataFrame
    verdict: str
    reasons: list[str]
    outages: list[tuple[int, int]]
    longest_absent_run: int

    @property
    def error_count(self) -> int:
        if self.issues.is_empty():
            return 0
        return int(self.issues.filter(pl.col("severity") == "ERROR")["n"].sum())


def assess(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    symbol: str,
    timeframe: str,
    *,
    venue: str,
    target_start_ms: int,
    target_end_ms: int,
) -> SymbolQuality:
    bars = read_bars_raw(
        con, venue, symbol, timeframe, start_ms=target_start_ms, end_ms=target_end_ms
    )
    covered = covered_intervals(con, venue, symbol, timeframe)
    expected = expected_bar_count(target_start_ms, target_end_ms, timeframe)

    present_ms = set(bars["bar_open_ms"].to_list()) if not bars.is_empty() else set()
    present, absent, unknown = classify_absent_bars(
        present_ms, covered, (target_start_ms, target_end_ms), timeframe
    )

    issues = con.execute(
        """
        SELECT check_name, severity, causal, count(*) AS n
        FROM quality_issues
        WHERE venue = ? AND symbol = ? AND timeframe = ?
        GROUP BY 1, 2, 3
        ORDER BY
            CASE severity WHEN 'ERROR' THEN 0 WHEN 'WARN' THEN 1 ELSE 2 END,
            n DESC
        """,
        [venue, symbol, timeframe],
    ).pl()

    coverage_pct = 100.0 * (present + absent) / expected if expected else 0.0
    denom = present + absent
    absent_pct = 100.0 * absent / denom if denom else 0.0

    runs = absent_runs(
        present_ms, covered, (target_start_ms, target_end_ms), timeframe
    )
    min_bars = outage_threshold_bars(timeframe, cfg.quality.outage_minutes)
    outages = [r for r in runs if r[1] >= min_bars]
    longest = max((n for _, n in runs), default=0)

    verdict, reasons = _verdict(cfg, expected, coverage_pct, absent_pct, issues)
    if outages:
        step_minutes = _tf_ms(timeframe) / 60_000
        worst = max(n for _, n in outages) * step_minutes
        reasons.append(
            f"{len(outages)} venue-outage run(s) of >={cfg.quality.outage_minutes:.0f}m, "
            f"longest {worst:.0f}m -- downtime, not quiet markets"
        )

    return SymbolQuality(
        symbol=symbol,
        timeframe=timeframe,
        expected=expected,
        present=present,
        absent_no_trades=absent,
        unknown=unknown,
        coverage_pct=coverage_pct,
        absent_pct=absent_pct,
        first_ms=int(bars["bar_open_ms"].min()) if not bars.is_empty() else 0,
        last_ms=int(bars["bar_open_ms"].max()) if not bars.is_empty() else 0,
        issues=issues,
        verdict=verdict,
        reasons=reasons,
        outages=outages,
        longest_absent_run=longest,
    )


def _verdict(
    cfg: Config,
    expected: int,
    coverage_pct: float,
    absent_pct: float,
    issues: pl.DataFrame,
) -> tuple[str, list[str]]:
    q = cfg.quality
    reasons: list[str] = []

    if expected == 0:
        return NO_DATA, ["no bars requested"]

    n_errors = 0
    if not issues.is_empty():
        errs = issues.filter(pl.col("severity") == "ERROR")
        n_errors = int(errs["n"].sum()) if not errs.is_empty() else 0

    blocked = False
    if n_errors:
        blocked = True
        reasons.append(f"{n_errors} ERROR-severity issues")
    if coverage_pct < q.min_coverage_pct:
        blocked = True
        reasons.append(
            f"coverage {coverage_pct:.2f}% below the {q.min_coverage_pct:.0f}% floor"
        )
    if absent_pct > q.absent_pct_block:
        blocked = True
        reasons.append(
            f"{absent_pct:.1f}% of bars had no trades -- too sparse for "
            "multi-bar pattern research"
        )
    if blocked:
        return BLOCKED, reasons

    if absent_pct > q.absent_pct_warn:
        reasons.append(
            f"{absent_pct:.1f}% of bars had no trades; multi-bar windows will "
            "frequently be non-contiguous"
        )
        return PROVISIONAL, reasons

    return USABLE, reasons


def _sparkline(counts: list[int]) -> str:
    if not counts or max(counts) == 0:
        return _SPARK[0] * len(counts)
    peak = max(counts)
    return "".join(_SPARK[min(len(_SPARK) - 1, v * (len(_SPARK) - 1) // peak)] for v in counts)


def hour_histogram(
    con: duckdb.DuckDBPyConnection, venue: str, symbol: str, timeframe: str
) -> list[int]:
    """Bars present per ET hour of day.

    Doubles as the DST canary: a timezone bug shows up here as a visible notch or a
    shifted profile rather than as a silent one-hour offset nobody notices.
    """
    df = con.execute(
        """
        SELECT hour(make_timestamp(bar_open_ms * 1000) AT TIME ZONE 'UTC'
                    AT TIME ZONE ?) AS et_hour,
               count(*) AS n
        FROM bars
        WHERE venue = ? AND symbol = ? AND timeframe = ?
        GROUP BY 1 ORDER BY 1
        """,
        [ET_NAME, venue, symbol, timeframe],
    ).pl()
    counts = [0] * 24
    for row in df.iter_rows(named=True):
        counts[int(row["et_hour"])] = int(row["n"])
    return counts


def render(
    console: Console,
    results: list[SymbolQuality],
    con: duckdb.DuckDBPyConnection,
    venue: str,
) -> None:
    for res in results:
        _render_one(console, res, con, venue)
        console.print()


def _render_one(
    console: Console, res: SymbolQuality, con: duckdb.DuckDBPyConnection, venue: str
) -> None:
    style = _VERDICT_STYLE.get(res.verdict, "bold")
    headline = Text()
    headline.append(f"{res.symbol} {res.timeframe}", style="bold cyan")
    headline.append(
        f"  ·  {res.expected:,} expected  ·  {res.present:,} present"
        f"  ·  {res.coverage_pct:.2f}% covered  ·  "
    )
    headline.append(f"VERDICT: {res.verdict}", style=style)
    console.print(headline)

    for reason in res.reasons:
        console.print(f"    ! {reason}", style=style)

    if res.present:
        span = Table.grid(padding=(0, 2))
        span.add_row("range", f"{from_ms(res.first_ms):%Y-%m-%d %H:%M} → "
                              f"{from_ms(res.last_ms):%Y-%m-%d %H:%M} UTC")
        span.add_row(
            "no-trade bars",
            f"{res.absent_no_trades:,} ({res.absent_pct:.2f}% of covered grid)",
        )
        span.add_row("not yet fetched", f"{res.unknown:,}")
        if res.longest_absent_run:
            span.add_row("longest absent run", f"{res.longest_absent_run} bars")
        counts = hour_histogram(con, venue, res.symbol, res.timeframe)
        span.add_row("bars by ET hour", f"{_sparkline(counts)}  (00→23, DST canary)")
        console.print(span)

    if res.outages:
        step_minutes = _tf_ms(res.timeframe) / 60_000
        table = Table(title="venue outages (contiguous absent runs)", box=None,
                      header_style="bold", title_justify="left", pad_edge=False)
        table.add_column("from (UTC)")
        table.add_column("bars", justify="right")
        table.add_column("duration", justify="right")
        for start_ms, n in res.outages[:10]:
            table.add_row(
                f"{from_ms(start_ms):%Y-%m-%d %H:%M}", str(n),
                f"{n * step_minutes:.0f}m",
            )
        if len(res.outages) > 10:
            table.add_row(f"... {len(res.outages) - 10} more", "")
        console.print(table)

    if not res.issues.is_empty():
        table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
        table.add_column("check")
        table.add_column("severity")
        table.add_column("causal")
        table.add_column("n", justify="right")
        for row in res.issues.iter_rows(named=True):
            sev = row["severity"]
            sev_style = {"ERROR": "red", "WARN": "yellow"}.get(sev, "dim")
            table.add_row(
                row["check_name"],
                Text(sev, style=sev_style),
                "yes" if row["causal"] else Text("no (offline)", style="dim"),
                f"{row['n']:,}",
            )
        console.print(table)
    else:
        console.print("    no quality issues logged", style="dim")
