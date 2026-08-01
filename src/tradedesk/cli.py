"""tradedesk CLI.

This tool never places orders. TradingView stays the execution surface.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from . import store
from .config import load_config, load_dotenv
from .coverage import covered_intervals
from .ingest import ingest, target_range_ms
from .quality import report as quality_report
from .timeutil import from_ms, now_ms
from .venues.coinbase import CoinbaseVenue

app = typer.Typer(add_completion=False, help=__doc__)
console = Console()


def _load(config_path: Optional[Path]):
    load_dotenv()
    return load_config(config_path)


def _parse_until(until: Optional[str]) -> Optional[int]:
    if until is None:
        return None
    from datetime import datetime, timezone

    from .timeutil import to_ms

    parsed = datetime.fromisoformat(until.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return to_ms(parsed)


def _venue(cfg):
    return CoinbaseVenue(
        max_bars_per_request=cfg.venue.max_bars_per_request,
        user_agent=cfg.venue.user_agent,
        timeout_ms=cfg.venue.request_timeout_ms,
        max_retries=cfg.venue.max_retries,
        backoff_seconds=cfg.venue.retry_backoff_seconds,
    )


@app.command()
def init(config: Optional[Path] = typer.Option(None, "--config")) -> None:
    """Create the DuckDB store and its schema."""
    cfg = _load(config)
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)
    console.print(f"[green]initialised[/green] {cfg.data.db_path}")


@app.command()
def fetch(
    config: Optional[Path] = typer.Option(None, "--config"),
    symbol: Optional[str] = typer.Option(None, "--symbol", help="limit to one symbol"),
    timeframe: Optional[str] = typer.Option(None, "--timeframe"),
    until: Optional[str] = typer.Option(
        None,
        "--until",
        help=(
            "ISO-8601 UTC upper bound, e.g. 2026-07-01T00:00:00Z. Pins the target end "
            "so a backfill is reproducible instead of drifting with wall clock."
        ),
    ),
) -> None:
    """Backfill missing candles. Idempotent -- safe to interrupt and re-run."""
    cfg = _load(config)
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)
    venue = _venue(cfg)

    symbols = [symbol] if symbol else cfg.data.symbols
    timeframes = [timeframe] if timeframe else cfg.data.timeframes
    until_ms = _parse_until(until)

    total_inserted = 0
    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total} windows"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        for sym in symbols:
            for tf in timeframes:
                start_ms, end_ms = target_range_ms(cfg, tf)
                if until_ms is not None:
                    end_ms = min(end_ms, until_ms)
                task = progress.add_task(f"{sym} {tf}", total=None)

                def on_progress(stats, _window, _task=task, _progress=progress):
                    _progress.update(
                        _task, total=stats.windows_planned, completed=stats.windows_fetched
                    )

                stats = ingest(
                    con,
                    venue,
                    cfg,
                    sym,
                    tf,
                    target_start_ms=start_ms,
                    target_end_ms=end_ms,
                    on_progress=on_progress,
                )
                progress.update(
                    task, total=max(stats.windows_planned, 1), completed=stats.windows_fetched
                )
                total_inserted += stats.bars_inserted
                note = (
                    f"  {sym} {tf}: {stats.bars_inserted:,} new bars "
                    f"from {stats.windows_fetched} requests"
                )
                if stats.bars_dropped_unsettled:
                    note += f", {stats.bars_dropped_unsettled} unsettled dropped"
                if stats.windows_planned == 0:
                    note = f"  {sym} {tf}: already complete"
                console.print(note, style="dim")

    console.print(f"[green]done[/green] · {total_inserted:,} bars inserted")


@app.command()
def quality(
    config: Optional[Path] = typer.Option(None, "--config"),
    symbol: Optional[str] = typer.Option(None, "--symbol"),
    timeframe: Optional[str] = typer.Option(None, "--timeframe"),
) -> None:
    """Print the data-quality report."""
    cfg = _load(config)
    con = store.connect(cfg.data.db_path, read_only=True)
    symbols = [symbol] if symbol else cfg.data.symbols
    timeframes = [timeframe] if timeframe else cfg.data.timeframes

    results = []
    for sym in symbols:
        for tf in timeframes:
            start_ms, end_ms = target_range_ms(cfg, tf)
            results.append(
                quality_report.assess(
                    con,
                    cfg,
                    sym,
                    tf,
                    venue=cfg.venue.name,
                    target_start_ms=start_ms,
                    target_end_ms=end_ms,
                )
            )
    quality_report.render(console, results, con, cfg.venue.name)


@app.command()
def status(config: Optional[Path] = typer.Option(None, "--config")) -> None:
    """What the store currently holds."""
    cfg = _load(config)
    con = store.connect(cfg.data.db_path, read_only=True)
    df = store.stored_symbols(con)

    table = Table(title="candle store", header_style="bold")
    for col in ("symbol", "tf", "bars", "first (UTC)", "last (UTC)", "coverage ranges"):
        table.add_column(col)
    for row in df.iter_rows(named=True):
        n_ranges = len(
            covered_intervals(con, row["venue"], row["symbol"], row["timeframe"])
        )
        table.add_row(
            row["symbol"],
            row["timeframe"],
            f"{row['n_bars']:,}",
            f"{from_ms(int(row['first_ms'])):%Y-%m-%d %H:%M}",
            f"{from_ms(int(row['last_ms'])):%Y-%m-%d %H:%M}",
            str(n_ranges),
        )
    console.print(table)
    console.print(f"now: {from_ms(now_ms()):%Y-%m-%d %H:%M:%S} UTC", style="dim")


if __name__ == "__main__":
    app()
