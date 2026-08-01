"""The backtest: one position at a time, entry at the next bar's open.

Two rules carry most of the honesty here.

ENTRY IS THE OPEN OF THE BAR AFTER THE SIGNAL BAR CLOSES. Never the signal bar's close.
Using the close means the backtest knew the bar's outcome before deciding to act on it,
which manufactures edge out of nothing.

THAT RULE CREATES A TRAP, and it is guarded: if the next bar is not contiguous with the
signal bar, its "open" may be six hours later at a price gapped far away. Such a signal
is SKIPPED, not filled at a stale price. SOL/USD 1m alone has 3,450 gap-adjacent bars.

ONE POSITION AT A TIME. Measured on the real store, 50-64% of pattern signals fire
within 12 bars of the previous one. Treating 36,000 overlapping trades as independent
observations yields a bootstrap CI near +/-0.01R -- tight, and false. Taking one
position at a time both matches how a single account actually trades and leaves the
trades close enough to independent for the interval to mean something.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from ..timeutil import tf_ms as _tf_ms
from .costs import CostModel
from .exits import IntrabarResolver, resolve_exit


@dataclass
class Trade:
    signal_index: int
    entry_index: int
    exit_index: int
    signal_ms: int
    entry_ms: int
    exit_ms: int
    is_long: bool
    entry_price: float
    stop: float
    target: float
    exit_price: float
    exit_reason: str
    bars_held: int
    r_gross: float
    r_net: float
    mfe_r: float
    mae_r: float
    # context captured at the SIGNAL bar, never later
    tod_ms: int | None = None
    rvol: float | None = None
    eff_ratio: float | None = None
    atr_pct: float | None = None


@dataclass
class BacktestConfig:
    stop_atr: float = 1.0
    target_r: float = 2.0
    max_bars: int = 48
    atr_column: str = "atr_intraday"


@dataclass
class BacktestResult:
    trades: list[Trade] = field(default_factory=list)
    signals_total: int = 0
    skipped_no_atr: int = 0
    skipped_gap: int = 0
    skipped_busy: int = 0
    skipped_no_next_bar: int = 0

    @property
    def n(self) -> int:
        return len(self.trades)

    def r_net(self) -> list[float]:
        return [t.r_net for t in self.trades]

    def to_frame(self) -> pl.DataFrame:
        if not self.trades:
            return pl.DataFrame(schema={"r_net": pl.Float64})
        return pl.DataFrame([t.__dict__ for t in self.trades])


def precompute_outcomes(
    df: pl.DataFrame,
    *,
    is_long: bool,
    timeframe: str,
    costs: CostModel,
    bt: BacktestConfig,
    resolver: IntrabarResolver | None = None,
) -> dict[int, tuple[int, float, float]]:
    """Outcome of entering at every eligible bar: {signal_index: (exit_index, gross, net)}.

    One pass over the series, reused across all Monte Carlo draws. Without this, each
    draw re-simulates from scratch and 1,000 draws costs ~50 minutes per series -- which
    in practice means running 60 draws and calling it a null distribution.

    Uses exactly the same eligibility and pricing rules as `run_backtest`, so a baseline
    built from this is subject to the identical constraints as the pattern it is
    compared against.
    """
    tf = _tf_ms(timeframe)
    o = df["open"].to_list()
    h = df["high"].to_list()
    l = df["low"].to_list()
    c = df["close"].to_list()
    ts = df["bar_open_ms"].to_list()
    sess = df["session_date"].to_list()
    gap = df["gap"].to_list()
    atr = df[bt.atr_column].to_list()

    out: dict[int, tuple[int, float, float]] = {}
    for i in range(len(o) - 1):
        if gap[i + 1]:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        mid = o[i + 1]
        risk = bt.stop_atr * a
        stop = mid - risk if is_long else mid + risk
        target = mid + bt.target_r * risk if is_long else mid - bt.target_r * risk
        entry = costs.fill_price(mid, is_long=is_long, is_entry=True)
        ex = resolve_exit(
            highs=h, lows=l, closes=c, opens=o, ts=ts, session_dates=sess, tf_ms=tf,
            entry_index=i + 1, entry_price=entry, stop=stop, target=target,
            is_long=is_long, max_bars=bt.max_bars, resolver=resolver,
        )
        exit_fill = costs.fill_price(ex.price, is_long=is_long, is_entry=False)
        gross = (ex.price - mid) / risk if is_long else (mid - ex.price) / risk
        net = (exit_fill - entry) / risk if is_long else (entry - exit_fill) / risk
        out[i] = (ex.bar_index, gross, net)
    return out


def run_backtest(
    df: pl.DataFrame,
    signals: pl.Series,
    *,
    is_long: bool,
    timeframe: str,
    costs: CostModel,
    bt: BacktestConfig,
    resolver: IntrabarResolver | None = None,
) -> BacktestResult:
    """Simulate one pattern on one series."""
    res = BacktestResult()
    tf = _tf_ms(timeframe)

    o = df["open"].to_list()
    h = df["high"].to_list()
    l = df["low"].to_list()
    c = df["close"].to_list()
    ts = df["bar_open_ms"].to_list()
    sess = df["session_date"].to_list()
    gap = df["gap"].to_list()
    atr = df[bt.atr_column].to_list()

    tod = df["ms_since_open"].to_list() if "ms_since_open" in df.columns else [None] * len(o)
    rvol = df["rvol_tod"].to_list() if "rvol_tod" in df.columns else [None] * len(o)
    eff = df["eff_ratio"].to_list() if "eff_ratio" in df.columns else [None] * len(o)
    apct = df["atr_pct_60d"].to_list() if "atr_pct_60d" in df.columns else [None] * len(o)

    sig_idx = [i for i, v in enumerate(signals.to_list()) if v]
    res.signals_total = len(sig_idx)
    busy_until = -1

    for i in sig_idx:
        if i <= busy_until:
            res.skipped_busy += 1
            continue
        if i + 1 >= len(o):
            res.skipped_no_next_bar += 1
            continue
        # The entry bar must be genuinely the next bar in time. If it opened a gap, its
        # open is a stale price on the far side of a hole.
        if gap[i + 1]:
            res.skipped_gap += 1
            continue
        a = atr[i]
        if a is None or a <= 0:
            res.skipped_no_atr += 1
            continue

        mid_entry = o[i + 1]
        risk = bt.stop_atr * a
        # Stop and target are MARKET levels, set from the decision price -- not from
        # your own adverse fill. Anchoring them to the fill would make the stop drift
        # with the cost assumption, and at 64bps per side the "stop" ends up above the
        # unadjusted entry, which is nonsense. In real trading you pick a level from
        # market structure and your fill is whatever it is.
        stop = mid_entry - risk if is_long else mid_entry + risk
        target = (
            mid_entry + bt.target_r * risk if is_long else mid_entry - bt.target_r * risk
        )
        entry = costs.fill_price(mid_entry, is_long=is_long, is_entry=True)

        ex = resolve_exit(
            highs=h, lows=l, closes=c, opens=o, ts=ts, session_dates=sess, tf_ms=tf,
            entry_index=i + 1, entry_price=entry, stop=stop, target=target,
            is_long=is_long, max_bars=bt.max_bars, resolver=resolver,
        )
        exit_fill = costs.fill_price(ex.price, is_long=is_long, is_entry=False)

        # Gross uses unadjusted mid prices; net uses the adverse fills on both sides.
        # The difference between them is exactly the cost drag, which the cost-sanity
        # test asserts is always negative.
        gross = (ex.price - mid_entry) / risk if is_long else (mid_entry - ex.price) / risk
        net = (exit_fill - entry) / risk if is_long else (entry - exit_fill) / risk

        res.trades.append(
            Trade(
                signal_index=i, entry_index=i + 1, exit_index=ex.bar_index,
                signal_ms=ts[i], entry_ms=ts[i + 1], exit_ms=ts[ex.bar_index],
                is_long=is_long, entry_price=entry, stop=stop, target=target,
                exit_price=exit_fill, exit_reason=ex.reason, bars_held=ex.bars_held,
                r_gross=gross, r_net=net, mfe_r=ex.mfe_r, mae_r=ex.mae_r,
                tod_ms=tod[i], rvol=rvol[i], eff_ratio=eff[i], atr_pct=apct[i],
            )
        )
        busy_until = ex.bar_index

    return res
