"""Ingest: idempotency, resumability, sparse semantics and the pagination traps."""

from __future__ import annotations

import polars as pl
import pytest

from tradedesk.coverage import covered_intervals
from tradedesk.ingest import ingest
from tradedesk.store import read_bars_raw
from tradedesk.timeutil import tf_ms
from tradedesk.venues.base import SinceIgnoredError

from .conftest import BASE_MS, FakeVenue, SinceIgnoringVenue, make_rows

TF = "5m"
STEP = tf_ms(TF)


def _now_after(rows, settle_ms):
    """A clock far enough past the last bar that every bar is final."""
    return int(rows[-1][0]) + STEP + settle_ms + 1


def test_ingest_is_idempotent_and_converges(con, cfg):
    """Re-running must add nothing and, crucially, must issue zero new requests.

    The second half matters more than the first. A fetcher that re-derives its resume
    point from missing timestamps looks idempotent -- the row count stops changing --
    while re-fetching the same holes forever and never converging.
    """
    rows = make_rows(BASE_MS, 500)
    venue = FakeVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)
    target_end = BASE_MS + 500 * STEP

    first = ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
                   target_end_ms=target_end, now=now)
    assert first.bars_inserted == 500
    requests_after_first = venue.request_count

    second = ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
                    target_end_ms=target_end, now=now)
    assert second.bars_inserted == 0
    assert second.windows_planned == 0
    assert venue.request_count == requests_after_first, "re-run issued new requests"


def test_ingest_resumes_after_interruption(con, cfg):
    """Interrupt mid-backfill, resume, and land exactly where an uninterrupted run would."""
    rows = make_rows(BASE_MS, 900)
    now = _now_after(rows, cfg.venue.settle_ms)
    target_end = BASE_MS + 900 * STEP

    # First pass covers only the first third.
    partial_end = BASE_MS + 300 * STEP
    venue = FakeVenue(rows)
    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=partial_end, now=now)
    assert read_bars_raw(con, "coinbase", "BTC/USD", TF).height == 300

    # Resume against the full target; only the missing two thirds are re-requested.
    venue.request_count = 0
    stats = ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
                   target_end_ms=target_end, now=now)
    assert stats.bars_inserted == 600
    assert read_bars_raw(con, "coinbase", "BTC/USD", TF).height == 900
    assert venue.request_count == 2  # 600 bars / 300 per request


def test_forming_bar_is_never_stored_then_is_stored_once_final(con, cfg):
    """The easiest lookahead bug to introduce and the hardest to notice.

    A forming bar looks perfectly valid -- it just has a close that will change. And
    it is exactly the bar a live signal would fire on.
    """
    rows = make_rows(BASE_MS, 10)
    venue = FakeVenue(rows)
    settle = cfg.venue.settle_ms
    last_open = int(rows[-1][0])
    target_end = last_open + STEP

    # Clock sits inside the final bar: it is still forming.
    now_forming = last_open + STEP // 2
    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=target_end, now=now_forming)
    stored = read_bars_raw(con, "coinbase", "BTC/USD", TF)["bar_open_ms"].to_list()
    assert last_open not in stored, "forming bar was written to the store"

    # Later, past close + settle, the same bar is legitimately final.
    now_final = last_open + STEP + settle + 1
    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=target_end, now=now_final)
    stored = read_bars_raw(con, "coinbase", "BTC/USD", TF)["bar_open_ms"].to_list()
    assert last_open in stored, "final bar was never picked up on a later run"


def test_coverage_is_not_claimed_over_unsettled_territory(con, cfg):
    """Claiming coverage over the unsettled tail would freeze a false gap forever.

    Coverage says "we looked here". If it covers bars that had not been published
    yet, no later run revisits them and the hole becomes permanent -- and
    indistinguishable from a genuine no-trade interval.
    """
    rows = make_rows(BASE_MS, 10)
    venue = FakeVenue(rows)
    last_open = int(rows[-1][0])
    now_forming = last_open + STEP // 2

    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=last_open + STEP, now=now_forming)
    covered = covered_intervals(con, "coinbase", "BTC/USD", TF)
    assert covered, "nothing was recorded as covered"
    assert max(end for _, end in covered) <= now_forming - cfg.venue.settle_ms


