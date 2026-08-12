"""The Alpaca adapter, tested against a fake transport.

No test here touches the network. The point is the adapter's contract -- window
clipping, pagination, monotonicity, the split-adjustment default, and the
`SinceIgnoredError` guard -- all of which are properties of our code rather than of
Alpaca's uptime, and all of which are exactly what a live smoke test would fail to
exercise deterministically.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from tradedesk.venues.alpaca import (
    AlpacaAuthError,
    AlpacaVenue,
    _parse_ms,
    _rfc3339,
)
from tradedesk.venues.base import SinceIgnoredError, VenueError

STEP = 300_000
BASE = 1_700_000_000_000 // STEP * STEP


def _bar(ms: int, price: float = 100.0) -> dict:
    return {
        "t": _rfc3339(ms), "o": price, "h": price + 1, "l": price - 1,
        "c": price + 0.5, "v": 1000.0,
    }


class FakeAlpaca(AlpacaVenue):
    """An AlpacaVenue whose HTTP layer is a scripted list of pages."""

    def __init__(self, pages, **kw):
        super().__init__(key_id="k", secret_key="s", **kw)
        self._pages = list(pages)
        self.calls: list[dict] = []

    def _get(self, path, params):
        self.calls.append({"path": path, **params})
        return self._pages.pop(0) if self._pages else {"bars": [], "next_page_token": None}


def test_split_adjustment_is_requested_not_raw():
    """The default that would silently fabricate an eight-year history.

    Alpaca defaults to `raw`. AAPL's 2020 4:1 split and NVDA's 2024 10:1 would each
    appear as a single-day collapse of 75% and 90%, and every breakout and 52-week-high
    detector would fire on an event that never happened.
    """
    v = FakeAlpaca([{"bars": [_bar(BASE)], "next_page_token": None}])
    v.fetch_ohlcv("AAPL", "5m", since_ms=BASE, limit=10)
    assert v.calls[0]["adjustment"] == "split"


def test_dividend_adjustment_is_deliberately_not_used():
    """`all` rewrites past prices every time a dividend is paid, so the level a
    detector fires on is one nobody could have seen at the time. The default is a
    documented choice, not an oversight."""
    assert AlpacaVenue(key_id="k", secret_key="s").adjustment == "split"


def test_bars_outside_the_requested_window_are_dropped():
    """The contract in venues/base.py: callers never reason about boundary conventions.

    Alpaca's `end` is inclusive in some paths; a bar landing exactly on the window's end
    belongs to the NEXT request, and returning it here would duplicate it or shift the
    grid by one.
    """
    limit = 3
    end = BASE + limit * STEP
    pages = [{
        "bars": [
            _bar(BASE - STEP),   # before the window
            _bar(BASE),
            _bar(BASE + STEP),
            _bar(BASE + 2 * STEP),
            _bar(end),           # exactly at the end -- excluded, half-open
        ],
        "next_page_token": None,
    }]
    rows = FakeAlpaca(pages).fetch_ohlcv("AAPL", "5m", since_ms=BASE, limit=limit)
    assert [r[0] for r in rows] == [BASE, BASE + STEP, BASE + 2 * STEP]


def test_pagination_follows_the_token_not_the_page_size():
    """Alpaca caps a page at 10,000 regardless of `limit`, so a loop that stopped when
    a page came back short would truncate every large backfill silently."""
    pages = [
        {"bars": [_bar(BASE), _bar(BASE + STEP)], "next_page_token": "a"},
        {"bars": [_bar(BASE + 2 * STEP)], "next_page_token": "b"},
        {"bars": [_bar(BASE + 3 * STEP)], "next_page_token": None},
    ]
    v = FakeAlpaca(pages)
    rows = v.fetch_ohlcv("AAPL", "5m", since_ms=BASE, limit=10)
    assert len(rows) == 4
    assert v.calls[1]["page_token"] == "a"
    assert v.calls[2]["page_token"] == "b"
    assert "page_token" not in v.calls[0]


def test_duplicate_timestamps_are_collapsed():
    """Two bars claiming the same instant would violate the store's primary key, and
    would mean the venue disagreed with itself about what happened."""
    pages = [{"bars": [_bar(BASE), _bar(BASE), _bar(BASE + STEP)], "next_page_token": None}]
    rows = FakeAlpaca(pages).fetch_ohlcv("AAPL", "5m", since_ms=BASE, limit=10)
    assert [r[0] for r in rows] == [BASE, BASE + STEP]


def test_rows_come_back_sorted_ascending():
    pages = [{"bars": [_bar(BASE + 2 * STEP), _bar(BASE), _bar(BASE + STEP)],
              "next_page_token": None}]
    rows = FakeAlpaca(pages).fetch_ohlcv("AAPL", "5m", since_ms=BASE, limit=10)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


def test_a_venue_ignoring_since_raises_rather_than_filling_the_store():
    """The Kraken failure mode, guarded for every venue.

    A venue that answers 200 and returns the newest bars regardless of `start` fills a
    database with one recent window labelled as years of history -- and every downstream
    check passes, because the data is internally consistent.
    """
    far_future = BASE + 10_000 * STEP
    pages = [{"bars": [_bar(far_future)], "next_page_token": None}]
    with pytest.raises(SinceIgnoredError, match="entirely after the requested window"):
        FakeAlpaca(pages).fetch_ohlcv("AAPL", "5m", since_ms=BASE, limit=3)


def test_unsupported_timeframe_is_refused_by_name():
    """4h is derived by aggregation, exactly as it is for Coinbase. Asking the venue
    for it must fail loudly rather than return something plausible."""
    with pytest.raises(VenueError, match="does not serve"):
        FakeAlpaca([]).fetch_ohlcv("AAPL", "4h", since_ms=BASE, limit=10)


def test_supported_timeframes_match_the_mapping():
    v = AlpacaVenue(key_id="k", secret_key="s")
    assert v.supported_timeframes() == frozenset({"1m", "5m", "15m", "1h", "1d"})


def test_from_env_refuses_without_keys(monkeypatch):
    """And says how to fix it without ever echoing a key."""
    for name in ("ALPACA_API_KEY_ID", "APCA_API_KEY_ID",
                 "ALPACA_API_SECRET_KEY", "APCA_API_SECRET_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("tradedesk.config.load_dotenv", lambda *a, **k: None)
    from tradedesk.venues import alpaca

    with pytest.raises(AlpacaAuthError, match="ALPACA_API_KEY_ID"):
        alpaca.from_env()


def test_auth_failure_does_not_echo_the_secret():
    """An exception message is a place secrets leak into logs and bug reports."""
    class Denying(AlpacaVenue):
        def _get(self, path, params):
            raise AlpacaAuthError(f"HTTP 403 from Alpaca for {path}")

    v = Denying(key_id="SECRET-KEY-VALUE", secret_key="SECRET-SECRET-VALUE")
    with pytest.raises(AlpacaAuthError) as exc:
        v.probe("AAPL")
    assert "SECRET-KEY-VALUE" not in str(exc.value)
    assert "SECRET-SECRET-VALUE" not in str(exc.value)


@pytest.mark.parametrize("stamp,ms", [
    ("2024-01-02T14:30:00Z", 1704205800000),
    ("2024-01-02T14:30:00.000Z", 1704205800000),
    ("2024-01-02T14:30:00+00:00", 1704205800000),
])
def test_timestamp_parsing_handles_alpacas_precision_variants(stamp, ms):
    assert _parse_ms(stamp) == ms


def test_rfc3339_round_trips():
    assert _parse_ms(_rfc3339(BASE)) == BASE
    assert _rfc3339(1704205800000) == "2024-01-02T14:30:00Z"


def test_probe_reports_which_feeds_answered():
    """The question the docs do not settle: what can THIS account actually pull."""
    class Probe(AlpacaVenue):
        def __init__(self, sip_ok, **kw):
            super().__init__(**kw)
            self.sip_ok = sip_ok

        def _get(self, path, params):
            if params.get("feed") == "sip" and not self.sip_ok:
                raise AlpacaAuthError("HTTP 403 subscription does not permit sip")
            return {"bars": [_bar(BASE)], "next_page_token": None}

    cap = Probe(True, key_id="k", secret_key="s").probe()
    assert cap.sip_available and cap.feed == "sip"

    cap = Probe(False, key_id="k", secret_key="s").probe()
    assert not cap.sip_available and cap.iex_available and cap.feed == "iex"
    assert any("sip" in n for n in cap.notes)
