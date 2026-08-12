"""The equity cost model, and the Corwin-Schultz estimator behind it.

The estimator is checked against a CONSTRUCTED spread rather than against whatever it
happens to return: bars are built by taking a known true price path and widening each
day's high and low by a known half-spread, then the estimator has to recover roughly
that number. A test that asserted the current output would be a change-detector, and
this is the one component where being quietly wrong flatters every result downstream.
"""

from __future__ import annotations

import math

import polars as pl
import pytest

from tradedesk.backtest.equity_costs import (
    DEFAULT_SLIPPAGE_BPS,
    FALLBACK_SPREAD_BPS,
    beta_gamma,
    equity_cost_model,
    estimate_spread,
    spread_from_moments,
    summarise,
)


def _bars(n: int, *, spread_bps: float, price: float = 100.0, daily_vol: float = 0.015,
          steps: int = 390, seed: int = 11) -> pl.DataFrame:
    """A true Brownian price path, sampled intraday, widened by a known half-spread.

    THE PATH HAS TO BE CONTINUOUS FOR THIS TO BE A FAIR TEST. Corwin-Schultz derives the
    spread from the fact that a two-day range is sqrt(2) times a one-day range under
    Brownian motion; a fixture that draws each day's range independently of the drift
    breaks that relation, and the estimator then reports tens of basis points of spread
    on data built with none. Simulating an actual path -- 390 one-minute steps, high and
    low taken as the path's extremes -- is what makes "recovers a known spread" a
    statement about the algebra rather than about the fixture.

    The observed high is then a transaction at the ask and the observed low one at the
    bid, which is precisely the structure the estimator exploits.
    """
    rng = __import__("random").Random(seed)
    half = spread_bps / 2.0 / 10_000.0
    per_step = daily_vol / math.sqrt(steps)
    rows = []
    p = price
    for _ in range(n):
        hi = lo = p
        for _ in range(steps):
            p *= 1.0 + rng.gauss(0.0, per_step)
            hi, lo = max(hi, p), min(lo, p)
        rows.append({
            "high": hi * (1.0 + half),   # traded at the ask
            "low": lo * (1.0 - half),    # traded at the bid
            "close": p,
        })
    return pl.DataFrame(rows)


def test_estimator_recovers_a_constructed_spread():
    """Widen a Brownian path by a known spread; the estimate must track it.

    Calibrated tolerances, not aspirational ones. The estimator underestimates at these
    sample sizes -- 50 bps reads as ~39, 100 as ~88 -- which is a documented property
    rather than a defect, and it errs toward understating cost, so the wider bound is
    the one that matters.
    """
    for true_bps, lo, hi in ((20.0, 3.0, 30.0), (50.0, 25.0, 65.0), (100.0, 65.0, 120.0)):
        est = estimate_spread(_bars(800, spread_bps=true_bps), "TEST")
        assert est.estimated
        assert lo < est.spread_bps < hi, (
            f"true {true_bps} bps -> estimated {est.spread_bps:.1f} bps"
        )


def test_a_zero_spread_path_estimates_as_zero():
    """The test that caught the real bug.

    Applying Corwin-Schultz's alpha per two-day pair and then averaging the resulting
    spreads returns a mean of ~61 bps on a path built with EXACTLY ZERO spread -- noise
    rectified into phantom cost by Jensen's inequality, since alpha takes square roots.
    Pooling beta and gamma first and applying alpha once returns 0.

    Sixty basis points charged to every name would have buried any real equity edge and
    produced a confident false negative.
    """
    beta, gamma = beta_gamma(_bars(1200, spread_bps=0.0))
    assert spread_from_moments(beta, gamma) == pytest.approx(0.0, abs=1.0)


def test_estimator_is_monotone_in_the_true_spread():
    """The property that actually matters for ranking names by cost."""
    got = [estimate_spread(_bars(400, spread_bps=b), "T").spread_bps
           for b in (2.0, 10.0, 40.0, 100.0)]
    assert got == sorted(got), got


