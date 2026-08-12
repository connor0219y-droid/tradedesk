"""Calendar-window indicators for the imported published strategies.

WINDOWS ARE MEASURED IN TIME, NOT IN ROWS. A "20-day high" in the Turtle rules is the
high of the last twenty days. Written the obvious way -- `rolling_max(window_size=N)`
where N is 20 x bars-per-day -- it becomes "the high of the last N bars", which is the
same thing only if no bar is ever missing. On this venue bars are missing: the quality
gate admits up to 5% absent, and a 20-day window on 5m bars spans 5,760 rows, so a
percent of absence silently stretches the window by days. Every window here therefore
uses polars' `rolling_*_by`, which takes an actual duration and is exact under absence.

`closed="left"` where the source says "the PRECEDING n days". A Donchian breakout
compares today's price against the high of the previous twenty days; including the
current bar in its own maximum makes the breakout condition `high >= high`, which is
either always or never true and in both cases measures nothing.

WHY THESE ARE NOT RUN-RESET, WHEN ATR IS. `scale.py` restarts Wilder smoothing at every
contiguity break, because an EWMA has infinite memory and one contaminated True Range
decays through every later value. A maximum, a mean or an n-day return over a long
window has no such memory: a six-hour hole inside a twenty-day window removes a few
observations from the sample but does not corrupt the statistic -- the highest price
that actually traded in those twenty days is still the highest price that actually
traded. Resetting them per run would instead be actively harmful, because a 52-week
window would be null for a year after each of the eleven breaks in the store, which
would delete the strategies rather than test them.

The bar-count indicators -- RSI(2), the LBR/RSI, and the narrow-range flags -- ARE
run-reset, because they are Wilder averages or short row-windows where a hole genuinely
changes what is being measured. That difference is the whole reason they live in
separate functions here rather than one loop.
"""

from __future__ import annotations

import polars as pl

from .base import LevelContext, level, safe_div
from .scale import seeded_average

#: Donchian lookbacks, in days. 20 and 55 are the Turtle System 1 and System 2 windows.
#: 2 and 3 exist only to date the previous extreme: "the previous 20-day low must have
#: occurred at least four sessions earlier" is exactly "the 20-day low is below the
#: 3-day low", and Turtle Soup Plus One's three-session version is the 2-day form.
DONCHIAN_DAYS: tuple[int, ...] = (2, 3, 20, 55)

#: 52 weeks, as days. George & Hwang's measure is the 52-week high.
YEAR_DAYS = 364

#: Bollinger's own defaults: a 20-period average, two standard deviations, and a squeeze
#: measured against the narrowest band width of the last 125 periods (~6 months daily).
BB_DAYS = 20
BB_SIGMA = 2.0
BB_SQUEEZE_DAYS = 125

#: Connors' trend filter and exit average, in days.
SMA_TREND_DAYS = 200
SMA_EXIT_DAYS = 5

#: Long windows are computed only at 1h and slower. No detector that reads them is
#: evaluated intraday (see PREREGISTRATION.md), and a 364-day window over 2.1M 1m bars
#: costs real time for a column nothing would consult. Null with a reason, never a
#: quietly shortened window.
LONG_WINDOW_MIN_TF_MS = 3_600_000

MS_PER_DAY = 86_400_000


def _ts(df: pl.DataFrame) -> pl.DataFrame:
    """A real datetime column, which `rolling_*_by` requires as its ordering key."""
    return df.with_columns(
        pl.from_epoch(pl.col("bar_open_ms"), time_unit="ms").alias("_ts")
    )


def _nulls(*names: str) -> list[pl.Expr]:
    return [pl.lit(None, dtype=pl.Float64).alias(n) for n in names]


@level(
    name="donchian",
    kind="rolling",
    depth=2,
    outputs=tuple(
        c for d in DONCHIAN_DAYS for c in (f"dc{d}_high", f"dc{d}_low")
    ),
)
def _donchian(ctx: LevelContext) -> pl.DataFrame:
    """Highest high and lowest low of the preceding n days, excluding this bar.

    Declared depth is 2, not the window length. The engine's contiguity mask asks "were
    the last `depth` bars adjacent", and the only adjacency this level actually needs is
    the previous bar -- every detector reading these columns compares bar `t` against
    bar `t-1` to make a breakout a fresh event. Requiring 55 days of unbroken history
    would null the column across most of the store for no gain in correctness; see the
    module docstring on why a maximum tolerates holes and an EWMA does not.
    """
    df = _ts(ctx.df)
    exprs: list[pl.Expr] = []
    for days in DONCHIAN_DAYS:
        window = f"{days}d"
        exprs += [
            pl.col("high")
            .rolling_max_by("_ts", window_size=window, closed="left")
            .alias(f"dc{days}_high"),
            pl.col("low")
            .rolling_min_by("_ts", window_size=window, closed="left")
            .alias(f"dc{days}_low"),
        ]
    return df.with_columns(exprs).drop("_ts")


