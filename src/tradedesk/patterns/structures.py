"""Multi-bar structures built on the Phase 2 levels.

These are the setups actually traded, as opposed to the textbook candles. Each one is a
FRESH event -- a state change on this bar -- not a standing condition. That distinction
is what stops "price is above VWAP" from firing on every bar of an uptrend and drowning
the sample in 10,000 copies of the same idea.

Every detector here declares the level columns it needs. The engine refuses to emit a
signal where those levels are null, so a setup can never fire on a session whose VWAP
was invalidated by a venue outage.
"""

from __future__ import annotations

import polars as pl

from .base import pattern

C, H, L = pl.col("close"), pl.col("high"), pl.col("low")
PC = C.shift(1)


@pattern(name="orb_long", depth=2, direction="long", requires=("or30_high",))
def _orb_long() -> pl.Expr:
    """First close above the 30-minute opening-range high.

    `PC <= prior or30_high` makes it a fresh break rather than a standing condition.
    The prior bar's OR value is used for the prior comparison because the opening range
    is still running while inside its own window.
    """
    return (C > pl.col("or30_high")) & (PC <= pl.col("or30_high").shift(1))


@pattern(name="orb_short", depth=2, direction="short", requires=("or30_low",))
def _orb_short() -> pl.Expr:
    """First close below the 30-minute opening-range low."""
    return (C < pl.col("or30_low")) & (PC >= pl.col("or30_low").shift(1))


@pattern(name="vwap_reclaim_long", depth=2, direction="long", requires=("vwap",))
def _vwap_reclaim_long() -> pl.Expr:
    """Close crosses back above session VWAP from below."""
    return (C > pl.col("vwap")) & (PC <= pl.col("vwap").shift(1))


@pattern(name="vwap_reclaim_short", depth=2, direction="short", requires=("vwap",))
def _vwap_reclaim_short() -> pl.Expr:
    """Close crosses back below session VWAP from above."""
    return (C < pl.col("vwap")) & (PC >= pl.col("vwap").shift(1))


@pattern(name="failed_breakout_long", depth=2, direction="long", requires=("or30_low",))
def _failed_breakout_long() -> pl.Expr:
    """Price broke below the opening-range low intrabar but closed back inside it.

    The bear trap. Traded long, because the break failed.
    """
    return (L < pl.col("or30_low")) & (C > pl.col("or30_low"))


@pattern(name="failed_breakout_short", depth=2, direction="short", requires=("or30_high",))
def _failed_breakout_short() -> pl.Expr:
    """Price broke above the opening-range high intrabar but closed back inside it."""
    return (H > pl.col("or30_high")) & (C < pl.col("or30_high"))


@pattern(
    name="ma_pullback_long",
    depth=2,
    direction="long",
    requires=("ema_fast", "ema_slow"),
)
def _ma_pullback_long() -> pl.Expr:
    """Uptrend, price dips to the fast EMA intrabar, and closes back above it.

    Uptrend is defined by the fast EMA above the slow one -- both computed causally and
    reset at contiguity breaks, so this cannot fire on a trend inferred across a gap.
    """
    return (
        (pl.col("ema_fast") > pl.col("ema_slow"))
        & (L <= pl.col("ema_fast"))
        & (C > pl.col("ema_fast"))
    )


@pattern(
    name="ma_pullback_short",
    depth=2,
    direction="short",
    requires=("ema_fast", "ema_slow"),
)
def _ma_pullback_short() -> pl.Expr:
    """Downtrend, price rallies to the fast EMA intrabar, and closes back below it."""
    return (
        (pl.col("ema_fast") < pl.col("ema_slow"))
        & (H >= pl.col("ema_fast"))
        & (C < pl.col("ema_fast"))
    )
