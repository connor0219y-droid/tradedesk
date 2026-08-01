"""The venue seam.

Coinbase's Exchange REST API is on a deprecation path toward Advanced Trade, whose
OHLCV endpoint has a different shape (seconds rather than milliseconds, no `limit`).
Equities arrive later via Alpaca. Both are reasons to keep the fetch contract narrow
and explicit rather than letting ccxt's surface leak through the codebase.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

OHLCVRow = list[float]  # [bar_open_ms, open, high, low, close, volume]


class VenueError(Exception):
    """Fetch failed in a way that is not worth retrying."""


class SinceIgnoredError(VenueError):
    """The venue disregarded the requested start and returned something else.

    Kraken does exactly this: it answers HTTP 200 and looks healthy, but ignores
    `since` and returns only the most recent ~720 bars. A backfill loop written
    against it fills the database with one recent week labelled as years of history --
    and every check downstream passes, because the data is internally consistent.

    This is raised for any venue, not just Kraken, because the failure mode is a
    property of the contract rather than of one exchange.
    """


@runtime_checkable
class Venue(Protocol):
    name: str
    max_bars_per_request: int

    def supported_timeframes(self) -> frozenset[str]: ...

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, *, since_ms: int, limit: int
    ) -> list[OHLCVRow]:
        """Bars with open times in [since_ms, since_ms + limit * timeframe).

        Implementations must return rows sorted ascending by open time, strictly
        monotonic, with every row inside the requested window. Bars outside it are
        dropped rather than returned, so callers never have to reason about a venue's
        boundary conventions.
        """
        ...
