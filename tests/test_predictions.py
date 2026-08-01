"""Grading the predict-first log.

Known-answer fixtures throughout: every bar series here is short enough to verify by
eye, so an assertion that passes for the wrong reason is visible rather than plausible.

Three tests carry the module:

  test_an_unfinished_horizon_is_pending_never_scored -- the causality rule. A report
      that graded whatever bars happened to exist would score fast resolutions early and
      slow ones late, which biases the sample toward whatever resolves quickest.
  test_costs_turn_a_correct_read_into_a_losing_trade -- the reason direction accuracy
      and net expectancy are reported as two numbers and never collapsed into one.
  test_a_prediction_written_by_live_is_graded_by_score -- live writes, score reads. A
      grader that works only on hand-built rows proves nothing about the real table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import polars as pl
import pytest

from tradedesk import predictions as pr
from tradedesk import store
from tradedesk.backtest.costs import CostModel
from tradedesk.live import LiveView, record_prediction
from tradedesk.timeutil import from_ms, tf_ms

BASE = 1_700_000_000_000 // 300_000 * 300_000  # an aligned 5m boundary
STEP = tf_ms("5m")
FREE = CostModel(spread_bps=0.0, slippage_bps=0.0, taker_fee_bps=0.0)
COINBASE_BASE_TIER = CostModel(spread_bps=2.0, slippage_bps=3.0, taker_fee_bps=120.0)


def put_bars(con, ohlc, *, symbol="BTC/USD", timeframe="5m", skip=()) -> None:
    """Store a hand-built series. `skip` omits bars, as a sparse venue does."""
    step = tf_ms(timeframe)
    rows = [
        {
            "venue": "coinbase", "symbol": symbol, "timeframe": timeframe,
            "bar_open_ms": BASE + i * step,
            "open": o, "high": h, "low": lo, "close": c, "volume": 10.0,
        }
        for i, (o, h, lo, c) in enumerate(ohlc)
        if i not in set(skip)
    ]
    df = pl.DataFrame(rows).with_columns(
        pl.lit("2025-01-01").str.to_date().alias("session_date"),
        pl.lit(1, dtype=pl.Int16).alias("calendar_version"),
        pl.lit(0, dtype=pl.Int32).alias("revision"),
        pl.lit(0, dtype=pl.Int64).alias("ingested_at_ms"),
    )
    store.insert_bars(con, df)


def predict(bar: int = 0, direction: str = "long", stop: float | None = 99.0,
            *, symbol: str = "BTC/USD", note: str = "") -> pr.Prediction:
    return pr.Prediction(
        prediction_id=f"p{bar}-{direction}-{stop}", made_at_ms=BASE,
        symbol=symbol, timeframe="5m", bar_open_ms=BASE + bar * STEP,
        direction=direction, stop=stop, note=note,
    )


def score(con, cfg, preds, *, max_bars=4, target_r=2.0, costs=FREE, after_bars=40):
    """Grade `preds` from a clock well past the fixture, so nothing is pending by accident."""
    return pr.grade(
        con, cfg, preds,
        as_of=from_ms(BASE + after_bars * STEP),
        max_bars=max_bars, target_r=target_r, costs=costs, use_intrabar=False,
    )


# A long that works: entry at 100, stop 99 (1R), target 102, reached on the third bar.
RISES = [
    (100.0, 100.0, 100.0, 100.0),    # 0 the prediction bar
    (100.0, 101.0, 99.5, 100.5),     # 1 entry at its open, 100
    (100.5, 102.5, 100.0, 102.2),    # 2 high 102.5 tags the 102 target
    (102.2, 102.5, 102.0, 102.3),    # 3
    (102.3, 103.0, 102.0, 102.8),    # 4 horizon close 102.8
]

# The same shape inverted: price falls away from a long, stop taken on the third bar.
FALLS = [
    (100.0, 100.0, 100.0, 100.0),    # 0
    (100.0, 100.5, 99.6, 99.8),      # 1 entry 100, low 99.6 spares the 99 stop
    (99.8, 99.9, 98.5, 98.8),        # 2 low 98.5 takes the stop at 99
    (98.8, 99.0, 98.0, 98.2),        # 3
    (98.2, 98.5, 97.5, 97.6),        # 4 horizon close 97.6
]


def test_a_correct_read_is_scored_positive(con, cfg):
    put_bars(con, RISES)
    rep = score(con, cfg, [predict()])

    assert not rep.ungraded
    (g,) = rep.graded
    assert g.exit_reason == "target"
    assert g.r_gross == pytest.approx(2.0)      # (102 - 100) / 1R
    assert g.horizon_r == pytest.approx(2.8)    # (102.8 - 100) / 1R
    assert g.direction_correct
    assert g.bars_held == 2


def test_a_wrong_read_is_scored_negative(con, cfg):
    put_bars(con, FALLS)
    rep = score(con, cfg, [predict()])

    (g,) = rep.graded
    assert g.exit_reason == "stop"
    assert g.r_gross == pytest.approx(-1.0)     # stopped at exactly 1R
    assert g.horizon_r == pytest.approx(-2.4)   # (97.6 - 100) / 1R
    assert not g.direction_correct


def test_entry_is_the_next_bar_open_never_the_prediction_bar_close(con, cfg):
    """The backtest's rule, applied here too, or the two are not comparable.

    The prediction bar closes at 100.0 and the next bar opens at 100.0 in `RISES`, so
    this fixture makes them differ to tell the two apart.
    """
    bars = [(100.0, 105.0, 100.0, 104.0)] + RISES[1:]   # prediction bar closes at 104
    put_bars(con, bars)
    (g,) = score(con, cfg, [predict()]).graded

    assert g.entry_mid == pytest.approx(100.0), "entry must be the NEXT bar's open"
    assert g.entry_mid != pytest.approx(104.0), "grading used the prediction bar's close"


def test_an_unfinished_horizon_is_pending_never_scored(con, cfg):
    """The causality rule. Half a horizon is not a result, it is a result in progress."""
    put_bars(con, RISES[:4])            # horizon needs index 4; only 0..3 exist
    rep = score(con, cfg, [predict()], max_bars=4)

    assert not rep.graded
    (u,) = rep.ungraded
    assert u.pending
    assert "still to close" in u.reason
    assert rep.n_pending == 1

    # ...and the same prediction grades once the missing bar arrives.
    put_bars(con, RISES)
    assert len(score(con, cfg, [predict()], max_bars=4).graded) == 1


def test_the_horizon_is_measured_from_the_full_window_not_the_exit(con, cfg):
    """The trade closes on bar 2; the read is still judged at the horizon on bar 4."""
    put_bars(con, RISES)
    (g,) = score(con, cfg, [predict()], max_bars=4).graded

    assert g.bars_held == 2                      # exited early, at the target
    assert g.horizon_r == pytest.approx(2.8)     # but judged on bar 4's close, 102.8
    assert g.horizon_r != pytest.approx(g.r_gross)


def test_costs_turn_a_correct_read_into_a_losing_trade(con, cfg):
    """The punchline the two-number report exists to show.

    Entry fills at 100 x 1.0124 = 101.24 and the target exit at 102 x 0.9876 = 100.7352,
    so a read that was right by 2R nets -0.50R at the base tier.
    """
    put_bars(con, RISES)
    (g,) = score(con, cfg, [predict()], costs=COINBASE_BASE_TIER).graded

    assert g.direction_correct
    assert g.r_gross == pytest.approx(2.0)
    assert g.r_net == pytest.approx(-0.5048)
    assert g.r_gross > 0 > g.r_net


def test_the_horizon_measure_is_independent_of_where_the_stop_went(con, cfg):
    """Direction accuracy must grade the read, not the stop placement.

    Two reads of the same bar, same direction, different stops: both are directionally
    right, and the move expressed in each one's own R differs only by that risk.
    """
    put_bars(con, RISES)
    rep = score(con, cfg, [predict(stop=99.0), predict(stop=98.0)])

    tight, wide = sorted(rep.graded, key=lambda g: -g.stop)
    assert tight.direction_correct and wide.direction_correct
    assert tight.horizon_r == pytest.approx(2.8)    # 2.8 / 1R
    assert wide.horizon_r == pytest.approx(1.4)     # 2.8 / 2R
    assert rep.n_correct == 2


def test_a_short_read_is_graded_in_its_own_direction(con, cfg):
    """A falling market is a correct short, not an incorrect long."""
    put_bars(con, FALLS)
    (g,) = score(con, cfg, [predict(direction="short", stop=101.0)]).graded

    assert g.direction_correct
    assert g.horizon_r == pytest.approx(2.4)    # -(97.6 - 100) / 1R
    assert g.r_gross > 0


def test_a_gapped_entry_bar_is_not_graded(con, cfg):
    """The backtest skips a signal whose next bar sits across a hole; so does this."""
    put_bars(con, RISES, skip=(1,))     # the entry bar never traded
    rep = score(con, cfg, [predict()])

    assert not rep.graded
    (u,) = rep.ungraded
    assert not u.pending
    assert u.reason == pr.GAPPED


def test_a_wrong_side_stop_is_refused_not_flipped(con, cfg):
    """A long stopped ABOVE its entry is not a long with a tiny stop. It is nonsense."""
    put_bars(con, RISES)
    rep = score(con, cfg, [predict(stop=105.0)])

    assert not rep.graded
    (u,) = rep.ungraded
    assert pr.WRONG_SIDE in u.reason
    assert not u.pending, "no amount of extra data makes this gradeable"


def test_reads_that_cannot_be_scored_are_listed_with_reasons(con, cfg):
    """Silently dropping them would flatter the scorecard by construction."""
    put_bars(con, RISES)
    rep = score(con, cfg, [
        predict(direction="none", stop=None),
        predict(direction="long", stop=None),
        predict(symbol="ETH/USD"),
    ])

    assert not rep.graded
    reasons = {u.reason for u in rep.ungraded}
    assert pr.STOOD_ASIDE in reasons
    assert pr.NO_STOP in reasons
    assert pr.NO_BARS in reasons
    assert len(rep.ungraded) == 3


def test_direction_accuracy_is_refused_under_the_sample_minimum(con, cfg):
    """n=1 is not a hit rate. The individual read is still shown -- it is an observation."""
    put_bars(con, RISES)
    rep = score(con, cfg, [predict()])

    assert rep.direction_accuracy() is None
    assert rep.n_correct == 1 and len(rep.graded) == 1
    assert not rep.net().shown          # REFUSED under min_n
    assert not rep.horizon().shown


def test_accuracy_is_reported_once_the_sample_clears_the_minimum(con, cfg):
    """The complement: the gate opens, so REFUSED means 'too few' and not 'never fires'.

    Thirty-five prediction bars on a series that rises by 1 a bar, alternating long and
    short, so the longs are right and the shorts are wrong by construction. Each stop
    sits 1 away from the entry -- the NEXT bar's open -- so 1R is 1 throughout.
    """
    n = 35
    bars = [(100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i) for i in range(n + 6)]
    put_bars(con, bars)
    preds = [
        predict(bar=i, direction="long" if i % 2 == 0 else "short",
                stop=(101.0 + i) - 1.0 if i % 2 == 0 else (101.0 + i) + 1.0)
        for i in range(n)
    ]
    rep = score(con, cfg, preds, max_bars=4, after_bars=n + 20)

    assert len(rep.graded) == n
    assert rep.n_correct == len([i for i in range(n) if i % 2 == 0])
    assert rep.direction_accuracy() == pytest.approx(18 / 35)
    assert rep.horizon().shown
    assert rep.horizon().reliability == "PROVISIONAL"   # n < 100


def _alternating(con, n=40):
    """n reads on a series that rises by 1 a bar, alternating long and short.

    Every long is right and every short is wrong, so the observed accuracy is exactly
    half and the sample is a known quantity for the null to be compared against.
    """
    bars = [(100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i) for i in range(n + 6)]
    put_bars(con, bars)
    return [
        predict(bar=i, direction="long" if i % 2 == 0 else "short",
                stop=(101.0 + i) - 1.0 if i % 2 == 0 else (101.0 + i) + 1.0)
        for i in range(n)
    ]


def test_the_mirror_is_the_same_bar_read_the_other_way(con, cfg):
    """The null's raw material. Same bar, same risk, stop moved to the far side."""
    put_bars(con, RISES)
    (g,) = score(con, cfg, [predict()]).graded

    assert g.horizon_r == pytest.approx(2.8)
    assert g.mirror_horizon_r == pytest.approx(-2.8)     # exact negation
    # Price runs to +2R, so the mirrored short is stopped out at its own 1R.
    assert g.r_gross == pytest.approx(2.0)
    assert g.mirror_gross == pytest.approx(-1.0)


