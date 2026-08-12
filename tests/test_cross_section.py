"""The cross-sectional engine, tested from both directions.

A backtester that finds an effect is worthless unless it also FAILS to find one in
noise. So there are two anchor tests here: a panel with a momentum effect planted in it,
which the engine must detect, and a panel of pure random walks, where it must not. The
second is the one that would have caught the bugs -- an engine that ranks on anything
correlated with its own forward return produces a beautiful, entirely false equity curve.
"""

from __future__ import annotations

import random
from datetime import date, timedelta

import polars as pl
import pytest

from tradedesk.backtest.cross_section import (
    CrossSectionError,
    CrossSectionalSpec,
    _members_asof,
    build_panel,
    run_cross_section,
)
from tradedesk.backtest.equity_costs import equity_cost_model

MOM = CrossSectionalSpec(
    name="test_12_1", source="test", lookback_days=252, skip_days=21,
    quantiles=5, rebalance_days=21, min_names=20,
)


def _panel(n_names: int, n_days: int, *, momentum: float = 0.0, seed: int = 3):
    """A panel of random walks, optionally with persistent per-name drift.

    `momentum` plants the effect: each name is assigned a fixed drift, so past winners
    genuinely do keep winning. At 0.0 the panel is pure noise and any "edge" the engine
    reports is manufactured by the engine.
    """
    rng = random.Random(seed)
    start = date(2018, 1, 1)
    drifts = [rng.gauss(0.0, momentum) for _ in range(n_names)]
    rows = []
    for i in range(n_names):
        p = 100.0
        for d in range(n_days):
            p *= 1.0 + drifts[i] + rng.gauss(0.0, 0.012)
            rows.append({
                "symbol": f"S{i:03d}",
                "session_date": start + timedelta(days=d),
                "close": p,
            })
    return pl.DataFrame(rows).sort(["symbol", "session_date"])


def test_a_planted_momentum_effect_is_detected():
    """The engine must find an effect that is genuinely there.

    Each name carries a fixed drift, so the past-12-month ranking really does predict
    the next month. If this fails, the engine cannot detect momentum at all and every
    negative result it produces is uninformative.
    """
    res = run_cross_section(_panel(120, 1400, momentum=0.0012), MOM, draws=300)
    assert res is not None
    assert res.gross_mean > 0, res.gross_mean
    assert res.beats_random, f"p={res.p_value}"


def test_pure_noise_is_not_detected():
    """The test that matters more.

    A long-short quintile portfolio on a hundred names produces a smooth equity curve
    whether or not the signal means anything -- it is diversified market-neutral noise.
    An engine that reports significance here would report it on real data too.
    """
    res = run_cross_section(_panel(120, 1400, momentum=0.0, seed=17), MOM, draws=300)
    assert res is not None
    assert not res.beats_random, (
        f"found p={res.p_value:.3f} on a panel with no effect planted in it"
    )


def test_the_null_holds_the_same_names_on_the_same_dates():
    """The null must differ from the strategy in exactly one respect: the ranking.

    A null drawn from a different eligible set, or a different number of names, would
    be measuring universe construction rather than the signal.
    """
    res = run_cross_section(_panel(100, 1000, momentum=0.0), MOM, draws=200)
    assert res is not None and res.null_mean is not None
    # With ranks shuffled, the long-short spread is a coin flip centred on zero.
    assert abs(res.null_mean) < 0.01, res.null_mean
    assert res.null_low < res.null_mean < res.null_high


def test_the_skip_window_keeps_the_signal_off_the_rebalance_date():
    """Jegadeesh and Titman skip the most recent month so short-term reversal does not
    contaminate a momentum signal.

    Checked structurally: with lookback 252 and skip 21, the signal on date t must be
    computable from data ending at t-21, so it cannot move when the last 20 days change.
    """
    panel = _panel(60, 600, momentum=0.001, seed=5)
    from tradedesk.backtest.cross_section import _signal_frame

    sig = _signal_frame(panel, MOM)
    one = sig.filter(pl.col("symbol") == "S000").drop_nulls("signal")
    row = one.tail(1)
    closes = panel.filter(pl.col("symbol") == "S000")["close"].to_list()
    expected = closes[-1 - 21] / closes[-1 - 21 - 252] - 1.0
    assert row["signal"][0] == pytest.approx(expected, rel=1e-9)


