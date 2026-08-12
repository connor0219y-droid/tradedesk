"""Requirement (1): every level is a total function -- never NaN, never inf.

The fixture in `degenerate_frame` contains every degenerate case the real store can
produce, all at once. It IS the specification of the degenerate semantics: the assertions
below say what each case must return, so changing the behaviour requires changing a
stated expectation rather than quietly changing an output.

Why the assertion is `(is_nan | is_infinite)` and not `is_finite`: verified in polars,
`1.0/0.0` gives inf and `0.0/0.0` gives NaN, and `is_nan()` catches only the second.
`is_finite()` on a null returns null rather than False, so a bare `.all()` silently
skips nulls. Nulls are permitted by design; NaN and inf are not.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import polars as pl
import pytest

from tradedesk.config import load_config
from tradedesk.frames import BarFrame
from tradedesk.levels import compute_levels
from tradedesk.levels.base import (
    NonFiniteError,
    assert_total,
    non_finite_columns,
    safe_div,
    safe_sqrt,
)

from tradedesk.timeutil import et_day_bounds

STEP = 60_000
# ET midnight of the first session date -- see the note in test_levels_known_answer.
BASE = et_day_bounds(date(2025, 6, 1))[0]


def _bar(ms, sess, o, h, l, c, v):
    return {
        "venue": "coinbase", "symbol": "X/USD", "timeframe": "1m",
        "bar_open_ms": ms, "open": o, "high": h, "low": l, "close": c, "volume": v,
        "session_date": sess, "calendar_version": 1, "revision": 0, "ingested_at_ms": 0,
    }


@pytest.fixture
def degenerate_frame():
    """Every degenerate case in one series. See the plan's degenerate-value table."""
    d0, d1, d2 = date(2025, 6, 1), date(2025, 6, 2), date(2025, 6, 3)
    rows = []
    t = BASE

    # --- session 0: normal bars, then a run of zero-range bars ---
    for i in range(20):
        rows.append(_bar(t + i * STEP, d0, 100.0, 101.0, 99.0, 100.5, 10.0))
    # single zero-range bar (high == low): 0/0 in every shape ratio
    rows.append(_bar(t + 20 * STEP, d0, 100.0, 100.0, 100.0, 100.0, 5.0))
    # consecutive zero-range bars -- a whole window of degenerate input
    for i in range(21, 25):
        rows.append(_bar(t + i * STEP, d0, 100.0, 100.0, 100.0, 100.0, 5.0))
    for i in range(25, 40):
        rows.append(_bar(t + i * STEP, d0, 100.0, 101.0, 99.0, 100.5, 10.0))
    # a 6-hour hole, then bars after it: TR here would measure the gap, not the bar
    gap_start = t + 400 * STEP
    for i in range(20):
        rows.append(_bar(gap_start + i * STEP, d0, 130.0, 131.0, 129.0, 130.5, 10.0))

    # --- session 1: a single bar (variance undefined, n == 1) ---
    t1 = BASE + 86_400_000
    rows.append(_bar(t1, d1, 100.0, 101.0, 99.0, 100.5, 10.0))

    # --- session 2: starts with a gap, contains a zero-volume bar ---
    t2 = BASE + 2 * 86_400_000
    for i in range(50, 70):
        v = 0.0 if i == 60 else 10.0  # zero volume: VWAP denominator hazard
        rows.append(_bar(t2 + i * STEP, d2, 100.0, 101.0, 99.0, 100.5, v))

    df = pl.DataFrame(rows).sort("bar_open_ms")
    return BarFrame(
        df=df, venue="coinbase", symbol="X/USD", timeframe="1m",
        calendar_version=1, as_of_ms=int(df["bar_open_ms"].max()) + 10 * STEP,
    )


@pytest.fixture
def cfg_levels():
    return load_config()


# --------------------------------------------------------------- primitives


def test_safe_div_requires_explicit_zero_case():
    """You cannot write a division without stating what zero means."""
    with pytest.raises(TypeError):
        safe_div(pl.col("a"), pl.col("b"))  # type: ignore[call-arg]


def test_safe_div_yields_null_not_nan():
    df = pl.DataFrame({"n": [1.0, 0.0, -1.0], "d": [0.0, 0.0, 0.0]})
    out = df.select(safe_div(pl.col("n"), pl.col("d"), when_zero=None).alias("q"))["q"]
    assert out.null_count() == 3
    assert not out.is_nan().any()
    assert not out.is_infinite().any()


