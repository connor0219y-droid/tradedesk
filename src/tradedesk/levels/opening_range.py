"""Opening range, anchored at 00:00 ET.

Crypto has no opening bell, so the session anchor is midnight ET -- chosen because the
trader is US-based, because midnight ET always exists and is never ambiguous (US DST
transitions happen at 02:00), and because it is the same anchor equities will use.

THE LOOKAHEAD TRAP this module exists to avoid: the obvious implementation computes the
final opening-range high and low for the session and attaches them to every bar,
including the bars INSIDE the opening-range window. That is a lookahead bug -- at
00:03 you cannot know the 00:00-00:30 range. Here the level is a RUNNING cum_max /
cum_min while inside the window, which then freezes once the window closes.

The lookahead harness catches a regression here by truncating the store mid-window and
checking the value is unchanged.
"""

from __future__ import annotations

import polars as pl

from ..timeutil import et_day_bounds
from .base import LevelContext, level, safe_div

OR_MINUTES = (5, 15, 30)


def add_et_midnight(df: pl.DataFrame) -> pl.DataFrame:
    """True 00:00 ET for each session, in UTC ms.

    Deliberately not `min(bar_open_ms)`: if a session's first bars are missing, the
    first PRESENT bar is not midnight, and every time-of-day offset would be silently
    shifted for that day.
    """
    if df.is_empty():
        return df.with_columns(pl.lit(None, dtype=pl.Int64).alias("et_midnight_ms"))
    dates = df["session_date"].unique().to_list()
    mapping = {d: et_day_bounds(d)[0] for d in dates}
    return df.with_columns(
        pl.col("session_date")
        .replace_strict(mapping, return_dtype=pl.Int64)
        .alias("et_midnight_ms")
    )


@level(
    name="opening_range",
    kind="session",
    depth=1,
    outputs=tuple(
        c
        for m in OR_MINUTES
        for c in (f"or{m}_high", f"or{m}_low", f"or{m}_mid", f"or{m}_pos")
    ),
)
def _opening_range(ctx: LevelContext) -> pl.DataFrame:
    # `ms_since_open` now comes from the session anchor, which knows where the session
    # actually starts: 00:00 ET for crypto, 09:30 for equities. Recomputing it here
    # would give an equity opening range anchored seven and a half hours early.
    df = ctx.df

    exprs: list[pl.Expr] = []
    for minutes in OR_MINUTES:
        window_ms = minutes * 60_000
        hi, lo = f"or{minutes}_high", f"or{minutes}_low"

        if ctx.tf_minutes > minutes:
            # A 5-minute opening range is not representable on 15m bars. Null with a
            # reason rather than silently returning the first bar's range.
            exprs += [
                pl.lit(None, dtype=pl.Float64).alias(hi),
                pl.lit(None, dtype=pl.Float64).alias(lo),
            ]
            continue

        # `>= 0` is what excludes the premarket. Without it every premarket bar has a
        # NEGATIVE ms_since_open, satisfies `< window_ms`, and lands inside the opening
        # range -- so an equity 30-minute range would silently be the 04:00-10:00 range.
        inside = (pl.col("ms_since_open") >= 0) & (pl.col("ms_since_open") < window_ms)
        exprs += [
            pl.when(inside)
            .then(pl.col("high"))
            .otherwise(None)
            .cum_max()
            .over("session_date")
            .forward_fill()
            .over("session_date")
            .alias(hi),
            pl.when(inside)
            .then(pl.col("low"))
            .otherwise(None)
            .cum_min()
            .over("session_date")
            .forward_fill()
            .over("session_date")
            .alias(lo),
        ]

    df = df.with_columns(exprs)

    derived: list[pl.Expr] = []
    for minutes in OR_MINUTES:
        hi, lo = pl.col(f"or{minutes}_high"), pl.col(f"or{minutes}_low")
        derived += [
            ((hi + lo) / 2.0).alias(f"or{minutes}_mid"),
            safe_div(pl.col("close") - lo, hi - lo, when_zero=None).alias(
                f"or{minutes}_pos"
            ),
        ]
    return df.with_columns(derived)
