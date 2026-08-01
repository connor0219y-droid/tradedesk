"""Backtest engine: the tests the brief names, plus the traps the design created.

Two of these are load-bearing in a way the others are not:

  test_random_baseline_is_unbiased  -- if random entries with a symmetric stop and
      target do NOT produce ~zero expectancy, the engine has a systematic bias and
      every pattern result it has ever produced is meaningless.
  test_cost_sanity                  -- zero gross edge must go negative once costs are
      applied, or costs are not really being charged.
"""

from __future__ import annotations

import math
import random
from datetime import date, timedelta

import polars as pl
import pytest

from tradedesk.backtest import (
    BacktestConfig,
    CostModel,
    IntrabarResolver,
    LeakageError,
    make_split,
    partition_trades,
    run_backtest,
)
from tradedesk.backtest.report import PatternReport, apply_multiple_testing_correction
from tradedesk.backtest.stats import compute

STEP = 300_000
BASE = 1_700_000_000_000 // STEP * STEP
NO_COST = CostModel(spread_bps=0.0, slippage_bps=0.0, taker_fee_bps=0.0)


def random_walk_frame(n: int = 20_000, *, seed: int = 7, atr: float = 10.0):
    """A driftless random walk with self-consistent OHLC.

    Built from a fine-grained path so high/low really bracket open/close and the
    intrabar sequence is realistic -- which matters, because the exit logic reads
    highs and lows to decide what was touched.
    """
    rng = random.Random(seed)
    price = 1000.0
    rows = []
    sub = 12
    for i in range(n):
        path = []
        for _ in range(sub):
            price += rng.gauss(0.0, atr / math.sqrt(sub) / 1.5)
            path.append(price)
        o, c = path[0], path[-1]
        rows.append({
            "bar_open_ms": BASE + i * STEP,
            "session_date": date(2025, 6, 1) + timedelta(days=(i * STEP) // 86_400_000),
            "open": o, "high": max(path), "low": min(path), "close": c,
            "volume": 10.0, "gap": i == 0,
            "atr_intraday": atr, "ms_since_open": (i * STEP) % 86_400_000,
            "rvol_tod": 1.0, "eff_ratio": 0.1, "atr_pct_60d": 50.0,
        })
    return pl.DataFrame(rows)


def _every_nth_signal(n_bars: int, step: int = 37) -> pl.Series:
    return pl.Series("s", [i % step == 0 for i in range(n_bars)])


# ------------------------------------------------------- the brief's named tests


def test_random_baseline_is_unbiased():
    """Random entries, symmetric stop and target, no costs -> expectancy ~0.

    The brief: "must produce expectancy within ~0.05R of zero before costs. If they
    don't, the backtest engine is biased." This is the single test that licenses every
    other number the engine produces.
    """
    df = random_walk_frame(30_000)
    res = run_backtest(
        df, _every_nth_signal(df.height), is_long=True, timeframe="5m",
        costs=NO_COST, bt=BacktestConfig(stop_atr=1.0, target_r=1.0, max_bars=200),
    )
    assert res.n > 500, f"too few trades to judge bias: {res.n}"
    exp = sum(t.r_gross for t in res.trades) / res.n
    assert abs(exp) < 0.05, f"engine is biased: symmetric random entries gave {exp:+.4f}R"


def test_random_baseline_unbiased_for_shorts_too():
    """A bias that affects only one direction would be invisible in the long-only test."""
    df = random_walk_frame(30_000, seed=11)
    res = run_backtest(
        df, _every_nth_signal(df.height), is_long=False, timeframe="5m",
        costs=NO_COST, bt=BacktestConfig(stop_atr=1.0, target_r=1.0, max_bars=200),
    )
    exp = sum(t.r_gross for t in res.trades) / res.n
    assert abs(exp) < 0.05, f"short side is biased: {exp:+.4f}R"


def test_cost_sanity_zero_edge_goes_negative():
    """The brief: a strategy with zero gross edge must be negative once costs apply."""
    df = random_walk_frame(20_000)
    sig = _every_nth_signal(df.height)
    bt = BacktestConfig(stop_atr=1.0, target_r=1.0, max_bars=200)

    free = run_backtest(df, sig, is_long=True, timeframe="5m", costs=NO_COST, bt=bt)
    gross = sum(t.r_net for t in free.trades) / free.n
    assert abs(gross) < 0.05

    costed = run_backtest(
        df, sig, is_long=True, timeframe="5m",
        costs=CostModel(spread_bps=2.0, slippage_bps=3.0, taker_fee_bps=120.0), bt=bt,
    )
    net = sum(t.r_net for t in costed.trades) / costed.n
    assert net < 0, f"costs did not make a zero-edge strategy negative: {net:+.4f}R"
    assert net < gross - 0.1, "cost drag is implausibly small"


def test_out_of_sample_windows_never_overlap():
    """The brief: fail the build if fit and reporting windows share a single bar."""
    df = random_walk_frame(5_000)
    res = run_backtest(
        df, _every_nth_signal(df.height), is_long=True, timeframe="5m",
        costs=NO_COST, bt=BacktestConfig(),
    )
    split = make_split(df, in_sample_pct=70.0)
    in_t, out_t = partition_trades(res.trades, split)
    assert in_t and out_t

    in_ms = [t.signal_ms for t in in_t]
    out_ms = [t.signal_ms for t in out_t]
    assert not (set(in_ms) & set(out_ms))
    split.assert_no_overlap(in_ms, out_ms)   # must not raise

    # And the guard must actually fire when the windows really do overlap.
    with pytest.raises(LeakageError):
        split.assert_no_overlap(in_ms + [out_ms[0]], out_ms)


# --------------------------------------------------- traps the design introduced


def test_entry_is_the_next_bar_open_never_the_signal_close():
    df = random_walk_frame(500)
    sig = pl.Series("s", [i == 100 for i in range(df.height)])
    res = run_backtest(df, sig, is_long=True, timeframe="5m", costs=NO_COST,
                       bt=BacktestConfig())
    assert res.n == 1
    t = res.trades[0]
    assert t.signal_index == 100
    assert t.entry_index == 101
    assert t.entry_price == pytest.approx(df["open"][101])
    assert t.entry_price != pytest.approx(df["close"][100])


def test_signal_before_a_gap_is_skipped_not_filled_stale():
    """Entry at the next bar's open is wrong when that bar sits after an outage.

    Its open can be hours later at a gapped price. The signal must be dropped.
    """
    df = random_walk_frame(500)
    df = df.with_columns(
        pl.Series("gap", [(i == 0) or (i == 101) for i in range(df.height)])
    )
    sig = pl.Series("s", [i == 100 for i in range(df.height)])
    res = run_backtest(df, sig, is_long=True, timeframe="5m", costs=NO_COST,
                       bt=BacktestConfig())
    assert res.n == 0
    assert res.skipped_gap == 1


def test_one_position_at_a_time_produces_no_overlapping_trades():
    """50-64% of real signals overlap; treating them as independent fakes the CI."""
    df = random_walk_frame(20_000)
    sig = pl.Series("s", [i % 3 == 0 for i in range(df.height)])   # very dense
    res = run_backtest(df, sig, is_long=True, timeframe="5m", costs=NO_COST,
                       bt=BacktestConfig(max_bars=50))
    assert res.n > 100
    assert res.skipped_busy > 0, "dense signals produced no skips -- filter not applied"
    for a, b in zip(res.trades, res.trades[1:]):
        assert b.entry_index > a.exit_index, "overlapping trades in the output"


def test_intrabar_ambiguity_prefers_the_stop_without_1m_data():
    """One bar containing both levels, no resolver: assume the stop. Pessimistic."""
    rows = []
    for i, (o, h, l, c) in enumerate([
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 130.0, 70.0, 100.0),   # spans both stop and target
    ]):
        rows.append({
            "bar_open_ms": BASE + i * STEP, "session_date": date(2025, 6, 1),
            "open": o, "high": h, "low": l, "close": c, "volume": 1.0,
            "gap": i == 0, "atr_intraday": 10.0, "ms_since_open": 0,
            "rvol_tod": 1.0, "eff_ratio": 0.1, "atr_pct_60d": 50.0,
        })
    df = pl.DataFrame(rows)
    res = run_backtest(df, pl.Series("s", [False, True, False]), is_long=True,
                       timeframe="5m", costs=NO_COST,
                       bt=BacktestConfig(stop_atr=1.0, target_r=2.0))
    assert res.n == 1
    assert res.trades[0].exit_reason == "stop"
    assert res.trades[0].r_gross == pytest.approx(-1.0)


