"""The EMA-cross-with-trend-filter setup: indicators, entry logic, and entry spacing.

The indicators are checked against an independent Wilder implementation written out in
this file rather than against blessed output, because the whole point of SMA-seeding
them is to agree with a TradingView chart -- and a change-detector test would happily
lock in a disagreement.

The detector is tested on hand-supplied indicator columns. That separates the two things
that can be wrong: whether the averages are computed correctly (above) and whether the
inferred entry condition is what was intended (below). The second is the part that was
reconstructed rather than read off the settings panel, so it gets the decoys.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tradedesk.backtest import BacktestConfig, CostModel, run_backtest
from tradedesk.frames import BarFrame
from tradedesk.levels import compute_levels
from tradedesk.levels.momentum import RSI_PERIOD
from tradedesk.patterns import detect

STEP = 60_000
BASE = 1_700_000_000_000 // 86_400_000 * 86_400_000
NO_COST = CostModel(spread_bps=0.0, slippage_bps=0.0, taker_fee_bps=0.0)


# --------------------------------------------------------------------- reference


def wilder_rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's RSI, written straight from the definition.

    Independent of the polars implementation on purpose: seeded with the SMA of the
    first `period` gains and losses, then smoothed as (prev*(n-1) + x)/n, which is the
    recursion ta.rma applies. The first defined value lands on bar `period`, because
    `period` changes require `period + 1` closes.
    """
    gains, losses = [], []
    for a, b in zip(closes, closes[1:]):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))

    out: list[float | None] = [None] * len(closes)
    ag = sum(gains[:period]) / period
    al = sum(losses[:period]) / period
    out[period] = 100.0 * ag / (ag + al) if (ag + al) else None
    for i in range(period, len(gains)):
        ag = (ag * (period - 1) + gains[i]) / period
        al = (al * (period - 1) + losses[i]) / period
        out[i + 1] = 100.0 * ag / (ag + al) if (ag + al) else None
    return out


def _bar_frame(closes: list[float]) -> BarFrame:
    """A contiguous 1m series whose close is the only thing that varies meaningfully."""
    rows = [
        {
            "venue": "coinbase", "symbol": "X/USD", "timeframe": "1m",
            "bar_open_ms": BASE + i * STEP,
            "open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 10.0,
            "session_date": date(2025, 6, 1), "calendar_version": 1, "revision": 0,
            "ingested_at_ms": 0,
        }
        for i, c in enumerate(closes)
    ]
    df = pl.DataFrame(rows)
    return BarFrame(df=df, venue="coinbase", symbol="X/USD", timeframe="1m",
                    calendar_version=1, as_of_ms=int(df["bar_open_ms"].max()) + STEP)


def _zigzag(n: int, *, seed: float = 100.0) -> list[float]:
    """A deterministic path with both up and down moves of varying size.

    A monotonic ramp would give RSI = 100 everywhere and test almost nothing.
    """
    out, p = [], seed
    for i in range(n):
        p += ((i * 7) % 11) - 5 + (0.5 if i % 3 else -1.25)
        out.append(round(p, 6))
    return out


# ------------------------------------------------------------------- indicators


def test_rsi_matches_an_independent_wilder_implementation(cfg):
    closes = _zigzag(120)
    df = compute_levels(_bar_frame(closes), cfg).to_polars()
    expected = wilder_rsi(closes, RSI_PERIOD)

    got = df["rsi_14"].to_list()
    assert got[RSI_PERIOD - 1] is None, "RSI defined before enough changes accumulated"
    for i in range(RSI_PERIOD, len(closes)):
        assert got[i] == pytest.approx(expected[i], abs=1e-9), f"bar {i}"


