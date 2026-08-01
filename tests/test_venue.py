"""The Coinbase adapter's defensive guards.

These test the adapter against a fake ccxt client, so no network is involved. Each
guard exists because the real API or the real ccxt version does something surprising.
"""

from __future__ import annotations

import ccxt
import pytest

from tradedesk.timeutil import tf_ms
from tradedesk.venues.base import SinceIgnoredError, VenueError
from tradedesk.venues.coinbase import CoinbaseVenue

STEP = tf_ms("5m")
BASE = 1_700_000_000_000 // STEP * STEP


class FakeClient:
    def __init__(self, rows, *, raises=None, raise_times=0):
        self.rows = rows
        self.raises = raises
        self.raise_times = raise_times
        self.calls = 0

    def fetch_ohlcv(self, symbol, timeframe, since=None, limit=None):
        self.calls += 1
        if self.raises and self.calls <= self.raise_times:
            raise self.raises
        return [list(r) for r in self.rows]


def _venue(client):
    return CoinbaseVenue(client=client, backoff_seconds=0)


def _rows(n, start=BASE):
    return [[start + i * STEP, 100.0, 101.0, 99.0, 100.5, 5.0] for i in range(n)]


def test_descending_response_is_sorted():
    """ccxt's sort order is a property of the installed version.

    Sorting explicitly costs nothing. Trusting it costs a silently reversed history --
    which passes every OHLC invariant check there is, because each individual bar is
    still perfectly valid.
    """
    venue = _venue(FakeClient(list(reversed(_rows(10)))))
    got = venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=300)
    ts = [int(r[0]) for r in got]
    assert ts == sorted(ts)


def test_bars_outside_the_requested_window_are_dropped():
    """Coinbase may return buckets preceding the declared start."""
    rows = _rows(5, start=BASE - 10 * STEP) + _rows(5, start=BASE)
    venue = _venue(FakeClient(rows))
    got = venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=5)
    assert [int(r[0]) for r in got] == [BASE + i * STEP for i in range(5)]


def test_since_ignored_is_detected():
    """Every returned bar past the requested window means a different question was answered."""
    far_future = _rows(10, start=BASE + 10_000 * STEP)
    venue = _venue(FakeClient(far_future))
    with pytest.raises(SinceIgnoredError):
        venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=300)


def test_unsupported_timeframe_is_refused():
    """Coinbase serves no 30m or 4h; those must be derived by aggregation."""
    venue = _venue(FakeClient([]))
    for tf in ("30m", "4h"):
        with pytest.raises((VenueError, ValueError)):
            venue.fetch_ohlcv("BTC/USD", tf, since_ms=BASE, limit=300)


def test_limit_above_venue_cap_is_refused():
    """The 300-bar cap is rejected server-side, not truncated -- fail before the call."""
    venue = _venue(FakeClient([]))
    with pytest.raises(VenueError):
        venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=301)


def test_network_errors_are_retried():
    client = FakeClient(_rows(5), raises=ccxt.RequestTimeout("timeout"), raise_times=2)
    venue = _venue(client)
    got = venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=300)
    assert len(got) == 5
    assert client.calls == 3


def test_exchange_errors_are_not_retried():
    """Retrying a BadRequest just hammers the API with the same bad request."""
    client = FakeClient(_rows(5), raises=ccxt.BadRequest("nope"), raise_times=99)
    venue = _venue(client)
    with pytest.raises(VenueError):
        venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=300)
    assert client.calls == 1


def test_duplicate_timestamps_are_rejected():
    rows = _rows(5)
    rows[3] = list(rows[2])
    venue = _venue(FakeClient(rows))
    with pytest.raises(VenueError):
        venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=300)


def test_empty_response_is_not_an_error():
    """A quiet window is normal on a sparse venue."""
    venue = _venue(FakeClient([]))
    assert venue.fetch_ohlcv("BTC/USD", "5m", since_ms=BASE, limit=300) == []
