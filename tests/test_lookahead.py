"""The causality seam, and a test that the lookahead harness actually works.

The harness itself is the thing most likely to be quietly broken. A lookahead test
that has never been observed to fail proves nothing -- if it compares an empty frame
to an empty frame, every case passes vacuously and the suite reports green forever.
So this module tests the harness against a deliberately leaky reader as well as
against the real one.
"""

from __future__ import annotations

from datetime import datetime, timezone

import polars as pl
import pytest

from tradedesk import store
from tradedesk.frames import BarFrame, MixedCalendarError, read_bars
from tradedesk.ingest import ingest
from tradedesk.timeutil import from_ms, tf_ms

from .conftest import BASE_MS, FakeVenue, make_rows

TF = "5m"
STEP = tf_ms(TF)


def _populate(con, cfg, n=400):
    rows = make_rows(BASE_MS, n)
    venue = FakeVenue(rows)
    now = int(rows[-1][0]) + STEP + cfg.venue.settle_ms + 1
    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=BASE_MS + n * STEP, now=now)
    return rows


def _truncated_store(con, cfg, tmp_path, cutoff_ms):
    """A second store holding only bars that had opened before `cutoff_ms`.

    This is the 'dataset truncated at bar t' half of the lookahead test: whatever a
    reader produces from the full history must be identical to what it produces when
    the future genuinely does not exist.
    """
    dst = store.connect(tmp_path / "truncated.duckdb")
    store.init_schema(dst)
    bars = store.read_bars_raw(con, "coinbase", "BTC/USD", TF)
    keep = bars.filter(pl.col("bar_open_ms") < cutoff_ms)  # noqa: F841
    dst.execute("INSERT INTO bars SELECT * FROM keep")
    return dst


def leaky_read_bars(con, symbol, timeframe, *, as_of, venue="coinbase", **_):
    """A reader with a planted lookahead bug: it ignores `as_of` entirely.

    This is the mistake the seam exists to prevent -- and the one the harness must be
    able to catch.
    """
    df = con.execute(
        "SELECT * FROM bars WHERE venue = ? AND symbol = ? AND timeframe = ? "
        "ORDER BY bar_open_ms",
        [venue, symbol, timeframe],
    ).pl()
    return BarFrame(df=df, venue=venue, symbol=symbol, timeframe=timeframe,
                    calendar_version=1, as_of_ms=0)


def harness_is_causal(reader, full_con, trunc_con, as_of) -> bool:
    """True when a reader gives the same answer with and without access to the future."""
    a = reader(full_con, "BTC/USD", TF, as_of=as_of).to_polars()
    b = reader(trunc_con, "BTC/USD", TF, as_of=as_of).to_polars()
    if a.is_empty() or b.is_empty():
        raise AssertionError("harness compared empty frames -- it would pass vacuously")
    return a.equals(b)


def test_read_bars_requires_as_of(con):
    """`as_of` is keyword-only with no default, so the future is unreadable by accident."""
    with pytest.raises(TypeError):
        read_bars(con, "BTC/USD", TF)  # type: ignore[call-arg]


def test_read_bars_hides_bars_that_had_not_closed(con, cfg):
    """A bar is visible only once it has closed: bar_open + timeframe <= as_of."""
    _populate(con, cfg, n=100)
    cutoff_open = BASE_MS + 50 * STEP
    as_of = from_ms(cutoff_open + STEP)  # the moment bar 50 closes

    frame = read_bars(con, "BTC/USD", TF, as_of=as_of)
    stored = frame.to_polars()["bar_open_ms"].to_list()
    assert max(stored) == cutoff_open, "a bar that had not closed was visible"
    assert cutoff_open + STEP not in stored

    # One millisecond earlier, bar 50 itself is still forming.
    earlier = read_bars(con, "BTC/USD", TF, as_of=from_ms(cutoff_open + STEP - 1))
    assert max(earlier.to_polars()["bar_open_ms"].to_list()) == cutoff_open - STEP


def test_truncating_the_dataset_changes_nothing(con, cfg, tmp_path):
    """The real lookahead test: bars <= t are identical with and without the future."""
    _populate(con, cfg, n=400)
    cutoff = BASE_MS + 200 * STEP
    as_of = from_ms(cutoff)
    trunc = _truncated_store(con, cfg, tmp_path, cutoff)
    assert harness_is_causal(read_bars, con, trunc, as_of)


def test_harness_detects_a_planted_leak(con, cfg, tmp_path):
    """Prove the harness can fail. Without this, green means nothing."""
    _populate(con, cfg, n=400)
    cutoff = BASE_MS + 200 * STEP
    as_of = from_ms(cutoff)
    trunc = _truncated_store(con, cfg, tmp_path, cutoff)
    assert not harness_is_causal(leaky_read_bars, con, trunc, as_of), (
        "the harness failed to notice a reader that ignores as_of -- "
        "every other lookahead assertion in this suite is therefore worthless"
    )


def test_window_mask_refuses_non_contiguous_windows(con, cfg):
    """Sparse storage means adjacent rows are not necessarily adjacent in time.

    A three-bar detector handed rows spanning a no-trade hole is detecting an
    artifact, so windows crossing a gap must never be marked usable.
    """
    rows = make_rows(BASE_MS, 40, skip={20, 21})
    venue = FakeVenue(rows)
    now = int(rows[-1][0]) + STEP + cfg.venue.settle_ms + 1
    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=BASE_MS + 40 * STEP, now=now)

    frame = read_bars(con, "BTC/USD", TF, as_of=from_ms(now))
    mask = frame.window_mask(3).to_list()
    ts = frame.to_polars()["bar_open_ms"].to_list()

    # The bar immediately after the hole, and the one after that, cannot have a
    # contiguous 3-bar window behind them.
    first_after_gap = ts.index(BASE_MS + 22 * STEP)
    assert mask[first_after_gap] is False
    assert mask[first_after_gap + 1] is False
    assert mask[first_after_gap + 2] is True  # three clean bars have accumulated

    # The first two rows overall also lack a full window.
    assert mask[0] is False and mask[1] is False and mask[2] is True

    assert frame.contiguous_windows(3).height == sum(mask)


def test_mixed_calendar_versions_are_refused(con, cfg):
    """Changing the session definition must not silently blend two definitions."""
    _populate(con, cfg, n=20)
    con.execute(
        "UPDATE bars SET calendar_version = 2 WHERE bar_open_ms > ?",
        [BASE_MS + 10 * STEP],
    )
    with pytest.raises(MixedCalendarError):
        read_bars(con, "BTC/USD", TF, as_of=datetime.now(timezone.utc))


def test_fill_gaps_forward_is_refused(con, cfg):
    """Forward-filling a sparse series fabricates prices that never traded."""
    _populate(con, cfg, n=20)
    with pytest.raises(NotImplementedError):
        read_bars(con, "BTC/USD", TF, as_of=datetime.now(timezone.utc), fill_gaps="forward")


def test_as_of_must_be_timezone_aware(con, cfg):
    _populate(con, cfg, n=20)
    with pytest.raises(ValueError):
        read_bars(con, "BTC/USD", TF, as_of=datetime(2025, 1, 1))
