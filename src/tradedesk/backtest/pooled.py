"""Pooled validation: one test per detector across a universe of symbols.

WHAT THIS MODULE IS NOW, AND WHAT IT WAS. It used to be a parallel implementation of the
scoring path -- its own sampler, its own splitter, its own Benjamini-Hochberg. That
duplication was the root cause of finding 10: reimplementing machinery that already
existed correctly reintroduced three defects the originals had already solved, including
one that `split.py` carries an explicit comment defending against. Two published
p-values were wrong as a result.

So there is no sampler here any more, and no splitter, and no correction. This module
does exactly one thing the single-series path cannot: it POOLS. Everything else is
called:

    sampling   -> baseline.draw_sums     (the same function run_baseline uses)
    splitting  -> split.Split + partition_trades
    correction -> report.benjamini_hochberg

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
symbol's own time-of-day histogram and its own trade count -- and then pooled into a
single null mean. Summing before dividing is what weights a symbol by its trade count
rather than by existing.

WHAT POOLING COSTS, stated here rather than discovered in the writeup. Part 1's
one-position-at-a-time rule kept trades close enough to independent that a bootstrap
interval meant something. Pooling across 50 names breaks that: positions overlap in time
and equities are cross-sectionally correlated through market beta, so the effective
sample is far smaller than the trade count suggests. The p-value comes from a matched
null rather than a parametric interval, but see PREREGISTRATION.md: the null is
*aggregated* identically and *sampled* independently per symbol, so it does not
reproduce the calendar clustering of real signals and the time-series p-values are
anti-conservative. The bootstrap CI is reported, is optimistic, and is not a gate.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .baseline import DrawSums
from .engine import BacktestConfig, Trade
from .report import benjamini_hochberg
from .split import Split, partition_trades
from .stats import Stats, compute


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


def pool_null(parts: list[DrawSums], draws: int) -> list[float]:
    """Pooled null means: total gross over total trades, per draw, across symbols.

    `parts` come from `baseline.draw_sums` -- the same sampler `run_baseline` uses, not
    a second copy of it. Summing before dividing is what weights a symbol by its trade
    count rather than by existing.
    """
    if not parts:
        return []
    out: list[float] = []
    for d in range(draws):
        total = sum(p.gross[d] for p in parts)
        n = sum(p.taken[d] for p in parts)
        if n:
            out.append(total / n)
    return out


def _mean(vals: list[float]) -> float | None:
    return sum(vals) / len(vals) if vals else None


def build_report(
    detector: str,
    *,
    timeframe: str,
    direction: str,
    trades_by_symbol: dict[str, list[Trade]],
    null_parts: list[DrawSums],
    signals: int,
    bt: BacktestConfig,
    round_trip_bps: float,
    draws: int,
    boundary_ms: int,
    min_n: int,
    provisional_n: int,
    bootstrap_iterations: int,
) -> PooledReport:
    all_trades = [t for ts in trades_by_symbol.values() for t in ts]
    # The project's own splitter, on the project's own Split object -- not a second
    # implementation. `partition_trades` assigns by SIGNAL time, and `assert_no_overlap`
    # fails loudly if the two windows ever share a trade.
    split = Split(boundary_ms=boundary_ms, in_sample_pct=0.0)
    in_t, out_t = partition_trades(all_trades, split)
    split.assert_no_overlap([t.signal_ms for t in in_t], [t.signal_ms for t in out_t])

    s_in = compute([t.r_net for t in in_t], bootstrap_iterations=bootstrap_iterations,
                   min_n=min_n, provisional_n=provisional_n)
    s_out = compute([t.r_net for t in out_t], bootstrap_iterations=bootstrap_iterations,
                    min_n=min_n, provisional_n=provisional_n)
    g_in = _mean([t.r_gross for t in in_t])
    g_out = _mean([t.r_gross for t in out_t])
    n_in = _mean([t.r_net for t in in_t])

    p = null_mean = low = high = None
    null = pool_null(null_parts, draws)
    if null and g_in is not None and in_t:
        # SCORED ON THE IN-SAMPLE TRADES, matching Part 1 and matching the gates that
        # consume it. The previous version compared the null against the mean over ALL
        # trades while every gate read in-sample statistics -- so the significance test
        # and the thing it was gating were computed on different samples, and the
        # holdout leaked into the p-value. `symbol_null` is now fed in-sample trades
        # only, so the null's time-of-day histogram and trade count match too.
        observed = g_in
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

    The arithmetic itself is `report.benjamini_hochberg`, shared with the per-series
    correction so the two cannot disagree about what BH means.

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
    largest_k, thresholds = benjamini_hochberg([p for _, p in scored], m=m, fdr=fdr)

    out: dict[str, tuple[bool, float]] = {
        label: (k <= largest_k, thresholds[k - 1])
        for k, (label, _p) in enumerate(scored, start=1)
    }
    for label, p in p_values:
        if p is None:
            out[label] = (False, 0.0)
    return out
