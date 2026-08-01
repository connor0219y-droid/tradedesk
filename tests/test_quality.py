"""Data-quality checks, and the causal/non-causal boundary."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pytest

from tradedesk.ingest import rows_to_frame
from tradedesk.quality import offline
from tradedesk.quality.checks import classify_absent_bars, run_causal_checks
from tradedesk.timeutil import tf_ms

STEP = tf_ms("5m")
BASE = 1_700_000_000_000 // STEP * STEP


def _frame(rows):
    return rows_to_frame(rows, venue="coinbase", symbol="BTC/USD", timeframe="5m",
                         calendar_version=1, ingested_at_ms=0)


def _checks(cfg, rows):
    return run_causal_checks(_frame(rows), venue="coinbase", symbol="BTC/USD",
                             timeframe="5m", detected_at_ms=0, cfg=cfg)


def _names(issues) -> set[str]:
    return set(issues["check_name"].to_list()) if not issues.is_empty() else set()


def _clean(n=50, start=BASE):
    return [[start + i * STEP, 100.0, 101.0, 99.0, 100.5, 10.0] for i in range(n)]


def test_clean_bars_produce_no_issues(cfg):
    assert _names(_checks(cfg, _clean())) == set()


def test_high_below_low_is_an_error(cfg):
    rows = _clean()
    rows[10] = [rows[10][0], 100.0, 98.0, 99.0, 100.5, 10.0]  # high < low
    issues = _checks(cfg, rows)
    assert "ohlc.high_lt_low" in _names(issues)
    assert issues.filter(pl.col("check_name") == "ohlc.high_lt_low")["severity"][0] == "ERROR"


def test_high_below_max_open_close_is_an_error(cfg):
    rows = _clean()
    rows[5] = [rows[5][0], 100.0, 100.2, 99.0, 105.0, 10.0]  # close above high
    assert "ohlc.high_lt_max_oc" in _names(_checks(cfg, rows))


def test_low_above_min_open_close_is_an_error(cfg):
    rows = _clean()
    rows[5] = [rows[5][0], 95.0, 101.0, 99.0, 100.5, 10.0]  # open below low
    assert "ohlc.low_gt_min_oc" in _names(_checks(cfg, rows))


def test_zero_range_is_informational_not_an_error(cfg):
    """high == low is legal and real, but it is a divide-by-zero waiting to happen.

    Any pattern feature of the form (close - low) / (high - low) emits inf or NaN
    here, and that propagates through rolling windows without raising.
    """
    rows = _clean()
    rows[7] = [rows[7][0], 100.0, 100.0, 100.0, 100.0, 5.0]
    issues = _checks(cfg, rows)
    assert "ohlc.zero_range" in _names(issues)
    assert issues.filter(pl.col("check_name") == "ohlc.zero_range")["severity"][0] == "INFO"


def test_zero_volume_bar_is_flagged(cfg):
    """Coinbase omits no-trade buckets, so a *returned* zero-volume bar is anomalous."""
    rows = _clean()
    rows[3] = [rows[3][0], 100.0, 101.0, 99.0, 100.5, 0.0]
    assert "volume.zero" in _names(_checks(cfg, rows))


def test_misaligned_timestamp_is_an_error(cfg):
    rows = _clean()
    rows[4] = [rows[4][0] + 1234, 100.0, 101.0, 99.0, 100.5, 10.0]
    assert "ts.misaligned" in _names(_checks(cfg, rows))


def test_non_monotonic_timestamps_are_an_error(cfg):
    """Reversed data passes every OHLC invariant; only this check catches it."""
    rows = _clean(10)
    rows = list(reversed(rows))
    assert "ts.non_monotonic" in _names(_checks(cfg, rows))


def test_outlier_return_is_flagged(cfg):
    rows = _clean(60)
    rows[30] = [rows[30][0], 100.0, 400.0, 99.0, 350.0, 10.0]  # ~250% jump
    assert "ret.outlier" in _names(_checks(cfg, rows))


def test_flat_series_does_not_flag_every_bar(cfg):
    """MAD collapses to zero in a dead-flat window.

    Without a sigma floor the robust z-score is infinite and every one-tick move
    becomes a 'bad tick'.
    """
    rows = [[BASE + i * STEP, 100.0, 100.0, 100.0, 100.0, 10.0] for i in range(60)]
    rows[30] = [rows[30][0], 100.0, 100.02, 100.0, 100.01, 10.0]  # 1bp move
    issues = _checks(cfg, rows)
    assert "ret.outlier" not in _names(issues)


def test_absent_bars_are_split_into_no_trades_and_unknown():
    """The distinction the whole coverage table exists to make.

    Inside a covered range a missing bar is market information; outside one it means
    only that we have not looked. Conflating them makes an outage and a quiet market
    look identical.
    """
    present = {BASE, BASE + STEP, BASE + 4 * STEP}
    covered = [(BASE, BASE + 5 * STEP)]          # bars 0-4 were requested
    target = (BASE, BASE + 8 * STEP)             # but we want 0-7

    p, absent, unknown = classify_absent_bars(present, covered, target, "5m")
    assert p == 3
    assert absent == 2      # bars 2 and 3: fetched, no trades
    assert unknown == 3     # bars 5, 6, 7: never requested


def test_offline_checks_detect_a_reverting_bad_tick():
    """A spike that fully reverses next bar. Detecting it requires hindsight."""
    rows = [[BASE + i * STEP, 100.0, 101.0, 99.0, 100.0, 10.0] for i in range(60)]
    rows[30] = [rows[30][0], 100.0, 140.0, 99.0, 130.0, 10.0]
    df = _frame(rows)
    issues = offline.run_offline_checks(df, venue="coinbase", symbol="BTC/USD",
                                        timeframe="5m", detected_at_ms=0)
    assert not issues.is_empty()
    assert issues["check_name"][0] == "tick.reversion"
    assert issues["causal"][0] is False


def test_offline_module_is_not_imported_by_feature_code():
    """The causal/non-causal boundary is physical, and must stay that way.

    A `causal=False` column is easy to ignore during a refactor. An import is not --
    so nothing outside the quality package may reach for the hindsight checks.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "tradedesk"
    offenders = []
    for path in src.rglob("*.py"):
        if path.parent.name == "quality":
            continue
        text = path.read_text()
        if "offline" in text and "import" in text:
            for line in text.splitlines():
                if "import" in line and "offline" in line:
                    offenders.append(f"{path.relative_to(src)}: {line.strip()}")
    assert not offenders, (
        "non-causal checks leaked into feature code: " + "; ".join(offenders)
    )