def test_membership_is_point_in_time_and_never_looks_forward():
    """`_members_asof` takes the most recent snapshot AT OR BEFORE the date.

    Taking the nearest snapshot in either direction would let a name that joins the
    index next month trade this month -- a small, invisible lookahead biased precisely
    toward names about to do well enough to be added.
    """
    membership = {
        date(2020, 1, 31): {"A", "B"},
        date(2020, 6, 30): {"A", "B", "C"},
        date(2021, 1, 31): {"A", "C", "D"},
    }
    assert _members_asof(membership, date(2020, 3, 15)) == {"A", "B"}
    assert _members_asof(membership, date(2020, 6, 30)) == {"A", "B", "C"}
    assert _members_asof(membership, date(2020, 12, 1)) == {"A", "B", "C"}
    # Before any snapshot exists there is no universe, not the earliest one.
    assert _members_asof(membership, date(2019, 1, 1)) == set()


def test_membership_actually_restricts_the_tradable_set():
    """A name outside the index on a date must not appear in that date's portfolio."""
    panel = _panel(60, 900, momentum=0.001, seed=9)
    allowed = {f"S{i:03d}" for i in range(30)}
    spec = CrossSectionalSpec(
        name="restricted", source="test", lookback_days=252, skip_days=21,
        rebalance_days=21, min_names=20,
    )
    res = run_cross_section(
        panel, spec, membership={date(2017, 1, 1): allowed}, draws=50
    )
    assert res is not None
    assert res.n_names_median <= 30


def test_costs_reduce_the_net_return_by_turnover():
    """Cost is charged on the fraction of the book replaced, not on the whole book.

    A strategy whose ranking barely moves should pay almost nothing; charging the full
    round trip every period would overstate the drag on exactly the strategies that
    trade least.
    """
    panel = _panel(100, 1200, momentum=0.001, seed=11)
    free = run_cross_section(panel, MOM, draws=50)
    charged = run_cross_section(
        panel, MOM, costs=equity_cost_model(spread_bps=20.0), draws=50
    )
    assert free is not None and charged is not None
    assert charged.net_mean < free.net_mean
    assert charged.gross_mean == pytest.approx(free.gross_mean)
    # The gap is turnover x round trip, not one round trip per period.
    round_trip = 2 * equity_cost_model(spread_bps=20.0).per_side_bps * 1e-4
    assert (free.net_mean - charged.net_mean) == pytest.approx(
        charged.turnover * round_trip, rel=1e-6
    )


def test_a_reversal_spec_flips_the_sign_of_the_same_ranking():
    """`sign=-1` goes long the bottom quantile. Short-term reversal is momentum's
    ranking read the other way round, and registering it as its own spec keeps the two
    from being one strategy quoted twice."""
    panel = _panel(100, 1200, momentum=0.0012, seed=13)
    up = run_cross_section(panel, MOM, draws=50)
    down = run_cross_section(
        panel,
        CrossSectionalSpec(name="rev", source="test", lookback_days=252, skip_days=21,
                           rebalance_days=21, min_names=20, sign=-1),
        draws=50,
    )
    assert up is not None and down is not None
    assert up.gross_mean == pytest.approx(-down.gross_mean, rel=1e-9)


def test_thin_dates_are_skipped_rather_than_ranked():
    """A quintile of two names is not a quintile. Dates below `min_names` contribute
    nothing instead of contributing noise."""
    panel = _panel(10, 900, momentum=0.001)
    res = run_cross_section(panel, MOM, draws=20)
    assert res is None, "a 10-name panel cannot support a 50-name minimum"


def test_a_misdeclared_spec_is_refused_at_construction():
    with pytest.raises(CrossSectionError, match="at least 2 quantiles"):
        CrossSectionalSpec(name="x", source="t", lookback_days=252, quantiles=1)
    with pytest.raises(CrossSectionError, match="windows must be positive"):
        CrossSectionalSpec(name="x", source="t", lookback_days=0)
    with pytest.raises(CrossSectionError, match="sign must be"):
        CrossSectionalSpec(name="x", source="t", lookback_days=252, sign=0)


def test_build_panel_handles_an_empty_universe():
    empty = build_panel({})
    assert empty.is_empty()
    assert run_cross_section(empty, MOM) is None


# ------------------------------------------------- the declared cross-sectional family


def test_the_declared_family_matches_the_pre_registration():
    """Six strategies, one configuration each, as PREREGISTRATION.md Part 2 declares.

    A guard on the thing the document actually promises. Adding a seventh, or a second
    configuration of an existing one, must fail here and force the question of whether
    the correction still covers the family that was declared.
    """
    from tradedesk.patterns.cross_sectional import CROSS_SECTIONAL, by_name, names

    assert len(CROSS_SECTIONAL) == 6
    assert len(set(names())) == 6, "duplicate strategy name"
    for spec in CROSS_SECTIONAL:
        assert spec.source, f"{spec.name} has no citation"
        assert spec.quantiles == 5, "quintiles were fixed in advance, not deciles"
        assert spec.rebalance_days == 21, "monthly rebalance was fixed in advance"
    with pytest.raises(KeyError):
        by_name("xs_not_a_strategy")


