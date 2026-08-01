"""Trade statistics, with the sample-size gates the brief insists on.

The gates are not advisory. A 68% win rate over 19 trades is noise, and a report that
prints it anyway trains you to believe noise. So:

    n < 30   -> REFUSED. No expectancy is shown at all.
    n < 100  -> PROVISIONAL. Shown, labelled, never quoted as settled.

Every statistic ships with n and a bootstrapped confidence interval. The bootstrap
resamples realised R-multiples, which is only legitimate because the backtest takes one
position at a time -- resampling overlapping trades would treat correlated observations
as independent and produce an interval far tighter than the truth.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

MIN_N = 30
PROVISIONAL_N = 100


@dataclass(frozen=True)
class Stats:
    n: int
    win_rate: float | None
    avg_win_r: float | None
    avg_loss_r: float | None
    expectancy_r: float | None
    profit_factor: float | None
    max_consecutive_losses: int | None
    ci_low: float | None
    ci_high: float | None
    reliability: str  # REFUSED | PROVISIONAL | OK

    @property
    def shown(self) -> bool:
        return self.reliability != "REFUSED"

    @property
    def ci_excludes_zero(self) -> bool:
        return (
            self.ci_low is not None
            and self.ci_high is not None
            and (self.ci_low > 0 or self.ci_high < 0)
        )


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = (len(sorted_vals) - 1) * q
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return sorted_vals[int(k)]
    return sorted_vals[lo] * (hi - k) + sorted_vals[hi] * (k - lo)


def bootstrap_ci(
    values: list[float], *, iterations: int = 10_000, alpha: float = 0.05, seed: int = 0
) -> tuple[float, float]:
    """Percentile bootstrap on the mean. Deterministic given a seed."""
    if not values:
        return float("nan"), float("nan")
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(iterations):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    return _percentile(means, alpha / 2), _percentile(means, 1 - alpha / 2)


def max_consecutive_losses(values: list[float]) -> int:
    worst = run = 0
    for v in values:
        if v < 0:
            run += 1
            worst = max(worst, run)
        else:
            run = 0
    return worst


def compute(
    r_values: list[float],
    *,
    bootstrap_iterations: int = 10_000,
    seed: int = 0,
    min_n: int = MIN_N,
    provisional_n: int = PROVISIONAL_N,
) -> Stats:
    n = len(r_values)
    if n < min_n:
        # Deliberately returns nothing to display. The brief: "Refuse to display any
        # base rate under n=30."
        return Stats(n, None, None, None, None, None, None, None, None, "REFUSED")

    wins = [v for v in r_values if v > 0]
    losses = [v for v in r_values if v <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)

    lo, hi = bootstrap_ci(r_values, iterations=bootstrap_iterations, seed=seed)
    return Stats(
        n=n,
        win_rate=len(wins) / n,
        avg_win_r=(gross_win / len(wins)) if wins else None,
        avg_loss_r=(sum(losses) / len(losses)) if losses else None,
        expectancy_r=sum(r_values) / n,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else None,
        max_consecutive_losses=max_consecutive_losses(r_values),
        ci_low=lo,
        ci_high=hi,
        reliability="PROVISIONAL" if n < provisional_n else "OK",
    )
