"""The random baseline: the thing that actually decides whether a pattern survives.

The brief specifies "entries at random bars in the same sessions, same stop/target/costs,
same n", then compares confidence intervals. This does two things beyond that, both
cheap and both material:

MATCHED ON TIME OF DAY. A pattern that concentrates in the first 90 minutes, compared
against randomness spread across the whole day, is being compared against a different
volatility regime. The comparison would flatter it for reasons that have nothing to do
with the pattern. So the random entries are drawn to reproduce the pattern's own
time-of-day histogram.

MONTE CARLO, NOT A SINGLE DRAW. One random sample of n entries is itself a noisy
estimate. Drawing many builds a proper null distribution, which yields a real p-value
instead of an eyeballed CI overlap: "how often does random trading in the same hours
beat what this pattern did?"

If the answer is "often", the pattern has no demonstrated edge, and the report says so.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict
from dataclasses import dataclass

import polars as pl

from .costs import CostModel
from .engine import BacktestConfig, Trade, precompute_outcomes, run_backtest
from .exits import IntrabarResolver

TOD_BUCKET_MS = 3_600_000  # one-hour buckets for matching


@dataclass(frozen=True)
class BaselineResult:
    draws: int
    n_per_draw: int
    mean_expectancy: float       # GROSS mean of the null distribution
    mean_expectancy_net: float
    p_value: float               # computed on GROSS
    null_low: float
    null_high: float
    observed: float              # observed GROSS expectancy

    @property
    def beats_random(self) -> bool:
        return self.p_value < 0.05


def _tod_bucket(ms: int | None) -> int:
    return -1 if ms is None else int(ms // TOD_BUCKET_MS)


def run_baseline(
    df: pl.DataFrame,
    observed_trades: list[Trade],
    *,
    is_long: bool,
    timeframe: str,
    costs: CostModel,
    bt: BacktestConfig,
    resolver: IntrabarResolver | None = None,
    draws: int = 1000,
    seed: int = 0,
) -> BaselineResult | None:
    """Null distribution of expectancy from time-of-day-matched random entries."""
    if not observed_trades:
        return None

    # The p-value is computed on GROSS. Pattern and random pay identical costs, so a
    # net-vs-net comparison gives the same answer -- but gross is what the question
    # "does this pattern have any edge" actually asks, and it stays legible when the
    # cost drag is an order of magnitude larger than any edge.
    observed = sum(t.r_gross for t in observed_trades) / len(observed_trades)
    target_hist = Counter(_tod_bucket(t.tod_ms) for t in observed_trades)

    # Candidate entry bars, grouped by the same time-of-day bucket. A bar is eligible
    # only if it would actually have been tradable -- same ATR and gap requirements the
    # real engine applies -- otherwise the baseline gets easier bars than the pattern.
    tod = df["ms_since_open"].to_list() if "ms_since_open" in df.columns else []
    gap = df["gap"].to_list()
    atr = df[bt.atr_column].to_list()
    n_bars = df.height

    pool: dict[int, list[int]] = defaultdict(list)
    for i in range(n_bars - 1):
        if gap[i + 1]:
            continue
        a = atr[i]
        if a is None or a <= 0:
            continue
        pool[_tod_bucket(tod[i] if tod else None)].append(i)

    # Precompute the outcome of entering at every eligible bar ONCE, then each draw is
    # just sampling from that table. Re-running the full simulation per draw made 1,000
    # draws cost ~50 minutes per series; this makes it seconds, which is the difference
    # between a defensible null distribution and a token one.
    outcomes = precompute_outcomes(
        df, is_long=is_long, timeframe=timeframe, costs=costs, bt=bt, resolver=resolver
    )

    rng = random.Random(seed)
    null: list[float] = []
    null_net: list[float] = []
    for _ in range(draws):
        picks: list[int] = []
        for bucket, count in target_hist.items():
            candidates = pool.get(bucket)
            if not candidates:
                continue
            # Sampled with replacement: sampling without replacement would exhaust thin
            # buckets and quietly shrink the draw.
            picks.extend(rng.choice(candidates) for _ in range(count))
        if not picks:
            continue
        picks.sort()

        # Same one-position-at-a-time rule the real backtest applies, so the baseline
        # is subject to the identical constraint rather than getting more trades.
        g_sum = n_sum = 0.0
        taken = 0
        busy_until = -1
        for i in picks:
            if i <= busy_until:
                continue
            o = outcomes.get(i)
            if o is None:
                continue
            exit_idx, g, nr = o
            g_sum += g
            n_sum += nr
            taken += 1
            busy_until = exit_idx
        if taken:
            null.append(g_sum / taken)
            null_net.append(n_sum / taken)

    if not null:
        return None

    mean_net = sum(null_net) / len(null_net)
    null.sort()
    # One-sided: how often does random do at least as well as the pattern?
    at_least = sum(1 for v in null if v >= observed)
    p = (at_least + 1) / (len(null) + 1)  # add-one keeps p strictly positive
    lo = null[int(0.025 * (len(null) - 1))]
    hi = null[int(0.975 * (len(null) - 1))]
    return BaselineResult(
        draws=len(null),
        n_per_draw=len(observed_trades),
        mean_expectancy=sum(null) / len(null),
        mean_expectancy_net=mean_net,
        p_value=p,
        null_low=lo,
        null_high=hi,
        observed=observed,
    )