def test_intrabar_resolver_can_overturn_the_pessimistic_default():
    """With 1m bars showing the target first, the trade is a winner, not a loss."""
    rows = []
    for i, (o, h, l, c) in enumerate([
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 100.5, 99.5, 100.0),
        (100.0, 130.0, 70.0, 100.0),
    ]):
        rows.append({
            "bar_open_ms": BASE + i * STEP, "session_date": date(2025, 6, 1),
            "open": o, "high": h, "low": l, "close": c, "volume": 1.0,
            "gap": i == 0, "atr_intraday": 10.0, "ms_since_open": 0,
            "rvol_tod": 1.0, "eff_ratio": 0.1, "atr_pct_60d": 50.0,
        })
    df = pl.DataFrame(rows)
    # Inside bar 2: the target (120) is touched in the first minute, the stop (90) later.
    m1 = pl.DataFrame({
        "bar_open_ms": [BASE + 2 * STEP + k * 60_000 for k in range(5)],
        "high": [125.0, 101.0, 101.0, 101.0, 101.0],
        "low": [99.0, 99.0, 99.0, 99.0, 70.0],
    })
    res = run_backtest(df, pl.Series("s", [False, True, False]), is_long=True,
                       timeframe="5m", costs=NO_COST,
                       bt=BacktestConfig(stop_atr=1.0, target_r=2.0),
                       resolver=IntrabarResolver.from_frame(m1))
    assert res.trades[0].exit_reason == "target"
    assert res.trades[0].r_gross == pytest.approx(2.0)


