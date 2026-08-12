"""Level orchestration.

The engine is where masking happens, and that is the whole point: level authors never
write masking code, so they cannot forget it. A level declares its `kind` and `depth` to
the @level decorator, and the engine derives the mask from that declaration.

Mask semantics by kind:

    per_bar        no time mask (the level looks at one bar)
    rolling        window_mask(depth) -- the last `depth` bars must be contiguous
    session        not session_broken -- no >=30min hole at or before this bar
    cross_session  valid_prior_sessions >= depth

Every level frame passes through assert_total on the way out, so a NaN or inf cannot
escape even if a level bypassed safe_div.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl

from ..config import Config
from ..frames import BarFrame
from ..timeutil import tf_ms as _tf_ms
from . import (  # noqa: F401
    barshape,
    context,
    intraday,
    momentum,
    opening_range,
    prior,
    profile,
    scale,
    vwap,
    windows,
)
from .base import REGISTRY, LevelContext, LevelSpec, assert_total, resolve_order
from .scale import atr_percentile_frame, daily_atr_frame, distance_in_atr
from .session import add_session_columns, session_valid

#: Levels computed by default, in declaration order (dependencies resolved separately).
DEFAULT_LEVELS = [
    "barshape",
    "ema",
    "ema_stack",
    "rsi",
    "bar_return",
    "true_range",
    "atr_intraday",
    "vwap",
    "opening_range",
    "prior_session",
    "rvol_tod",
    "volume_profile",
    # Indicators the imported published strategies read. Listed after the originals so
    # the existing levels compute in exactly the order they always did.
    "donchian",
    "year_extremes",
    "trend_averages",
    "horizon_returns",
    "bollinger",
    "narrow_range",
    "rsi_2",
    "lbr_rsi",
    "session_anchor",
    "noise_band",
    "stretch",
    "prior_session_momentum",
]

#: Intraday-scale levels: measured in INTRADAY ATR, which answers "how many stop-widths
#: away is this" -- the number that matters when sizing a trade on this timeframe.
DISTANCE_LEVELS = [
    "vwap",
    "vwap_upper_1s",
    "vwap_lower_1s",
    "vwap_upper_2s",
    "vwap_lower_2s",
    "or5_high", "or5_low", "or15_high", "or15_low", "or30_high", "or30_low",
    "poc",
]

#: Day-scale levels: measured in DAILY ATR. Measuring a prior-day level against a 1m
#: ATR gives ~20 units and against a 1h ATR gives ~2, purely because of the ATR's
#: timescale. In daily ATR the distribution is identical across every timeframe
#: (p50 0.35 on all four), which is what makes the number comparable and the threshold
#: meaningful.
DAILY_DISTANCE_LEVELS = [
    "prior_day_high", "prior_day_low", "prior_day_close",
]

#: Auxiliary columns the engine adds but never masks -- they are bookkeeping, always
#: well defined, and nulling them would hide why a level was masked.
AUX_COLUMNS = frozenset(
    {
        "gap", "run_id", "session_broken", "bar_idx_in_session", "session_start_ms",
        "hole_ms", "valid_prior_sessions", "ms_since_open", "tod_ms",
        "clean_tr_in_run", "typical_price", "bar_range", "atr_daily", "atr_pct_60d",
    }
)


@dataclass(frozen=True)
class LevelFrame:
    df: pl.DataFrame
    symbol: str
    timeframe: str
    as_of_ms: int
    computed: tuple[str, ...]

    def to_polars(self) -> pl.DataFrame:
        return self.df

    def at(self, bar_open_ms: int) -> dict:
        row = self.df.filter(pl.col("bar_open_ms") == bar_open_ms)
        return row.to_dicts()[0] if not row.is_empty() else {}

    def last(self) -> dict:
        return self.df.to_dicts()[-1] if not self.df.is_empty() else {}


def _keep_mask(spec: LevelSpec, df: pl.DataFrame, bf: BarFrame) -> pl.Series | None:
    if spec.kind == "per_bar":
        return None
    if spec.kind == "rolling":
        return bf.window_mask(spec.depth)
    if spec.kind == "session":
        return (~df["session_broken"]).rename("keep")
    if spec.kind == "cross_session":
        return (df["valid_prior_sessions"] >= spec.depth).rename("keep")
    raise ValueError(f"unhandled kind {spec.kind!r}")


def compute_levels(
    bf: BarFrame,
    cfg: Config,
    *,
    levels: list[str] | None = None,
) -> LevelFrame:
    """Compute every requested level, causally and with contiguity enforced."""
    names = list(levels) if levels else list(DEFAULT_LEVELS)
    specs = resolve_order(names)

    df = bf.to_polars()
    if df.is_empty():
        return LevelFrame(df, bf.symbol, bf.timeframe, bf.as_of_ms, tuple(names))

    df = df.sort("bar_open_ms")
    df = add_session_columns(
        df, timeframe=bf.timeframe, outage_minutes=cfg.quality.outage_minutes
    )

    # Count of VALID sessions strictly before each bar's session. Strictly prior, so a
    # cross-session level can never see its own session.
    sessions = session_valid(df)
    sessions = sessions.with_columns(
        pl.col("valid").cast(pl.Int32).cum_sum().shift(1).fill_null(0).alias(
            "valid_prior_sessions"
        )
    )
    df = df.join(
        sessions.select("session_date", "valid_prior_sessions"),
        on="session_date",
        how="left",
    )

    # Daily ATR and its percentile live at session granularity (about 1,463 rows), then
    # join back onto bars. Both are shifted so they only ever reflect completed sessions.
    period = int(cfg.backtest.get("atr_period", 14))
    daily = daily_atr_frame(sessions, period=period)
    daily = atr_percentile_frame(daily, lookback=60)
    df = df.join(
        daily.select("session_date", "atr_daily", "atr_pct_60d"),
        on="session_date",
        how="left",
    )

    ctx = LevelContext(
        df=df, symbol=bf.symbol, timeframe=bf.timeframe,
        tf_ms=_tf_ms(bf.timeframe), config=cfg,
    )

    for spec in specs:
        ctx.df = spec.fn(ctx)
        keep = _keep_mask(spec, ctx.df, bf)
        if keep is None:
            continue
        maskable = [c for c in spec.outputs if c not in AUX_COLUMNS and c in ctx.df.columns]
        if not maskable:
            continue
        ctx.df = ctx.df.with_columns(
            [
                pl.when(pl.Series("keep", keep)).then(pl.col(c)).otherwise(None).alias(c)
                for c in maskable
            ]
        )

    ctx.df = _add_distances(ctx.df)
    ctx.df = extreme_distance_flags(ctx.df, cfg, bf.timeframe)
    assert_total(ctx.df, context=f"{bf.symbol} {bf.timeframe}")
    return LevelFrame(ctx.df, bf.symbol, bf.timeframe, bf.as_of_ms, tuple(names))


def _add_distances(df: pl.DataFrame) -> pl.DataFrame:
    price = pl.col("close")
    exprs = [
        distance_in_atr(price, name, "atr_intraday").alias(f"dist_{name}_atr")
        for name in DISTANCE_LEVELS
        if name in df.columns
    ]
    exprs += [
        distance_in_atr(price, name, "atr_daily").alias(f"dist_{name}_datr")
        for name in DAILY_DISTANCE_LEVELS
        if name in df.columns and "atr_daily" in df.columns
    ]
    return df.with_columns(exprs) if exprs else df


def extreme_distance_flags(
    df: pl.DataFrame, cfg: Config, timeframe: str
) -> pl.DataFrame:
    """Bars whose distance to some level is beyond the calibrated threshold.

    Flagged, never capped: a tiny-but-positive ATR yields a large FINITE distance, which
    is honest. Capping would fabricate a value. Phase 3 filters on this rather than the
    engine hiding it.
    """
    thresholds = cfg.levels.get("extreme_distance_atr", {})
    intra = float(thresholds.get(timeframe, 25.0)) if isinstance(thresholds, dict) else float(thresholds)
    daily = float(cfg.levels.get("extreme_distance_daily_atr", 4.0))

    intra_cols = [f"dist_{n}_atr" for n in DISTANCE_LEVELS if f"dist_{n}_atr" in df.columns]
    daily_cols = [f"dist_{n}_datr" for n in DAILY_DISTANCE_LEVELS if f"dist_{n}_datr" in df.columns]
    if not intra_cols and not daily_cols:
        return df.with_columns(pl.lit(False).alias("extreme_distance"))

    conds = [pl.col(c).abs() > intra for c in intra_cols]
    conds += [pl.col(c).abs() > daily for c in daily_cols]
    combined = conds[0]
    for c in conds[1:]:
        combined = combined | c
    return df.with_columns(combined.fill_null(False).alias("extreme_distance"))
