"""Journal: R arithmetic, behavioural flags, risk of ruin, and live-vs-backtest."""

from __future__ import annotations

import pytest

from tradedesk import store
from tradedesk.journal import (
    behavioural_flags,
    live_vs_backtest,
    load_trades,
    log_trade,
    risk_of_ruin,
    rolling_stats,
)
from tradedesk.qualification import SetupStats, record

MIN = 60_000
BASE = 1_700_000_000_000


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "j.duckdb")
    store.init_schema(c)
    yield c
    c.close()


def _add(con, **kw):
    base = dict(symbol="BTC/USD", direction="long", thesis="t" * 20,
                entry_ms=BASE, entry=100.0, stop=99.0, account_size=10_000.0)
    base.update(kw)
    return log_trade(con, **base)


def test_realised_r_long_and_short(con):
    _add(con, entry=100.0, stop=99.0, exit_price=102.0, exit_ms=BASE + MIN)   # +2R
    _add(con, direction="short", entry=100.0, stop=101.0, exit_price=98.0,
         entry_ms=BASE + 10 * MIN, exit_ms=BASE + 11 * MIN)                    # +2R
    rs = [t.realised_r for t in load_trades(con)]
    assert rs == pytest.approx([2.0, 2.0])


def test_losing_trade_is_minus_one_r(con):
    _add(con, exit_price=99.0, exit_ms=BASE + MIN)
    assert load_trades(con)[0].realised_r == pytest.approx(-1.0)


def test_revenge_entry_flag(con):
    _add(con, entry_ms=BASE, exit_ms=BASE + 5 * MIN, exit_price=99.0)           # loss
    _add(con, entry_ms=BASE + 7 * MIN, exit_price=101.0, exit_ms=BASE + 9 * MIN)  # 2 min later
    flags = behavioural_flags(load_trades(con), max_risk_pct=2.0)
    assert any("after a" in f and "loss" in f for fs in flags.values() for f in fs)


def test_revenge_flag_not_raised_outside_the_window(con):
    _add(con, entry_ms=BASE, exit_ms=BASE + 5 * MIN, exit_price=99.0)
    _add(con, entry_ms=BASE + 30 * MIN, exit_price=101.0, exit_ms=BASE + 31 * MIN)
    flags = behavioural_flags(load_trades(con), max_risk_pct=2.0)
    assert not any("loss" in f for fs in flags.values() for f in fs)


def test_oversize_flag(con):
    # risk per unit 1.0, size 300 -> $300 risk on a $10k account = 3%
    _add(con, size=300.0, exit_price=101.0, exit_ms=BASE + MIN)
    flags = behavioural_flags(load_trades(con), max_risk_pct=2.0)
    assert any("above your stated max" in f for fs in flags.values() for f in fs)


def test_gave_back_a_winner_flag(con):
    """Reached 1.5R favourable, exited at 0.2R -- the cut-your-winners failure."""
    _add(con, exit_price=100.2, exit_ms=BASE + MIN, mfe_r=1.5)
    flags = behavioural_flags(load_trades(con), max_risk_pct=2.0)
    assert any("favourable but exited" in f for fs in flags.values() for f in fs)


def test_stop_overrun_flag(con):
    _add(con, exit_price=98.5, exit_ms=BASE + MIN)   # -1.5R
    flags = behavioural_flags(load_trades(con), max_risk_pct=2.0)
    assert any("worse than" in f for fs in flags.values() for f in fs)


def test_clean_trade_has_no_flags(con):
    _add(con, exit_price=102.0, exit_ms=BASE + MIN, size=50.0, mfe_r=2.1)
    assert behavioural_flags(load_trades(con), max_risk_pct=2.0) == {}