def test_sample_size_gates():
    """n<30 shows nothing at all; n<100 is labelled provisional."""
    assert compute([0.1] * 29, bootstrap_iterations=50).reliability == "REFUSED"
    assert compute([0.1] * 29, bootstrap_iterations=50).expectancy_r is None
    assert compute([0.1] * 50, bootstrap_iterations=50).reliability == "PROVISIONAL"
    assert compute([0.1] * 150, bootstrap_iterations=50).reliability == "OK"


def test_benjamini_hochberg_rejects_a_lone_chance_hit():
    """20 patterns at alpha=0.05 produces ~1 hit by chance. It must not become a rule.

    Mirrors what the real run found: one pattern at p=0.025 out of 20 tested.
    """
    def _rep(p):
        from tradedesk.backtest.baseline import BaselineResult
        b = BaselineResult(1000, 100, 0.0, 0.0, p, -0.03, 0.03, 0.02)
        return PatternReport(
            pattern=f"p{p}", symbol="X", timeframe="5m", direction="long",
            stop_atr=1.0, target_r=2.0,
            in_sample=compute([0.1] * 200, bootstrap_iterations=50),
            out_sample=compute([0.1] * 200, bootstrap_iterations=50),
            in_sample_gross=None, gross_in=0.02, gross_out=0.0, drag_in=-1.0,
            baseline=b, signals=1, trades=1, slices=[], round_trip_bps=248.0,
        )

    ps = [0.025] + [0.3 + 0.03 * i for i in range(19)]
    reports = [_rep(p) for p in ps]
    survivors = apply_multiple_testing_correction(reports, fdr=0.05)
    assert survivors == 0, "a lone p=0.025 among 20 tests survived correction"
    assert reports[0].verdict == "NO DEMONSTRATED EDGE"

    # A genuinely strong result must still get through, or the correction is useless.
    strong = [_rep(1e-6) for _ in range(5)] + [_rep(0.5 + 0.02 * i) for i in range(15)]
    assert apply_multiple_testing_correction(strong, fdr=0.05) == 5
