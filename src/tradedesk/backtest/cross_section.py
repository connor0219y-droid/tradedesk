"""Cross-sectional backtesting: rank a universe, hold the extremes, rebalance.

WHY THIS IS A SEPARATE ENGINE. `engine.py` takes one instrument, one position at a time,
an ATR stop and an R-multiple target. None of those concepts exist here. A cross-sectional
strategy holds a hundred positions simultaneously, sizes them by rank rather than by risk,
has no stop at all, and exits only because the calendar said to rebalance. Forcing it
through the event engine would not be a port, it would be a different strategy.

This is also the reason findings 1-8 could not test Jegadeesh-Titman, the cross-sectional
leg of George-Hwang, or Liu-Tsyvinski's quintile sorts: three crypto symbols cannot
support a quintile, and the machinery to hold a portfolio did not exist. Both problems
are fixed by the equity data, so the exclusion in PREREGISTRATION.md is lifted here.

THE NULL IS THE POINT, exactly as it is for the event engine. A long-short quintile
portfolio on 500 names produces a smooth-looking equity curve whether or not the ranking
signal means anything, because it is 200 positions of diversified market-neutral noise.
So every strategy is compared against RANDOM PORTFOLIOS drawn the same way: same number
of names, same rebalance dates, same holding period, same costs, ranks shuffled. If the
signal cannot beat a coin flip that holds the same number of names on the same days, it
is not a signal.

WHAT IS MEASURED. Returns here are simple period returns on a portfolio, not R-multiples
-- there is no stop, so there is no R. That makes these numbers not directly comparable
with findings 1-8, and the reports say so rather than putting them in the same column.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date

import polars as pl

from .costs import CostModel

#: Trading days per calendar month and year, the conventional approximations. Used to
#: turn a source's "12 months" into a bar count on a daily grid.
DAYS_PER_MONTH = 21
DAYS_PER_YEAR = 252


#: Ranking variables this engine knows how to build. Each is computed so that RANKING
#: ASCENDING puts the strategy's disfavoured names at the bottom, which keeps `sign`
#: meaning the same thing for every strategy.
SIGNAL_KINDS = frozenset({"return", "nearness_52w", "realised_vol"})


class CrossSectionError(Exception):
    """A cross-sectional strategy is misdeclared, or the panel cannot support it."""


@dataclass(frozen=True)
class CrossSectionalSpec:
    """One cross-sectional strategy, specified the way its source specifies it."""

    name: str
    source: str
    #: Formation window, in trading days, ending `skip_days` before the rebalance.
    lookback_days: int
    #: Days between the end of the formation window and the rebalance. Jegadeesh and
    #: Titman skip the most recent month to avoid the short-term reversal effect
    #: contaminating a momentum signal; a spec that forgets this measures both at once.
    skip_days: int = DAYS_PER_MONTH
    #: 5 for quintiles, 10 for deciles.
    quantiles: int = 5
    #: Trading days between rebalances.
    rebalance_days: int = DAYS_PER_MONTH
    #: +1 goes long the top rank (momentum), -1 goes long the bottom (reversal).
    sign: int = 1
    #: Minimum names required at a rebalance for that date to be used at all.
    min_names: int = 50
    #: What the names are RANKED ON. Most sources rank on a trailing return, but not
    #: all: George & Hwang rank on nearness to the 52-week high, and the low-volatility
    #: literature ranks on realised volatility. Carried on the spec rather than applied
    #: by the caller, so a strategy cannot be run against a ranking variable its source
    #: never used.
    signal_kind: str = "return"

    def __post_init__(self) -> None:
        if self.quantiles < 2:
            raise CrossSectionError(f"{self.name}: need at least 2 quantiles")
        if self.lookback_days < 1 or self.rebalance_days < 1:
            raise CrossSectionError(f"{self.name}: windows must be positive")
        if self.sign not in (1, -1):
            raise CrossSectionError(f"{self.name}: sign must be +1 or -1")
        if self.signal_kind not in SIGNAL_KINDS:
            raise CrossSectionError(
                f"{self.name}: unknown signal_kind {self.signal_kind!r}; "
                f"known: {sorted(SIGNAL_KINDS)}"
            )


@dataclass
class CrossSectionalResult:
    spec: str
    n_periods: int
    #: Mean per-period return of the long-short portfolio, gross and net of costs.
    gross_mean: float
    net_mean: float
    gross_t: float
    turnover: float
    #: Fraction of random-rank portfolios that matched or beat the observed gross mean.
    p_value: float | None
    null_mean: float | None
    null_low: float | None
    null_high: float | None
    periods: list[tuple[date, float, float]] = field(default_factory=list)
    n_names_median: int = 0

    @property
    def beats_random(self) -> bool:
        return self.p_value is not None and self.p_value < 0.05


def build_panel(frames: dict[str, pl.DataFrame]) -> pl.DataFrame:
    """One long frame of daily closes: {symbol, session_date, close}.

    Takes a dict of per-symbol daily frames because that is how the store hands them
    over, and because building it symbol-by-symbol keeps each name's contiguity handling
    where it already works.
    """
    parts = [
        df.select(
            pl.lit(sym).alias("symbol"),
            pl.col("session_date"),
            pl.col("close"),
        )
        for sym, df in frames.items()
        if not df.is_empty()
    ]
    if not parts:
        return pl.DataFrame(
            schema={"symbol": pl.String, "session_date": pl.Date, "close": pl.Float64}
        )
    return pl.concat(parts).sort(["symbol", "session_date"])


def _signal_frame(panel: pl.DataFrame, spec: CrossSectionalSpec) -> pl.DataFrame:
    """Formation return per symbol per date, and the forward return it predicts.

    THE SKIP IS APPLIED TO THE SIGNAL, NOT THE HOLDING. The formation window ends
    `skip_days` before the rebalance, so a 12-1 momentum signal on date t is the return
    from t-252 to t-21. Shifting the holding period instead would leave the signal
    touching the rebalance date, which is the contamination the skip exists to prevent.

    `fwd` is the return actually earned: close at the NEXT rebalance over close at this
    one. It is computed by shifting BACKWARD within each symbol, which is the one place
    in this file where a negative shift is correct rather than a lookahead bug -- the
    value is attached to the date the position is opened, and consumed only as the
    outcome of that position.
    """
    c = pl.col("close")
    start = spec.lookback_days + spec.skip_days

    if spec.signal_kind == "return":
        # Formation: close[t - skip] / close[t - skip - lookback] - 1
        signal = (c.shift(spec.skip_days) / c.shift(start) - 1.0).over("symbol")
    elif spec.signal_kind == "nearness_52w":
        # George & Hwang: price relative to its own 52-week high. A name at 0.99 of its
        # high ranks above one at 0.60 regardless of how either got there, which is the
        # claim that distinguishes this from a trailing-return ranking.
        high = (
            pl.col("high") if "high" in panel.columns else c
        ).rolling_max(window_size=spec.lookback_days, min_samples=spec.lookback_days)
        signal = (c / high.over("symbol")).over("symbol")
    else:  # realised_vol
        # Standard deviation of daily log returns over the formation window. NEGATED so
        # that ascending rank still runs from "worst by this strategy's lights" to
        # "best" -- without that, `sign` would silently mean the opposite thing here
        # than it does everywhere else.
        ret = (c / c.shift(1)).log()
        signal = -(
            ret.rolling_std(window_size=spec.lookback_days, min_samples=spec.lookback_days)
        ).over("symbol")

    return panel.with_columns(
        signal.alias("signal"),
        (c.shift(-spec.rebalance_days) / c - 1.0).over("symbol").alias("fwd"),
    )


def _rebalance_dates(panel: pl.DataFrame, spec: CrossSectionalSpec) -> list[date]:
    """Every `rebalance_days`-th trading date in the panel."""
    days = panel["session_date"].unique().sort().to_list()
    warmup = spec.lookback_days + spec.skip_days
    return days[warmup :: spec.rebalance_days]


def _eligible(
    signals: pl.DataFrame, when: date, members: set[str] | None
) -> pl.DataFrame:
    """Names rankable on this date: in the index THEN, with a signal and an outcome.

    `members` is the point-in-time constituent set. Passing today's constituents instead
    is the survivorship bias this whole exercise is built to avoid: every name would be
    one that survived, which is precisely the sample momentum looks best on.
    """
    day = signals.filter(pl.col("session_date") == when).drop_nulls(["signal", "fwd"])
    if members is not None:
        day = day.filter(pl.col("symbol").is_in(list(members)))
    return day


def _long_short_return(day: pl.DataFrame, spec: CrossSectionalSpec, order: pl.Series) -> float:
    """Equal-weighted top-minus-bottom quantile return, given a ranking."""
    n = day.height
    bucket = max(1, n // spec.quantiles)
    idx = order.to_list()
    fwd = day["fwd"].to_list()
    top = [fwd[i] for i in idx[-bucket:]]
    bottom = [fwd[i] for i in idx[:bucket]]
    long_leg = sum(top) / len(top)
    short_leg = sum(bottom) / len(bottom)
    return spec.sign * (long_leg - short_leg)


def run_cross_section(
    panel: pl.DataFrame,
    spec: CrossSectionalSpec,
    *,
    membership: dict[date, set[str]] | None = None,
    costs: CostModel | None = None,
    draws: int = 1000,
    seed: int = 0,
) -> CrossSectionalResult | None:
    """Backtest one cross-sectional strategy against random-rank portfolios."""
    if panel.is_empty():
        return None
    signals = _signal_frame(panel, spec)
    dates = _rebalance_dates(panel, spec)

    per_period: list[tuple[date, float, float]] = []
    name_counts: list[int] = []
    held: set[str] = set()
    turnovers: list[float] = []
    # Per-rebalance snapshots the null re-uses, so the random portfolios face exactly
    # the same eligible names on exactly the same dates.
    snapshots: list[tuple[date, pl.DataFrame]] = []

    for when in dates:
        members = _members_asof(membership, when) if membership else None
        day = _eligible(signals, when, members)
        if day.height < spec.min_names:
            continue

        order = day["signal"].arg_sort()
        gross = _long_short_return(day, spec, order)

        bucket = max(1, day.height // spec.quantiles)
        idx = order.to_list()
        syms = day["symbol"].to_list()
        now_held = {syms[i] for i in idx[-bucket:]} | {syms[i] for i in idx[:bucket]}
        # Turnover is the fraction of the book replaced, which is what actually gets
        # charged. A strategy whose ranking barely moves pays almost nothing; one that
        # reshuffles every month pays the round trip on nearly the whole portfolio.
        churn = 1.0 if not held else len(now_held - held) / max(1, len(now_held))
        turnovers.append(churn)
        held = now_held

        net = gross
        if costs is not None:
            net -= churn * 2.0 * costs.per_side_bps * 1e-4
        per_period.append((when, gross, net))
        name_counts.append(day.height)
        snapshots.append((when, day))

    if not per_period:
        return None

    gross_vals = [g for _, g, _ in per_period]
    net_vals = [n for _, _, n in per_period]
    gross_mean = sum(gross_vals) / len(gross_vals)
    net_mean = sum(net_vals) / len(net_vals)
    t_stat = _t_stat(gross_vals)

    null = _random_rank_null(snapshots, spec, draws=draws, seed=seed)
    p = low = high = null_mean = None
    if null:
        null.sort()
        at_least = sum(1 for v in null if v >= gross_mean)
        p = (at_least + 1) / (len(null) + 1)
        null_mean = sum(null) / len(null)
        low = null[int(0.025 * (len(null) - 1))]
        high = null[int(0.975 * (len(null) - 1))]

    counts = sorted(name_counts)
    return CrossSectionalResult(
        spec=spec.name,
        n_periods=len(per_period),
        gross_mean=gross_mean,
        net_mean=net_mean,
        gross_t=t_stat,
        turnover=sum(turnovers) / len(turnovers),
        p_value=p,
        null_mean=null_mean,
        null_low=low,
        null_high=high,
        periods=per_period,
        n_names_median=counts[len(counts) // 2],
    )


def _members_asof(membership: dict[date, set[str]], when: date) -> set[str]:
    """Index members as of the most recent snapshot AT OR BEFORE `when`.

    At or before, never after. Taking the nearest snapshot in either direction would
    let a name that joined the index next month be tradable this month -- a small,
    entirely invisible lookahead that biases exactly toward the names that were about
    to do well enough to be added.
    """
    keys = [d for d in membership if d <= when]
    return membership[max(keys)] if keys else set()


def _random_rank_null(
    snapshots: list[tuple[date, pl.DataFrame]],
    spec: CrossSectionalSpec,
    *,
    draws: int,
    seed: int,
) -> list[float]:
    """Null distribution from shuffled ranks on the same names and the same dates.

    The signal is replaced by a random permutation; everything else -- which names were
    eligible, how many were held, when the book turned over -- is held fixed. So the
    comparison isolates the ranking rule, which is the only thing the strategy claims.
    """
    if not snapshots:
        return []
    rng = random.Random(seed)
    out: list[float] = []
    for _ in range(draws):
        total = 0.0
        for _when, day in snapshots:
            order = pl.Series("o", rng.sample(range(day.height), day.height))
            total += _long_short_return(day, spec, order)
        out.append(total / len(snapshots))
    return out


def _t_stat(values: list[float]) -> float:
    """Newey-West would be better; this is the plain t and is labelled as such.

    Monthly long-short returns are close enough to independent that a plain t is not
    misleading at this sample size, but it does not correct for the overlap a shorter
    rebalance would introduce. Stated rather than silently assumed away.
    """
    n = len(values)
    if n < 2:
        return 0.0
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    if var <= 0:
        return 0.0
    return mean / ((var / n) ** 0.5)