def test_rolling_stats(con):
    for px in (102.0, 102.0, 99.0, 99.0):        # +2R, +2R, -1R, -1R
        _add(con, exit_price=px, exit_ms=BASE + MIN, entry_ms=BASE)
    s = rolling_stats(load_trades(con), risk_pct=1.0)
    assert s.n == 4
    assert s.win_rate == pytest.approx(0.5)
    assert s.expectancy_r == pytest.approx(0.5)
    assert s.profit_factor == pytest.approx(2.0)


def test_max_drawdown_in_r(con):
    for px in (102.0, 99.0, 99.0, 102.0):        # +2, -1, -1 -> peak 2, trough 0 => DD 2
        _add(con, exit_price=px, exit_ms=BASE + MIN)
    s = rolling_stats(load_trades(con))
    assert s.max_drawdown_r == pytest.approx(2.0)


def test_risk_of_ruin_formula():
    """RoR = ((1-A)/(1+A))^N with A = winRate*avgWin - lossRate*avgLoss."""
    # A = 0.5*2 - 0.5*1 = 0.5 ; base = 0.5/1.5 = 1/3 ; N=30 -> essentially zero
    assert risk_of_ruin(0.5, 2.0, 1.0, 30) == pytest.approx((1 / 3) ** 30, abs=1e-12)
    # A negative edge is certain ruin given enough trades.
    assert risk_of_ruin(0.3, 1.0, 1.0, 30) == 1.0
    assert risk_of_ruin(0.5, 1.0, 1.0, 30) == 1.0   # A == 0 exactly


def test_risk_of_ruin_is_reported_for_a_losing_system(con):
    for px in (99.0, 99.0, 99.0, 102.0):
        _add(con, exit_price=px, exit_ms=BASE + MIN)
    s = rolling_stats(load_trades(con), risk_pct=1.0)
    assert s.expectancy_r < 0
    assert s.risk_of_ruin == 1.0


def test_live_vs_backtest_localises_execution_problems(con):
    """The key report: backtest positive, live negative -> execution, not strategy."""
    record(
        con,
        SetupStats(setup="orb_long", symbol="BTC/USD", timeframe="5m", direction="long",
                   stop_atr=1.0, target_r=2.0, max_bars=48, round_trip_bps=248.0,
                   n_in=500, n_out=200, gross_in=0.3, gross_out=0.2, net_in=0.10,
                   ci_low=0.02, ci_high=0.18, p_value=0.001, survives_bh=True),
        validated_at_ms=0,
    )
    for px in (99.0, 99.0, 99.0):
        _add(con, setup="orb_long", exit_price=px, exit_ms=BASE + MIN)

    d = live_vs_backtest(con, load_trades(con))[0]
    assert d.setup == "orb_long"
    assert d.live_expectancy == pytest.approx(-1.0)
    assert d.backtest_expectancy == pytest.approx(0.10)
    assert d.gap < 0
    assert "too few live trades" in d.reading    # n=3 is honestly reported as too few


def test_live_vs_backtest_reads_execution_with_enough_trades(con):
    record(
        con,
        SetupStats(setup="orb_long", symbol="BTC/USD", timeframe="5m", direction="long",
                   stop_atr=1.0, target_r=2.0, max_bars=48, round_trip_bps=248.0,
                   n_in=500, n_out=200, gross_in=0.3, gross_out=0.2, net_in=0.10,
                   ci_low=0.02, ci_high=0.18, p_value=0.001, survives_bh=True),
        validated_at_ms=0,
    )
    for i in range(25):
        _add(con, setup="orb_long", exit_price=99.0, exit_ms=BASE + MIN,
             entry_ms=BASE + i * 60 * MIN)
    d = live_vs_backtest(con, load_trades(con))[0]
    assert "EXECUTION" in d.reading


def test_untagged_setup_reports_no_baseline(con):
    for i in range(25):
        _add(con, setup="never_tested", exit_price=101.0, exit_ms=BASE + MIN,
             entry_ms=BASE + i * 60 * MIN)
    d = live_vs_backtest(con, load_trades(con))[0]
    assert d.backtest_expectancy is None
    assert "never validated" in d.disqualifiers
