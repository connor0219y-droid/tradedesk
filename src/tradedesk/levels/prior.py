"""Prior-session levels, and the premarket columns that crypto deliberately leaves null.

A prior day's high/low/close is judged as a WHOLE session, unlike an intra-session level
which is judged at each bar. The reason is how it gets consumed: downstream it is a
single summary number for the day, so a session containing a venue outage produces a
high or low that never really traded through, and must not be offered as a level.

Premarket (04:00-09:30 ET) is an equities concept. Crypto trades continuously, so there
is no premarket and the columns are null -- present in the schema for when Alpaca lands,
but never invented for crypto. Emitting the overnight range under a 'premarket' label
would be a fabricated level, and every downstream statistic sliced on it would be
measuring nothing.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level


@level(
    name="prior_session",
    kind="cross_session",
    depth=1,
    outputs=("prior_day_high", "prior_day_low", "prior_day_close",
             "premarket_high", "premarket_low"),
)
def _prior_session(ctx: LevelContext) -> pl.DataFrame:
    from .session import session_valid

    sessions = session_valid(ctx.df)

    # Only unbroken sessions may supply a prior-day level.
    sessions = sessions.with_columns(
        pl.when(pl.col("valid")).then(pl.col("s_high")).otherwise(None).alias("_h"),
        pl.when(pl.col("valid")).then(pl.col("s_low")).otherwise(None).alias("_l"),
        pl.when(pl.col("valid")).then(pl.col("s_close")).otherwise(None).alias("_c"),
    )
    prior = sessions.select(
        "session_date",
        pl.col("_h").shift(1).alias("prior_day_high"),
        pl.col("_l").shift(1).alias("prior_day_low"),
        pl.col("_c").shift(1).alias("prior_day_close"),
    )

    return ctx.df.join(prior, on="session_date", how="left").with_columns(
        # Crypto has no premarket. Null, never invented.
        pl.lit(None, dtype=pl.Float64).alias("premarket_high"),
        pl.lit(None, dtype=pl.Float64).alias("premarket_low"),
    )
