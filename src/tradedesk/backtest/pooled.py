"""Pooled validation: one test per detector across a universe of symbols.

WHY POOLING, AND WHAT IT CLAIMS. Part 1 tested three crypto symbols and treated each as
its own family. At 50 symbols that stops working twice over. Arithmetically, 26 detectors
against 50 names is 1,300 tests whose smallest Benjamini-Hochberg threshold is 4e-5 --
below what any affordable number of Monte Carlo draws can resolve, so every result would
be "not significant" by construction rather than by evidence. And substantively it
answers a question nobody asked: Connors does not claim RSI(2) works on Cisco, he claims
it works on equities.

So each detector yields ONE sample: every trade it took, on every symbol, pooled. That is
the claim being tested, and it is one test.

THE NULL IS POOLED IDENTICALLY, which is the part that makes the comparison mean
anything. For each draw, random entries are sampled PER SYMBOL -- matched to that
symbol's own time-of-day histogram and its own trade count on that symbol -- and then
pooled into a single null mean. So the strategy and its null face identical symbol
composition, identical per-symbol trade counts, and identical hours. The only thing that
differs is which bars were chosen.

WHAT POOLING COSTS, stated here rather than discovered in the writeup. Part 1's
one-position-at-a-time rule kept trades close enough to independent that a bootstrap
interval meant something. Pooling across 50 names breaks that: positions overlap in time
and equities are cross-sectionally correlated through market beta, so the effective
sample is far smaller than the trade count suggests. Two things limit the damage -- the
null carries the same correlation, since it is pooled the same way, and the p-value comes
from that matched null rather than from a parametric interval. The bootstrap CI is still
computed and reported, and it is still too tight. It is not a gate.

MEMORY. The per-draw null sums are accumulated symbol by symbol rather than held as a
full trade matrix. At 5m a single symbol carries ~290,000 bars, and materialising every
draw's picks for 50 of them at once would be gigabytes for no gain.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import polars as pl

from .costs import CostModel
from .engine import BacktestConfig, Trade, precompute_outcomes, run_backtest
from .exits import IntrabarResolver
from .stats import Stats, compute

TOD_BUCKET_MS = 3_600_000


@dataclass
class PooledReport:
    """One detector's result across the whole universe."""

    detector: str
    timeframe: str
    direction: str
    n_symbols: int
    signals: int
    in_sample: Stats
    out_sample: Stats
    gross_in: float | None
    gross_out: float | None
    net_in: float | None
    drag_in: float | None
    p_value: float | None
    null_mean: float | None
    null_low: float | None
    null_high: float | None
    round_trip_bps: float
    stop_atr: float
    target_r: float
    max_bars: int
    #: Set by the shared correction across the whole 42-test family.
    survives_correction: bool | None = None
    bh_threshold: float | None = None
    per_symbol_trades: dict[str, int] = field(default_factory=dict)

    @property
    def beats_random(self) -> bool:
        return self.p_value is not None and self.p_value < 0.05

    @property
    def oos_sign_held(self) -> bool:
        if self.gross_in is None or self.gross_out is None:
            return False
        return (self.gross_in >= 0) == (self.gross_out >= 0)


