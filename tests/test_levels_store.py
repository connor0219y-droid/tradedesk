"""The headline requirement, against the real 4-year store.

"No level or feature ever returns NaN or inf across the full 4-year store."

Marked `store` because it reads the real DuckDB file and takes a few seconds per
series. It SKIPS WITH A REASON when the store is absent -- never silently passes, which
would be the worst outcome for a test whose whole job is to certify the data.

Run with:  make levels-sweep      (or: uv run pytest -m store)
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from tradedesk import store
from tradedesk.config import load_config
from tradedesk.frames import read_bars
from tradedesk.levels import compute_levels
from tradedesk.levels.base import non_finite_columns
from tradedesk.levels.profile import value_area

pytestmark = pytest.mark.store


@pytest.fixture(scope="module")
def con():
    cfg = load_config()
    if not cfg.data.db_path.exists():
        pytest.skip(f"no candle store at {cfg.data.db_path}; run `make fetch` first")
    c = store.connect(cfg.data.db_path, read_only=True)
    counts = c.execute("SELECT count(*) FROM bars").fetchone()[0]
    if counts == 0:
        pytest.skip("candle store is empty; run `make fetch` first")
    yield c
    c.close()


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def _series(con):
    return con.execute(
        "SELECT DISTINCT symbol, timeframe FROM bars ORDER BY symbol, timeframe"
    ).fetchall()


def test_no_level_is_nan_or_inf_across_the_whole_store(con, cfg):
    """Requirement (1), over every bar of every series."""
    now = datetime.now(timezone.utc)
    checked_bars = 0
    checked_series = 0
    failures = []

    for symbol, timeframe in _series(con):
        bf = read_bars(con, symbol, timeframe, as_of=now)
        if bf.is_empty:
            continue
        df = compute_levels(bf, cfg).to_polars()
        offenders = non_finite_columns(df)
        if offenders:
            failures.append(f"{symbol} {timeframe}: {offenders}")
        checked_bars += df.height
        checked_series += 1

    assert checked_series >= 1, "no series were checked -- the sweep would pass vacuously"
    assert not failures, "non-finite values found: " + "; ".join(failures)
    print(f"\nswept {checked_bars:,} bars across {checked_series} series: all finite")


def test_value_area_is_total_on_sampled_points(con, cfg):
    """The value area is computed on demand rather than for every bar.

    Expanding outward from the POC needs the whole histogram sorted around it at that
    instant, which does not reduce to a cumulative sum the way the running POC does. So
    it is sampled here, and this test says so rather than implying full coverage.
    """
    now = datetime.now(timezone.utc)
    cfg_levels = cfg.levels
    sampled = 0
    for symbol, timeframe in _series(con):
        if timeframe != "5m":
            continue
        bf = read_bars(con, symbol, timeframe, as_of=now)
        df = compute_levels(bf, cfg).to_polars()
        stamps = df["bar_open_ms"].to_list()
        for idx in range(0, len(stamps), max(1, len(stamps) // 40)):
            poc, vah, val = value_area(
                df, at_ms=stamps[idx],
                buckets_per_atr=int(cfg_levels.get("profile_buckets_per_atr", 100)),
                tick_size=float(cfg_levels.get("tick_size", 0.01)),
                area_pct=float(cfg_levels.get("value_area_pct", 70.0)),
            )
            for name, v in (("poc", poc), ("vah", vah), ("val", val)):
                assert v is None or (v == v and abs(v) != float("inf")), (
                    f"{symbol} {timeframe} {name} non-finite at {stamps[idx]}"
                )
            if poc is not None:
                assert val <= poc <= vah, f"value area does not bracket POC ({val},{poc},{vah})"
            sampled += 1
    assert sampled > 0, "no value-area points sampled -- would pass vacuously"
    print(f"\nsampled {sampled} value-area points")


def test_null_counts_match_the_phase1_measurements(con, cfg):
    """The design was justified by specific measured counts; confirm they hold.

    These are the numbers that motivated the whole degenerate-value table:
    SOL/USD 1m has 19,852 zero-range bars and 3,450 gap-adjacent bars.
    """
    now = datetime.now(timezone.utc)
    bf = read_bars(con, "SOL/USD", "1m", as_of=now)
    if bf.is_empty:
        pytest.skip("SOL/USD 1m not present in the store")
    df = compute_levels(bf, cfg).to_polars()

    zero_range = df.filter(pl.col("high") == pl.col("low")).height
    assert df["close_pos_in_range"].null_count() == zero_range
    assert zero_range == 19_852, f"expected 19,852 zero-range bars, found {zero_range:,}"

    # gap-adjacent bars + the first bar of the series
    gaps = int(df["gap"].sum())
    assert df["true_range"].null_count() == gaps
    assert gaps == 3_451, f"expected 3,450 gaps + 1 series start, found {gaps:,}"


def test_atr_is_null_wherever_its_run_is_too_short(con, cfg):
    """Contiguity, stated as an invariant rather than a count."""
    now = datetime.now(timezone.utc)
    period = int(cfg.backtest.get("atr_period", 14))
    for symbol, timeframe in _series(con):
        bf = read_bars(con, symbol, timeframe, as_of=now)
        if bf.is_empty:
            continue
        df = compute_levels(bf, cfg).to_polars()
        bad = df.filter(
            (pl.col("clean_tr_in_run") < period) & pl.col("atr_intraday").is_not_null()
        )
        assert bad.is_empty(), (
            f"{symbol} {timeframe}: {bad.height} ATR values computed from fewer than "
            f"{period} clean TRs"
        )


def test_session_levels_are_null_exactly_when_broken(con, cfg):
    now = datetime.now(timezone.utc)
    for symbol, timeframe in _series(con):
        bf = read_bars(con, symbol, timeframe, as_of=now)
        if bf.is_empty:
            continue
        df = compute_levels(bf, cfg).to_polars()
        leaked = df.filter(pl.col("session_broken") & pl.col("vwap").is_not_null())
        assert leaked.is_empty(), (
            f"{symbol} {timeframe}: {leaked.height} VWAP values survived past a "
            "session-invalidating hole"
        )