def test_rsi_is_100_not_inf_when_nothing_ticked_down(cfg):
    """The textbook form divides by average loss, which is genuinely zero here.

    `100 * gain / (gain + loss)` gives 100; `100 - 100/(1 + gain/0)` gives inf, and
    assert_total would refuse the frame. This is not hypothetical -- a 14-bar stretch
    with no down bar happens constantly on real data.
    """
    df = compute_levels(_bar_frame([100.0 + i for i in range(40)]), cfg).to_polars()
    assert df["rsi_14"][39] == pytest.approx(100.0)


def test_rsi_is_null_on_a_perfectly_flat_series(cfg):
    """Neither gains nor losses: the ratio is undefined, so it must be null, not 50."""
    df = compute_levels(_bar_frame([100.0] * 40), cfg).to_polars()
    assert df["rsi_14"][39] is None


def test_ema_seeds_on_the_sma_like_tradingview(cfg):
    """ta.ema's first output is the SMA of the first `length` closes.

    polars' ewm_mean(adjust=False) would instead seed on close[0], which still carries
    ~15% weight at this bar -- so a chart comparison would disagree visibly.
    """
    closes = _zigzag(60)
    df = compute_levels(_bar_frame(closes), cfg).to_polars()

    assert df["ema_9"][7] is None, "EMA(9) defined before 9 closes existed"
    assert df["ema_9"][8] == pytest.approx(sum(closes[:9]) / 9)
    assert df["ema_21"][20] == pytest.approx(sum(closes[:21]) / 21)


def test_ema_200_needs_200_contiguous_bars(cfg):
    closes = _zigzag(210)
    df = compute_levels(_bar_frame(closes), cfg).to_polars()
    assert df["ema_200"][198] is None
    assert df["ema_200"][199] == pytest.approx(sum(closes[:200]) / 200)


# ---------------------------------------------------------------- entry logic


def _signal_frame(rows) -> pl.DataFrame:
    """rows: (close, ema_9, ema_21, ema_200, rsi_14). Indicators supplied directly."""
    return pl.DataFrame([
        {
            "bar_open_ms": BASE + i * STEP, "session_date": date(2025, 6, 1),
            "open": c, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": 10.0,
            "gap": i == 0,
            "ema_9": f, "ema_21": s, "ema_200": t, "rsi_14": r,
        }
        for i, (c, f, s, t, r) in enumerate(rows)
    ])


def test_long_fires_only_on_the_cross_bar_and_only_when_every_filter_agrees():
    """Four near-misses and one hit, so a detector that is too loose is visible.

    bar 1  fast crosses up, but close is BELOW the 200 EMA        -> no
    bar 2  fast still above slow (a standing condition, not a cross) -> no
    bar 4  fast crosses up above the 200 EMA, but RSI is 45       -> no
    bar 6  fast crosses up, above the 200 EMA, RSI 58             -> YES
    bar 7  everything still true, but no fresh cross              -> no
    """
    rows = [
        # close  ema_9  ema_21 ema_200 rsi
        (90.0,   89.0,  90.0,  100.0,  55.0),   # 0 fast below slow, below trend
        (95.0,   91.0,  90.0,  100.0,  55.0),   # 1 cross up, but close < ema_200
        (96.0,   92.0,  90.0,  100.0,  55.0),   # 2 standing, not a cross
        (105.0,  89.0,  90.0,  100.0,  45.0),   # 3 fast back below slow
        (106.0,  91.0,  90.0,  100.0,  45.0),   # 4 cross up, above trend, RSI 45
        (107.0,  89.0,  90.0,  100.0,  58.0),   # 5 fast back below slow
        (108.0,  91.0,  90.0,  100.0,  58.0),   # 6 the one real signal
        (109.0,  93.0,  90.0,  100.0,  58.0),   # 7 standing, not a cross
    ]
    sig = detect(_signal_frame(rows), "ema_cross_trend_long")
    assert [i for i, v in enumerate(sig.to_list()) if v] == [6]


