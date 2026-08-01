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
def levels(
    symbol: str = typer.Option(..., "--symbol", help='e.g. "BTC/USD"'),
    timeframe: str = typer.Option("5m", "--timeframe"),
    at: Optional[str] = typer.Option(
        None, "--at", help="ISO-8601 UTC instant; defaults to the latest settled bar"
    ),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Print every level for a symbol, sorted by distance from price in ATR units."""
    from datetime import datetime, timezone

    from .frames import read_bars
    from .levels import compute_levels
    from .levels.profile import value_area
    from .timeutil import ET, from_ms

    cfg = _load(config)
    con = store.connect(cfg.data.db_path, read_only=True)
    as_of = (
        datetime.fromisoformat(at.replace("Z", "+00:00"))
        if at
        else datetime.now(timezone.utc)
    )
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)

    bf = read_bars(con, symbol, timeframe, as_of=as_of, venue=cfg.venue.name)
    if bf.is_empty:
        console.print(f"[red]no bars[/red] for {symbol} {timeframe} at or before {as_of}")
        raise typer.Exit(1)

    lf = compute_levels(bf, cfg)
    row = lf.last()
    df = lf.to_polars()

    price = row["close"]
    ts = from_ms(int(row["bar_open_ms"])).astimezone(ET)
    atr = row.get("atr_intraday")
    atr_pct = row.get("atr_pct_60d")
    broken = row.get("session_broken")

    header = (
        f"[bold cyan]{symbol}[/bold cyan] {timeframe} · {ts:%Y-%m-%d %H:%M} ET · "
        f"session {'[red]BROKEN[/red]' if broken else '[green]OK[/green]'} · "
        f"ATR(14) {_fmt(atr)} · ATR pct {_fmt(atr_pct, 0)}"
    )
    console.print(header)
    if row.get("rvol_tod") is not None:
        console.print(f"rel vol (same time of day, 20 sessions): {row['rvol_tod']:.2f}×", style="dim")

    poc, vah, val = value_area(
        df,
        at_ms=int(row["bar_open_ms"]),
        buckets_per_atr=int(cfg.levels.get("profile_buckets_per_atr", 100)),
        tick_size=float(cfg.levels.get("tick_size", 0.01)),
        area_pct=float(cfg.levels.get("value_area_pct", 70.0)),
    )

    named = [
        ("OR-5 high", row.get("or5_high")), ("OR-5 low", row.get("or5_low")),
        ("OR-15 high", row.get("or15_high")), ("OR-15 low", row.get("or15_low")),
        ("OR-30 high", row.get("or30_high")), ("OR-30 low", row.get("or30_low")),
        ("VWAP +2σ", row.get("vwap_upper_2s")), ("VWAP +1σ", row.get("vwap_upper_1s")),
        ("VWAP", row.get("vwap")),
        ("VWAP −1σ", row.get("vwap_lower_1s")), ("VWAP −2σ", row.get("vwap_lower_2s")),
        ("POC", poc), ("value area high", vah), ("value area low", val),
        ("prior day high", row.get("prior_day_high")),
        ("prior day low", row.get("prior_day_low")),
        ("prior day close", row.get("prior_day_close")),
        ("premarket high", row.get("premarket_high")),
        ("premarket low", row.get("premarket_low")),
    ]

    rows = []
    for name, value in named:
        if value is None:
            rows.append((name, None, None))
            continue
        dist = (price - value) / atr if atr else None
        rows.append((name, value, dist))
    rows.append(("price", price, 0.0))
    # Sorted by distance so the nearest levels sit next to price, which is how they
    # matter intraday. Unavailable levels sink to the bottom rather than being hidden --
    # knowing a level is missing is itself information.
    rows.sort(key=lambda r: (r[2] is None, -(r[2] if r[2] is not None else 0)))

    table = Table(box=None, header_style="bold", pad_edge=False)
    table.add_column("level")
    table.add_column("price", justify="right")
    table.add_column("dist (ATR)", justify="right")
    for name, value, dist in rows:
        if value is None:
            table.add_row(f"[dim]{name}[/dim]", "[dim]—[/dim]", "[dim]n/a[/dim]")
        elif name == "price":
            table.add_row(f"[bold]{name}[/bold]", f"[bold]{value:,.2f}[/bold]", "—")
        else:
            style = "green" if dist is not None and dist < 0 else "red"
            d = f"[{style}]{dist:+.2f}[/{style}]" if dist is not None else "[dim]n/a[/dim]"
            table.add_row(name, f"{value:,.2f}", d)
    console.print(table)


@app.command()
def validate(
    symbol: str = typer.Option(..., "--symbol", help='e.g. "BTC/USD"'),
    timeframe: str = typer.Option("5m", "--timeframe"),
    pattern: Optional[str] = typer.Option(None, "--pattern", help="limit to one pattern"),
    stop_atr: float = typer.Option(1.0, "--stop-atr"),
    target_r: float = typer.Option(2.0, "--target-r"),
    max_bars: int = typer.Option(48, "--max-bars", help="bar cap on holding period"),
    draws: int = typer.Option(1000, "--draws", help="Monte Carlo baseline draws"),
    detail: bool = typer.Option(False, "--detail", help="per-pattern breakdown"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Backtest every pattern against a time-of-day-matched random baseline."""
    from datetime import datetime, timezone

    from .backtest import render, render_detail, validate_series

    cfg = _load(config)
    con = store.connect(cfg.data.db_path, read_only=True)
    reports = validate_series(
        con, cfg, symbol, timeframe,
        as_of=datetime.now(timezone.utc),
        patterns=[pattern] if pattern else None,
        stop_atr=stop_atr, target_r=target_r, max_bars=max_bars,
        baseline_draws=draws,
    )
    if not reports:
        console.print(f"[red]no data[/red] for {symbol} {timeframe}")
        raise typer.Exit(1)

    render(console, reports)
    if detail or pattern:
        for rep in sorted(reports, key=lambda r: -(r.gross_in or -9e9)):
            render_detail(console, rep)


def _fmt(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


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
