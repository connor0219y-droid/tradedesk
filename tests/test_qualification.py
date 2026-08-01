"""The gate: the hard rule that governs whether the tool may tell you to trade.

Two tests carry this module, and they are complementary:

  test_nothing_qualifies_unless_every_criterion_passes  -- failing ANY criterion means
      no card, tested one criterion at a time so a partial implementation can't sneak by.
  test_a_qualifying_setup_does_produce_a_card           -- the emitting path DEMONSTRABLY
      works. Without this, "no qualifying setups" could mean "this never fires", and the
      whole gate would be indistinguishable from a bug.
"""

from __future__ import annotations

import pytest
from rich.console import Console

from tradedesk import store
from tradedesk.config import load_config
from tradedesk.live import LiveView, Position, ThesisRequired, _render_card, render
from tradedesk.qualification import (
    Criteria,
    SetupStats,
    all_setups,
    evaluate,
    qualifying_setups,
    record,
)


def _stats(**over) -> SetupStats:
    """A setup that passes every criterion. Tests break it one field at a time."""
    base = dict(
        setup="good_setup", symbol="BTC/USD", timeframe="5m", direction="long",
        stop_atr=1.0, target_r=2.0, max_bars=48, round_trip_bps=248.0,
        n_in=500, n_out=200,
        gross_in=0.30, gross_out=0.20, net_in=0.15,
        ci_low=0.05, ci_high=0.25, p_value=0.001, survives_bh=True,
    )
    base.update(over)
    return SetupStats(**base)


@pytest.fixture
def con(tmp_path):
    c = store.connect(tmp_path / "q.duckdb")
    store.init_schema(c)
    yield c
    c.close()


def test_the_baseline_setup_qualifies():
    """Sanity: the fixture must pass, or every negative test below is vacuous."""
    ok, reasons = evaluate(_stats())
    assert ok, f"baseline fixture should qualify but did not: {reasons}"


@pytest.mark.parametrize(
    "broken,expected_phrase",
    [
        ({"survives_bh": False}, "Benjamini-Hochberg"),
        ({"gross_out": -0.05}, "out-of-sample sign not held"),
        ({"gross_out": None}, "out-of-sample sign not held"),
        ({"net_in": -0.10}, "not positive"),
        ({"net_in": 0.0}, "not positive"),
        ({"net_in": None}, "not positive"),
        ({"n_in": 99}, "below the 100 minimum"),
    ],
)
def test_nothing_qualifies_unless_every_criterion_passes(broken, expected_phrase):
    """Each criterion tested in isolation, so a partial gate cannot pass this."""
    ok, reasons = evaluate(_stats(**broken))
    assert not ok, f"{broken} should have disqualified the setup"
    assert any(expected_phrase in r for r in reasons), reasons


def test_in_sample_positive_but_out_of_sample_negative_is_rejected():
    """The overfitting signature the gate exists to catch.

    Mirrors the real case: ma_pullback_long was +0.0222R in sample and −0.0092R out.
    """
    ok, reasons = evaluate(_stats(gross_in=0.30, gross_out=-0.01))
    assert not ok
    assert any("sign not held" in r for r in reasons)


def test_disqualifiers_are_recorded_not_just_the_verdict(con):
    """A rejected setup must say WHY. Absence and rejection look identical otherwise."""
    record(con, _stats(survives_bh=False, net_in=-0.2, n_in=50),
           validated_at_ms=0)
    rows = all_setups(con)
    assert len(rows) == 1
    d = rows[0]["disqualifiers"]
    assert "Benjamini-Hochberg" in d and "not positive" in d and "minimum" in d


def test_qualifying_setups_returns_nothing_when_nothing_qualifies(con):
    for broken in ({"survives_bh": False}, {"gross_out": -0.1}, {"net_in": -0.1}, {"n_in": 10}):
        record(con, _stats(setup=f"bad_{list(broken)[0]}", **broken), validated_at_ms=0)
    assert all_setups(con), "setups were recorded"
    assert qualifying_setups(con) == [], "a disqualified setup reached the signal path"


