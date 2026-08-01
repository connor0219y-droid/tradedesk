"""Grading the predict-first log.

`tradedesk live` records your read before it reveals anything. This module is the other
half of that exercise: it replays what price actually did and scores the read against
it, using the SAME entry, exit and cost rules as the backtest. That is the whole point
of reusing the engine -- "your expectancy" and "the backtested expectancy" have to be
the same measurement applied to different signals, or comparing them means nothing.

Four rules carry the honesty here.

ONLY A FULLY CLOSED HORIZON IS GRADED. A prediction whose `max_bars` window has not
finished is reported as PENDING with the number of bars still to run. It is never
scored on the bars that happen to exist yet: that would grade early exits promptly and
slow ones late, which biases the sample toward whatever resolves fastest. This is the
`as_of` discipline applied to the one table that is written in real time.

TWO NUMBERS, NEVER COLLAPSED. **Direction accuracy** asks whether your read of the
market was right, measured at the horizon, ignoring your stop and ignoring costs.
**Net expectancy** asks whether the trade would have made money at your actual fee
tier. They answer different questions and they routinely disagree -- at a 248bps round
trip you can be right about direction and still lose on every single call. Collapsing
them into one score hides exactly that.

THE STOP IS THE LEVEL YOU NAMED. Risk is measured from the entry fill to the price you
typed, not re-derived from ATR. You committed to a level; you are graded on it. A stop
on the wrong side of the entry is refused rather than silently flipped.

REFUSALS ARE LISTED, NOT DROPPED. A prediction that cannot be graded is shown with the
reason. A scoring report that quietly discards the reads it cannot handle is a report
that flatters you by construction.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import duckdb

from .backtest.costs import CostModel
from .backtest.engine import BacktestConfig
from .backtest.exits import IntrabarResolver, resolve_exit
from .backtest.stats import Stats, compute
from .config import Config
from .frames import read_bars
from .timeutil import tf_ms as _tf_ms

#: Reasons a prediction is not scored. `pending` ones become gradeable with more data;
#: the rest never will.
STOOD_ASIDE = "stood aside (direction: none)"
NO_STOP = "no stop stated, so there is no R to measure"
WRONG_SIDE = "stop on the wrong side of the entry"
GAPPED = "entry bar was not contiguous with the prediction bar"
NO_BARS = "no stored bars for this symbol and timeframe"
UNKNOWN_BAR = "prediction bar is not in the store"


@dataclass(frozen=True)
class Prediction:
    prediction_id: str
    made_at_ms: int
    symbol: str
    timeframe: str
    bar_open_ms: int
    direction: str
    stop: float | None
    note: str | None

    @property
    def is_long(self) -> bool:
        return self.direction == "long"


@dataclass(frozen=True)
class Graded:
    """One prediction, replayed through the backtest's own entry and exit rules."""

    prediction: Prediction
    entry_ms: int
    entry_mid: float
    entry_fill: float
    stop: float
    target: float
    exit_ms: int
    exit_price: float
    exit_reason: str
    bars_held: int
    r_gross: float
    r_net: float
    #: Signed move from entry to the end of the horizon, in R, in the predicted
    #: direction. Independent of where the stop was placed and of costs.
    horizon_r: float

    @property
    def direction_correct(self) -> bool:
        return self.horizon_r > 0


@dataclass(frozen=True)
class Ungraded:
    prediction: Prediction
    reason: str
    #: True when more data will make this gradeable, False when nothing will.
    pending: bool = False


@dataclass
class ScoreReport:
    graded: list[Graded]
    ungraded: list[Ungraded]
    round_trip_bps: float
    max_bars: int
    target_r: float
    #: Sample gates, echoed so the caller can explain a REFUSED verdict.
    min_n: int
    provisional_n: int

    @property
    def n_pending(self) -> int:
        return sum(1 for u in self.ungraded if u.pending)

    def net(self) -> Stats:
        return compute([g.r_net for g in self.graded],
                       min_n=self.min_n, provisional_n=self.provisional_n)

    def gross(self) -> Stats:
        return compute([g.r_gross for g in self.graded],
                       min_n=self.min_n, provisional_n=self.provisional_n)

    def horizon(self) -> Stats:
        """The read itself: signed move at the horizon, no stop, no costs."""
        return compute([g.horizon_r for g in self.graded],
                       min_n=self.min_n, provisional_n=self.provisional_n)

    @property
    def n_correct(self) -> int:
        return sum(1 for g in self.graded if g.direction_correct)

    def direction_accuracy(self) -> float | None:
        """Share of graded reads that moved the way you said. None under the gate.

        Gated at the same n as every other base rate in this tool. A 2-from-3 hit rate
        is not a skill measurement, and printing it teaches you to believe noise.
        """
        if len(self.graded) < self.min_n:
            return None
        return self.n_correct / len(self.graded)


