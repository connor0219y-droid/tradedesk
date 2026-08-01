"""Candlestick detectors.

Every definition here is written out explicitly rather than gestured at, because
"engulfing" means slightly different things in different books and an unstated choice
becomes an untestable result. Where a definition uses a threshold it is named and
defaults to a conventional value.

The body/wick ratios come from the Phase 2 level engine, which already returns **null**
on a zero-range bar rather than NaN. That matters here: 19,852 SOL/USD 1m bars have
high == low, and a detector comparing NaN thresholds would silently never fire on them
instead of visibly declining to classify them.
"""

from __future__ import annotations

import polars as pl

from .base import pattern

O, H, L, C = pl.col("open"), pl.col("high"), pl.col("low"), pl.col("close")
PO, PH, PL_, PC = O.shift(1), H.shift(1), L.shift(1), C.shift(1)

DOJI_BODY_MAX = 0.10
HAMMER_BODY_MAX = 0.30
HAMMER_WICK_MIN = 0.50
HAMMER_OPPOSITE_MAX = 0.15


@pattern(name="bullish_engulfing", depth=2, direction="long")
def _bullish_engulfing() -> pl.Expr:
    """Prior bar down, this bar up, and this body fully covers the prior body.

    Bodies, not ranges: the wick-inclusive variant fires far more often and is the
    looser of the two conventions.
    """
    return (PC < PO) & (C > O) & (C >= PO) & (O <= PC)


@pattern(name="bearish_engulfing", depth=2, direction="short")
def _bearish_engulfing() -> pl.Expr:
    """Prior bar up, this bar down, and this body fully covers the prior body."""
    return (PC > PO) & (C < O) & (C <= PO) & (O >= PC)


@pattern(name="hammer", depth=1, direction="long")
def _hammer() -> pl.Expr:
    """Small body, long lower wick, negligible upper wick.

    Null-safe by construction: on a zero-range bar the ratios are null, so the
    comparison is null and `fill_null(False)` in the engine declines to classify it.
    """
    return (
        (pl.col("body_frac") <= HAMMER_BODY_MAX)
        & (pl.col("lower_wick_frac") >= HAMMER_WICK_MIN)
        & (pl.col("upper_wick_frac") <= HAMMER_OPPOSITE_MAX)
    )


@pattern(name="shooting_star", depth=1, direction="short")
def _shooting_star() -> pl.Expr:
    """Small body, long upper wick, negligible lower wick."""
    return (
        (pl.col("body_frac") <= HAMMER_BODY_MAX)
        & (pl.col("upper_wick_frac") >= HAMMER_WICK_MIN)
        & (pl.col("lower_wick_frac") <= HAMMER_OPPOSITE_MAX)
    )


@pattern(name="doji_long", depth=2, direction="long")
def _doji_long() -> pl.Expr:
    """Indecision bar after a down bar -- read as a potential bullish turn.

    A doji alone has no direction, so it is registered twice with the prior bar's
    direction supplying the bias. Registering it once as "both" would make the
    expectancy meaningless: the long and short cases would cancel.
    """
    return (pl.col("body_frac") <= DOJI_BODY_MAX) & (PC < PO)


@pattern(name="doji_short", depth=2, direction="short")
def _doji_short() -> pl.Expr:
    """Indecision bar after an up bar."""
    return (pl.col("body_frac") <= DOJI_BODY_MAX) & (PC > PO)


@pattern(name="inside_bar_long", depth=2, direction="long")
def _inside_bar_long() -> pl.Expr:
    """Range contained within the prior bar's, after an up bar (continuation read)."""
    return (H <= PH) & (L >= PL_) & (PC > PO)


@pattern(name="inside_bar_short", depth=2, direction="short")
def _inside_bar_short() -> pl.Expr:
    return (H <= PH) & (L >= PL_) & (PC < PO)


@pattern(name="outside_bar_long", depth=2, direction="long")
def _outside_bar_long() -> pl.Expr:
    """Range engulfs the prior bar's and closes up."""
    return (H > PH) & (L < PL_) & (C > O)


@pattern(name="outside_bar_short", depth=2, direction="short")
def _outside_bar_short() -> pl.Expr:
    return (H > PH) & (L < PL_) & (C < O)


@pattern(name="three_bar_reversal_long", depth=3, direction="long")
def _three_bar_reversal_long() -> pl.Expr:
    """Middle bar makes the lowest low of three, and this bar closes above its high.

    A clean V: `low[t-1]` is the pivot, and the close reclaiming `high[t-1]` is the
    confirmation. Stated this precisely so the known-answer fixture is unambiguous.
    """
    return (PL_ < L.shift(2)) & (PL_ < L) & (C > PH)


@pattern(name="three_bar_reversal_short", depth=3, direction="short")
def _three_bar_reversal_short() -> pl.Expr:
    """Middle bar makes the highest high of three, and this bar closes below its low."""
    return (PH > H.shift(2)) & (PH > H) & (C < PL_)