def test_the_baseline_is_refused_under_the_sample_minimum(con, cfg):
    """A p-value on two reads is a number that looks like evidence and is not."""
    put_bars(con, RISES)
    assert score(con, cfg, [predict()]).baseline() is None

    preds = _alternating(con, n=29)
    assert score(con, cfg, preds, after_bars=60).baseline() is None


def test_a_coin_flip_read_does_not_beat_random(con, cfg):
    """The headline case: 50% accuracy must not read as skill.

    Half right by construction, which is exactly what random direction produces, so
    the p-value should sit far from significance and the band should contain it.
    """
    preds = _alternating(con, n=40)
    rep = score(con, cfg, preds, after_bars=70)
    base = rep.baseline(draws=500)

    assert base is not None
    assert base.n_per_draw == 40
    assert base.accuracy.observed == pytest.approx(0.5)
    assert base.accuracy.mean == pytest.approx(0.5, abs=0.05)
    assert base.accuracy.low <= 0.5 <= base.accuracy.high
    assert not base.accuracy.beats_random
    assert base.accuracy.p_value > 0.2


def test_reading_every_bar_right_beats_random(con, cfg):
    """The complement, or 'does not beat random' would be indistinguishable from a stub.

    All 40 reads long on a series that only rises: 100% accuracy, which no coin flip
    over 40 bars reproduces.
    """
    n = 40
    bars = [(100.0 + i, 100.0 + i, 100.0 + i, 100.0 + i) for i in range(n + 6)]
    put_bars(con, bars)
    preds = [predict(bar=i, direction="long", stop=100.0 + i) for i in range(n)]
    base = score(con, cfg, preds, after_bars=70).baseline(draws=500)

    assert base.accuracy.observed == pytest.approx(1.0)
    assert base.accuracy.beats_random
    assert base.accuracy.p_value < 0.05
    assert base.horizon_r.beats_random


