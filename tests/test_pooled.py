"""Pooled validation: the correction, the split, and the weighting.

The load-bearing property is that pooling must not quietly change what is being
measured. A symbol with three trades should not count as much as one with three hundred,
the holdout must be one calendar boundary rather than fifty, and the correction must be
applied over every test that was run rather than over the ones that survived.
"""

from __future__ import annotations

import pytest

from tradedesk.backtest.baseline import DrawSums
from tradedesk.backtest.pooled import apply_correction, pool_null
from tradedesk.backtest.split import Split, partition_trades


class _T:
    """A stand-in trade; only the fields the pooling logic reads."""

    def __init__(self, signal_ms, r_gross=0.0, r_net=0.0):
        self.signal_ms = signal_ms
        self.r_gross = r_gross
        self.r_net = r_net


def test_pooled_null_weights_by_trade_count_not_by_symbol():
    """Total gross over total trades, never the mean of per-symbol means.

    A symbol contributing 3 trades and one contributing 300 must not carry equal weight
    -- averaging the averages would let a thin, noisy name swing the null as much as the
    whole rest of the universe.
    """
    big = DrawSums(gross=[300.0], net=[300.0], taken=[300])    # mean 1.0
    small = DrawSums(gross=[30.0], net=[30.0], taken=[3])     # mean 10.0
    pooled = pool_null([big, small], draws=1)
    assert pooled[0] == pytest.approx((300.0 + 30.0) / 303)
    # The mean-of-means answer would be 5.5; that is the bug this guards.
    assert pooled[0] < 2.0


def test_pooled_null_skips_draws_where_nothing_traded():
    """A draw in which no symbol produced a fill contributes no observation rather
    than a zero -- a zero would be a real portfolio that broke even."""
    empty = DrawSums(gross=[0.0, 5.0], net=[0.0, 5.0], taken=[0, 5])
    pooled = pool_null([empty], draws=2)
    assert pooled == [1.0]


def test_the_boundary_is_calendar_time_not_trade_density():
    """The bug this replaces: the cut was the signal time of the 70th-percentile TRADE.

    That is one instant, so symbols did share it -- but WHICH instant was decided by
    trade density. A detector firing often on three busy names let those names drag the
    cut, so two detectors in the same family were held out against different spans of
    market history. Deriving it from the calendar makes the holdout the same period for
    everything.

    Here 90 of 100 trades are crammed into the first tenth of the window. A
    trade-percentile split would cut inside that cluster; a calendar split must not.
    """
    span_lo, span_hi = 0, 1_000_000
    boundary = Split.from_window(span_lo, span_hi, in_sample_pct=70).boundary_ms
    assert boundary == 700_000, "boundary must be 70% of the calendar span"

    clustered = [_T(i * 100) for i in range(90)] + [_T(900_000 + i * 100) for i in range(10)]
    in_s, out_s = partition_trades(clustered, Split(boundary_ms=boundary, in_sample_pct=70))
    assert len(in_s) == 90 and len(out_s) == 10
    assert max(t.signal_ms for t in in_s) < boundary <= min(t.signal_ms for t in out_s)


def test_the_boundary_does_not_move_with_the_detector():
    """Two detectors with wildly different trade counts must share one holdout."""
    boundary = Split.from_window(0, 1_000_000, in_sample_pct=70).boundary_ms
    busy = [_T(i * 1000) for i in range(1000)]
    sparse = [_T(i * 100_000) for i in range(10)]
    b_in, b_out = partition_trades(busy, Split(boundary_ms=boundary, in_sample_pct=70))
    s_in, s_out = partition_trades(sparse, Split(boundary_ms=boundary, in_sample_pct=70))
    assert all(t.signal_ms < boundary for t in b_in + s_in)
    assert all(t.signal_ms >= boundary for t in b_out + s_out)


def test_split_is_chronological_regardless_of_input_order():
    boundary = Split.from_window(0, 1000, in_sample_pct=50).boundary_ms
    trades = [_T(500), _T(100), _T(900), _T(300)]
    in_s, out_s = partition_trades(trades, Split(boundary_ms=boundary, in_sample_pct=50))
    assert [t.signal_ms for t in in_s] == [100, 300]
    assert [t.signal_ms for t in out_s] == [500, 900]


def test_empty_input_is_handled():
    assert partition_trades([], Split(boundary_ms=500, in_sample_pct=70)) == ([], [])
    assert pool_null([], draws=10) == []


# ------------------------------------------------------------------ the correction


def test_correction_spans_both_families_as_one():
    """The instruction the pre-registration commits to: 42 tests, one correction.

    Two separate corrections at FDR 0.05 do not control the false discovery rate over
    their union, and the union is what gets looked at.
    """
    ts = [(f"ts_{i}", 0.4) for i in range(36)]
    xs = [(f"xs_{i}", 0.001) for i in range(6)]
    got = apply_correction(ts + xs)
    assert len(got) == 42
    # 0.001 <= (k/42)*0.05 holds for k up to 42*0.001/0.05 = 0.84 -> k=0, so none pass.
    # Raise one clearly below 0.05/42 and it must survive.
    got2 = apply_correction(ts + [("xs_0", 0.0002)] + xs[1:])
    assert got2["xs_0"][0] is True
    assert all(not got2[f"ts_{i}"][0] for i in range(36))


def test_unscored_tests_stay_in_the_denominator():
    """A detector that produced too few trades to score still counts in m.

    Dropping empty tests after seeing which ones failed inflates every surviving
    threshold -- the denominator would then have been chosen by the data.
    """
    with_none = [("a", 0.001)] + [(f"z{i}", None) for i in range(39)] + [("b", 0.5), ("c", 0.6)]
    got = apply_correction(with_none)
    assert len(got) == 42
    assert got["z0"] == (False, 0.0)
    # m is 42, so the rank-1 threshold is 0.05/42; 0.001 clears it.
    assert got["a"][1] == pytest.approx(0.05 / 42)
    assert got["a"][0] is True


def test_a_lone_significant_test_needs_to_clear_the_full_family_threshold():
    """With m = 42 the rank-1 bar is 0.00119, not 0.05."""
    family = [("winner", 0.01)] + [(f"x{i}", 0.9) for i in range(41)]
    got = apply_correction(family)
    assert got["winner"][0] is False, "0.01 must not survive a 42-test correction"
    family = [("winner", 0.0005)] + [(f"x{i}", 0.9) for i in range(41)]
    assert apply_correction(family)["winner"][0] is True


def test_correction_on_an_empty_family_is_empty():
    assert apply_correction([]) == {}
