"""Swing-horizon machinery: derived bars and multi-day holds.

Both of these are new capabilities rather than new detectors, and both can be wrong in
ways that look plausible in a summary table -- an aggregated bar with the wrong open
moves every entry price, and a hold that silently still stops at midnight turns a swing
study into an intraday one with a bigger stop. So they are pinned by hand-checked values
rather than by whether the resulting expectancy looks sensible.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tradedesk.backtest import BacktestConfig, CostModel, run_backtest
from tradedesk.resample import ResampleError, aggregate_bars
from tradedesk.timeutil import session_date_et, tf_ms

HOUR = 3_600_000
#: A 00:00 UTC boundary, so bucket arithmetic starts somewhere unambiguous.
BASE = 1_700_000_000_000 // 86_400_000 * 86_400_000
NO_COST = CostModel(spread_bps=0.0, slippage_bps=0.0, taker_fee_bps=0.0)


def _hourly(n: int, *, start: int = BASE, skip: set[int] | None = None) -> pl.DataFrame:
    """`n` 1h bars where bar i has open i, close i+0.5, and a distinctive high/low.

    High is i+10 and low is i-10 scaled by position so max/min over a bucket land on
    known bars rather than being ties.
    """
    skip = skip or set()
    rows = [
        {
            "bar_open_ms": start + i * HOUR,
            "open": float(i), "high": float(i) + 10.0 - i * 0.1,
            "low": float(i) - 10.0 + i * 0.1, "close": float(i) + 0.5,
            "volume": 2.0, "calendar_version": 1,
        }
        for i in range(n)
        if i not in skip
    ]
    return pl.DataFrame(rows)


# ------------------------------------------------------------------ aggregation


def test_4h_bucket_takes_open_from_first_and_close_from_last():
    """Four 1h bars -> one 4h bar. Every field checkable by hand.

    high(i) = 0.9i + 10 and low(i) = 1.1i - 10 both increase, so the bucket's high
    comes from bar 3 (12.7) and its low from bar 0 (-10.0). Deliberately different
    bars: if high and low both came from bar 0, a bug taking everything from the first
    sub-bar would pass.

    open = bar 0's open = 0.0; close = bar 3's close = 3.5.
    """
    out = aggregate_bars(_hourly(4), source_tf="1h", target_tf="4h")
    assert out.height == 1
    r = out.to_dicts()[0]
    assert r["bar_open_ms"] == BASE
    assert r["open"] == pytest.approx(0.0)
    assert r["close"] == pytest.approx(3.5)
    assert r["high"] == pytest.approx(12.7)
    assert r["low"] == pytest.approx(-10.0)
    assert r["volume"] == pytest.approx(8.0)
    assert r["n_source_bars"] == 4


def test_incomplete_bucket_is_dropped_rather_than_given_a_wrong_open():
    """Bucket 0 is missing its first hour, so its "open" would be hour 1's open.

    Emitting it would put a price that is not the bucket's open into the frame, and the
    backtest enters at the open. Dropping it makes the hole visible to the gap machinery
    instead.
    """
    out = aggregate_bars(_hourly(8, skip={0}), source_tf="1h", target_tf="4h")
    assert out["bar_open_ms"].to_list() == [BASE + 4 * HOUR]
    assert out["open"][0] == pytest.approx(4.0)


def test_a_hole_anywhere_in_the_bucket_drops_it_too():
    """Not just the first sub-bar: a missing middle hour means the high/low are partial."""
    out = aggregate_bars(_hourly(8, skip={2}), source_tf="1h", target_tf="4h")
    assert out["bar_open_ms"].to_list() == [BASE + 4 * HOUR]


def test_daily_buckets_are_utc_anchored_and_labelled_with_their_et_date():
    """A 00:00 UTC daily bucket belongs to the PREVIOUS ET day (19:00 or 20:00 ET).

    Inheriting the label from the first sub-bar would be right by accident at this
    bucket size and wrong at others, so it is recomputed from the bucket's own open.
    """
    out = aggregate_bars(_hourly(48), source_tf="1h", target_tf="1d")
    assert out["bar_open_ms"].to_list() == [BASE, BASE + 24 * HOUR]
    for ms, got in zip(out["bar_open_ms"].to_list(), out["session_date"].to_list()):
        assert got == session_date_et(ms)


def test_bucket_boundaries_are_true_multiples():
    out = aggregate_bars(_hourly(24), source_tf="1h", target_tf="4h")
    assert all(ms % tf_ms("4h") == 0 for ms in out["bar_open_ms"].to_list())
    assert out.height == 6


def test_refuses_an_aggregation_that_is_not_a_whole_multiple():
    with pytest.raises(ResampleError):
        aggregate_bars(_hourly(10), source_tf="1h", target_tf="1m")


# ------------------------------------------------------- holding across sessions


def _multiday_frame(n_days: int, bars_per_day: int = 6) -> pl.DataFrame:
    """Bars that drift upward forever, so a long never stops out and only the session
    boundary or the bar cap can end the trade."""
    step = 4 * HOUR
    rows = []
    for i in range(n_days * bars_per_day):
        p = 100.0 + i * 0.5
        rows.append({
            "bar_open_ms": BASE + i * step,
            "session_date": date(2025, 6, 1) + timedelta(days=i // bars_per_day),
            "open": p, "high": p + 0.5, "low": p - 0.5, "close": p + 0.25,
            "volume": 10.0, "gap": i == 0, "atr_intraday": 1.0, "atr_daily": 6.0,
            "ms_since_open": (i % bars_per_day) * step,
            "rvol_tod": 1.0, "eff_ratio": 0.1, "atr_pct_60d": 50.0,
        })
    return pl.DataFrame(rows)


def _one_signal(n: int) -> pl.Series:
    return pl.Series("s", [i == 0 for i in range(n)])


def test_default_still_closes_at_the_session_boundary():
    """The intraday behaviour every earlier result was measured with, unchanged."""
    df = _multiday_frame(5)
    res = run_backtest(
        df, _one_signal(df.height), is_long=True, timeframe="4h", costs=NO_COST,
        bt=BacktestConfig(stop_atr=20.0, target_r=20.0, max_bars=100),
    )
    assert res.trades[0].exit_reason == "session_close"
    assert res.trades[0].bars_held <= 6


def test_holding_across_sessions_reaches_the_bar_cap_instead():
    """Same frame, same trade -- the only difference is the boundary rule.

    A stop and target far enough away that neither is reachable isolates the exit rule:
    with the boundary off, the trade must survive to the bar cap several days later.
    """
    df = _multiday_frame(5)
    res = run_backtest(
        df, _one_signal(df.height), is_long=True, timeframe="4h", costs=NO_COST,
        bt=BacktestConfig(stop_atr=20.0, target_r=20.0, max_bars=18,
                          hold_across_sessions=True),
    )
    t = res.trades[0]
    assert t.exit_reason == "bar_cap"
    assert t.bars_held == 18
    entry_day = df["session_date"][t.entry_index]
    assert df["session_date"][t.exit_index] > entry_day, "never left the entry session"


def test_daily_risk_scale_widens_the_stop_by_the_ratio_of_the_two_atrs():
    """`atr_column` is what puts a swing stop on a daily scale from faster bars.

    The frame carries atr_intraday=1.0 and atr_daily=6.0, so the same stop_atr must
    produce a stop six times further from the entry.
    """
    df = _multiday_frame(5)
    kw = dict(is_long=True, timeframe="4h", costs=NO_COST)
    sig = _one_signal(df.height)
    intraday = run_backtest(
        df, sig, bt=BacktestConfig(stop_atr=1.0, hold_across_sessions=True), **kw
    ).trades[0]
    daily = run_backtest(
        df, sig,
        bt=BacktestConfig(stop_atr=1.0, atr_column="atr_daily", hold_across_sessions=True),
        **kw,
    ).trades[0]
    assert daily.entry_price == pytest.approx(intraday.entry_price)
    risk_i = intraday.entry_price - intraday.stop
    risk_d = daily.entry_price - daily.stop
    assert risk_d == pytest.approx(6.0 * risk_i)
