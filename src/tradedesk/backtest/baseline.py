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


@dataclass(frozen=True)
class DrawSums:
    """Per-draw totals from one series: gross sum, net sum, and trades taken.

    Sums and counts rather than means, because a POOLED null has to add across symbols
    before it divides. Averaging each symbol first and then averaging the averages would
    give a name with three trades the same weight as one with three hundred.

    `run_baseline` divides these itself and gets exactly the number it always did.
    """

    gross: list[float]
    net: list[float]
    taken: list[int]


def draw_sums(
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
) -> DrawSums | None:
    """THE sampling primitive. One implementation, two callers.

    Draws time-of-day-matched random entries under exactly the constraints the real
    backtest applies -- same ATR and gap eligibility, same one-position-at-a-time rule,
    same entry cooldown -- and reports what each draw earned.

    This exists as its own function because it was previously written twice. The pooled
    equity scorer reimplemented it and, in doing so, reintroduced three defects the
    original had already solved: a seed that was not reproducible, a holdout derived
    from trade density, and a null matched to a different sample than the one being
    scored (finding 10). A second implementation of a sampler is a second place for the
    sampling rules to drift.
    """
    if not observed_trades:
        return None

    target_hist = Counter(_tod_bucket(t.tod_ms) for t in observed_trades)
    tod = df["ms_since_open"].to_list() if "ms_since_open" in df.columns else []
    gap = df["gap"].to_list()
    atr = df[bt.atr_column].to_list()

    # Candidate entry bars, grouped by the same time-of-day bucket. A bar is eligible
    # only if it would actually have been tradable -- same ATR and gap requirements the
    # real engine applies -- otherwise the baseline gets easier bars than the pattern.
    pool: dict[int, list[int]] = defaultdict(list)
    for i in range(df.height - 1):
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
    if not outcomes:
        return None

    rng = random.Random(seed)
    g_sums: list[float] = []
    n_sums: list[float] = []
    counts: list[int] = []
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
            g_sums.append(0.0)
            n_sums.append(0.0)
            counts.append(0)
            continue
        picks.sort()

        # Same one-position-at-a-time rule AND the same entry cooldown the real backtest
        # applies, so the baseline is subject to identical constraints rather than
        # getting more trades. A cooldown enforced on the pattern but not on the null
        # would compare a spaced-out rule against an unspaced one.
        g_sum = n_sum = 0.0
        taken = 0
        busy_until = -1
        last_entry = None
        for i in picks:
            if i <= busy_until:
                continue
            if last_entry is not None and (i + 1) - last_entry < bt.min_bars_between_entries:
                continue
            o = outcomes.get(i)
            if o is None:
                continue
            exit_idx, g, nr = o
            g_sum += g
            n_sum += nr
            taken += 1
            busy_until = exit_idx
            last_entry = i + 1
        g_sums.append(g_sum)
        n_sums.append(n_sum)
        counts.append(taken)

    return DrawSums(gross=g_sums, net=n_sums, taken=counts)


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
    sums = draw_sums(
        df, observed_trades, is_long=is_long, timeframe=timeframe, costs=costs,
        bt=bt, resolver=resolver, draws=draws, seed=seed,
    )
    if sums is None:
        return None

    # The p-value is computed on GROSS. Pattern and random pay identical costs, so a
    # net-vs-net comparison gives the same answer -- but gross is what the question
    # "does this pattern have any edge" actually asks, and it stays legible when the
    # cost drag is an order of magnitude larger than any edge.
    observed = sum(t.r_gross for t in observed_trades) / len(observed_trades)
    null = [g / n for g, n in zip(sums.gross, sums.taken) if n]
    null_net = [x / n for x, n in zip(sums.net, sums.taken) if n]

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