def test_the_null_is_symmetric_around_zero_in_r(con, cfg):
    """Flipping direction negates the horizon move, so the null must centre on zero."""
    base = score(con, cfg, _alternating(con, n=40), after_bars=70).baseline(draws=500)

    assert base.horizon_r.mean == pytest.approx(0.0, abs=0.35)
    assert base.horizon_r.low < 0 < base.horizon_r.high


def test_the_baseline_is_deterministic_given_a_seed(con, cfg):
    """Two runs of the same report must not disagree about whether you beat random."""
    rep = score(con, cfg, _alternating(con, n=40), after_bars=70)

    a = rep.baseline(draws=200, seed=7)
    b = rep.baseline(draws=200, seed=7)
    assert a == b
    assert rep.baseline(draws=200, seed=8) != a, "a different seed should redraw"


def test_the_null_pays_the_same_costs_as_you_do(con, cfg):
    """Random must not be handed a cheaper trade than the one you actually took."""
    rep = score(con, cfg, _alternating(con, n=40), after_bars=70,
                costs=COINBASE_BASE_TIER)
    base = rep.baseline(draws=500)

    assert base.net_mean < base.gross_r.mean, "costs must drag the null too"
    drag = base.gross_r.mean - base.net_mean
    assert drag > 0
    # Gross and net differ by that same drag for the observed reads as well.
    assert rep.gross().expectancy_r - rep.net().expectancy_r == pytest.approx(drag, rel=0.02)