def load_predictions(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[Prediction]:
    sql = "SELECT * FROM predictions WHERE 1=1"
    params: list[object] = []
    if symbol:
        sql += " AND symbol = ?"
        params.append(symbol)
    if timeframe:
        sql += " AND timeframe = ?"
        params.append(timeframe)
    sql += " ORDER BY bar_open_ms"
    return [
        Prediction(
            prediction_id=r["prediction_id"], made_at_ms=r["made_at_ms"],
            symbol=r["symbol"], timeframe=r["timeframe"],
            bar_open_ms=r["bar_open_ms"], direction=r["direction"],
            stop=r["stop"], note=r["note"],
        )
        for r in con.execute(sql, params).pl().to_dicts()
    ]


def grade(
    con: duckdb.DuckDBPyConnection,
    cfg: Config,
    predictions: list[Prediction],
    *,
    as_of: datetime,
    target_r: float = BacktestConfig.target_r,
    max_bars: int = BacktestConfig.max_bars,
    use_intrabar: bool = True,
    costs: CostModel | None = None,
) -> ScoreReport:
    """Score every prediction whose horizon has fully closed at `as_of`."""
    graded: list[Graded] = []
    ungraded: list[Ungraded] = []
    round_trip = 0.0

    # One bar read per (symbol, timeframe) rather than per prediction.
    groups: dict[tuple[str, str], list[Prediction]] = {}
    for p in predictions:
        groups.setdefault((p.symbol, p.timeframe), []).append(p)

    for (symbol, timeframe), group in sorted(groups.items()):
        # Per symbol, because SOL carries a wider spread and slippage override. An
        # explicit model overrides all of them, which is what the tests use.
        cm = costs or CostModel.for_symbol(cfg, symbol)
        round_trip = max(round_trip, 2 * cm.per_side_bps)

        bf = read_bars(con, symbol, timeframe, as_of=as_of, venue=cfg.venue.name)
        if bf.is_empty:
            ungraded.extend(Ungraded(p, NO_BARS) for p in group)
            continue

        df = bf.to_polars()
        gap = bf.gap_mask().to_list()
        o = df["open"].to_list()
        h = df["high"].to_list()
        low = df["low"].to_list()
        c = df["close"].to_list()
        ts = df["bar_open_ms"].to_list()
        sess = df["session_date"].to_list()
        index = {t: i for i, t in enumerate(ts)}
        last = len(ts) - 1

        resolver = None
        if use_intrabar and timeframe != "1m":
            m1 = read_bars(con, symbol, "1m", as_of=as_of, venue=cfg.venue.name)
            if not m1.is_empty:
                resolver = IntrabarResolver.from_frame(m1.to_polars())

        for p in group:
            if p.direction == "none":
                ungraded.append(Ungraded(p, STOOD_ASIDE))
                continue
            if p.stop is None:
                ungraded.append(Ungraded(p, NO_STOP))
                continue

            i = index.get(p.bar_open_ms)
            if i is None:
                ungraded.append(Ungraded(p, UNKNOWN_BAR))
                continue

            entry_index = i + 1
            if entry_index > last:
                ungraded.append(
                    Ungraded(p, "the bar you would have entered on has not closed yet",
                             pending=True)
                )
                continue

            # Permanent refusals come before the pending check on purpose. A prediction
            # that can never be graded should say so now, not sit in PENDING for the
            # length of a horizon and only then admit it was never gradeable.
            if gap[entry_index]:
                ungraded.append(Ungraded(p, GAPPED))
                continue

            mid = o[entry_index]
            # Wrong-side stops are refused, not flipped: a long with its stop above the
            # entry is not a long with a small stop, it is a read that cannot be graded.
            if (p.is_long and p.stop >= mid) or (not p.is_long and p.stop <= mid):
                ungraded.append(
                    Ungraded(p, f"{WRONG_SIDE} ({p.stop:,.2f} vs entry {mid:,.2f})")
                )
                continue

            # The whole horizon must have closed. Grading a trade on whatever bars
            # happen to exist would score fast resolutions sooner than slow ones, and
            # bias the sample toward whatever resolves quickest.
            horizon_index = entry_index + max_bars - 1
            if horizon_index > last:
                ungraded.append(
                    Ungraded(
                        p,
                        f"outcome window still open — {horizon_index - last} of "
                        f"{max_bars} {timeframe} bars still to close",
                        pending=True,
                    )
                )
                continue

            risk = abs(mid - p.stop)
            target = mid + target_r * risk if p.is_long else mid - target_r * risk
            entry_fill = cm.fill_price(mid, is_long=p.is_long, is_entry=True)

            ex = resolve_exit(
                highs=h, lows=low, closes=c, opens=o, ts=ts, session_dates=sess,
                tf_ms=_tf_ms(timeframe), entry_index=entry_index,
                entry_price=entry_fill, stop=p.stop, target=target,
                is_long=p.is_long, max_bars=max_bars, resolver=resolver,
            )
            exit_fill = cm.fill_price(ex.price, is_long=p.is_long, is_entry=False)

            gross = (ex.price - mid) / risk if p.is_long else (mid - ex.price) / risk
            net = ((exit_fill - entry_fill) / risk if p.is_long
                   else (entry_fill - exit_fill) / risk)
            move = c[horizon_index] - mid
            horizon_r = (move if p.is_long else -move) / risk

            graded.append(
                Graded(
                    prediction=p, entry_ms=ts[entry_index], entry_mid=mid,
                    entry_fill=entry_fill, stop=p.stop, target=target,
                    exit_ms=ts[ex.bar_index], exit_price=exit_fill,
                    exit_reason=ex.reason, bars_held=ex.bars_held,
                    r_gross=gross, r_net=net, horizon_r=horizon_r,
                )
            )

    if not round_trip:
        # Nothing was graded, so no symbol chose the tier. Quote the configured one
        # rather than the dataclass default, which is a different (cheaper) venue tier
        # and would understate the drag on an empty report.
        fallback = costs or CostModel.for_symbol(cfg, cfg.data.symbols[0])
        round_trip = 2 * fallback.per_side_bps

    graded.sort(key=lambda g: g.entry_ms)
    ungraded.sort(key=lambda u: u.prediction.bar_open_ms)
    return ScoreReport(
        graded=graded, ungraded=ungraded,
        round_trip_bps=round_trip,
        max_bars=max_bars, target_r=target_r,
        min_n=int(cfg.backtest.get("min_sample_size", 30)),
        provisional_n=int(cfg.backtest.get("provisional_n", 100)),
    )