def test_short_is_the_exact_mirror():
    rows = [
        (110.0,  91.0,  90.0,  100.0,  45.0),   # 0 fast above slow
        (95.0,   89.0,  90.0,  100.0,  42.0),   # 1 cross down, below trend, RSI 42
        (94.0,   88.0,  90.0,  100.0,  42.0),   # 2 standing, not a cross
    ]
    sig = detect(_signal_frame(rows), "ema_cross_trend_short")
    assert [i for i, v in enumerate(sig.to_list()) if v] == [1]


def test_touching_emas_that_separate_upward_count_as_a_cross():
    """`fast.shift(1) <= slow.shift(1)`, not `<`.

    Two EMAs that print equal and then separate is a cross by any reading, and a strict
    `<` would silently drop it. Rare on real data, but the kind of thing that makes a
    trade count disagree with the chart for no visible reason.
    """
    rows = [
        (105.0,  88.0,  90.0,  100.0,  60.0),
        (106.0,  90.0,  90.0,  100.0,  60.0),   # exactly equal
        (107.0,  92.0,  90.0,  100.0,  60.0),   # separates upward -> a cross
    ]
    sig = detect(_signal_frame(rows), "ema_cross_trend_long")
    assert [i for i, v in enumerate(sig.to_list()) if v] == [2]


def test_no_signal_where_an_indicator_is_null():
    """Before EMA(200) warms up, `close > ema_200` is unknown -- not True.

    `detect` nulls the signal wherever a required column is null, which is what keeps
    the strategy from trading its own warm-up period.
    """
    rows = [
        (105.0,  88.0,  90.0,  100.0,  60.0),
        (106.0,  92.0,  90.0,  None,   60.0),   # a real cross, but no trend filter yet
    ]
    sig = detect(_signal_frame(rows), "ema_cross_trend_long")
    assert not any(sig.to_list())


# -------------------------------------------------------------- entry spacing


def _spacing_frame(n: int) -> pl.DataFrame:
    """A flat series: no trade can ever hit its stop or target, so every exit is the
    bar cap. That isolates the cooldown from the one-position-at-a-time rule."""
    return pl.DataFrame([
        {
            "bar_open_ms": BASE + i * 300_000,
            "session_date": date(2025, 6, 1) + timedelta(days=i // 288),
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
            "volume": 10.0, "gap": i == 0, "atr_intraday": 1.0,
            "ms_since_open": (i * 300_000) % 86_400_000,
            "rvol_tod": 1.0, "eff_ratio": 0.1, "atr_pct_60d": 50.0,
        }
        for i in range(n)
    ])


def test_min_bars_between_entries_is_enforced():
    """Signal on every bar, a 1-bar holding cap, and a 6-bar cooldown.

    Without the cooldown the engine would enter every other bar (held one bar, free the
    next). With it, entries must sit at least 6 bars apart.
    """
    df = _spacing_frame(60)
    sig = pl.Series("s", [True] * df.height)
    res = run_backtest(
        df, sig, is_long=True, timeframe="5m", costs=NO_COST,
        bt=BacktestConfig(stop_atr=1.0, target_r=2.0, max_bars=1,
                          min_bars_between_entries=6),
    )
    entries = [t.entry_index for t in res.trades]
    assert len(entries) > 5, "cooldown suppressed everything"
    gaps = [b - a for a, b in zip(entries, entries[1:])]
    assert min(gaps) >= 6, f"entries closer than 6 bars apart: {gaps}"
    assert res.skipped_cooldown > 0


def test_zero_cooldown_is_the_previous_behaviour():
    """The default must not change any result validated before the option existed."""
    df = _spacing_frame(60)
    sig = pl.Series("s", [True] * df.height)
    kw = dict(is_long=True, timeframe="5m", costs=NO_COST)
    a = run_backtest(df, sig, bt=BacktestConfig(max_bars=1), **kw)
    b = run_backtest(
        df, sig, bt=BacktestConfig(max_bars=1, min_bars_between_entries=0), **kw
    )
    assert [t.entry_index for t in a.trades] == [t.entry_index for t in b.trades]
    assert a.skipped_cooldown == 0
