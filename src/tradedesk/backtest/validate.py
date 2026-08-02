"""Orchestration: run every pattern on a series and build its report.

The order here is load-bearing. Bucket thresholds are fitted on the IN-SAMPLE trades
only, then applied unchanged to the out-of-sample ones. Fitting them on everything and
then reporting out-of-sample results against those boundaries leaks the holdout's
distribution into the labels -- small, invisible, and exactly the kind of thing that
makes an out-of-sample number quietly optimistic.
"""

from __future__ import annotations

from datetime import datetime

import polars as pl

from ..config import Config
from ..frames import read_bars
from ..levels import compute_levels
from ..patterns import REGISTRY, detect
from ..patterns.regime import add_regime_columns
from .baseline import run_baseline
from .costs import CostModel
from .engine import BacktestConfig, Trade, run_backtest
from .exits import IntrabarResolver
from .report import PatternReport, apply_multiple_testing_correction
from .split import make_split, partition_trades
from .stats import compute


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def _slices(trades: list[Trade], thresholds: dict[str, float]) -> list[tuple]:
    """Context breakdown: time of day, relative volume, regime.

    'A pattern that works only in the first 90 minutes is a real finding' -- so the
    slices are reported even when a cell is thin, with the sample gate stated rather
    than the cell silently dropped.
    """
    out: list[tuple[str, int, float | None, float | None]] = []
    six_h = 6 * 3_600_000

    for label, lo, hi in (
        ("time 00-06 ET", 0, six_h),
        ("time 06-12 ET", six_h, 2 * six_h),
        ("time 12-18 ET", 2 * six_h, 3 * six_h),
        ("time 18-24 ET", 3 * six_h, 4 * six_h),
    ):
        sel = [t for t in trades if t.tod_ms is not None and lo <= t.tod_ms < hi]
        out.append((label, len(sel), _mean([t.r_gross for t in sel]),
                    _mean([t.r_net for t in sel])))

    rv = thresholds.get("rvol")
    if rv is not None and rv == rv:
        for label, pred in (
            (f"rel vol >= {rv:.2f}x", lambda t: t.rvol is not None and t.rvol >= rv),
            (f"rel vol <  {rv:.2f}x", lambda t: t.rvol is not None and t.rvol < rv),
        ):
            sel = [t for t in trades if pred(t)]
            out.append((label, len(sel), _mean([t.r_gross for t in sel]),
                        _mean([t.r_net for t in sel])))

    ef = thresholds.get("efficiency")
    if ef is not None and ef == ef:
        for label, pred in (
            (f"trending (eff >= {ef:.3f})",
             lambda t: t.eff_ratio is not None and t.eff_ratio >= ef),
            (f"ranging  (eff <  {ef:.3f})",
             lambda t: t.eff_ratio is not None and t.eff_ratio < ef),
        ):
            sel = [t for t in trades if pred(t)]
            out.append((label, len(sel), _mean([t.r_gross for t in sel]),
                        _mean([t.r_net for t in sel])))
    return out


