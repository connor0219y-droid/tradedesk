"""Requirement (2): contiguity masking on every level, plus the causality harness.

The planted-leak meta-test is the load-bearing one. A lookahead test that has never
been observed to fail proves nothing -- it might be comparing empty frames, or masking
the very difference it is meant to detect. So this module also registers a deliberately
non-causal level and asserts the harness catches it.
"""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from tradedesk.config import load_config
from tradedesk.frames import BarFrame
from tradedesk.levels import compute_levels
from tradedesk.levels.base import REGISTRY, LevelError, level

STEP = 60_000
BASE = 1_700_000_000_000 // 86_400_000 * 86_400_000


def _series(n_sessions=3, bars=200, hole_at=None, hole_bars=0):
    """A clean multi-session series, optionally with a hole punched in session 0."""
    rows = []
    for s in range(n_sessions):
        sess = date(2025, 6, 1 + s)
        t0 = BASE + s * 86_400_000
        i = 0
        placed = 0
        while placed < bars:
            if hole_at is not None and s == 0 and placed == hole_at:
                i += hole_bars
            price = 100.0 + placed * 0.1
            rows.append({
                "venue": "coinbase", "symbol": "X/USD", "timeframe": "1m",
                "bar_open_ms": t0 + i * STEP, "open": price, "high": price + 1,
                "low": price - 1, "close": price + 0.5, "volume": 10.0 + placed,
                "session_date": sess, "calendar_version": 1, "revision": 0,
                "ingested_at_ms": 0,
            })
            i += 1
            placed += 1
    df = pl.DataFrame(rows).sort("bar_open_ms")
    return BarFrame(df=df, venue="coinbase", symbol="X/USD", timeframe="1m",
                    calendar_version=1, as_of_ms=int(df["bar_open_ms"].max()) + STEP)


@pytest.fixture
def cfg():
    return load_config()


# ------------------------------------------------------------------ declarations


def test_every_level_declares_a_depth():
    assert REGISTRY, "no levels registered"
    for name, spec in REGISTRY.items():
        assert spec.depth >= 1, name
        assert spec.kind in {"per_bar", "rolling", "session", "cross_session"}, name
        assert spec.outputs, name


def test_registering_a_level_without_depth_is_an_error():
    """Adding a level in six months must not be possible without stating its lookback."""
    with pytest.raises(TypeError):
        @level(name="bad", kind="rolling", outputs=("x",))  # type: ignore[call-arg]
        def _bad(ctx):
            return ctx.df


def test_per_bar_level_cannot_claim_lookback():
    with pytest.raises(LevelError):
        @level(name="bad_depth", kind="per_bar", depth=5, outputs=("x",))
        def _bad(ctx):
            return ctx.df


# ------------------------------------------------------------------- contiguity


def test_atr_is_null_across_a_gap_and_recovers(cfg):
    bf = _series(n_sessions=1, bars=200, hole_at=100, hole_bars=400)
    df = compute_levels(bf, cfg).to_polars()
    gap_idx = df.select(pl.arg_where(pl.col("gap"))).to_series().to_list()
    assert len(gap_idx) == 2  # series start + the punched hole
    g = gap_idx[1]
    # Nulled from the gap bar through the warm-up, then valid again.
    assert df["atr_intraday"][g] is None
    assert df["atr_intraday"][g + 12] is None
    assert df["atr_intraday"][g + 14] is not None


def test_wilder_resets_rather_than_carrying_contamination(cfg):
    """The 135x TR spike measured on the real store must not persist past a gap."""
    bf = _series(n_sessions=1, bars=200, hole_at=100, hole_bars=400)
    df = compute_levels(bf, cfg).to_polars()
    run_ids = df["run_id"].unique().to_list()
    assert len(run_ids) == 2
    # ATR in the second run is computed only from second-run bars, whose ranges are the
    # same as the first run's -- so it converges to the same value, not an inflated one.
    a = df.filter(pl.col("run_id") == run_ids[0])["atr_intraday"].drop_nulls()
    b = df.filter(pl.col("run_id") == run_ids[1])["atr_intraday"].drop_nulls()
    assert abs(a[-1] - b[-1]) < 0.05, "contamination leaked across the gap"