def test_a_prediction_written_by_live_is_graded_by_score(con, cfg):
    """live writes, score reads. Hand-built rows would not prove the columns line up."""
    put_bars(con, RISES)
    view = LiveView(
        symbol="BTC/USD", timeframe="5m",
        as_of=datetime.now(timezone.utc), bar_open_ms=BASE,
        price=100.0, atr_intraday=1.0, session_broken=False,
    )
    record_prediction(con, view, {"direction": "long", "stop": 99.0, "note": "VWAP hold"})

    loaded = pr.load_predictions(con, symbol="BTC/USD")
    assert len(loaded) == 1
    assert loaded[0].note == "VWAP hold"

    (g,) = score(con, cfg, loaded).graded
    assert g.r_gross == pytest.approx(2.0)
    assert g.prediction.note == "VWAP hold"


def test_grading_reads_no_bar_the_prediction_could_not_have_seen(con, cfg):
    """`as_of` still governs. A clock inside the horizon cannot grade past its own edge."""
    put_bars(con, RISES)
    early = pr.grade(
        con, cfg, [predict()],
        as_of=from_ms(BASE + 3 * STEP) + timedelta(milliseconds=1),
        max_bars=4, costs=FREE, use_intrabar=False,
    )
    assert not early.graded
    assert early.n_pending == 1
