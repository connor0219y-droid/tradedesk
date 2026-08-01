"""Tests that belong to later phases.

These are `skip`, not `xfail`, and deliberately not stubbed into passing. A test that
asserts nothing but reports green is worse than a missing test, because it tells you
a guarantee exists when it does not.

Each one names the phase that will make it real and what it must assert then.
"""

from __future__ import annotations

import pytest


@pytest.mark.skip(reason="Phase 3: pattern detection does not exist yet")
def test_known_answer_bullish_engulfing():
    """Hand-built 20-bar fixture with exactly one unambiguous bullish engulfing.

    Must assert the detector finds that bar and no other -- a detector that fires on
    two bars in a fixture containing one pattern is not measuring what it claims.
    """


@pytest.mark.skip(reason="Phase 3: the backtester does not exist yet")
def test_cost_sanity_zero_edge_goes_negative_net():
    """A strategy with zero gross edge must show negative net expectancy once
    spread, slippage and fees are applied on both sides."""


@pytest.mark.skip(reason="Phase 3: the backtester does not exist yet")
def test_random_baseline_is_unbiased():
    """Random entries with a symmetric stop and target must produce expectancy
    within ~0.05R of zero before costs. If they do not, the engine is biased and
    every pattern result it produces is meaningless."""


@pytest.mark.skip(reason="Phase 3: the backtester does not exist yet")
def test_intrabar_stop_fills_before_target():
    """When a bar's range contains both stop and target, the stop fills.

    With 1m bars stored alongside the signal timeframe, this becomes a measurement
    rather than an assumption: replay the 1m sequence inside the bar and resolve
    which level was actually touched first.
    """


@pytest.mark.skip(reason="Phase 5: rule fitting does not exist yet")
def test_out_of_sample_windows_never_overlap():
    """No rule's base rate may be computed on bars used to fit or tune that rule.

    Must fail the build if the fit window and the reporting window overlap by a
    single bar.
    """
