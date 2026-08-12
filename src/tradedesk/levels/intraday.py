"""Session-anchored levels for the imported intraday strategies.

Everything here is anchored to 00:00 ET, the same synthetic session boundary the rest of
the project uses for crypto. That anchor is a modelling choice, not a market fact: SPY
has an opening bell and BTC does not, so "the first half-hour return" is the first half
hour of an ET calendar day rather than of a trading session that actually starts. Where
a source's rule depends on there being a real open -- Zarattini's overnight-gap
adjustment, which shifts the noise band by the distance between yesterday's close and
today's open -- the term is DROPPED rather than approximated, because on a 24/7 venue
the gap it corrects for does not exist. That is noted per level below and in
PREREGISTRATION.md; it makes these tests of the rule's shape on crypto, not replications.

THE LOOKAHEAD TRAP, again. A "first half-hour return" attached to every bar of the
session, including the bars inside that first half hour, is lookahead: at 00:05 you do
not know the 00:00-00:30 return. As in `opening_range.py`, every level here is null
while its window is still running and freezes once the window closes.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div
from .opening_range import add_et_midnight

#: Gao, Han, Li & Zhou measure the first and last half-hour of the session.
HALF_HOUR_MS = 1_800_000
#: Raschke & Connors' Momentum Pinball enters on a break of the first hour's range.
HOUR_MS = 3_600_000
#: Crabel averages the stretch over the last 10 sessions.
STRETCH_SESSIONS = 10
#: Zarattini's noise band averages the move-from-open over the last 14 sessions.
NOISE_SESSIONS = 14

MS_PER_DAY = 86_400_000


def _with_tod(df: pl.DataFrame) -> pl.DataFrame:
    """Milliseconds since 00:00 ET, recomputed here rather than assumed.

    `context.py` does the same. Depending on another level's aux column would make the
    ordering load-bearing and silent when it broke.
    """
    df = add_et_midnight(df)
    return df.with_columns(
        (pl.col("bar_open_ms") - pl.col("et_midnight_ms")).alias("_tod")
    ).drop("et_midnight_ms")


@level(
    name="session_anchor",
    kind="session",
    depth=1,
    outputs=("session_open", "ms_to_session_end", "ret_first30m", "first_hour_high",
             "first_hour_low"),
)
def _session_anchor(ctx: LevelContext) -> pl.DataFrame:
    """The session's opening price, its remaining time, and its first-window summaries.

    `ms_to_session_end` counts from the END of this bar, which is what a rule phrased as
    "at the beginning of the last half hour" needs: the engine enters at the next bar's
    open, so the bar that must carry the signal is the one whose close leaves exactly
    thirty minutes in the session.

    `session_open` is the open of the session's first PRESENT bar. If a session's first
    bars are absent it is therefore not the 00:00 price -- but a level that invented the
    00:00 price would be worse, and `session_broken` already masks the sessions where
    this matters.
    """
    df = _with_tod(ctx.df)
    tod = pl.col("_tod")

    df = df.with_columns(
        pl.col("open").first().over("session_date").alias("session_open"),
        (MS_PER_DAY - (tod + ctx.tf_ms)).alias("ms_to_session_end"),
    )

    # A thirty-minute window is not representable on a four-hour bar. Computed anyway,
    # `ret_first30m` would be the first FOUR HOURS' return wearing a label that says
    # thirty minutes -- the most dangerous kind of wrong, because every downstream
    # number stays plausible. Null with a reason instead, exactly as `opening_range`
    # nulls a 5-minute range on 15m bars.
    too_coarse_30 = ctx.tf_ms > HALF_HOUR_MS
    too_coarse_60 = ctx.tf_ms > HOUR_MS
    if too_coarse_30 and too_coarse_60:
        return df.with_columns(
            pl.lit(None, dtype=pl.Float64).alias("ret_first30m"),
            pl.lit(None, dtype=pl.Float64).alias("first_hour_high"),
            pl.lit(None, dtype=pl.Float64).alias("first_hour_low"),
        ).drop("_tod")

    in_half = tod < HALF_HOUR_MS
    in_hour = tod < HOUR_MS

    # Frozen at the window's close: the last in-window close, forward-filled, and null
    # for every bar still inside the window.
    df = df.with_columns(
        pl.when(in_half).then(pl.col("close")).otherwise(None)
        .forward_fill().over("session_date").alias("_c30"),
        pl.when(in_hour).then(pl.col("high")).otherwise(None)
        .cum_max().over("session_date")
        .forward_fill().over("session_date").alias("_h60"),
        pl.when(in_hour).then(pl.col("low")).otherwise(None)
        .cum_min().over("session_date")
        .forward_fill().over("session_date").alias("_l60"),
    )

    return df.with_columns(
        pl.when(in_half | too_coarse_30)
        .then(None)
        .otherwise(
            safe_div(
                pl.col("_c30") - pl.col("session_open"),
                pl.col("session_open"),
                when_zero=None,
            )
        )
        .alias("ret_first30m"),
        # The first hour's range is not knowable until the hour is over. Inside it the
        # running cum_max above is correct but premature, so it is withheld.
        pl.when(in_hour | too_coarse_60).then(None).otherwise(pl.col("_h60"))
        .alias("first_hour_high"),
        pl.when(in_hour | too_coarse_60).then(None).otherwise(pl.col("_l60"))
        .alias("first_hour_low"),
    ).drop("_tod", "_c30", "_h60", "_l60")


@level(
    name="noise_band",
    kind="cross_session",
    depth=NOISE_SESSIONS,
    outputs=("noise_upper", "noise_lower"),
    requires=("session_anchor",),
)
def _noise_band(ctx: LevelContext) -> pl.DataFrame:
    """Zarattini's intraday-momentum noise area.

    The band is the session open times one plus and minus the average absolute move from
    the open observed at THIS TIME OF DAY over the previous 14 sessions. The
    time-of-day conditioning is the substance of it: markets move further from the open
    by hour eight than by hour one, so a single daily average would put the boundary in
    the wrong place at every hour but one.

    Built the same way `rvol_tod` builds its baseline -- sort by time of day, take a
    rolling mean of STRICTLY PRIOR sessions within that time-of-day group. The shift(1)
    is what makes it strictly prior; without it, today's own move sits inside the band
    that is supposed to decide whether today's move is unusual, which is circular and
    would understate every breakout.

    The paper's overnight-gap adjustment is deliberately absent; see the module
    docstring. Sessions that broke contribute nothing to the average.
    """
    df = _with_tod(ctx.df)
    df = df.with_columns(
        pl.when(pl.col("session_broken"))
        .then(None)
        .otherwise(
            safe_div(
                (pl.col("close") - pl.col("session_open")).abs(),
                pl.col("session_open"),
                when_zero=None,
            )
        )
        .alias("_move")
    )

    df = df.sort(["_tod", "bar_open_ms"])
    df = df.with_columns(
        pl.col("_move")
        .shift(1)
        .rolling_mean(window_size=NOISE_SESSIONS, min_samples=NOISE_SESSIONS)
        .over("_tod")
        .alias("_sigma")
    )
    df = df.sort("bar_open_ms")

    return df.with_columns(
        (pl.col("session_open") * (1.0 + pl.col("_sigma"))).alias("noise_upper"),
        (pl.col("session_open") * (1.0 - pl.col("_sigma"))).alias("noise_lower"),
    ).drop("_tod", "_move", "_sigma")


@level(
    name="stretch",
    kind="cross_session",
    depth=STRETCH_SESSIONS,
    outputs=("stretch",),
)
def _stretch(ctx: LevelContext) -> pl.DataFrame:
    """Crabel's stretch: the 10-session mean of the smaller of the two open-side moves.

    For each prior session, take `min(high - open, open - low)` -- the distance price
    travelled on the QUIETER side of the open -- and average ten of them. Crabel's
    opening-range breakout then triggers at `open +/- stretch`, so the trigger is
    calibrated to how far this instrument routinely moves against itself before going
    anywhere.

    Strictly prior sessions only, and only unbroken ones: a session with a six-hour hole
    has an understated quiet-side move, which would tighten the trigger on the following
    days for a reason that has nothing to do with volatility.
    """
    from .session import session_valid

    sessions = session_valid(ctx.df)
    quiet = pl.min_horizontal(
        pl.col("s_high") - pl.col("s_open"), pl.col("s_open") - pl.col("s_low")
    )
    sessions = sessions.with_columns(
        pl.when(pl.col("valid")).then(quiet).otherwise(None).alias("_quiet")
    )
    prior = sessions.select(
        "session_date",
        pl.col("_quiet")
        .shift(1)
        .rolling_mean(window_size=STRETCH_SESSIONS, min_samples=STRETCH_SESSIONS)
        .alias("stretch"),
    )
    return ctx.df.join(prior, on="session_date", how="left")


@level(
    name="prior_session_momentum",
    kind="cross_session",
    depth=1,
    outputs=("prior_day_lbr_rsi3",),
    requires=("lbr_rsi",),
)
def _prior_session_momentum(ctx: LevelContext) -> pl.DataFrame:
    """The LBR/RSI as it stood at the prior session's last bar.

    Momentum Pinball is a two-day setup: day one's closing indicator decides the
    direction, day two supplies the entry. Reading the CURRENT bar's LBR/RSI instead
    would collapse the two days into one and test a different rule -- one that is also
    much easier to satisfy, since the indicator is by then reacting to the same move the
    entry is trying to catch.
    """
    per_session = (
        ctx.df.group_by("session_date")
        .agg(
            pl.col("lbr_rsi3").last().alias("_last"),
            pl.col("session_broken").last().alias("_broken"),
        )
        .sort("session_date")
    )
    per_session = per_session.with_columns(
        pl.when(pl.col("_broken")).then(None).otherwise(pl.col("_last")).alias("_ok")
    )
    prior = per_session.select(
        "session_date", pl.col("_ok").shift(1).alias("prior_day_lbr_rsi3")
    )
    return ctx.df.join(prior, on="session_date", how="left")
