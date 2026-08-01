"""Phase 6 -- the journal and the feedback loop.

The key report is the last one: **live expectancy per setup against the backtested
number for that setup.** Divergence localises the problem. If the backtest says +0.05R
and you realise −0.20R, the strategy is not what is broken -- execution is. If both are
negative, the strategy is.

The behavioural flags exist because the failure modes that cost real money are not
statistical, they are human: revenge entries, oversizing, cutting winners, and letting
losers run past the stop. Each flag is computed from the logged trade, not
self-reported.
"""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass, field

import duckdb
import polars as pl

from .qualification import all_setups
from .timeutil import now_ms

REVENGE_WINDOW_MS = 5 * 60_000
MFE_GIVEBACK_THRESHOLD = 1.0     # reached at least 1R favourable...
MFE_GIVEBACK_KEPT = 0.4          # ...but kept less than 0.4R
STOP_OVERRUN_R = -1.3            # exited worse than -1.3R


@dataclass
class Trade:
    trade_id: str
    symbol: str
    setup: str | None
    direction: str
    thesis: str | None
    entry_ms: int
    exit_ms: int | None
    entry: float
    stop: float
    target: float | None
    exit_price: float | None
    exit_reason: str | None
    size: float | None
    mfe_r: float | None
    mae_r: float | None
    account_size: float | None

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    @property
    def risk_per_unit(self) -> float:
        return abs(self.entry - self.stop)

    @property
    def realised_r(self) -> float | None:
        if self.exit_price is None or self.risk_per_unit <= 0:
            return None
        move = (self.exit_price - self.entry) if self.is_long else (self.entry - self.exit_price)
        return move / self.risk_per_unit

    @property
    def risk_pct_of_account(self) -> float | None:
        if not self.account_size or self.size is None:
            return None
        return 100.0 * (self.size * self.risk_per_unit) / self.account_size


def log_trade(con: duckdb.DuckDBPyConnection, **kw) -> str:
    tid = kw.pop("trade_id", None) or str(uuid.uuid4())
    con.execute(
        "INSERT INTO trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [tid, kw["symbol"], kw.get("setup"), kw["direction"], kw.get("thesis"),
         kw["entry_ms"], kw.get("exit_ms"), kw["entry"], kw["stop"], kw.get("target"),
         kw.get("exit_price"), kw.get("exit_reason"), kw.get("size"),
         kw.get("mfe_r"), kw.get("mae_r"), kw.get("account_size"),
         kw.get("signal_id"), now_ms()],
    )
    return tid


def load_trades(con: duckdb.DuckDBPyConnection, *, symbol: str | None = None) -> list[Trade]:
    sql = "SELECT * FROM trades"
    params: list[object] = []
    if symbol:
        sql += " WHERE symbol = ?"
        params.append(symbol)
    sql += " ORDER BY entry_ms"
    rows = con.execute(sql, params).pl().to_dicts()
    return [
        Trade(
            trade_id=r["trade_id"], symbol=r["symbol"], setup=r["setup"],
            direction=r["direction"], thesis=r["thesis"], entry_ms=r["entry_ms"],
            exit_ms=r["exit_ms"], entry=r["entry"], stop=r["stop"], target=r["target"],
            exit_price=r["exit_price"], exit_reason=r["exit_reason"], size=r["size"],
            mfe_r=r["mfe_r"], mae_r=r["mae_r"], account_size=r["account_size"],
        )
        for r in rows
    ]


# ------------------------------------------------------------- behavioural flags


def behavioural_flags(trades: list[Trade], *, max_risk_pct: float) -> dict[str, list[str]]:
    """Per-trade flags, computed from the log rather than self-reported.

    These are the failure modes that cost money without showing up in any expectancy
    statistic, because they are about *which* trades you take and *when* you leave.
    """
    flags: dict[str, list[str]] = {}
    closed = [t for t in trades if t.realised_r is not None]

    for i, t in enumerate(trades):
        f: list[str] = []

        # Revenge entry: opened within 5 minutes of a losing exit.
        for prior in trades[:i]:
            if prior.exit_ms is None:
                continue
            r = prior.realised_r
            if r is not None and r < 0 and 0 <= t.entry_ms - prior.exit_ms <= REVENGE_WINDOW_MS:
                mins = (t.entry_ms - prior.exit_ms) / 60_000
                f.append(f"entered {mins:.1f} min after a {r:+.2f}R loss")
                break

        # Oversized against your own stated maximum.
        pct = t.risk_pct_of_account
        if pct is not None and pct > max_risk_pct:
            f.append(f"risked {pct:.2f}% of account, above your stated max {max_risk_pct:.2f}%")

        r = t.realised_r
        # Gave back a winner: was up 1R or more, kept less than 0.4R.
        if (
            r is not None and t.mfe_r is not None
            and t.mfe_r >= MFE_GIVEBACK_THRESHOLD and r < MFE_GIVEBACK_KEPT
        ):
            f.append(f"reached {t.mfe_r:.2f}R favourable but exited at {r:+.2f}R")

        # Exited worse than the stop should have allowed.
        if r is not None and r < STOP_OVERRUN_R:
            f.append(f"exited at {r:+.2f}R, worse than the {STOP_OVERRUN_R}R the stop implied")

        if f:
            flags[t.trade_id] = f
    return flags


