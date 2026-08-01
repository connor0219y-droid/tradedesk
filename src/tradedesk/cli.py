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
from rich.text import Text

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
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)
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

    from datetime import datetime, timezone

    from .backtest import persist
    n_ok = persist(con, reports, validated_at_ms=int(datetime.now(timezone.utc).timestamp()*1000))
    render(console, reports)
    console.print(
        f"[dim]{len(reports)} results written to the qualification registry · "
        f"{n_ok} qualify for signalling[/dim]"
    )
    if detail or pattern:
        for rep in sorted(reports, key=lambda r: -(r.gross_in or -9e9)):
            render_detail(console, rep)


def _fmt(value, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


@app.command()
def brief(
    symbol: Optional[str] = typer.Option(None, "--symbol"),
    timeframe: str = typer.Option("5m", "--timeframe"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Pre-session brief: regime, levels, validated setups, and the cost drag."""
    from datetime import datetime, timezone

    from . import brief as brief_mod

    cfg = _load(config)
    con = store.connect(cfg.data.db_path, read_only=True)
    now = datetime.now(timezone.utc)
    symbols = [symbol] if symbol else cfg.data.symbols
    for i, sym in enumerate(symbols):
        b = brief_mod.build(con, cfg, sym, timeframe, as_of=now)
        if b is None:
            console.print(f"[red]no data[/red] for {sym} {timeframe}")
            continue
        if i:
            console.print("\n" + "─" * 78 + "\n")
        brief_mod.render(console, b)


@app.command()
def live(
    symbol: str = typer.Option(..., "--symbol"),
    timeframe: str = typer.Option("5m", "--timeframe"),
    predict: bool = typer.Option(
        True, "--predict/--no-predict",
        help="prompt for your own read before revealing anything (default on)",
    ),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Live companion. Signal cards ONLY for setups that passed the gate."""
    from datetime import datetime, timezone

    from . import live as live_mod

    cfg = _load(config)
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)
    view = live_mod.build(con, cfg, symbol, timeframe, as_of=datetime.now(timezone.utc))
    if view is None:
        console.print(f"[red]no data[/red] for {symbol} {timeframe}")
        raise typer.Exit(1)

    prediction = None
    if predict:
        prediction = live_mod.prompt_prediction(console, view)
        if prediction:
            live_mod.record_prediction(con, view, prediction)
    live_mod.render(console, view, cfg, prediction=prediction)


@app.command()
def size(
    entry: float = typer.Option(..., "--entry"),
    stop: float = typer.Option(..., "--stop"),
    thesis: str = typer.Option(
        ..., "--thesis", help="required: why this trade, in your own words"
    ),
    account: Optional[float] = typer.Option(None, "--account"),
    risk_pct: Optional[float] = typer.Option(None, "--risk-pct"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Position size. Refuses to compute one without a stated thesis."""
    from .live import Position, ThesisRequired

    cfg = _load(config)
    try:
        p = Position(
            account_size=account or float(cfg.risk.get("account_size", 100000.0)),
            risk_pct=risk_pct or float(cfg.risk.get("risk_pct", 1.0)),
            entry=entry, stop=stop, thesis=thesis,
        )
    except ThesisRequired as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    t = Table(box=None, header_style="bold", pad_edge=False)
    t.add_column("field"); t.add_column("value", justify="right")
    t.add_row("account", f"{p.account_size:,.2f}")
    t.add_row("risk", f"{p.risk_pct:.2f}%  =  {p.risk_dollars:,.2f}")
    t.add_row("risk per unit", f"{p.risk_per_unit:,.4f}")
    t.add_row("size", f"{p.units:,.6f} units")
    t.add_row("notional", f"{p.notional:,.2f}")
    t.add_row("1R", f"{p.one_r_dollars():,.2f}")
    lev = p.leverage
    t.add_row("leverage", Text(f"{lev:.2f}×", style="red" if lev > 1.0 else "white"))
    console.print(t)
    if lev > 1.0:
        console.print(
            f"[red]This stop requires {lev:.1f}× leverage on the stated account. "
            "A tighter stop needs a bigger position for the same risk.[/red]"
        )
    max_pct = float(cfg.risk.get("max_risk_pct", 2.0))
    if p.risk_pct > max_pct:
        console.print(f"[red]risk {p.risk_pct:.2f}% exceeds your stated max {max_pct:.2f}%[/red]")


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


journal_app = typer.Typer(help="Trade journal and the feedback loop.")
app.add_typer(journal_app, name="journal")


@journal_app.command("add")
def journal_add(
    symbol: str = typer.Option(..., "--symbol"),
    direction: str = typer.Option(..., "--direction", help="long or short"),
    entry: float = typer.Option(..., "--entry"),
    stop: float = typer.Option(..., "--stop"),
    thesis: str = typer.Option(..., "--thesis", help="required"),
    setup: Optional[str] = typer.Option(None, "--setup"),
    exit_price: Optional[float] = typer.Option(None, "--exit"),
    exit_reason: Optional[str] = typer.Option(None, "--exit-reason"),
    size_units: Optional[float] = typer.Option(None, "--size"),
    mfe_r: Optional[float] = typer.Option(None, "--mfe-r"),
    mae_r: Optional[float] = typer.Option(None, "--mae-r"),
    entry_ms: Optional[int] = typer.Option(None, "--entry-ms"),
    exit_ms: Optional[int] = typer.Option(None, "--exit-ms"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Log a trade. TradingView has no public fill API, so this is manual by design."""
    from . import journal as jr
    from .timeutil import now_ms

    cfg = _load(config)
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)
    tid = jr.log_trade(
        con, symbol=symbol, setup=setup, direction=direction, thesis=thesis,
        entry_ms=entry_ms or now_ms(), exit_ms=exit_ms, entry=entry, stop=stop,
        exit_price=exit_price, exit_reason=exit_reason, size=size_units,
        mfe_r=mfe_r, mae_r=mae_r,
        account_size=float(cfg.risk.get("account_size", 100000.0)),
    )
    console.print(f"[green]logged[/green] {tid}")


@journal_app.command("report")
def journal_report(
    symbol: Optional[str] = typer.Option(None, "--symbol"),
    config: Optional[Path] = typer.Option(None, "--config"),
) -> None:
    """Rolling stats, behavioural flags, and live expectancy vs the backtested number."""
    from . import journal as jr

    cfg = _load(config)
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)
    trades = jr.load_trades(con, symbol=symbol)
    if not trades:
        console.print("[dim]no trades logged — `tradedesk journal add` to start[/dim]")
        return

    risk_pct = float(cfg.risk.get("risk_pct", 1.0))
    s = jr.rolling_stats(trades, risk_pct=risk_pct)
    t = Table(title="rolling", box=None, header_style="bold", title_justify="left",
              pad_edge=False)
    for c in ("n", "win", "expectancy", "profit factor", "max DD", "risk of ruin"):
        t.add_column(c, justify="right")
    t.add_row(
        f"{s.n:,}",
        "—" if s.win_rate is None else f"{s.win_rate:.1%}",
        "—" if s.expectancy_r is None else f"{s.expectancy_r:+.3f}R",
        "—" if s.profit_factor is None else f"{s.profit_factor:.2f}",
        "—" if s.max_drawdown_r is None else f"{s.max_drawdown_r:.2f}R",
        "—" if s.risk_of_ruin is None else
        Text(f"{s.risk_of_ruin:.1%}", style="red" if s.risk_of_ruin > 0.1 else "white"),
    )
    console.print(t)

    flags = jr.behavioural_flags(trades, max_risk_pct=float(cfg.risk.get("max_risk_pct", 2.0)))
    console.print()
    if flags:
        ft = Table(title="behavioural flags", box=None, header_style="bold",
                   title_justify="left", pad_edge=False)
        ft.add_column("trade"); ft.add_column("flag")
        for tid, fs in flags.items():
            for f in fs:
                ft.add_row(tid[:8], Text(f, style="yellow"))
        console.print(ft)
    else:
        console.print("[dim]no behavioural flags[/dim]")

    console.print()
    dt = Table(title="live vs backtest — the report that localises the problem", box=None,
               header_style="bold", title_justify="left", pad_edge=False)
    for c in ("setup", "live n", "live exp", "backtest exp", "gap", "reading"):
        dt.add_column(c, justify="right" if c not in ("setup", "reading") else "left")
    divs = jr.live_vs_backtest(con, trades, symbol=symbol)
    if not divs:
        dt.add_row("[dim]no trades tagged with a setup[/dim]", "", "", "", "", "")
    for d in divs:
        dt.add_row(
            d.setup, f"{d.live_n:,}",
            "—" if d.live_expectancy is None else f"{d.live_expectancy:+.3f}R",
            "—" if d.backtest_expectancy is None else f"{d.backtest_expectancy:+.3f}R",
            "—" if d.gap is None else f"{d.gap:+.3f}R",
            Text(d.reading, style="yellow" if "EXECUTION" in d.reading else "dim"),
        )
    console.print(dt)
