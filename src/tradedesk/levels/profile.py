"""Session volume profile: running POC, plus value area on demand.

RUNNING POC IS O(n) AND FULLY VECTORISED, which is not obvious. The naive reading --
"recompute the histogram at every bar" -- is quadratic and would be hopeless over 8.1M
bars. But cumulative volumes only ever INCREASE, so a bucket's running maximum is
achieved at the moment it was last updated. Only one bucket updates per bar. Therefore

    running_max_volume[i] = cum_max over rows of (that row's bucket cumulative volume)

and the POC is simply the bucket belonging to whichever row set that maximum. One
cum_sum, one cum_max, one forward-fill.

THE VALUE AREA DOES NOT REDUCE THIS WAY. Expanding outward from the POC to 70% of
session volume needs the whole histogram sorted around the POC at that instant, so it is
computed ON DEMAND at a requested timestamp rather than for every bar. That is honest
about what the full-store sweep covers: POC is checked everywhere, the value area is
checked at sampled points.

Bucket width is scaled to the instrument via the PRIOR session's daily ATR, so it is
comparable across BTC at 60,000 and SOL at 150, and is fixed for the whole session -- a
width derived from the running range would shift every bucket boundary each bar and make
the POC jitter for reasons that have nothing to do with volume.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div


def bucket_width_expr(buckets_per_atr: int, tick_size: float) -> pl.Expr:
    return pl.max_horizontal(
        safe_div(pl.col("atr_daily"), pl.lit(float(buckets_per_atr)), when_zero=None),
        pl.lit(tick_size),
    )


@level(
    name="volume_profile",
    kind="session",
    depth=1,
    outputs=("poc", "poc_bucket_volume"),
    requires=("vwap",),
)
def _volume_profile(ctx: LevelContext) -> pl.DataFrame:
    levels_cfg = getattr(ctx.config, "levels", {}) or {}
    buckets_per_atr = int(levels_cfg.get("profile_buckets_per_atr", 100))
    tick_size = float(levels_cfg.get("tick_size", 0.01))

    df = ctx.df
    if "atr_daily" not in df.columns:
        # Bucket width depends on the prior session's ATR; without it there is no
        # instrument-appropriate scale and the profile is null rather than guessed.
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("poc"),
            pl.lit(None, dtype=pl.Float64).alias("poc_bucket_volume"),
        )

    df = df.with_columns(bucket_width_expr(buckets_per_atr, tick_size).alias("_w"))
    df = df.with_columns(
        pl.col("typical_price").first().over("session_date").alias("_ref")
    )
    df = df.with_columns(
        safe_div(pl.col("typical_price") - pl.col("_ref"), pl.col("_w"), when_zero=None)
        .floor()
        .alias("_bucket")
    )

    # Cumulative volume of THIS bar's bucket, through this bar.
    df = df.with_columns(
        pl.col("volume").cum_sum().over(["session_date", "_bucket"]).alias("_bucket_cum")
    )
    # Because volumes only increase and one bucket updates per bar, the session maximum
    # so far is the running max of that quantity.
    df = df.with_columns(
        pl.col("_bucket_cum").cum_max().over("session_date").alias("poc_bucket_volume")
    )
    df = df.with_columns(
        pl.when(pl.col("_bucket_cum") == pl.col("poc_bucket_volume"))
        .then(pl.col("_bucket"))
        .otherwise(None)
        .forward_fill()
        .over("session_date")
        .alias("_poc_bucket")
    )
    return df.with_columns(
        (pl.col("_ref") + (pl.col("_poc_bucket") + 0.5) * pl.col("_w")).alias("poc")
    ).drop("_w", "_ref", "_bucket", "_bucket_cum", "_poc_bucket")


def value_area(
    df: pl.DataFrame,
    *,
    at_ms: int,
    buckets_per_atr: int = 100,
    tick_size: float = 0.01,
    area_pct: float = 70.0,
) -> tuple[float | None, float | None, float | None]:
    """(poc, vah, val) for the session containing `at_ms`, using bars up to `at_ms`.

    Causal: only bars at or before `at_ms` within that session contribute.

    Ties are broken deterministically -- when two candidate buckets carry equal volume,
    the one nearer the POC wins, then the lower price. Left unspecified, this is the
    kind of detail that produces different answers on different runs.

    When all volume sits in one bucket the value area genuinely IS that single price, so
    vah == val == poc. That is a correct answer, not a degenerate one.
    """
    session = df.filter(pl.col("bar_open_ms") <= at_ms)
    if session.is_empty():
        return None, None, None
    session_date = session["session_date"][-1]
    session = session.filter(pl.col("session_date") == session_date)
    if session.is_empty():
        return None, None, None

    atr_daily = session["atr_daily"][-1] if "atr_daily" in session.columns else None
    if atr_daily is None:
        return None, None, None
    width = max(atr_daily / buckets_per_atr, tick_size)

    ref = float(session["typical_price"][0])
    hist: dict[int, float] = {}
    for tp, vol in zip(session["typical_price"].to_list(), session["volume"].to_list()):
        if tp is None or vol is None:
            continue
        b = int((tp - ref) // width)
        hist[b] = hist.get(b, 0.0) + float(vol)
    if not hist:
        return None, None, None

    total = sum(hist.values())
    poc_bucket = max(hist, key=lambda b: (hist[b], -abs(b), -b))
    poc_price = ref + (poc_bucket + 0.5) * width
    if total <= 0:
        return poc_price, poc_price, poc_price

    target = total * (area_pct / 100.0)
    lo = hi = poc_bucket
    acc = hist[poc_bucket]
    while acc < target:
        up = hist.get(hi + 1, 0.0)
        down = hist.get(lo - 1, 0.0)
        if up == 0.0 and down == 0.0:
            break
        # Nearer-to-POC is symmetric here; break the remaining tie toward lower price.
        if up > down:
            hi += 1
            acc += up
        else:
            lo -= 1
            acc += down

    return poc_price, ref + (hi + 0.5) * width, ref + (lo + 0.5) * width
