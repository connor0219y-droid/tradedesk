"""Causal context: trending vs ranging, relative volume, time of day.

THE LOOKAHEAD TRAP THIS MODULE EXISTS TO AVOID. "Was the day trending or ranging" is
normally computed from the whole day and then used to slice signals that fired at 10:00.
That is lookahead, and a particularly seductive kind: the resulting split looks
enormously informative, because it is partly reporting what happened after the signal.

Everything here is therefore SESSION-TO-DATE. The efficiency ratio at bar `t` uses only
bars from the session open through `t`, so it is knowable at signal time and a Phase 5
live rule can condition on it.

Only the raw continuous values live here. BUCKET BOUNDARIES ARE NOT COMPUTED HERE --
a median taken over all four years and then used to split in-sample results leaks the
out-of-sample distribution into the in-sample labels. The split module fits boundaries
on the in-sample slice alone.
"""

from __future__ import annotations

import polars as pl

from .base import PatternError  # noqa: F401  (re-exported for convenience)

#: Kaufman efficiency ratio: net displacement divided by total path travelled. Near 1.0
#: the session is moving in a straight line (trending); near 0 it is churning (ranging).
EFFICIENCY_MIN_BARS = 5


def add_regime_columns(df: pl.DataFrame) -> pl.DataFrame:
    """Attach session-to-date efficiency ratio and the time-of-day offset.

    `rvol_tod` already comes from the Phase 2 level engine and is causal there (it uses
    the previous 20 sessions at the same time of day, strictly prior).
    """
    from ..levels.base import safe_div

    net = (pl.col("close") - pl.col("close").first().over("session_date")).abs()
    path = (
        pl.col("close").diff().abs().fill_null(0.0).cum_sum().over("session_date")
    )
    bars_so_far = pl.int_range(pl.len()).over("session_date")

    return df.with_columns(
        # Too few bars to distinguish a trend from a single move.
        pl.when(bars_so_far < EFFICIENCY_MIN_BARS)
        .then(None)
        .otherwise(safe_div(net, path, when_zero=None))
        .alias("eff_ratio")
    )


def bucket_time_of_day(col: str = "ms_since_open") -> pl.Expr:
    """Four six-hour ET buckets. Fixed boundaries, so nothing is fitted."""
    ms = pl.col(col)
    six = 6 * 3_600_000
    return (
        pl.when(ms < six).then(pl.lit("00-06"))
        .when(ms < 2 * six).then(pl.lit("06-12"))
        .when(ms < 3 * six).then(pl.lit("12-18"))
        .otherwise(pl.lit("18-24"))
        .alias("tod_bucket")
    )


def bucket_by_threshold(col: str, threshold: float, low: str, high: str, out: str) -> pl.Expr:
    """Two-way split at a threshold fitted elsewhere (on in-sample data only)."""
    return (
        pl.when(pl.col(col).is_null())
        .then(None)
        .when(pl.col(col) >= threshold)
        .then(pl.lit(high))
        .otherwise(pl.lit(low))
        .alias(out)
    )


def fit_thresholds(df: pl.DataFrame) -> dict[str, float]:
    """Median split points for relative volume and efficiency ratio.

    MUST be called on the in-sample slice only. Fitting on the full history and then
    reporting out-of-sample results against those boundaries leaks the holdout's
    distribution into the labels -- a subtle enough leak that it usually goes unnoticed,
    which is why `split.py` owns the call.
    """
    out: dict[str, float] = {}
    for col, key in (("rvol_tod", "rvol"), ("eff_ratio", "efficiency")):
        if col in df.columns:
            v = df[col].drop_nulls()
            out[key] = float(v.median()) if v.len() else float("nan")
    return out