def test_sparse_bars_are_not_synthesised(con, cfg):
    """A venue that omits no-trade buckets must not gain fabricated rows."""
    rows = make_rows(BASE_MS, 100, skip={10, 11, 12, 50})
    venue = FakeVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)

    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
           target_end_ms=BASE_MS + 100 * STEP, now=now)
    stored = read_bars_raw(con, "coinbase", "BTC/USD", TF)
    assert stored.height == 96
    for missing in (10, 11, 12, 50):
        assert BASE_MS + missing * STEP not in stored["bar_open_ms"].to_list()


def test_empty_page_mid_range_does_not_truncate_backfill(con, cfg):
    """A quiet window is normal on a sparse venue; it must not end the backfill.

    `if not bars: break` is the natural thing to write and it silently truncates
    history at the first quiet window on an illiquid pair.
    """
    # A 300-bar hole, so one whole request window comes back empty.
    rows = make_rows(BASE_MS, 900, skip=set(range(300, 600)))
    venue = FakeVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)

    stats = ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
                   target_end_ms=BASE_MS + 900 * STEP, now=now)
    assert stats.windows_fetched == 3
    stored = read_bars_raw(con, "coinbase", "BTC/USD", TF)
    assert stored.height == 600
    # Bars after the hole were still collected.
    assert stored["bar_open_ms"].max() == BASE_MS + 899 * STEP


def test_page_overlap_is_deduplicated(con, cfg):
    """Adjacent pages can overlap by a bar depending on boundary conventions."""

    class OverlappingVenue(FakeVenue):
        def fetch_ohlcv(self, symbol, timeframe, *, since_ms, limit):
            rows = super().fetch_ohlcv(symbol, timeframe, since_ms=since_ms, limit=limit)
            if rows:  # replay the first bar at the end, as an off-by-one page would
                rows = rows + [list(rows[0])]
            return rows

    rows = make_rows(BASE_MS, 600)
    venue = OverlappingVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)

    stats = ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
                   target_end_ms=BASE_MS + 600 * STEP, now=now)
    assert stats.bars_dropped_duplicate == 2
    assert read_bars_raw(con, "coinbase", "BTC/USD", TF).height == 600


def test_since_ignoring_venue_is_rejected(con, cfg):
    """The Kraken failure: HTTP 200, healthy-looking, entirely wrong data.

    Raised for any venue -- it is a property of the fetch contract, not of one
    exchange. Without this the store fills with recent bars labelled as history, and
    every downstream check passes because the data is internally consistent.
    """
    rows = make_rows(BASE_MS, 5000)
    venue = SinceIgnoringVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)

    with pytest.raises(SinceIgnoredError):
        ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
               target_end_ms=BASE_MS + 5000 * STEP, now=now)


def test_session_date_is_et_not_utc(con, cfg):
    """Bars are labelled by ET calendar day, so the label flips at 00:00 ET."""
    # 2025-06-02 03:00 UTC is 2025-06-01 23:00 ET -- previous ET day.
    from datetime import datetime, timezone

    from tradedesk.timeutil import to_ms

    start = to_ms(datetime(2025, 6, 2, 3, 0, tzinfo=timezone.utc))
    rows = make_rows(start, 24)  # two hours, crossing 04:00 UTC = 00:00 ET
    venue = FakeVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)

    ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=start,
           target_end_ms=start + 24 * STEP, now=now)
    stored = read_bars_raw(con, "coinbase", "BTC/USD", TF)
    dates = sorted({d.isoformat() for d in stored["session_date"].to_list()})
    assert dates == ["2025-06-01", "2025-06-02"]


def test_transaction_rollback_leaves_no_partial_state(con, cfg, monkeypatch):
    """Bars and coverage land together or not at all.

    A crash between them leaves coverage claiming bars that were never stored, which
    silently promotes UNKNOWN to ABSENT_NO_TRADES. Over ~7,000 requests, partial
    failure is the normal case rather than an edge case.
    """
    rows = make_rows(BASE_MS, 300)
    venue = FakeVenue(rows)
    now = _now_after(rows, cfg.venue.settle_ms)

    import tradedesk.ingest as ingest_mod

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash after bars were inserted")

    monkeypatch.setattr(ingest_mod, "record_coverage", boom)
    with pytest.raises(RuntimeError):
        ingest(con, venue, cfg, "BTC/USD", TF, target_start_ms=BASE_MS,
               target_end_ms=BASE_MS + 300 * STEP, now=now)

    assert read_bars_raw(con, "coinbase", "BTC/USD", TF).height == 0
    assert covered_intervals(con, "coinbase", "BTC/USD", TF) == []