@level(
    name="year_extremes",
    kind="rolling",
    depth=2,
    outputs=("hh_52w", "ll_52w"),
)
def _year_extremes(ctx: LevelContext) -> pl.DataFrame:
    """52-week high and low of the preceding year, excluding this bar."""
    if ctx.tf_ms < LONG_WINDOW_MIN_TF_MS:
        return ctx.df.with_columns(_nulls("hh_52w", "ll_52w"))
    df = _ts(ctx.df)
    return df.with_columns(
        pl.col("high")
        .rolling_max_by("_ts", window_size=f"{YEAR_DAYS}d", closed="left")
        .alias("hh_52w"),
        pl.col("low")
        .rolling_min_by("_ts", window_size=f"{YEAR_DAYS}d", closed="left")
        .alias("ll_52w"),
    ).drop("_ts")


@level(
    name="trend_averages",
    kind="rolling",
    depth=2,
    outputs=("sma_5d", "sma_200d"),
)
def _trend_averages(ctx: LevelContext) -> pl.DataFrame:
    """Simple moving averages over 5 and 200 days of closes.

    `closed="right"` because an average that excluded the current bar would not be the
    average Connors' rule compares the close against.
    """
    df = _ts(ctx.df)
    short = pl.col("close").rolling_mean_by(
        "_ts", window_size=f"{SMA_EXIT_DAYS}d", closed="right"
    ).alias("sma_5d")
    if ctx.tf_ms < LONG_WINDOW_MIN_TF_MS:
        return df.with_columns(short, *_nulls("sma_200d")).drop("_ts")
    return df.with_columns(
        short,
        pl.col("close")
        .rolling_mean_by("_ts", window_size=f"{SMA_TREND_DAYS}d", closed="right")
        .alias("sma_200d"),
    ).drop("_ts")


@level(
    name="horizon_returns",
    kind="rolling",
    depth=2,
    outputs=("ret_1w", "ret_12m"),
)
def _horizon_returns(ctx: LevelContext) -> pl.DataFrame:
    """Trailing 1-week and 12-month returns, referenced to a real point in time.

    The reference close is found by an as-of join against the frame's own history rather
    than by shifting a fixed number of rows. A row shift assumes no bar is ever missing;
    where one is, `close.shift(k)` silently reaches to a different date, and a 12-month
    momentum signal that is actually measuring 11 months and change is exactly the sort
    of error that never announces itself.
    """
    df = _ts(ctx.df).with_columns(pl.col("close").alias("_ref_close"))
    hist = df.select("_ts", "_ref_close").sort("_ts")

    out = df
    for label, days in (("ret_1w", 7), ("ret_12m", 365)):
        if days > 30 and ctx.tf_ms < LONG_WINDOW_MIN_TF_MS:
            out = out.with_columns(_nulls(label))
            continue
        keyed = out.with_columns(
            (pl.col("_ts") - pl.duration(days=days)).alias("_asof")
        ).sort("_asof")
        # `strategy="backward"` takes the last close at or before the reference instant,
        # which is what "the price a year ago" means when that exact bar is absent.
        joined = keyed.join_asof(
            hist.rename({"_ts": "_hist_ts", "_ref_close": "_past"}),
            left_on="_asof",
            right_on="_hist_ts",
            strategy="backward",
        ).sort("_ts")
        out = joined.with_columns(
            safe_div(
                pl.col("close") - pl.col("_past"), pl.col("_past"), when_zero=None
            ).alias(label)
        ).drop("_asof", "_hist_ts", "_past")
    return out.drop("_ts", "_ref_close")


@level(
    name="bollinger",
    kind="rolling",
    depth=2,
    outputs=("bb_mid", "bb_upper", "bb_lower", "bb_width", "bb_width_min_125d"),
)
def _bollinger(ctx: LevelContext) -> pl.DataFrame:
    """Bollinger bands (20 days, 2 sigma) and the squeeze reference width.

    `bb_width` is the band width as a fraction of the middle band, which is Bollinger's
    own BandWidth. `bb_width_min_125d` is its lowest value over the preceding 125 days,
    excluding the current bar -- the squeeze test is "is width now at the low end of its
    six-month range", and comparing width against a window containing itself would make
    the comparison trivially true at every new low.
    """
    df = _ts(ctx.df)
    mid = pl.col("close").rolling_mean_by("_ts", window_size=f"{BB_DAYS}d", closed="right")
    sd = pl.col("close").rolling_std_by("_ts", window_size=f"{BB_DAYS}d", closed="right")
    df = df.with_columns(mid.alias("bb_mid"), sd.alias("_bb_sd"))
    df = df.with_columns(
        (pl.col("bb_mid") + BB_SIGMA * pl.col("_bb_sd")).alias("bb_upper"),
        (pl.col("bb_mid") - BB_SIGMA * pl.col("_bb_sd")).alias("bb_lower"),
    )
    df = df.with_columns(
        safe_div(
            pl.col("bb_upper") - pl.col("bb_lower"), pl.col("bb_mid"), when_zero=None
        ).alias("bb_width")
    )
    if ctx.tf_ms < LONG_WINDOW_MIN_TF_MS:
        return df.with_columns(_nulls("bb_width_min_125d")).drop("_ts", "_bb_sd")
    return df.with_columns(
        pl.col("bb_width")
        .rolling_min_by("_ts", window_size=f"{BB_SQUEEZE_DAYS}d", closed="left")
        .alias("bb_width_min_125d")
    ).drop("_ts", "_bb_sd")