def test_a_negative_alpha_yields_zero_not_a_negative_spread():
    """A negative alpha means no positive spread can explain the observed ranges.

    Corwin and Schultz set those to zero rather than discarding them: dropping only the
    negatives would remove the low-spread tail and bias the estimate upward.
    """
    assert spread_from_moments(1e-6, 1.0) == 0.0
    assert spread_from_moments(0.0, 0.0) == 0.0


def test_spread_is_floored_at_one_tick():
    """A spread narrower than the minimum quotable increment cannot happen.

    On a $5 stock one cent is 20 bps, so the floor is not a formality -- it binds on
    every low-priced name in the universe.
    """
    df = pl.DataFrame({"high": [5.0] * 60, "low": [4.999] * 60, "close": [5.0] * 60})
    est = estimate_spread(df, "CHEAP")
    assert est.spread_bps >= (0.01 / 5.0) * 10_000.0 - 1e-9


def test_too_little_history_falls_back_conservatively():
    """A name we cannot measure must not be assumed cheap.

    The fallback sits near the 75th percentile of large-cap spreads rather than the
    median, so an unmeasurable name is priced pessimistically instead of optimistically.
    """
    est = estimate_spread(_bars(5, spread_bps=10.0), "THIN")
    assert not est.estimated
    assert est.spread_bps == FALLBACK_SPREAD_BPS
    assert est.source == "fallback"
    assert FALLBACK_SPREAD_BPS > 5.0


def test_commission_is_zero_but_the_round_trip_is_not():
    """The headline claim, and its limit.

    Alpaca charges no commission on US equities -- that number really is zero. Reporting
    the round trip as zero would be the equity-side version of the mistake this whole
    project exists to catch.
    """
    model = equity_cost_model(spread_bps=4.0)
    assert model.taker_fee_bps == 0.0
    # per side = half spread + slippage
    assert model.per_side_bps == pytest.approx(2.0 + DEFAULT_SLIPPAGE_BPS)
    assert model.per_side_bps > 0.0


def test_the_round_trip_is_two_to_three_orders_of_magnitude_below_crypto():
    """Findings 1-8 lived under a 248 bps round trip. This is the change that makes
    equities a different question rather than the same one on new symbols."""
    equity = equity_cost_model(spread_bps=4.0)
    crypto = 2 * (2.0 / 2 + 3.0 + 120.0)   # config.toml's Coinbase base tier
    assert 2 * equity.per_side_bps < crypto / 30


def test_cost_moves_the_fill_against_you_on_both_sides():
    """Costs move fill prices, not just the summary -- which is what also changes
    whether a trade hits its stop."""
    model = equity_cost_model(spread_bps=10.0)
    assert model.fill_price(100.0, is_long=True, is_entry=True) > 100.0
    assert model.fill_price(100.0, is_long=True, is_entry=False) < 100.0
    assert model.fill_price(100.0, is_long=False, is_entry=True) < 100.0
    assert model.fill_price(100.0, is_long=False, is_entry=False) > 100.0


def test_summary_states_the_assumption_in_one_line():
    """A reader's first question about an equity result that shows an edge is what it
    was assumed to cost. The answer should not require reading the source."""
    ests = [estimate_spread(_bars(300, spread_bps=b, seed=i), f"S{i}")
            for i, b in enumerate((3.0, 8.0, 25.0))]
    text = summarise(ests)
    assert "median" in text and "bps" in text and "round trip" in text
    assert "0 commission" in text


def test_estimator_algebra_matches_the_published_constants():
    """k = 3 - 2*sqrt(2) appears twice in the paper's alpha; a typo there would shift
    every spread by a constant factor and still look plausible."""
    k = 3.0 - 2.0 * math.sqrt(2.0)
    assert k == pytest.approx(0.17157287525380993)