# ------------------------------------------------------------------- statistics


@dataclass
class Rolling:
    n: int
    expectancy_r: float | None
    profit_factor: float | None
    win_rate: float | None
    max_drawdown_r: float | None
    risk_of_ruin: float | None
    avg_win_r: float | None
    avg_loss_r: float | None


def risk_of_ruin(win_rate: float, avg_win_r: float, avg_loss_r: float, n_units: float) -> float:
    """RoR = ((1-A)/(1+A))^N, with A = (winRate x avgWinR) - (lossRate x avgLossR).

    N is the number of R-units between current equity and a 30% drawdown. A negative
    edge gives A <= 0, for which the formula is undefined or >= 1 -- reported as 1.0,
    i.e. ruin is certain given enough trades, which is the honest reading.
    """
    a = win_rate * avg_win_r - (1.0 - win_rate) * abs(avg_loss_r)
    if a <= 0:
        return 1.0
    if n_units <= 0:
        return 1.0
    base = (1.0 - a) / (1.0 + a)
    if base <= 0:
        return 0.0
    return min(1.0, base ** n_units)


def rolling_stats(trades: list[Trade], *, risk_pct: float = 1.0) -> Rolling:
    rs = [t.realised_r for t in trades if t.realised_r is not None]
    if not rs:
        return Rolling(0, None, None, None, None, None, None, None)

    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    win_rate = len(wins) / len(rs)
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (sum(losses) / len(losses)) if losses else 0.0

    equity, peak, max_dd = 0.0, 0.0, 0.0
    for r in rs:
        equity += r
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    # R-units to a 30% drawdown, given each trade risks `risk_pct` of the account.
    n_units = 30.0 / risk_pct if risk_pct > 0 else 0.0
    ror = risk_of_ruin(win_rate, avg_win, avg_loss, n_units) if wins or losses else None

    return Rolling(
        n=len(rs),
        expectancy_r=sum(rs) / len(rs),
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        win_rate=win_rate,
        max_drawdown_r=max_dd,
        risk_of_ruin=ror,
        avg_win_r=avg_win if wins else None,
        avg_loss_r=avg_loss if losses else None,
    )


# --------------------------------------------------- the key report: live vs backtest


@dataclass
class Divergence:
    setup: str
    live_n: int
    live_expectancy: float | None
    backtest_n: int | None
    backtest_expectancy: float | None
    validated: bool
    disqualifiers: str

    @property
    def gap(self) -> float | None:
        if self.live_expectancy is None or self.backtest_expectancy is None:
            return None
        return self.live_expectancy - self.backtest_expectancy

    @property
    def reading(self) -> str:
        """What the divergence localises: strategy, execution, or too little data."""
        if self.live_n < 20:
            return "too few live trades to read"
        if self.backtest_expectancy is None:
            return "no backtested baseline — setup was never validated"
        g = self.gap
        if g is None:
            return "—"
        if abs(g) < 0.05:
            return "live matches backtest"
        if g < 0 and self.backtest_expectancy > 0:
            return "EXECUTION — the setup tested positive, your fills did not"
        if g < 0:
            return "both negative — the strategy, not the execution"
        return "live better than backtest — check for survivorship in your logging"


def live_vs_backtest(
    con: duckdb.DuckDBPyConnection, trades: list[Trade], *, symbol: str | None = None
) -> list[Divergence]:
    registry = {s["setup"]: s for s in all_setups(con, symbol=symbol)}
    by_setup: dict[str, list[float]] = {}
    for t in trades:
        r = t.realised_r
        if t.setup and r is not None:
            by_setup.setdefault(t.setup, []).append(r)

    out: list[Divergence] = []
    for setup, rs in sorted(by_setup.items()):
        v = registry.get(setup)
        out.append(
            Divergence(
                setup=setup, live_n=len(rs),
                live_expectancy=sum(rs) / len(rs) if rs else None,
                backtest_n=v["n_in"] if v else None,
                backtest_expectancy=v["net_in"] if v else None,
                validated=bool(v and v["qualifies"]),
                disqualifiers=(v["disqualifiers"] if v else "never validated"),
            )
        )
    return out