def test_session_levels_null_from_the_hole_onward_but_valid_before(cfg):
    """The causal refinement: a 14:00 hole does not invalidate the 10:00 VWAP."""
    bf = _series(n_sessions=1, bars=200, hole_at=100, hole_bars=60)  # 60min hole
    df = compute_levels(bf, cfg).to_polars()
    before = df.filter(~pl.col("session_broken"))
    after = df.filter(pl.col("session_broken"))
    assert before.height > 0 and after.height > 0
    assert before["vwap"].null_count() == 0, "VWAP nulled before the hole occurred"
    assert after["vwap"].null_count() == after.height, "VWAP survived past the hole"


def test_small_hole_does_not_invalidate_the_session(cfg):
    """A 1-minute quiet gap must not discard a day's VWAP -- that rule costs 19.4% of
    SOL 1m sessions, which is why the threshold is a duration."""
    bf = _series(n_sessions=1, bars=200, hole_at=100, hole_bars=3)  # 3min hole
    df = compute_levels(bf, cfg).to_polars()
    assert df["session_broken"].sum() == 0
    assert df["vwap"].null_count() == 0


def test_cross_session_levels_wait_for_enough_valid_history(cfg):
    bf = _series(n_sessions=3, bars=200)
    df = compute_levels(bf, cfg).to_polars()
    first = df.filter(pl.col("valid_prior_sessions") == 0)
    assert first["prior_day_close"].null_count() == first.height
    # rvol needs 20 prior sessions; three sessions can never satisfy it.
    assert df["rvol_tod"].null_count() == df.height


# -------------------------------------------------------------- causality harness


def _levels_at(bf: BarFrame, cfg, cutoff_ms: int, columns: list[str]) -> pl.DataFrame:
    truncated = BarFrame(
        df=bf.df.filter(pl.col("bar_open_ms") <= cutoff_ms),
        venue=bf.venue, symbol=bf.symbol, timeframe=bf.timeframe,
        calendar_version=bf.calendar_version, as_of_ms=cutoff_ms,
    )
    out = compute_levels(truncated, cfg).to_polars()
    return out.select(["bar_open_ms"] + [c for c in columns if c in out.columns])


def harness_is_causal(bf: BarFrame, cfg, cutoff_ms: int, columns: list[str]) -> bool:
    """Levels at bars <= t must be identical with and without the future present."""
    full = compute_levels(bf, cfg).to_polars()
    full = full.filter(pl.col("bar_open_ms") <= cutoff_ms).select(
        ["bar_open_ms"] + [c for c in columns if c in full.columns]
    )
    trunc = _levels_at(bf, cfg, cutoff_ms, columns)
    if full.is_empty() or trunc.is_empty():
        raise AssertionError("harness compared empty frames -- it would pass vacuously")
    return full.equals(trunc)


CAUSAL_COLUMNS = [
    "close_pos_in_range", "true_range", "atr_intraday", "vwap", "vwap_sigma",
    "or5_high", "or15_high", "or30_high", "poc", "prior_day_close",
    # The indicators added for the imported published strategies. Same harness, because
    # a new level is exactly as capable of reading the future as an old one.
    "dc3_low", "dc20_high", "sma_5d", "bb_upper", "bb_width", "nr7", "inside_bar",
    "rsi_2", "lbr_rsi3", "session_open", "ms_to_session_end", "prior_day_open",
]


def test_all_levels_are_causal(cfg):
    bf = _series(n_sessions=3, bars=200)
    cutoff = int(bf.df["bar_open_ms"][350])
    assert harness_is_causal(bf, cfg, cutoff, CAUSAL_COLUMNS)


