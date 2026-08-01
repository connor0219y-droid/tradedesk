"""Pattern detection: known answers, contiguity, and a planted causality leak."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from tradedesk.patterns import REGISTRY, detect
from tradedesk.patterns.base import PatternError, pattern

STEP = 300_000
BASE = 1_700_000_000_000 // STEP * STEP


def _frame(bars, *, gaps=None):
    """bars: list of (open, high, low, close). `gaps` marks indices as gap bars."""
    gaps = gaps or set()
    rows = []
    for i, (o, h, l, c) in enumerate(bars):
        rows.append({
            "bar_open_ms": BASE + i * STEP, "session_date": date(2025, 6, 1),
            "open": o, "high": h, "low": l, "close": c, "volume": 10.0,
            "gap": (i == 0) or (i in gaps),
        })
    return pl.DataFrame(rows)


def _up(base):
    """An up bar that cannot participate in a bullish engulfing as the PRIOR bar."""
    return (base, base + 2.0, base - 1.0, base + 1.0)


def test_known_answer_bullish_engulfing_finds_exactly_one():
    """The brief's test, verbatim: a 20-bar fixture with one unambiguous engulfing.

    Construction makes the answer checkable by hand. Every bar is an UP bar except bar
    9, and a bullish engulfing requires the PRIOR bar to be down -- so only bar 10 can
    possibly qualify, and it is built to qualify:

        bar 9  : open 105, close 100   (down)
        bar 10 : open  99, close 106   (up; body 99-106 covers 100-105)

    'and no other' is the half that matters. A detector firing twice on a fixture
    containing one pattern is not measuring what it claims to measure.
    """
    bars = [_up(100.0 + i) for i in range(9)]
    bars.append((105.0, 106.0, 99.0, 100.0))    # bar 9: down
    bars.append((99.0, 107.0, 98.0, 106.0))     # bar 10: engulfs bar 9's body
    bars += [_up(110.0 + i) for i in range(9)]  # bars 11-19
    assert len(bars) == 20

    sig = detect(_frame(bars), "bullish_engulfing")
    hits = [i for i, v in enumerate(sig.to_list()) if v]
    assert hits == [10], f"expected exactly bar 10, got {hits}"


def test_bearish_engulfing_mirror():
    bars = [(100.0 - i, 102.0 - i, 97.0 - i, 99.0 - i) for i in range(9)]  # down bars
    bars.append((92.0, 98.0, 91.0, 97.0))    # bar 9: up
    bars.append((98.0, 99.0, 90.0, 91.0))    # bar 10: engulfs downward
    bars += [(85.0 - i, 87.0 - i, 82.0 - i, 84.0 - i) for i in range(9)]
    sig = detect(_frame(bars), "bearish_engulfing")
    assert [i for i, v in enumerate(sig.to_list()) if v] == [10]


def test_pattern_does_not_fire_across_a_gap():
    """A two-bar pattern whose bars are not adjacent in time is an artifact.

    Same engulfing fixture, but bar 10 is marked as opening a gap. The shape is
    unchanged and still 'looks like' an engulfing -- it must not fire, because bar 9 may
    be six hours earlier.
    """
    bars = [_up(100.0 + i) for i in range(9)]
    bars.append((105.0, 106.0, 99.0, 100.0))
    bars.append((99.0, 107.0, 98.0, 106.0))
    bars += [_up(110.0 + i) for i in range(9)]

    assert detect(_frame(bars), "bullish_engulfing").sum() == 1
    assert detect(_frame(bars, gaps={10}), "bullish_engulfing").sum() == 0


def test_three_bar_pattern_needs_three_contiguous_bars():
    """depth=3, so a gap two bars back must also suppress the signal."""
    bars = [
        _up(100.0), _up(101.0),
        (100.0, 101.0, 90.0, 95.0),     # pivot low
        (95.0, 103.0, 94.0, 102.0),     # closes above the pivot's high
    ] + [_up(105.0 + i) for i in range(6)]
    assert detect(_frame(bars), "three_bar_reversal_long").sum() >= 1
    # A gap at the pivot bar breaks the three-bar window.
    assert detect(_frame(bars, gaps={2}), "three_bar_reversal_long").sum() == 0


def test_zero_range_bar_does_not_fire_wick_patterns():
    """Body/wick ratios are null on a zero-range bar, so the detector declines.

    19,852 SOL/USD 1m bars are like this. A detector comparing NaN thresholds would
    silently never fire; here the null propagates and `fill_null(False)` makes the
    decision explicit.
    """
    df = _frame([_up(100.0), (100.0, 100.0, 100.0, 100.0)])
    df = df.with_columns(
        pl.Series("body_frac", [0.5, None]),
        pl.Series("upper_wick_frac", [0.2, None]),
        pl.Series("lower_wick_frac", [0.3, None]),
    )
    assert detect(df, "hammer").to_list() == [False, False]


def test_every_pattern_declares_depth_and_direction():
    assert REGISTRY
    for name, spec in REGISTRY.items():
        assert spec.depth >= 1, name
        assert spec.direction in ("long", "short"), name


def test_registering_without_depth_is_an_error():
    with pytest.raises(TypeError):
        @pattern(name="bad", direction="long")  # type: ignore[call-arg]
        def _bad():
            return pl.lit(True)


def test_pattern_requiring_a_missing_level_raises():
    df = _frame([_up(100.0 + i) for i in range(5)])
    with pytest.raises(PatternError, match="needs level columns"):
        detect(df, "vwap_reclaim_long")


def test_pattern_does_not_fire_where_its_level_is_null():
    """A setup keyed to VWAP must not fire in a session whose VWAP was invalidated."""
    bars = [_up(100.0 + i) for i in range(6)]
    df = _frame(bars).with_columns(
        pl.Series("vwap", [100.0, 100.0, None, None, 100.0, 100.0])
    )
    # Force the crossing condition everywhere it could apply.
    sig = detect(df, "vwap_reclaim_long")
    assert not any(sig.to_list()[2:4]), "fired on bars where VWAP was null"


def test_planted_leak_is_detectable():
    """A detector reading the NEXT bar must be visibly different from a causal one.

    This is the meta-test: it proves a truncation comparison can distinguish causal from
    non-causal detection. Without it, a green causality suite might be comparing
    identical frames.
    """
    @pattern(name="_leaky_next_up", depth=1, direction="long")
    def _leaky():
        return pl.col("close").shift(-1) > pl.col("close")

    try:
        bars = [_up(100.0 + i) for i in range(10)]
        full = detect(_frame(bars), "_leaky_next_up").to_list()
        truncated = detect(_frame(bars[:6]), "_leaky_next_up").to_list()
        # The last bar of the truncated frame has no successor, so the leaky detector
        # gives a different answer there than it does with the future present.
        assert full[:6] != truncated, (
            "a detector reading shift(-1) produced identical output with and without "
            "the future -- the comparison cannot detect leaks"
        )
    finally:
        REGISTRY.pop("_leaky_next_up", None)


def test_causal_detector_is_unchanged_by_truncation():
    bars = [_up(100.0 + i) for i in range(9)]
    bars.append((105.0, 106.0, 99.0, 100.0))
    bars.append((99.0, 107.0, 98.0, 106.0))
    bars += [_up(110.0 + i) for i in range(9)]

    full = detect(_frame(bars), "bullish_engulfing").to_list()[:12]
    trunc = detect(_frame(bars[:12]), "bullish_engulfing").to_list()
    assert full == trunc
