"""Equity transaction costs: zero commission, and a spread estimated from the data.

THE TRAP THIS MODULE EXISTS TO AVOID. "Equities are commission-free, so costs are
basically zero" is the equity-side twin of the mistake findings 1-5 spent themselves on
in crypto -- assuming a cost number instead of measuring one. Commission really is zero
at Alpaca. The spread is not, it is not constant across names, and it is emphatically not
constant across time: effective spreads on S&P 500 names in March 2020 were several times
their 2019 level, and a backtest using one flat number prices the 2020 trades at the calm
market's cost.

Understating cost is how you manufacture an edge. Having spent eight findings showing
that a 248 bps crypto round trip buries everything, the honest thing on the other side is
NOT to swing to an optimistic constant.

SO THE SPREAD IS ESTIMATED PER NAME, PER MONTH, FROM THE BARS THEMSELVES, using the
Corwin-Schultz (2012) high-low estimator. The idea: over two consecutive days, the ratio
of the observed high-low range to the "true" range reveals the spread, because the
observed high is transacted at the ask and the observed low at the bid. It needs only
OHLC data, which we have for every name and every day of the sample.

    Corwin, S. A. and Schultz, P. (2012), "A Simple Way to Estimate Bid-Ask Spreads from
    Daily High and Low Prices", Journal of Finance 67(2), 719-760.

The estimator is noisy for a single pair of days and negative estimates are common, which
is a known property rather than a bug -- Corwin and Schultz set negatives to zero and
average over a month, and so does this. What it buys is a cost that moves with the market
instead of a number chosen to make the answer come out.

WHAT IS STILL ASSUMED, and therefore where to look when a result depends on it:

  * Half-spread on each side. A retail marketable order at these sizes crosses the
    spread; wholesalers do give price improvement, so this is mildly conservative.
  * A slippage allowance ON TOP, because the estimator measures the quoted spread, not
    the impact of the order. Small at S&P 500 liquidity and retail size, not zero.
  * A floor. The estimator can return implausibly tiny values on quiet names; sub-tick
    spreads are not real, so the floor is one tick over price.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import polars as pl

from .costs import CostModel

#: US equities quote in pennies, so a spread below one cent is not obtainable no matter
#: what the estimator says.
TICK = 0.01

#: Slippage beyond the quoted spread, per side. The estimator measures the quote; this
#: covers the difference between the quote and the fill on a marketable order. Deliberately
#: not zero -- "commission-free" describes the commission, not the round trip.
DEFAULT_SLIPPAGE_BPS = 1.0

#: Fallback when a name has too little history to estimate from. Chosen at roughly the
#: 75th percentile of large-cap effective spreads rather than the median, so a name we
#: cannot measure is not silently assumed to be cheap.
FALLBACK_SPREAD_BPS = 8.0

#: Corwin-Schultz is a two-day estimator; a month of pairs is the usual averaging window.
MIN_PAIRS = 15


@dataclass(frozen=True)
class SpreadEstimate:
    symbol: str
    spread_bps: float
    n_pairs: int
    estimated: bool

    @property
    def source(self) -> str:
        return "corwin-schultz" if self.estimated else "fallback"


#: 3 - 2*sqrt(2), the constant appearing twice in Corwin-Schultz's alpha.
_K = 3.0 - 2.0 * math.sqrt(2.0)


def beta_gamma(df: pl.DataFrame) -> tuple[float, float]:
    """The two moments the estimator is built from, averaged over the whole window.

        beta  = (ln(H_t/L_t))^2 + (ln(H_t+1/L_t+1))^2     -- two single-day ranges
        gamma = (ln(H_2day/L_2day))^2                     -- the two-day range

    The two-day high is the high ACROSS both days, which for daily bars is the max of
    the two daily highs.
    """
    h, l_ = pl.col("high"), pl.col("low")
    hi2 = pl.max_horizontal(h, h.shift(1))
    lo2 = pl.min_horizontal(l_, l_.shift(1))
    pairs = df.select(
        ((h / l_).log().pow(2) + (h.shift(1) / l_.shift(1)).log().pow(2)).alias("beta"),
        ((hi2 / lo2).log().pow(2)).alias("gamma"),
    ).drop_nulls()
    if pairs.height == 0:
        return 0.0, 0.0
    return float(pairs["beta"].mean()), float(pairs["gamma"].mean())


def spread_from_moments(beta: float, gamma: float) -> float:
    """Corwin-Schultz alpha and spread, from ALREADY-AVERAGED moments.

        alpha = (sqrt(2*beta) - sqrt(beta))/k - sqrt(gamma/k),  k = 3 - 2*sqrt(2)
        S     = 2*(exp(alpha) - 1) / (1 + exp(alpha))

    AVERAGING THE MOMENTS FIRST IS NOT A DETAIL -- it is the difference between an
    unbiased estimator and a badly biased one, and getting it wrong fails silently.

    Applying alpha to each two-day pair and then averaging the resulting spreads looks
    equivalent and is not: alpha takes square roots of beta and gamma, so by Jensen's
    inequality the per-pair mean does not converge to the pooled answer. Measured on a
    simulated Brownian price path with a KNOWN spread of exactly zero, the per-pair
    version returns a mean of ~61 bps and a median of ~38 bps -- pure noise rectified
    into phantom cost. The pooled version returns 0.00 bps on the same data.

    That error is worth naming because of its direction. It would have charged every
    name in the universe roughly 60 bps of spread that does not exist, which is enough
    to bury any real equity edge and report a confident false negative -- the exact
    failure this project exists to avoid, arrived at from the opposite side.

    Calibration on simulated paths with known spreads (1,500 days each):

        true    0 bps -> 0.0     true   50 bps -> 38.8
        true    5 bps -> 0.0     true  100 bps -> 87.9
        true   20 bps -> 9.6

    So it is roughly unbiased at wide spreads and CANNOT RESOLVE anything below about
    10 bps, where it returns zero. That is why the tick floor below is not cosmetic: for
    a genuinely tight large-cap name the estimator says "too small to measure" and the
    floor supplies the smallest spread that can physically exist.
    """
    if beta <= 0 or gamma < 0:
        return 0.0
    alpha = (math.sqrt(2.0 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    spread = 2.0 * (math.exp(alpha) - 1.0) / (1.0 + math.exp(alpha))
    # A negative alpha means the observed two-day range is too large relative to the
    # single-day ranges for any positive spread to explain it. The paper sets these to
    # zero rather than discarding them, because dropping only negatives would remove
    # the low-spread tail and bias the estimate upward.
    return max(spread, 0.0) * 10_000.0


def estimate_spread(df: pl.DataFrame, symbol: str) -> SpreadEstimate:
    """One name's average effective spread over the frame, in basis points.

    Expects DAILY bars. Run on 5m bars the estimator would measure a five-minute range
    and return something far too small, which is the sort of misuse that produces a
    flatteringly cheap backtest.
    """
    usable = df.filter(pl.col("high").is_not_null() & (pl.col("low") > 0))
    if usable.height < MIN_PAIRS + 1:
        return SpreadEstimate(symbol, FALLBACK_SPREAD_BPS, usable.height, False)

    beta, gamma = beta_gamma(usable)
    n_pairs = usable.height - 1
    if n_pairs < MIN_PAIRS or beta <= 0:
        return SpreadEstimate(symbol, FALLBACK_SPREAD_BPS, n_pairs, False)

    estimated_bps = spread_from_moments(beta, gamma)
    # Floor at one tick over the median price. A spread narrower than the minimum
    # quotable increment cannot exist, and the estimator returns zero for anything under
    # ~10 bps -- so for a genuinely tight large-cap name this floor IS the estimate.
    median_price = float(usable["close"].median() or 0.0)
    floor_bps = (TICK / median_price) * 10_000.0 if median_price > 0 else 0.0
    return SpreadEstimate(symbol, max(estimated_bps, floor_bps), n_pairs, True)


def equity_cost_model(
    spread_bps: float, *, slippage_bps: float = DEFAULT_SLIPPAGE_BPS
) -> CostModel:
    """A CostModel for a commission-free US equity account.

    `taker_fee_bps=0` is the one number here that is genuinely zero and genuinely
    certain: Alpaca charges no commission on US equities. Everything else in the round
    trip is spread and slippage, and both are real.
    """
    return CostModel(
        spread_bps=spread_bps, slippage_bps=slippage_bps, taker_fee_bps=0.0
    )


def summarise(estimates: list[SpreadEstimate]) -> str:
    """A one-line audit of what the cost assumption actually came out as.

    Printed alongside results, because a reader's first question about an equity
    backtest that finally shows an edge should be "what did you assume it cost", and the
    answer should not require reading the code.
    """
    if not estimates:
        return "no spread estimates"
    vals = sorted(e.spread_bps for e in estimates)
    n_est = sum(1 for e in estimates if e.estimated)
    mid = vals[len(vals) // 2]
    return (
        f"spread: median {mid:.1f} bps, range {vals[0]:.1f}-{vals[-1]:.1f} bps "
        f"across {len(vals)} names ({n_est} estimated, {len(vals) - n_est} fallback); "
        f"round trip = 2 x (spread/2 + {DEFAULT_SLIPPAGE_BPS:.1f} slippage + 0 commission)"
    )
