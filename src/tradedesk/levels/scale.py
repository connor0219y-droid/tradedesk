"""ATR: the scale that everything else is measured against.

Wilder's smoothing is an EWMA with alpha = 1/n, which has INFINITE memory. That fights
directly with contiguity masking: a single contaminated True Range would otherwise
decay through every subsequent value forever. Measured on the real store, the largest
TR is 135x the median, so an unmasked ATR(14) is inflated roughly 10x for 14 bars and
still visibly wrong long after.

Two mechanisms handle it, both verified empirically before being written here:

  1. The gap bar's TR is nulled FIRST. Its 'previous close' can be 6.6 hours stale, so
     the True Range is measuring the gap, not the bar.
  2. The EWMA runs .over(run_id), which restarts it at every contiguity break. Verified:
     a TR spike of 50 against a baseline of 1 decays 17.3 -> 11.9 -> 8.3 -> 5.8 globally,
     but restarts clean per run. A leading null is skipped by ewm_mean, so the EWMA
     seeds on the first clean TR rather than being poisoned by the nulled one.

Because the reset restarts accumulation, ATR immediately after a gap would be computed
from fewer than 14 observations. Rather than emit a thin-sample number that silently
disagrees with the TradingView chart, it is null until 14 clean TRs have accumulated.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div


@level(name="true_range", kind="rolling", depth=2, outputs=("true_range",))
def _true_range(ctx: LevelContext) -> pl.DataFrame:
    prev_close = pl.col("close").shift(1)
    return ctx.df.with_columns(
        pl.max_horizontal(
            pl.col("high") - pl.col("low"),
            (pl.col("high") - prev_close).abs(),
            (pl.col("low") - prev_close).abs(),
        ).alias("true_range")
    )


@level(
    name="atr_intraday",
    kind="rolling",
    depth=2,
    outputs=("atr_intraday",),
    requires=("true_range",),
)
def _atr_intraday(ctx: LevelContext) -> pl.DataFrame:
    """Wilder ATR on the intraday timeframe, reset at every contiguity break.

    Declared depth is 2 (inherited from true_range); the 14-observation requirement is
    enforced explicitly below against the RUN, not against a fixed window, because the
    EWMA's warm-up is per-run rather than per-window.
    """
    period = int(ctx.config.backtest.get("atr_period", 14))
    alpha = 1.0 / period

    df = ctx.df.with_columns(
        # The gap bar's TR is measuring the hole, not the bar. Drop it before smoothing.
        pl.when(pl.col("gap")).then(None).otherwise(pl.col("true_range")).alias("_tr_clean")
    )
    df = df.with_columns(
        pl.col("_tr_clean").is_not_null().cum_sum().over("run_id").cast(pl.UInt32).alias(
            "clean_tr_in_run"
        ),
        pl.col("_tr_clean")
        .rolling_mean(window_size=period, min_samples=period)
        .over("run_id")
        .alias("_sma_seed"),
    )
    # TradingView's ta.atr uses ta.rma, which seeds with the SMA of the first `period`
    # values and only then applies the recursion. polars' ewm_mean(adjust=False) seeds
    # with the FIRST value instead -- a difference that still carries ~36% weight 14
    # bars later, so it is not cosmetic.
    #
    # Feeding nulls before the seed point reproduces rma exactly: ewm_mean skips leading
    # nulls and seeds on the first non-null, which we make the SMA.
    df = df.with_columns(
        pl.when(pl.col("clean_tr_in_run") < period)
        .then(None)
        .when(pl.col("clean_tr_in_run") == period)
        .then(pl.col("_sma_seed"))
        .otherwise(pl.col("_tr_clean"))
        .alias("_atr_input")
    )
    return df.with_columns(
        pl.col("_atr_input")
        .ewm_mean(alpha=alpha, adjust=False, ignore_nulls=True)
        .over("run_id")
        .alias("atr_intraday")
    ).drop("_tr_clean", "_sma_seed", "_atr_input")


def daily_atr_frame(
    sessions: pl.DataFrame, *, period: int
) -> pl.DataFrame:
    """Wilder ATR over ET-day bars, available at the START of each session.

    Causal by construction: the value attached to session S is the ATR through session
    S-1, because S is not complete while it is being traded. Daily bars are derived by
    aggregating intraday bars over ET days rather than fetched as Coinbase 1d candles,
    which are UTC-anchored and would silently disagree with the ET session model.

    Only unbroken sessions contribute; a session containing a venue outage has a
    meaningless high/low and must not set the scale for the following fortnight.
    """
    s = sessions.sort("session_date")
    # Mask broken sessions out of the daily series entirely.
    s = s.with_columns(
        pl.when(pl.col("valid")).then(pl.col("s_high")).otherwise(None).alias("d_high"),
        pl.when(pl.col("valid")).then(pl.col("s_low")).otherwise(None).alias("d_low"),
        pl.when(pl.col("valid")).then(pl.col("s_close")).otherwise(None).alias("d_close"),
    )
    prev_close = pl.col("d_close").shift(1)
    s = s.with_columns(
        pl.max_horizontal(
            pl.col("d_high") - pl.col("d_low"),
            (pl.col("d_high") - prev_close).abs(),
            (pl.col("d_low") - prev_close).abs(),
        ).alias("daily_tr")
    )
    s = s.with_columns(
        pl.col("daily_tr").is_not_null().cum_sum().alias("_n_tr"),
        pl.col("daily_tr")
        .ewm_mean(alpha=1.0 / period, adjust=False, ignore_nulls=True)
        .alias("_atr_through_today"),
    )
    # Shift by one session: at any bar inside session S we may only know through S-1.
    return s.with_columns(
        pl.when(pl.col("_n_tr").shift(1) < period)
        .then(None)
        .otherwise(pl.col("_atr_through_today").shift(1))
        .alias("atr_daily")
    ).select("session_date", "atr_daily", "valid", "s_high", "s_low", "s_close")


def atr_percentile_frame(daily: pl.DataFrame, *, lookback: int = 60) -> pl.DataFrame:
    """Percentile rank of each session's ATR among the prior `lookback` sessions.

    Computed in plain Python over the session-level frame (about 1,463 rows per series,
    not 2.1M bars), which keeps it obvious and avoids a rolling-rank expression whose
    API surface has moved between polars versions.

    Strictly prior: the window excludes the current session's own value, so this can
    never see its own future.
    """
    values = daily["atr_daily"].to_list()
    out: list[float | None] = []
    for i, cur in enumerate(values):
        if cur is None or i < lookback:
            out.append(None)
            continue
        window = [v for v in values[i - lookback : i] if v is not None]
        if len(window) < lookback:
            out.append(None)
            continue
        below = sum(1 for v in window if v < cur)
        out.append(100.0 * below / len(window))
    return daily.with_columns(pl.Series("atr_pct_60d", out, dtype=pl.Float64))


def distance_in_atr(price: pl.Expr, level_col: str, atr_col: str = "atr_intraday") -> pl.Expr:
    """Signed distance from a level, in ATR units.

    Null whenever the level or the ATR is null, and whenever ATR is non-positive.
    Deliberately NOT capped: a tiny-but-positive ATR yields a large FINITE number,
    which is honest and passes the totality check. Capping would fabricate a value.
    Extreme cases are flagged by the engine instead.
    """
    atr = pl.col(atr_col)
    return safe_div(
        price - pl.col(level_col),
        pl.when(atr <= 0).then(None).otherwise(atr),
        when_zero=None,
    )