def validate_series(
    con,
    cfg: Config,
    symbol: str,
    timeframe: str,
    *,
    as_of: datetime,
    patterns: list[str] | None = None,
    stop_atr: float = 1.0,
    target_r: float = 2.0,
    max_bars: int = 48,
    min_bars_between_entries: int = 0,
    baseline_draws: int = 200,
    bootstrap_iterations: int = 2000,
    use_intrabar: bool = True,
    costs: CostModel | None = None,
) -> list[PatternReport]:
    bf = read_bars(con, symbol, timeframe, as_of=as_of, venue=cfg.venue.name)
    if bf.is_empty:
        return []
    df = add_regime_columns(compute_levels(bf, cfg).to_polars())

    resolver = None
    if use_intrabar and timeframe != "1m":
        m1 = read_bars(con, symbol, "1m", as_of=as_of, venue=cfg.venue.name)
        if not m1.is_empty:
            resolver = IntrabarResolver.from_frame(m1.to_polars())

    costs = costs or CostModel.for_symbol(cfg, symbol)
    bt = BacktestConfig(
        stop_atr=stop_atr, target_r=target_r, max_bars=max_bars,
        min_bars_between_entries=min_bars_between_entries,
    )
    split = make_split(df, in_sample_pct=float(cfg.backtest.get("in_sample_pct", 70)))
    min_n = int(cfg.backtest.get("min_sample_size", 30))
    prov_n = int(cfg.backtest.get("provisional_n", 100))

    names = patterns or sorted(REGISTRY)
    reports: list[PatternReport] = []

    for name in names:
        spec = REGISTRY[name]
        missing = [c for c in spec.requires if c not in df.columns]
        if missing:
            continue
        sig = detect(df, name)
        res = run_backtest(
            df, sig, is_long=spec.is_long, timeframe=timeframe,
            costs=costs, bt=bt, resolver=resolver,
        )
        in_t, out_t = partition_trades(res.trades, split)
        split.assert_no_overlap([t.signal_ms for t in in_t], [t.signal_ms for t in out_t])

        # Thresholds fitted on IN-SAMPLE trades only.
        thresholds: dict[str, float] = {}
        if in_t:
            rv = sorted(t.rvol for t in in_t if t.rvol is not None)
            ef = sorted(t.eff_ratio for t in in_t if t.eff_ratio is not None)
            if rv:
                thresholds["rvol"] = rv[len(rv) // 2]
            if ef:
                thresholds["efficiency"] = ef[len(ef) // 2]

        s_in = compute([t.r_net for t in in_t], bootstrap_iterations=bootstrap_iterations,
                       min_n=min_n, provisional_n=prov_n)
        s_out = compute([t.r_net for t in out_t], bootstrap_iterations=bootstrap_iterations,
                        min_n=min_n, provisional_n=prov_n)
        # Win rate, profit factor and loss streaks are computed on GROSS. At a 248bps
        # round trip every net outcome is negative, so a net win rate is 0.0% for every
        # pattern -- arithmetically right and completely uninformative.
        s_in_gross = compute([t.r_gross for t in in_t],
                             bootstrap_iterations=bootstrap_iterations,
                             min_n=min_n, provisional_n=prov_n)
        g_in = _mean([t.r_gross for t in in_t])
        g_out = _mean([t.r_gross for t in out_t])
        drag = (s_in.expectancy_r - g_in) if (s_in.expectancy_r is not None and g_in is not None) else None

        base = None
        if in_t and len(in_t) >= min_n:
            base = run_baseline(
                df, in_t, is_long=spec.is_long, timeframe=timeframe, costs=costs,
                bt=bt, resolver=resolver, draws=baseline_draws,
            )

        reports.append(
            PatternReport(
                pattern=name, symbol=symbol, timeframe=timeframe,
                direction=spec.direction, stop_atr=stop_atr, target_r=target_r,
                max_bars=max_bars, min_bars_between_entries=min_bars_between_entries,
                in_sample=s_in, out_sample=s_out, in_sample_gross=s_in_gross,
                gross_in=g_in, gross_out=g_out,
                drag_in=drag, baseline=base, signals=res.signals_total,
                trades=res.n, slices=_slices(in_t, thresholds),
                round_trip_bps=2 * costs.per_side_bps,
            )
        )

    # Applied across the whole family tested on this series, not per pattern.
    apply_multiple_testing_correction(reports)
    return reports


def persist(con, reports, *, validated_at_ms: int) -> int:
    """Write every validation result into the qualification registry.

    Records rejections as well as passes: the brief shows a rejected setup with its
    disqualifiers, because absence and rejection look identical to a reader and are not
    the same thing. Returns how many qualify.
    """
    from ..qualification import SetupStats, record

    n_ok = 0
    for r in reports:
        stats = SetupStats(
            setup=r.pattern, symbol=r.symbol, timeframe=r.timeframe,
            direction=r.direction, stop_atr=r.stop_atr, target_r=r.target_r,
            max_bars=r.max_bars, round_trip_bps=r.round_trip_bps,
            n_in=r.in_sample.n, n_out=r.out_sample.n,
            gross_in=r.gross_in, gross_out=r.gross_out,
            net_in=r.in_sample.expectancy_r,
            ci_low=r.in_sample.ci_low, ci_high=r.in_sample.ci_high,
            p_value=r.baseline.p_value if r.baseline else None,
            survives_bh=bool(r.survives_correction),
        )
        if record(con, stats, validated_at_ms=validated_at_ms):
            n_ok += 1
    return n_ok