def test_momentum_skips_a_month_and_reversal_does_not():
    """The one-month skip is what separates these two opposing effects.

    Momentum skips it precisely to avoid short-term reversal; short-term reversal IS
    that effect. A momentum spec without the skip measures both at once and is not the
    published strategy -- and a reversal spec WITH one would delete itself.
    """
    from tradedesk.patterns.cross_sectional import by_name

    assert by_name("xs_momentum_12_1").skip_days == 21
    assert by_name("xs_momentum_6_1").skip_days == 21
    assert by_name("xs_reversal_1m").skip_days == 0
    assert by_name("xs_momentum_12_1").sign == 1
    assert by_name("xs_reversal_1m").sign == -1


def test_alternative_ranking_variables_are_declared_on_the_spec():
    """Two of the six do not rank on a trailing return."""
    from tradedesk.patterns.cross_sectional import by_name

    assert by_name("xs_52w_high").signal_kind == "nearness_52w"
    assert by_name("xs_low_volatility").signal_kind == "realised_vol"
    assert by_name("xs_momentum_12_1").signal_kind == "return"
    with pytest.raises(CrossSectionError, match="unknown signal_kind"):
        CrossSectionalSpec(name="x", source="t", lookback_days=252, signal_kind="vibes")


def test_nearness_to_the_52_week_high_ranks_on_position_not_on_gain():
    """George & Hwang's claim, and what makes it distinct from momentum.

    A stock that rose 5% but sits at its 52-week high must rank ABOVE one that rose 50%
    and sits well below its own high. A trailing-return ranking gets this backwards.
    """
    from tradedesk.backtest.cross_section import _signal_frame

    spec = CrossSectionalSpec(
        name="n", source="t", lookback_days=120, skip_days=0,
        rebalance_days=21, signal_kind="nearness_52w", min_names=1,
    )
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(200)]
    rows = []
    for i, d in enumerate(days):
        # AT_HIGH grinds steadily upward and ends AT its own high: +10% total.
        rows.append({"symbol": "AT_HIGH", "session_date": d, "close": 100.0 + i * 0.05})
        # BIG_GAIN doubles to 200 by day 100, then slides back to 130 -- a far larger
        # trailing gain, but sitting well below its own high when measured.
        close = 100.0 + i if i <= 100 else 200.0 - (i - 100) * 0.7
        rows.append({"symbol": "BIG_GAIN", "session_date": d, "close": close})
    sig = _signal_frame(pl.DataFrame(rows).sort(["symbol", "session_date"]), spec)
    last = sig.filter(pl.col("session_date") == days[-1]).drop_nulls("signal")
    got = dict(zip(last["symbol"].to_list(), last["signal"].to_list()))
    # BIG_GAIN has by far the larger trailing return, so a return-ranking would put it
    # first. Nearness must not.
    assert got["BIG_GAIN"] < 0.8, got
    assert got["AT_HIGH"] > got["BIG_GAIN"], got


def test_realised_vol_is_negated_so_sign_keeps_its_meaning():
    """Ranked ascending, the calmest name must sit at the TOP.

    Without the negation `sign` would silently mean the opposite thing for this strategy
    than for every other one -- and the result would look plausible either way.
    """
    from tradedesk.backtest.cross_section import _signal_frame

    spec = CrossSectionalSpec(
        name="v", source="t", lookback_days=60, skip_days=0,
        rebalance_days=21, signal_kind="realised_vol", min_names=1,
    )
    rng = random.Random(4)
    days = [date(2020, 1, 1) + timedelta(days=i) for i in range(200)]
    rows = []
    calm = wild = 100.0
    for d in days:
        calm *= 1 + rng.gauss(0, 0.001)
        wild *= 1 + rng.gauss(0, 0.05)
        rows.append({"symbol": "CALM", "session_date": d, "close": calm})
        rows.append({"symbol": "WILD", "session_date": d, "close": wild})
    sig = _signal_frame(pl.DataFrame(rows).sort(["symbol", "session_date"]), spec)
    last = sig.filter(pl.col("session_date") == days[-1]).drop_nulls("signal")
    got = dict(zip(last["symbol"].to_list(), last["signal"].to_list()))
    assert got["CALM"] > got["WILD"], got