def test_first_window_levels_are_causal_inside_their_own_window(cfg):
    """The same trap as the opening range, in the levels the intraday imports read.

    `ret_first30m` and `first_hour_high` summarise a window that is still running for
    the bars inside it. The obvious implementation computes the finished value and
    attaches it to every bar of the session, which at 00:05 reports a number that will
    not be knowable for another twenty-five minutes -- and which looks perfectly
    reasonable in the output.

    Truncating INSIDE the window is the only way to catch it: by the end of the session
    the running value and the final value agree, so a whole-series comparison passes.
    """
    bf = _series(n_sessions=2, bars=200)
    inside_first_half_hour = int(bf.df["bar_open_ms"][10])   # 1m bars, so 00:10
    assert harness_is_causal(
        bf, cfg, inside_first_half_hour,
        ["ret_first30m", "first_hour_high", "first_hour_low", "session_open"],
    )


def test_noise_band_and_stretch_are_causal(cfg):
    """Both average over prior sessions, and both would be trivial to make circular.

    The noise band decides whether today's move is unusual; if today's own move sat in
    the average it is compared against, the band would widen exactly when it should
    fire. The `shift(1)` in each is what prevents that, and this asserts it survives.

    Fifteen sessions so that the 14-session noise average and the 10-session stretch
    both have enough history to produce values rather than passing vacuously on nulls.
    """
    bf = _series(n_sessions=15, bars=60)
    cutoff = int(bf.df["bar_open_ms"][14 * 60 + 30])
    full = compute_levels(bf, cfg).to_polars()
    assert full["stretch"].drop_nulls().len() > 0, "stretch never produced a value"
    assert full["noise_upper"].drop_nulls().len() > 0, "noise band never produced a value"
    assert harness_is_causal(bf, cfg, cutoff, ["noise_upper", "noise_lower", "stretch"])


def test_opening_range_is_causal_inside_its_own_window(cfg):
    """The specific trap: computing the final OR and back-applying it to bars inside it.

    Truncating mid-window is the only way to catch this -- at the end of the session the
    running value and the final value agree, so a full-series comparison looks fine.
    """
    bf = _series(n_sessions=1, bars=200)
    mid_window = int(bf.df["bar_open_ms"][3])  # inside the 5-minute opening range
    assert harness_is_causal(bf, cfg, mid_window, ["or5_high", "or5_low", "or5_mid"])


def test_harness_detects_a_planted_leak(cfg):
    """Prove the harness can fail. Without this, every assertion above is worthless."""

    @level(name="_leaky", kind="per_bar", depth=1, outputs=("leaky_next_close",))
    def _leaky(ctx):
        # Deliberately non-causal: reads the NEXT bar's close.
        return ctx.df.with_columns(pl.col("close").shift(-1).alias("leaky_next_close"))

    try:
        bf = _series(n_sessions=2, bars=200)
        cutoff = int(bf.df["bar_open_ms"][250])
        full = compute_levels(bf, cfg, levels=["_leaky"]).to_polars()
        full = full.filter(pl.col("bar_open_ms") <= cutoff).select(
            "bar_open_ms", "leaky_next_close"
        )
        trunc_bf = BarFrame(
            df=bf.df.filter(pl.col("bar_open_ms") <= cutoff), venue=bf.venue,
            symbol=bf.symbol, timeframe=bf.timeframe,
            calendar_version=bf.calendar_version, as_of_ms=cutoff,
        )
        trunc = compute_levels(trunc_bf, cfg, levels=["_leaky"]).to_polars().select(
            "bar_open_ms", "leaky_next_close"
        )
        assert not full.equals(trunc), (
            "the harness failed to notice a level reading shift(-1) -- "
            "every other causality assertion in this suite is therefore worthless"
        )
    finally:
        REGISTRY.pop("_leaky", None)
