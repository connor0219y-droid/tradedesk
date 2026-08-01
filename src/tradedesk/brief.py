"""Phase 4 -- the pre-session brief.

One screen, before the open, answering: is this instrument worth trading today, what is
a realistic range, where are the levels, and which of my setups have actually earned the
right to be traded.

Two things are always visible, by design:

  THE COST DRAG. At the configured fee tier a 1xATR(5m) stop on BTC costs ~19R per round
  trip. That number belongs on the screen every session, not buried in a research
  document, because it is the single largest term in every expectancy calculation.

  NOT VALIDATED. A setup that has not passed the gate is shown with its disqualifiers
  rather than omitted. Absence and rejection look identical to a reader, and they are
  not the same thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .backtest.costs import CostModel
from .config import Config
from .frames import read_bars
from .levels import compute_levels
from .levels.engine import DAILY_DISTANCE_LEVELS, DISTANCE_LEVELS
from .qualification import all_setups
from .timeutil import ET, from_ms

#: Levels shown in the brief, with the ATR unit each is measured in. Cross-session
#: levels use DAILY ATR -- measured in intraday ATR a prior-day level reads ~20 units
#: away on 1m and ~2 on 1h purely because of the ATR's timescale.
_LEVEL_LABELS = {
    "or5_high": "OR-5 high", "or5_low": "OR-5 low",
    "or15_high": "OR-15 high", "or15_low": "OR-15 low",
    "or30_high": "OR-30 high", "or30_low": "OR-30 low",
    "vwap": "VWAP", "vwap_upper_1s": "VWAP +1σ", "vwap_lower_1s": "VWAP −1σ",
    "vwap_upper_2s": "VWAP +2σ", "vwap_lower_2s": "VWAP −2σ",
    "poc": "POC",
    "prior_day_high": "prior day high", "prior_day_low": "prior day low",
    "prior_day_close": "prior day close",
    "premarket_high": "premarket high", "premarket_low": "premarket low",
}


@dataclass
class Brief:
    symbol: str
    timeframe: str
    as_of: datetime
    price: float | None
    atr_intraday: float | None
    atr_daily: float | None
    atr_pct_60d: float | None
    atr_trend_pct: float | None
    expected_range: float | None
    session_broken: bool
    rvol: float | None
    levels: list[tuple[str, float, float | None, str]] = field(default_factory=list)
    setups: list[dict] = field(default_factory=list)
    drag_r: dict[float, float] = field(default_factory=dict)
    round_trip_bps: float = 0.0


def _regime_words(pct: float | None, trend: float | None) -> tuple[str, str]:
    if pct is None:
        return "unknown", "dim"
    if pct >= 70:
        base, style = "HIGH volatility", "red"
    elif pct <= 30:
        base, style = "LOW volatility", "cyan"
    else:
        base, style = "normal volatility", "white"
    if trend is not None:
        base += " · expanding" if trend > 5 else " · contracting" if trend < -5 else " · stable"
    return base, style


def build(
    con, cfg: Config, symbol: str, timeframe: str, *, as_of: datetime
) -> Brief | None:
    bf = read_bars(con, symbol, timeframe, as_of=as_of, venue=cfg.venue.name)
    if bf.is_empty:
        return None
    df = compute_levels(bf, cfg).to_polars()
    row = df.to_dicts()[-1]

    price = row.get("close")
    atr_i = row.get("atr_intraday")
    atr_d = row.get("atr_daily")

    # Is volatility expanding? Compare today's daily ATR against five sessions ago.
    trend = None
    daily = (
        df.group_by("session_date").agg(__import__("polars").col("atr_daily").last())
        .sort("session_date")["atr_daily"].drop_nulls().to_list()
    )
    if len(daily) >= 6 and daily[-6]:
        trend = 100.0 * (daily[-1] - daily[-6]) / daily[-6]

    levels: list[tuple[str, float, float | None, str]] = []
    for col, label in _LEVEL_LABELS.items():
        v = row.get(col)
        if v is None:
            levels.append((label, float("nan"), None, "—"))
            continue
        if col in DAILY_DISTANCE_LEVELS:
            d = row.get(f"dist_{col}_datr")
            unit = "daily ATR"
        elif col in DISTANCE_LEVELS:
            d = row.get(f"dist_{col}_atr")
            unit = f"{timeframe} ATR"
        else:
            d, unit = None, "—"
        levels.append((label, v, d, unit))

    costs = CostModel.for_symbol(cfg, symbol)
    drag: dict[float, float] = {}
    if price and atr_i and atr_i > 0:
        for stop_atr in (1.0, 4.0, 8.0, 16.0):
            drag[stop_atr] = costs.round_trip_in_r(price, price - stop_atr * atr_i)

    return Brief(
        symbol=symbol, timeframe=timeframe, as_of=as_of, price=price,
        atr_intraday=atr_i, atr_daily=atr_d, atr_pct_60d=row.get("atr_pct_60d"),
        atr_trend_pct=trend,
        expected_range=atr_d,
        session_broken=bool(row.get("session_broken")),
        rvol=row.get("rvol_tod"),
        levels=levels,
        setups=all_setups(con, symbol=symbol, timeframe=timeframe),
        drag_r=drag,
        round_trip_bps=2 * costs.per_side_bps,
    )


def render(console: Console, brief: Brief) -> None:
    ts = brief.as_of.astimezone(ET)
    regime, style = _regime_words(brief.atr_pct_60d, brief.atr_trend_pct)

    console.print(
        f"[bold cyan]{brief.symbol}[/bold cyan] {brief.timeframe} · "
        f"{ts:%Y-%m-%d %H:%M} ET · pre-session brief"
    )
    head = Table.grid(padding=(0, 2))
    head.add_row("regime", Text(regime, style=style) if brief.atr_pct_60d is not None
                 else Text("unknown", style="dim"))
    head.add_row("ATR percentile (60d)",
                 "—" if brief.atr_pct_60d is None else f"{brief.atr_pct_60d:.0f}")
    head.add_row(f"ATR({brief.timeframe})",
                 "—" if brief.atr_intraday is None else f"{brief.atr_intraday:,.2f}")
    head.add_row("expected daily range",
                 "—" if brief.expected_range is None else f"{brief.expected_range:,.2f}"
                 + (f"  ({100*brief.expected_range/brief.price:.2f}% of price)"
                    if brief.price else ""))
    head.add_row("rel vol (same time of day)",
                 "—" if brief.rvol is None else f"{brief.rvol:.2f}×")
    if brief.session_broken:
        head.add_row("session", Text("BROKEN — a ≥30min hole occurred; session levels are null",
                                     style="red"))
    console.print(head)

    # --- the cost drag, always on screen -------------------------------------
    if brief.drag_r:
        console.print()
        drag = Table(title=f"cost drag at {brief.round_trip_bps:.0f} bps round trip",
                     box=None, header_style="bold", title_justify="left", pad_edge=False,
                     title_style="bold red")
        drag.add_column("stop width")
        drag.add_column("cost per round trip", justify="right")
        drag.add_column("gross edge needed to break even", justify="right")
        for stop_atr, r in sorted(brief.drag_r.items()):
            drag.add_row(f"{stop_atr:g}×ATR", Text(f"{r:.2f}R", style="red"),
                         f"+{r:.2f}R")
        console.print(drag)

    # --- levels, sorted by distance ------------------------------------------
    console.print()
    lt = Table(title="levels by distance from price", box=None, header_style="bold",
               title_justify="left", pad_edge=False)
    lt.add_column("level")
    lt.add_column("price", justify="right")
    lt.add_column("distance", justify="right")
    lt.add_column("unit")
    known = [x for x in brief.levels if x[2] is not None]
    unknown = [x for x in brief.levels if x[2] is None]
    for label, value, dist, unit in sorted(known, key=lambda x: -x[2]):
        style = "green" if dist < 0 else "red"
        lt.add_row(label, f"{value:,.2f}", Text(f"{dist:+.2f}", style=style), unit)
    if brief.price:
        lt.add_row(Text("price", style="bold"),
                   Text(f"{brief.price:,.2f}", style="bold"), "—", "")
    for label, _v, _d, _u in unknown:
        lt.add_row(f"[dim]{label}[/dim]", "[dim]—[/dim]", "[dim]n/a[/dim]", "")
    console.print(lt)

    # --- setups: validated, or NOT VALIDATED with the reason ------------------
    console.print()
    st = Table(title="setups", box=None, header_style="bold", title_justify="left",
               pad_edge=False)
    st.add_column("setup")
    st.add_column("status")
    st.add_column("net exp", justify="right")
    st.add_column("n", justify="right")
    st.add_column("why not")
    if not brief.setups:
        st.add_row(Text("none validated", style="dim"),
                   Text("NOT VALIDATED", style="yellow"), "—", "—",
                   "run `tradedesk validate` to populate the registry")
    for s in brief.setups:
        if s["qualifies"]:
            st.add_row(s["setup"], Text("VALIDATED", style="bold green"),
                       f"{s['net_in']:+.4f}R", f"{s['n_in']:,}", "")
        else:
            st.add_row(s["setup"], Text("NOT VALIDATED", style="yellow"),
                       "—" if s["net_in"] is None else f"{s['net_in']:+.2f}R",
                       f"{s['n_in']:,}",
                       Text(s["disqualifiers"] or "—", style="dim"))
    console.print(st)

    qualifying = [s for s in brief.setups if s["qualifies"]]
    if not qualifying:
        console.print()
        console.print(
            Text("NO QUALIFYING SETUPS — nothing has passed the gate on this instrument. "
                 "The live companion will emit no signal cards today.",
                 style="bold yellow")
        )