def _tod_bucket(ms: int | None) -> int:
    return -1 if ms is None else int(ms // TOD_BUCKET_MS)


@dataclass
class SymbolNull:
    """Per-draw gross sums and counts contributed by one symbol.

    Kept as two flat lists rather than a trade matrix so that pooling is an addition
    across symbols and memory stays proportional to the draw count, not to bar count.
    """

    sums: list[float]
    counts: list[int]


def symbol_null(
    df: pl.DataFrame,
    trades: list[Trade],
    *,
    is_long: bool,
    timeframe: str,
    costs: CostModel,
    bt: BacktestConfig,
    resolver: IntrabarResolver | None,
    draws: int,
    seed: int,
) -> SymbolNull | None:
    """This symbol's contribution to each of `draws` pooled null portfolios.

    Reproduces `baseline.run_baseline`'s sampling exactly -- time-of-day matched,
    sampled with replacement, subject to the same one-position-at-a-time and cooldown
    rules the real backtest applies -- but returns the per-draw sums instead of a mean,
    so the caller can pool before dividing. Averaging per symbol first and then averaging
    the averages would weight a symbol with three trades the same as one with three
    hundred.
    """
    if not trades:
        return None

    target_hist = Counter(_tod_bucket(t.tod_ms) for t in trades)
    tod = df["ms_since_open"].to_list() if "ms_since_open" in df.columns else []
    gap = df["gap"].to_list()
    atr = df[bt.atr_column].to_list()

    pool: dict[int, list[int]] = defaultdict(list)
    for i in range(df.height - 1):
        if gap[i + 1]:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        pool[_tod_bucket(tod[i] if tod else None)].append(i)

    outcomes = precompute_outcomes(
        df, is_long=is_long, timeframe=timeframe, costs=costs, bt=bt, resolver=resolver
    )
    if not outcomes:
        return None

    rng = random.Random(seed)
    sums: list[float] = []
    counts: list[int] = []
    for _ in range(draws):
        picks: list[int] = []
        for bucket, count in target_hist.items():
            candidates = pool.get(bucket)
            if not candidates:
                continue
            picks.extend(rng.choice(candidates) for _ in range(count))
        picks.sort()

        total = 0.0
        taken = 0
        busy_until = -1
        last_entry = None
        for i in picks:
            if i <= busy_until:
                continue
            if last_entry is not None and (i + 1) - last_entry < bt.min_bars_between_entries:
                continue
            got = outcomes.get(i)
            if got is None:
                continue
            exit_idx, gross, _net = got
            total += gross
            taken += 1
            busy_until = exit_idx
            last_entry = i + 1
        sums.append(total)
        counts.append(taken)

    return SymbolNull(sums=sums, counts=counts)


def pool_null(parts: list[SymbolNull], draws: int) -> list[float]:
    """Pooled null means: total gross over total trades, per draw, across symbols."""
    if not parts:
        return []
    out: list[float] = []
    for d in range(draws):
        total = sum(p.sums[d] for p in parts)
        n = sum(p.counts[d] for p in parts)
        if n:
            out.append(total / n)
    return out


def split_trades(
    trades: list[Trade], *, in_sample_pct: float
) -> tuple[list[Trade], list[Trade]]:
    """Chronological 70/30 split on SIGNAL TIME, pooled across symbols.

    The boundary is a single instant applied to every symbol, not a per-symbol
    percentile. A per-symbol split would put different calendar periods in the holdout
    for different names, so the out-of-sample set would blend 2019 for one stock with
    2025 for another -- and any regime effect would be smeared across both halves
    instead of being held out.
    """
    if not trades:
        return [], []
    ordered = sorted(trades, key=lambda t: t.signal_ms)
    stamps = [t.signal_ms for t in ordered]
    cut = stamps[min(len(stamps) - 1, int(len(stamps) * in_sample_pct / 100.0))]
    in_s = [t for t in ordered if t.signal_ms < cut]
    out_s = [t for t in ordered if t.signal_ms >= cut]
    return in_s, out_s


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def build_report(
    detector: str,
    *,
    timeframe: str,
    direction: str,
    trades_by_symbol: dict[str, list[Trade]],
    null_parts: list[SymbolNull],
    signals: int,
    bt: BacktestConfig,
    round_trip_bps: float,
    draws: int,
    in_sample_pct: float,
    min_n: int,
    provisional_n: int,
    bootstrap_iterations: int,
) -> PooledReport:
    all_trades = [t for ts in trades_by_symbol.values() for t in ts]
    in_t, out_t = split_trades(all_trades, in_sample_pct=in_sample_pct)

    s_in = compute([t.r_net for t in in_t], bootstrap_iterations=bootstrap_iterations,
                   min_n=min_n, provisional_n=provisional_n)
    s_out = compute([t.r_net for t in out_t], bootstrap_iterations=bootstrap_iterations,
                    min_n=min_n, provisional_n=provisional_n)
    g_in = _mean([t.r_gross for t in in_t])
    g_out = _mean([t.r_gross for t in out_t])
    n_in = _mean([t.r_net for t in in_t])

    p = null_mean = low = high = None
    null = pool_null(null_parts, draws)
    if null and g_in is not None and all_trades:
        # The null is built from the SAME trades the strategy took, so it is compared
        # against the pooled gross over the same set -- in-sample only, matching the
        # gate that consumes it.
        observed = _mean([t.r_gross for t in all_trades])
        null.sort()
        at_least = sum(1 for v in null if v >= observed)
        p = (at_least + 1) / (len(null) + 1)
        null_mean = sum(null) / len(null)
        low = null[int(0.025 * (len(null) - 1))]
        high = null[int(0.975 * (len(null) - 1))]

    return PooledReport(
        detector=detector, timeframe=timeframe, direction=direction,
        n_symbols=len([s for s, ts in trades_by_symbol.items() if ts]),
        signals=signals, in_sample=s_in, out_sample=s_out,
        gross_in=g_in, gross_out=g_out, net_in=n_in,
        drag_in=(n_in - g_in) if (n_in is not None and g_in is not None) else None,
        p_value=p, null_mean=null_mean, null_low=low, null_high=high,
        round_trip_bps=round_trip_bps,
        stop_atr=bt.stop_atr, target_r=bt.target_r, max_bars=bt.max_bars,
        per_symbol_trades={s: len(ts) for s, ts in trades_by_symbol.items() if ts},
    )


def apply_correction(
    p_values: list[tuple[str, float | None]], *, fdr: float = 0.05
) -> dict[str, tuple[bool, float]]:
    """Benjamini-Hochberg across EVERY test in the family, of both kinds.

    Takes (label, p) pairs so that time-series detectors and cross-sectional strategies
    can be corrected together despite being different objects measured in different
    units. That joint correction is the point: two separate corrections at FDR 0.05 do
    not control the false discovery rate over their union, and the union is what gets
    looked at.

    Tests with no p-value -- too few trades to score -- are returned as (False, 0.0) and
    still counted in `m`. Dropping them from the denominator after seeing which ones
    failed would inflate every surviving threshold.
    """
    m = len(p_values)
    if m == 0:
        return {}
    scored = sorted(
        ((label, p) for label, p in p_values if p is not None), key=lambda x: x[1]
    )
    largest_k = 0
    for k, (_label, p) in enumerate(scored, start=1):
        if p <= (k / m) * fdr:
            largest_k = k

    out: dict[str, tuple[bool, float]] = {}
    for k, (label, _p) in enumerate(scored, start=1):
        out[label] = (k <= largest_k, (k / m) * fdr)
    for label, p in p_values:
        if p is None:
            out[label] = (False, 0.0)
    return out