def test_a_qualifying_setup_does_produce_a_card(con, capsys):
    """THE COMPANION TEST. Proves the emitting path works.

    Without this, "no qualifying setups" is indistinguishable from a code path that
    never fires, and the honest output would be an accident rather than a measurement.
    """
    assert record(con, _stats(), validated_at_ms=0) is True
    q = qualifying_setups(con)
    assert len(q) == 1

    view = LiveView(
        symbol="BTC/USD", timeframe="5m", as_of=__import__("datetime").datetime.now(
            __import__("datetime").timezone.utc),
        bar_open_ms=1_700_000_000_000, price=60_000.0, atr_intraday=100.0,
        session_broken=False, levels=[], triggered=[], qualifying=q,
    )
    console = Console(width=100, force_terminal=False)
    _render_card(console, view, q[0], load_config())
    out = capsys.readouterr().out
    assert "SIGNAL" in out
    assert "LONG" in out
    assert "Base rate" in out and "n=500" in out
    assert "Stop" in out and "Target" in out and "Invalidation" in out


def test_tightening_the_gate_invalidates_stored_passes(con):
    """A stored pass must not be grandfathered in when the criteria change."""
    record(con, _stats(n_in=150), validated_at_ms=0, criteria=Criteria())
    assert len(qualifying_setups(con, criteria=Criteria())) == 1
    stricter = Criteria(min_n=1000)
    assert qualifying_setups(con, criteria=stricter) == [], (
        "a pass recorded under looser criteria survived a tightening"
    )


def test_live_view_says_no_qualifying_setups(con, capsys):
    """The honest output, rendered."""
    from datetime import datetime, timezone

    view = LiveView(
        symbol="BTC/USD", timeframe="5m", as_of=datetime.now(timezone.utc),
        bar_open_ms=1_700_000_000_000, price=60_000.0, atr_intraday=100.0,
        session_broken=False, levels=[], triggered=[], qualifying=[],
    )
    render(Console(width=100, force_terminal=False), view, load_config())
    out = capsys.readouterr().out
    assert "NO QUALIFYING SETUPS" in out
    assert "SIGNAL" not in out.replace("NO QUALIFYING SETUPS", "")


# ------------------------------------------------------------- position sizing


def test_sizing_requires_a_thesis():
    with pytest.raises(ThesisRequired):
        Position(account_size=10_000, risk_pct=1.0, entry=100.0, stop=99.0, thesis="")
    with pytest.raises(ThesisRequired):
        Position(account_size=10_000, risk_pct=1.0, entry=100.0, stop=99.0, thesis="hmm")


def test_sizing_math():
    p = Position(account_size=10_000, risk_pct=1.0, entry=100.0, stop=99.0,
                 thesis="reclaimed VWAP on rising relative volume")
    assert p.risk_dollars == pytest.approx(100.0)
    assert p.risk_per_unit == pytest.approx(1.0)
    assert p.units == pytest.approx(100.0)
    assert p.notional == pytest.approx(10_000.0)
    assert p.leverage == pytest.approx(1.0)


def test_tight_stop_exposes_required_leverage():
    """A 0.14% stop -- a 1xATR(5m) BTC stop -- needs ~7x leverage at 1% risk.

    Worth surfacing: it is the practical reason tight-stop intraday sizing is not
    available on an unlevered retail account.
    """
    p = Position(account_size=10_000, risk_pct=1.0, entry=63_000.0,
                 stop=63_000.0 * (1 - 0.0014), thesis="opening range break with volume")
    assert p.leverage > 5.0


def test_zero_risk_is_refused():
    with pytest.raises(ValueError):
        Position(account_size=10_000, risk_pct=1.0, entry=100.0, stop=100.0,
                 thesis="a perfectly reasonable sounding thesis")