def test_safe_sqrt_clamps_float_error_negatives():
    """The stable VWAP formulation can produce about -1e-18; sqrt of that is NaN."""
    df = pl.DataFrame({"v": [-1e-18, 0.0, 4.0, None]})
    out = df.select(safe_sqrt(pl.col("v")).alias("s"))["s"]
    assert out[0] == 0.0
    assert out[2] == 2.0
    assert out[3] is None
    assert not out.is_nan().any()


def test_assert_total_catches_nan_and_inf():
    """The canary: prove the guard can actually fail, or its passing means nothing."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(NonFiniteError):
            assert_total(pl.DataFrame({"x": [1.0, bad]}))
    # nulls are permitted -- they are the designed representation of "undefined"
    assert_total(pl.DataFrame({"x": [1.0, None]}))


def test_non_finite_detection_does_not_skip_nulls():
    """is_nan()/is_infinite() return NULL for null input; a bare sum would skip them."""
    df = pl.DataFrame({"x": [None, float("nan"), 1.0]})
    assert non_finite_columns(df) == {"x": 1}


# --------------------------------------------------- the degenerate specification


def test_no_level_is_ever_nan_or_inf(degenerate_frame, cfg_levels):
    """Requirement (1), on the fixture containing every degenerate case at once."""
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    assert non_finite_columns(df) == {}


def test_zero_range_bars_null_every_shape_ratio(degenerate_frame, cfg_levels):
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    zero_range = df.filter(pl.col("high") == pl.col("low"))
    assert zero_range.height == 5  # 1 isolated + 4 consecutive
    for col in ("close_pos_in_range", "body_frac", "upper_wick_frac", "lower_wick_frac"):
        assert zero_range[col].null_count() == zero_range.height, col


def test_zero_range_does_not_cascade_into_atr(degenerate_frame, cfg_levels):
    """A zero-range bar has a perfectly well-defined True Range.

    This is why nulling the shape ratios is affordable: it is a per-bar decision that
    does not delete the rolling levels around it.
    """
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    zr = df.filter((pl.col("high") == pl.col("low")) & ~pl.col("gap"))
    assert zr["true_range"].null_count() == 0


def test_bar_after_gap_has_null_true_range(degenerate_frame, cfg_levels):
    """Its 'previous close' is six hours stale -- the TR would measure the hole."""
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    assert df.filter(pl.col("gap"))["true_range"].null_count() == df.filter(pl.col("gap")).height


def test_single_bar_session_has_null_sigma(degenerate_frame, cfg_levels):
    """One observation has no dispersion; n == 1 must not yield 0.0."""
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    first_bars = df.filter(pl.col("bar_idx_in_session") == 0)
    assert first_bars["vwap_sigma"].null_count() == first_bars.height
    # ...but VWAP itself is defined from the very first bar.
    assert first_bars["vwap"].null_count() == 0


def test_zero_volume_bar_does_not_break_vwap(degenerate_frame, cfg_levels):
    """A zero-volume bar contributes nothing but must not null the running VWAP."""
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    zv = df.filter(pl.col("volume") == 0)
    assert zv.height == 1
    assert zv["vwap"].null_count() == 0


def test_premarket_is_null_for_crypto(degenerate_frame, cfg_levels):
    """Crypto has no premarket. The columns exist for equities; they are never invented."""
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    assert df["premarket_high"].null_count() == df.height
    assert df["premarket_low"].null_count() == df.height


def test_first_session_has_no_prior_day_levels(degenerate_frame, cfg_levels):
    df = compute_levels(degenerate_frame, cfg_levels).to_polars()
    first = df.filter(pl.col("session_date") == pl.col("session_date").min())
    assert first["prior_day_close"].null_count() == first.height


def test_empty_frame_is_handled(cfg_levels):
    empty = pl.DataFrame(
        schema={
            "venue": pl.String, "symbol": pl.String, "timeframe": pl.String,
            "bar_open_ms": pl.Int64, "open": pl.Float64, "high": pl.Float64,
            "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
            "session_date": pl.Date, "calendar_version": pl.Int16,
            "revision": pl.Int32, "ingested_at_ms": pl.Int64,
        }
    )
    bf = BarFrame(df=empty, venue="coinbase", symbol="X/USD", timeframe="1m",
                  calendar_version=1, as_of_ms=0)
    assert compute_levels(bf, cfg_levels).to_polars().is_empty()