@level(
    name="narrow_range",
    kind="rolling",
    depth=7,
    outputs=("nr4", "nr7", "inside_bar"),
)
def _narrow_range(ctx: LevelContext) -> pl.DataFrame:
    """Crabel's NR4 and NR7: this bar's range is the narrowest of the last 4 or 7 bars.

    These are BAR counts, not calendar windows -- Crabel's NR7 is the narrowest of seven
    bars, and on a daily chart that is seven days only because a daily bar is a day. So
    the declared depth is the real window and the engine's contiguity mask applies in
    full: a "narrowest of seven" spanning a venue outage is comparing six bars against a
    hole.

    Emitted as Float64 0.0/1.0 rather than Boolean because `assert_total` and the level
    frame's null handling are written for floats; the detectors compare against 1.0.
    """
    rng = pl.col("high") - pl.col("low")
    prior_range = rng.shift(1)
    return ctx.df.with_columns(
        (rng <= rng.rolling_min(window_size=4, min_samples=4))
        .cast(pl.Float64)
        .alias("nr4"),
        (rng <= rng.rolling_min(window_size=7, min_samples=7))
        .cast(pl.Float64)
        .alias("nr7"),
        (
            (pl.col("high") <= pl.col("high").shift(1))
            & (pl.col("low") >= pl.col("low").shift(1))
            & prior_range.is_not_null()
        )
        .cast(pl.Float64)
        .alias("inside_bar"),
    )


def _wilder_rsi(
    df: pl.DataFrame, change: pl.Expr, *, period: int, out: str
) -> pl.DataFrame:
    """Wilder's RSI over an arbitrary change series, reset at contiguity breaks.

    Factored out of `momentum.py`'s RSI(14) rather than copied: the gap-nulling and the
    null-preserving split into gains and losses are the subtle parts, and two copies of
    them drift. Written in the `100 * gain / (gain + loss)` form for the same reason as
    there -- the textbook form divides by average loss, which is genuinely zero on any
    window that never ticked down.
    """
    g, l_, ag, al = f"_{out}_g", f"_{out}_l", f"_{out}_ag", f"_{out}_al"
    df = df.with_columns(
        pl.when(pl.col("gap")).then(None).otherwise(change).alias(f"_{out}_chg")
    )
    chg = pl.col(f"_{out}_chg")
    df = df.with_columns(
        pl.when(chg.is_null()).then(None)
        .when(chg > 0).then(chg)
        .otherwise(pl.lit(0.0)).alias(g),
        pl.when(chg.is_null()).then(None)
        .when(chg < 0).then(-chg)
        .otherwise(pl.lit(0.0)).alias(l_),
    )
    df = seeded_average(df, g, period=period, alpha=1.0 / period, out=ag)
    df = seeded_average(df, l_, period=period, alpha=1.0 / period, out=al)
    return df.with_columns(
        safe_div(pl.col(ag) * 100.0, pl.col(ag) + pl.col(al), when_zero=None).alias(out)
    ).drop(f"_{out}_chg", g, l_, ag, al)


@level(name="rsi_2", kind="rolling", depth=2, outputs=("rsi_2",))
def _rsi_2(ctx: LevelContext) -> pl.DataFrame:
    """Wilder's RSI(2), the Connors mean-reversion trigger.

    A 2-period RSI is extremely fast by design -- that is the point of the strategy -- so
    the SMA seeding and the per-run reset matter proportionally more here than at 14.
    """
    return _wilder_rsi(ctx.df, pl.col("close") - pl.col("close").shift(1),
                       period=2, out="rsi_2")


@level(name="lbr_rsi", kind="rolling", depth=3, outputs=("lbr_rsi3",))
def _lbr_rsi(ctx: LevelContext) -> pl.DataFrame:
    """The LBR/RSI: a 3-period RSI of the 1-period rate of change.

    Raschke and Connors describe it as "a three-period RSI of a one-period rate of
    change (the daily net change)" -- a study on a study. So the series fed to the RSI
    is `close - close[-1]`, and the RSI's own differencing then operates on THAT, giving
    the second difference of price. Stated explicitly because the natural misreading --
    running RSI(3) on price directly -- is a different and much slower indicator.
    """
    roc = pl.col("close") - pl.col("close").shift(1)
    return _wilder_rsi(ctx.df, roc - roc.shift(1), period=3, out="lbr_rsi3")
