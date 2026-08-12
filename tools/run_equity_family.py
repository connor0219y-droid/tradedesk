"""Run the pre-registered equity family: 42 tests, one correction across all of them.

Implements PREREGISTRATION.md Part 2 exactly. Where this script and that document
disagree, the document is the claim and this file is the bug.

    time-series, 5m    10 detectors, pooled across the 50 names picked by 2018 liquidity
    time-series, 1d    26 detectors, pooled across the same 50
    cross-sectional    6 strategies, long-short quintiles on the 681-name universe
    ----------------------------------------------------------------------------
    42 tests, Benjamini-Hochberg at FDR 0.05 applied ONCE across all of them

The two families are printed separately -- a per-trade R-multiple and a per-month
portfolio return are different quantities and belong in different tables -- but the
correction spans the union, because the union is what gets looked at.

COSTS ARE ESTIMATED PER SYMBOL, from that symbol's own daily bars, using the pooled
Corwin-Schultz estimator. Commission is zero; the spread is not, and assuming it away
would be the equity-side version of the mistake findings 1-8 exist to catch.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
import zlib
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tradedesk import store  # noqa: E402
from tradedesk.backtest.cross_section import build_panel, run_cross_section  # noqa: E402
from tradedesk.backtest.engine import BacktestConfig, run_backtest  # noqa: E402
from tradedesk.backtest.equity_costs import (  # noqa: E402
    equity_cost_model,
    estimate_spread,
    summarise,
)
from tradedesk.backtest.pooled import (  # noqa: E402
    apply_correction,
    build_report,
    symbol_null,
)
from tradedesk.backtest.validate import _bt_for  # noqa: E402
from tradedesk.calendars import EquityCalendar  # noqa: E402
from tradedesk.config import load_config  # noqa: E402
from tradedesk.equity_integrity import drop_after_hours  # noqa: E402
from tradedesk.frames import BarFrame  # noqa: E402
from tradedesk.levels import compute_levels  # noqa: E402
from tradedesk.patterns import REGISTRY, detect, registered  # noqa: E402
from tradedesk.patterns.cross_sectional import CROSS_SECTIONAL  # noqa: E402
from tradedesk.patterns.regime import add_regime_columns  # noqa: E402
from tradedesk.resample import read_bars_any  # noqa: E402
from tradedesk.timeutil import tf_ms  # noqa: E402
from tradedesk.universe import default as default_universe  # noqa: E402

VENUE = "alpaca"
DRAWS = 4000
INTRADAY_LIST = REPO_ROOT / "universe" / "intraday50_2018_liquidity.csv"


def _seed(*parts: str) -> int:
    """A STABLE per-(detector, symbol) seed.

    Not `hash()`. Python randomises string hashing per process unless PYTHONHASHSEED is
    set, so `hash((detector, symbol))` produced a different seed on every run -- and with
    it a different null, different p-values, and a result that could not be reproduced by
    re-running the command that generated it. For a project whose entire claim is that
    its numbers survive scrutiny, a non-reproducible p-value is a defect of the same
    class as a lookahead bug: it fails silently and only shows up if someone checks.

    crc32 is stable across processes, versions and platforms.
    """
    return zlib.crc32("|".join(parts).encode()) & 0x7FFFFFFF


def intraday_names() -> list[str]:
    with INTRADAY_LIST.open() as fh:
        return [r["ticker"] for r in csv.DictReader(
            line for line in fh if not line.startswith("#"))]


def load_symbol(con, cfg, symbol: str, timeframe: str, as_of):
    """Bars for one symbol, restricted to the declared session before levels compute.

    After-hours bars are dropped HERE, ahead of `compute_levels`, not afterwards. A
    quarter of Alpaca's intraday series is post-market; leaving it in and filtering
    later would already have contaminated session VWAP, the noise band and every
    contiguity run by the time anyone looked.
    """
    bf = read_bars_any(con, symbol, timeframe, as_of=as_of, venue=VENUE)
    if bf.is_empty:
        return None
    kept, dropped = drop_after_hours(
        bf.to_polars().sort("bar_open_ms"), EquityCalendar(), timeframe_ms=tf_ms(timeframe)
    )
    if kept.is_empty():
        return None
    bf = BarFrame(df=kept, venue=bf.venue, symbol=bf.symbol, timeframe=bf.timeframe,
                  calendar_version=bf.calendar_version, as_of_ms=bf.as_of_ms)
    return add_regime_columns(compute_levels(bf, cfg).to_polars())


def run_timeseries(con, cfg, symbols, timeframe, as_of, spreads):
    """Every detector declared for this timeframe, pooled across `symbols`."""
    names = [n for n in registered(family="published")
             if REGISTRY[n].runs_on(timeframe)]
    print(f"\n=== time-series {timeframe}: {len(names)} detectors x {len(symbols)} symbols ===")

    trades: dict[str, dict[str, list]] = {n: {} for n in names}
    nulls: dict[str, list] = {n: [] for n in names}
    signals: dict[str, int] = {n: 0 for n in names}
    run_bt = BacktestConfig()
    tfms = tf_ms(timeframe)

    for i, sym in enumerate(symbols, 1):
        df = load_symbol(con, cfg, sym, timeframe, as_of)
        if df is None or df.height < 300:
            print(f"  [{i}/{len(symbols)}] {sym}: too little data, skipped")
            continue
        costs = equity_cost_model(spread_bps=spreads.get(sym, 8.0))
        for n in names:
            spec = REGISTRY[n]
            if any(c not in df.columns for c in spec.requires):
                continue
            bt = _bt_for(spec, run_bt, tf_ms=tfms)
            sig = detect(df, n)
            res = run_backtest(df, sig, is_long=spec.is_long, timeframe=timeframe,
                               costs=costs, bt=bt, resolver=None)
            signals[n] += res.signals_total
            if not res.trades:
                continue
            trades[n][sym] = res.trades
            part = symbol_null(df, res.trades, is_long=spec.is_long,
                               timeframe=timeframe, costs=costs, bt=bt, resolver=None,
                               draws=DRAWS, seed=_seed(n, sym))
            if part:
                nulls[n].append(part)
        if i % 10 == 0 or i == len(symbols):
            done = sum(len(v) for v in trades.values())
            print(f"  [{i}/{len(symbols)}] {sym} · {done} detector-symbol samples so far")

    reports = []
    for n in names:
        spec = REGISTRY[n]
        bt = _bt_for(spec, run_bt, tf_ms=tfms)
        rt = 2 * equity_cost_model(
            spread_bps=sum(spreads.values()) / max(1, len(spreads))
        ).per_side_bps
        reports.append(build_report(
            n, timeframe=timeframe, direction=spec.direction,
            trades_by_symbol=trades[n], null_parts=nulls[n], signals=signals[n],
            bt=bt, round_trip_bps=rt, draws=DRAWS,
            in_sample_pct=float(cfg.backtest.get("in_sample_pct", 70)),
            min_n=int(cfg.backtest.get("min_sample_size", 30)),
            provisional_n=int(cfg.backtest.get("provisional_n", 100)),
            bootstrap_iterations=2000,
        ))
    return reports


def run_cross_sectional(con, cfg, members, as_of, spread_bps):
    """The six quintile strategies on the full point-in-time universe."""
    tickers = sorted(members.all_tickers())
    print(f"\n=== cross-sectional: {len(CROSS_SECTIONAL)} strategies x {len(tickers)} names ===")
    frames = {}
    for i, sym in enumerate(tickers, 1):
        bf = read_bars_any(con, sym, "1d", as_of=as_of, venue=VENUE)
        if bf.is_empty:
            continue
        df = bf.to_polars()
        if df.height >= 60:
            frames[sym] = df
        if i % 200 == 0:
            print(f"  loaded {i}/{len(tickers)} ({len(frames)} usable)")
    panel = build_panel(frames)
    print(f"  panel: {panel.height:,} rows, {panel['symbol'].n_unique()} symbols")

    membership = {d: set(v) for d, v in members.snapshots.items()}
    costs = equity_cost_model(spread_bps=spread_bps)
    out = []
    for spec in CROSS_SECTIONAL:
        t0 = time.time()
        # DRAWS is fixed by PREREGISTRATION.md for BOTH families. Running the
        # cross-sectional leg at 1,000 put xs_low_volatility at the 1/1001 floor,
        # where it 'survived' the correction by 0.0002 as an artifact of the draw
        # count rather than on evidence, and left xs_reversal_1m on the wrong side
        # of the line. One constant, both legs.
        res = run_cross_section(panel, spec, membership=membership, costs=costs,
                                draws=DRAWS, seed=7,
                                in_sample_pct=float(cfg.backtest.get('in_sample_pct', 70)))
        out.append((spec, res))
        if res is None:
            print(f"  {spec.name:24s} no periods ({time.time()-t0:.0f}s)")
        else:
            print(f"  {spec.name:24s} n={res.n_periods:3d} gross={res.gross_mean:+.4f} "
                  f"net={res.net_mean:+.4f} p={res.p_value} ({time.time()-t0:.0f}s)")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument("--skip-5m", action="store_true")
    ap.add_argument("--skip-1d", action="store_true")
    ap.add_argument("--skip-xs", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    con = store.connect(cfg.data.db_path, read_only=True)
    as_of = datetime.now(timezone.utc)
    members = default_universe()
    names = intraday_names()
    if args.limit_symbols:
        names = names[: args.limit_symbols]

    # --- costs, estimated per symbol from its own daily bars
    print(f"estimating spreads for {len(names)} names")
    spreads, ests = {}, []
    for sym in names:
        bf = read_bars_any(con, sym, "1d", as_of=as_of, venue=VENUE)
        if bf.is_empty:
            continue
        est = estimate_spread(bf.to_polars(), sym)
        spreads[sym] = est.spread_bps
        ests.append(est)
    print(" ", summarise(ests))

    reports = []
    if not args.skip_5m:
        reports += run_timeseries(con, cfg, names, "5m", as_of, spreads)
    if not args.skip_1d:
        reports += run_timeseries(con, cfg, names, "1d", as_of, spreads)

    xs = []
    if not args.skip_xs:
        avg = sum(spreads.values()) / max(1, len(spreads))
        xs = run_cross_sectional(con, cfg, members, as_of, avg)

    # --- ONE correction across everything actually tested
    labels = [(f"ts:{r.detector}@{r.timeframe}", r.p_value) for r in reports]
    labels += [(f"xs:{s.name}", (r.p_value if r else None)) for s, r in xs]
    verdicts = apply_correction(labels, fdr=0.05)
    for r in reports:
        ok, thr = verdicts[f"ts:{r.detector}@{r.timeframe}"]
        r.survives_correction, r.bh_threshold = ok, thr

    out = REPO_ROOT / "equity_family_results.json"
    import json
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "m_total": len(labels),
        "time_series": [
            {
                "detector": r.detector, "timeframe": r.timeframe,
                "direction": r.direction, "symbols": r.n_symbols,
                "signals": r.signals, "n_in": r.in_sample.n, "n_out": r.out_sample.n,
                "gross_in": r.gross_in, "gross_out": r.gross_out, "net_in": r.net_in,
                "drag_in": r.drag_in, "p": r.p_value, "null_mean": r.null_mean,
                "ci_low": r.in_sample.ci_low, "ci_high": r.in_sample.ci_high,
                "survives_bh": r.survives_correction, "bh_threshold": r.bh_threshold,
                "oos_sign_held": r.oos_sign_held,
                "stop_atr": r.stop_atr, "target_r": r.target_r, "max_bars": r.max_bars,
            } for r in reports
        ],
        "cross_sectional": [
            {
                "strategy": s.name, "source": s.source,
                "n_periods": (r.n_periods if r else 0),
                "gross_mean": (r.gross_mean if r else None),
                "net_mean": (r.net_mean if r else None),
                "t_stat": (r.gross_t if r else None),
                "turnover": (r.turnover if r else None),
                "p": (r.p_value if r else None),
                "null_mean": (r.null_mean if r else None),
                "n_names_median": (r.n_names_median if r else 0),
                "n_in": (r.n_in if r else 0), "n_out": (r.n_out if r else 0),
                "gross_in": (r.gross_in if r else None),
                "gross_out": (r.gross_out if r else None),
                "oos_sign_held": (r.oos_sign_held if r else False),
                "survives_bh": verdicts[f"xs:{s.name}"][0],
                "bh_threshold": verdicts[f"xs:{s.name}"][1],
            } for s, r in xs
        ],
    }
    out.write_text(json.dumps(payload, indent=2))
    print(f"\nwrote {out}  (m = {len(labels)} tests, "
          f"{sum(1 for _, p in labels if p is not None and p < 0.05)} raw p<0.05, "
          f"{sum(1 for v in verdicts.values() if v[0])} survive BH)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
