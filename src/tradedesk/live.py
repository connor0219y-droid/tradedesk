"""Phase 5 -- the live companion.

Four rules, in descending order of how easy they'd be to quietly violate:

1. **A signal card is emitted only for a setup that passed the gate.** Not "highlighted
   differently" for others -- not emitted. `qualification.qualifying_setups()` is the
   only source of signallable setups, and as of the Phase 3 study it returns nothing.
   The honest output is "no qualifying setups", and there is a test proving the emitting
   path works, so that message means "nothing passed" rather than "this never fires".

2. **Triggered patterns are still shown, with their measured expectancy attached** --
   including negative ones, under a NEGATIVE EXPECTANCY banner. Hiding the setups that
   tempt you removes exactly the information that would teach you what they cost.

3. **Predict-first is on by default.** You are prompted for your own read before the
   tool reveals anything. The point is to train your eye against a measured baseline,
   not to outsource it.

4. **Position sizing requires a thesis.** `size()` refuses without one. A size computed
   from a stop you cannot justify is a number that makes a bad trade feel rigorous.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .backtest.costs import CostModel
from .config import Config
from .frames import read_bars
from .levels import compute_levels
from .levels.engine import DAILY_DISTANCE_LEVELS, DISTANCE_LEVELS
from .patterns import REGISTRY, detect
from .patterns.regime import add_regime_columns
from .qualification import all_setups, qualifying_setups
from .timeutil import ET, from_ms, now_ms


class ThesisRequired(ValueError):
    """Position sizing was requested without a stated thesis."""


MIN_THESIS_CHARS = 12


@dataclass
class Position:
    account_size: float
    risk_pct: float
    entry: float
    stop: float
    thesis: str

    def __post_init__(self) -> None:
        if not self.thesis or len(self.thesis.strip()) < MIN_THESIS_CHARS:
            raise ThesisRequired(
                "state a thesis of at least "
                f"{MIN_THESIS_CHARS} characters before sizing. A size computed from a "
                "stop you cannot justify is a number that makes a bad trade feel rigorous."
            )
        if self.entry == self.stop:
            raise ValueError("entry and stop are equal; risk per unit would be zero")

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def risk_dollars(self) -> float:
        return self.account_size * self.risk_pct / 100.0

    @property
    def units(self) -> float:
        return self.risk_dollars / self.risk_per_unit

    @property
    def notional(self) -> float:
        return self.units * self.entry

    @property
    def leverage(self) -> float:
        return self.notional / self.account_size if self.account_size else float("inf")

    def one_r_dollars(self) -> float:
        return self.risk_dollars


@dataclass
class TriggeredPattern:
    name: str
    direction: str
    validated: bool
    net_in: float | None
    n_in: int | None
    ci_low: float | None
    ci_high: float | None
    disqualifiers: str


@dataclass
class LiveView:
    symbol: str
    timeframe: str
    as_of: datetime
    bar_open_ms: int
    price: float
    atr_intraday: float | None
    session_broken: bool
    levels: list[tuple[str, float, float | None, str]] = field(default_factory=list)
    triggered: list[TriggeredPattern] = field(default_factory=list)
    qualifying: list[dict] = field(default_factory=list)


_LEVELS = {
    "or5_high": "OR-5 high", "or5_low": "OR-5 low",
    "or15_high": "OR-15 high", "or15_low": "OR-15 low",
    "or30_high": "OR-30 high", "or30_low": "OR-30 low",
    "vwap": "VWAP", "vwap_upper_1s": "VWAP +1σ", "vwap_lower_1s": "VWAP −1σ",
    "vwap_upper_2s": "VWAP +2σ", "vwap_lower_2s": "VWAP −2σ", "poc": "POC",
    "prior_day_high": "prior day high", "prior_day_low": "prior day low",
    "prior_day_close": "prior day close",
}


def build(con, cfg: Config, symbol: str, timeframe: str, *, as_of: datetime) -> LiveView | None:
    """Snapshot of the LAST CLOSED bar. Never the forming one."""
    bf = read_bars(con, symbol, timeframe, as_of=as_of, venue=cfg.venue.name)
    if bf.is_empty:
        return None
    df = add_regime_columns(compute_levels(bf, cfg).to_polars())
    row = df.to_dicts()[-1]

    levels = []
    for col, label in _LEVELS.items():
        v = row.get(col)
        if v is None:
            continue
        if col in DAILY_DISTANCE_LEVELS:
            d, unit = row.get(f"dist_{col}_datr"), "daily ATR"
        elif col in DISTANCE_LEVELS:
            d, unit = row.get(f"dist_{col}_atr"), f"{timeframe} ATR"
        else:
            d, unit = None, "—"
        levels.append((label, v, d, unit))

    registry = {s["setup"]: s for s in all_setups(con, symbol=symbol, timeframe=timeframe)}
    triggered: list[TriggeredPattern] = []
    for name, spec in sorted(REGISTRY.items()):
        if any(c not in df.columns for c in spec.requires):
            continue
        if not bool(detect(df, name).to_list()[-1]):
            continue
        v = registry.get(name)
        triggered.append(
            TriggeredPattern(
                name=name, direction=spec.direction,
                validated=bool(v and v["qualifies"]),
                net_in=v["net_in"] if v else None,
                n_in=v["n_in"] if v else None,
                ci_low=v["ci_low"] if v else None,
                ci_high=v["ci_high"] if v else None,
                disqualifiers=(v["disqualifiers"] if v else "never validated"),
            )
        )

    return LiveView(
        symbol=symbol, timeframe=timeframe, as_of=as_of,
        bar_open_ms=int(row["bar_open_ms"]), price=row["close"],
        atr_intraday=row.get("atr_intraday"),
        session_broken=bool(row.get("session_broken")),
        levels=levels, triggered=triggered,
        qualifying=qualifying_setups(con, symbol=symbol, timeframe=timeframe),
    )


# ------------------------------------------------------------------ predict-first


def prompt_prediction(console: Console, view: LiveView) -> dict | None:
    """Ask for the user's own read BEFORE revealing anything measured.

    Shows price and time only. No levels, no triggered patterns, no expectancies --
    revealing those first would make the "prediction" a reaction to the tool's opinion,
    which is the opposite of the exercise.
    """
    ts = from_ms(view.bar_open_ms).astimezone(ET)
    console.print(
        Panel(
            f"[bold]{view.symbol}[/bold] {view.timeframe} · bar closed {ts:%H:%M ET} · "
            f"price [bold]{view.price:,.2f}[/bold]\n\n"
            "[dim]Your read first. Nothing else is shown until you answer.[/dim]",
            title="predict-first", border_style="cyan",
        )
    )
    try:
        direction = console.input("  direction [long/short/none]: ").strip().lower()
        if direction not in ("long", "short", "none", "l", "s", "n"):
            direction = "none"
        direction = {"l": "long", "s": "short", "n": "none"}.get(direction, direction)
        stop = None
        if direction != "none":
            raw = console.input("  where would your stop go: ").strip()
            try:
                stop = float(raw.replace(",", ""))
            except ValueError:
                stop = None
        note = console.input("  one-line reasoning (optional): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return {"direction": direction, "stop": stop, "note": note}


def record_prediction(con, view: LiveView, pred: dict) -> str:
    pid = str(uuid.uuid4())
    con.execute(
        "INSERT INTO predictions VALUES (?,?,?,?,?,?,?,?,?)",
        [pid, now_ms(), view.symbol, view.timeframe, view.bar_open_ms,
         pred["direction"], pred.get("stop"), pred.get("note"), None],
    )
    return pid


# ------------------------------------------------------------------ rendering


def render(console: Console, view: LiveView, cfg: Config, *, prediction: dict | None = None) -> None:
    ts = from_ms(view.bar_open_ms).astimezone(ET)
    console.print(
        f"\n[bold cyan]{view.symbol}[/bold cyan] {view.timeframe} · last closed bar "
        f"{ts:%H:%M ET} · [bold]{view.price:,.2f}[/bold]"
        + ("  [red]session BROKEN[/red]" if view.session_broken else "")
    )

    lt = Table(box=None, header_style="bold", pad_edge=False)
    lt.add_column("level"); lt.add_column("price", justify="right")
    lt.add_column("distance", justify="right"); lt.add_column("unit")
    known = [x for x in view.levels if x[2] is not None]
    for label, value, dist, unit in sorted(known, key=lambda x: -x[2]):
        lt.add_row(label, f"{value:,.2f}",
                   Text(f"{dist:+.2f}", style="green" if dist < 0 else "red"), unit)
    console.print(lt)

    # --- triggered patterns: shown WITH expectancy, negatives banner-flagged ---
    console.print()
    if not view.triggered:
        console.print("[dim]no patterns triggered on the last closed bar[/dim]")
    for t in view.triggered:
        _render_triggered(console, t)

    # --- signal cards: ONLY for qualifying setups -----------------------------
    console.print()
    fired = [t.name for t in view.triggered]
    cards = [q for q in view.qualifying if q["setup"] in fired]
    if not view.qualifying:
        console.print(
            Panel(
                "No setup on this instrument has passed the gate:\n"
                "  • survived Benjamini-Hochberg correction, AND\n"
                "  • held its expectancy sign out of sample, AND\n"
                "  • positive net expectancy at your actual fee tier, AND\n"
                "  • n ≥ 100\n\n"
                "[bold]No signal cards will be emitted.[/bold] This is the measured "
                "answer, not a missing feature.",
                title="NO QUALIFYING SETUPS", border_style="yellow", style="yellow",
            )
        )
    elif not cards:
        console.print("[dim]qualifying setups exist, but none triggered on this bar[/dim]")
    else:
        for q in cards:
            _render_card(console, view, q, cfg)

    if prediction:
        console.print()
        console.print(
            f"[dim]your read: {prediction['direction']}"
            + (f" · stop {prediction['stop']:,.2f}" if prediction.get("stop") else "")
            + (f" · {prediction['note']}" if prediction.get("note") else "")
            + "  (recorded — `tradedesk journal score` compares it to outcomes)[/dim]"
        )


def _render_triggered(console: Console, t: TriggeredPattern) -> None:
    head = f"{t.name} · {t.direction}"
    if t.net_in is None:
        console.print(f"  [dim]▸ {head} — never validated[/dim]")
        return
    if t.net_in < 0:
        console.print(
            Panel(
                f"{head}\n"
                f"measured net expectancy [bold]{t.net_in:+.3f}R[/bold] over n={t.n_in:,}"
                + (f"  [95% CI {t.ci_low:+.2f} … {t.ci_high:+.2f}]"
                   if t.ci_low is not None else "")
                + f"\n[dim]{t.disqualifiers}[/dim]",
                title="⚠  NEGATIVE EXPECTANCY", border_style="red", style="red",
                title_align="left",
            )
        )
    else:
        style = "green" if t.validated else "yellow"
        console.print(
            f"  ▸ [bold]{head}[/bold] — net {t.net_in:+.3f}R (n={t.n_in:,}) "
            f"[{style}]{'VALIDATED' if t.validated else 'NOT VALIDATED'}[/{style}]"
            + (f" [dim]· {t.disqualifiers}[/dim]" if not t.validated else "")
        )


def _render_card(console: Console, view: LiveView, q: dict, cfg: Config) -> None:
    atr = view.atr_intraday
    if not atr or atr <= 0:
        return
    is_long = q["direction"] == "long"
    stop_atr, target_r = q["stop_atr"], q["target_r"]
    risk = stop_atr * atr
    entry = view.price
    stop = entry - risk if is_long else entry + risk
    target = entry + target_r * risk if is_long else entry - target_r * risk
    costs = CostModel.for_symbol(cfg, view.symbol)

    body = (
        f"[bold]{q['direction'].upper()}[/bold] · {q['setup']} · {view.symbol} "
        f"{view.timeframe}\n"
        f"├ Entry        {entry:,.2f}  (next bar open)\n"
        f"├ Stop         {stop:,.2f}  ({stop_atr:g}×ATR)   1R = {risk:,.2f}\n"
        f"├ Target       {target:,.2f}  ({target_r:g}R)\n"
        f"├ Invalidation a close beyond the stop, or the session's ≥30min hole rule\n"
        f"├ Base rate    n={q['n_in']:,} · net {q['net_in']:+.4f}R "
        f"[95% CI {q['ci_low']:+.3f} … {q['ci_high']:+.3f}]\n"
        f"├ Out-of-sample gross {q['gross_out']:+.4f}R (sign held)\n"
        f"├ vs random    p={q['p_value']:.4f}, survived BH correction\n"
        f"└ Cost drag    {costs.round_trip_in_r(entry, stop):.2f}R at "
        f"{q['round_trip_bps']:.0f} bps round trip"
    )
    console.print(Panel(body, border_style="green", title="SIGNAL", title_align="left"))
