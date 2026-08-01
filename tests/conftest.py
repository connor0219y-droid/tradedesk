"""Shared fixtures. No test in this suite touches the network."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from tradedesk import store
from tradedesk.config import load_config
from tradedesk.timeutil import tf_ms
from tradedesk.venues.base import OHLCVRow, SinceIgnoredError

BASE_MS = 1_700_000_000_000 // 300_000 * 300_000  # an aligned 5m boundary


@pytest.fixture
def cfg(tmp_path):
    base = load_config()
    data = replace(base.data, db_path=tmp_path / "test.duckdb", history_start=date(2024, 1, 1))
    return replace(base, data=data)


@pytest.fixture
def con(cfg):
    c = store.connect(cfg.data.db_path)
    store.init_schema(c)
    yield c
    c.close()


def make_rows(start_ms: int, n: int, timeframe: str = "5m", *, price: float = 100.0,
              skip: set[int] | None = None) -> list[OHLCVRow]:
    """A clean ascending bar series. `skip` omits bar indices, as a sparse venue does."""
    step = tf_ms(timeframe)
    skip = skip or set()
    rows: list[OHLCVRow] = []
    for i in range(n):
        if i in skip:
            continue
        p = price + i
        rows.append([start_ms + i * step, p, p + 1.0, p - 1.0, p + 0.5, 10.0])
    return rows


class FakeVenue:
    """An in-memory venue with the real one's semantics.

    Serves from a fixed bar universe, honours `since`, omits no-trade buckets the way
    Coinbase does, and can be told to include the currently-forming bar.
    """

    name = "coinbase"

    def __init__(self, rows: list[OHLCVRow], *, max_bars_per_request: int = 300):
        self.universe = sorted(rows, key=lambda r: r[0])
        self.max_bars_per_request = max_bars_per_request
        self.request_count = 0
        self.requests: list[tuple[int, int]] = []

    def supported_timeframes(self):
        return frozenset({"1m", "5m", "15m", "1h"})

    def fetch_ohlcv(self, symbol, timeframe, *, since_ms, limit):
        self.request_count += 1
        self.requests.append((since_ms, limit))
        end = since_ms + limit * tf_ms(timeframe)
        return [list(r) for r in self.universe if since_ms <= r[0] < end]


class SinceIgnoringVenue(FakeVenue):
    """Behaves like Kraken: answers 200, ignores `since`, returns the newest bars."""

    def fetch_ohlcv(self, symbol, timeframe, *, since_ms, limit):
        self.request_count += 1
        rows = [list(r) for r in self.universe[-limit:]]
        window_end = since_ms + limit * tf_ms(timeframe)
        if rows and min(int(r[0]) for r in rows) >= window_end:
            raise SinceIgnoredError(
                f"venue ignored since={since_ms}; returned newest bars instead"
            )
        return rows
