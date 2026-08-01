"""Coinbase Exchange adapter (ccxt id `coinbaseexchange`).

Behaviour verified live on 2026-08-01 against ccxt 4.5.70:

  * timestamps are bar OPEN time, UTC epoch milliseconds
  * `since` is inclusive at both the wire and ccxt layers (first bar == since exactly)
  * ccxt returns rows sorted ascending and strictly monotonic
  * the cap is 300 buckets per request, REJECTED (HTTP 400) rather than truncated
  * a request with `start > now` hard-errors rather than returning an empty list
  * buckets with no trades are omitted entirely, so a short response is normal
  * publication lags a bucket's close by ~140s, so "not the current bucket" is not
    a sufficient test for finality

We deliberately do NOT use ccxt's `params={'paginate': True}`: it caps at 10 calls,
i.e. 3,000 bars, and returns a short list with no error and no warning. That is the
single most likely way to ship a backfill that looks like it worked while silently
missing 98% of history.
"""

from __future__ import annotations

import time

import ccxt

from ..timeutil import tf_ms
from .base import OHLCVRow, SinceIgnoredError, VenueError

# Coinbase serves only these. 30m and 4h are rejected outright and must be derived by
# aggregation from a stored base timeframe.
SUPPORTED = frozenset({"1m", "5m", "15m", "1h", "6h", "1d"})


class CoinbaseVenue:
    name = "coinbase"

    def __init__(
        self,
        *,
        max_bars_per_request: int = 300,
        user_agent: str = "tradedesk/0.1",
        timeout_ms: int = 20_000,
        max_retries: int = 5,
        backoff_seconds: float = 1.0,
        client: object | None = None,
    ) -> None:
        self.max_bars_per_request = max_bars_per_request
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self.request_count = 0
        # The bare urllib User-Agent gets HTTP 403 from this API; ccxt sets its own,
        # but we make ours explicit so the traffic is identifiable.
        self.client = client or ccxt.coinbaseexchange(
            {
                "enableRateLimit": True,
                "timeout": timeout_ms,
                "headers": {"User-Agent": user_agent},
            }
        )

    def supported_timeframes(self) -> frozenset[str]:
        return SUPPORTED

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, *, since_ms: int, limit: int
    ) -> list[OHLCVRow]:
        if timeframe not in SUPPORTED:
            raise VenueError(
                f"coinbase does not serve {timeframe!r}; supported: {sorted(SUPPORTED)}"
            )
        if limit > self.max_bars_per_request:
            raise VenueError(
                f"limit {limit} exceeds venue cap {self.max_bars_per_request}"
            )

        step = tf_ms(timeframe)
        window_end = since_ms + limit * step
        raw = self._fetch_with_retry(symbol, timeframe, since_ms, limit)

        if not raw:
            return []

        # Guard 1: did the venue honour `since` at all? Every returned bar sitting
        # beyond the requested window means it answered a different question.
        if min(int(r[0]) for r in raw) >= window_end:
            raise SinceIgnoredError(
                f"{self.name} ignored since={since_ms} for {symbol} {timeframe}: "
                f"returned bars start at {min(int(r[0]) for r in raw)}, "
                f"past the requested window end {window_end}"
            )

        # Guard 2: Coinbase may return buckets preceding the declared start, and ccxt's
        # sort order is a property of the version we happen to have installed. Sorting
        # explicitly costs nothing; trusting it costs a silently reversed history,
        # which passes every OHLC invariant check there is.
        rows = sorted((list(r) for r in raw), key=lambda r: int(r[0]))
        rows = [r for r in rows if since_ms <= int(r[0]) < window_end]

        # Guard 3: strict monotonicity after filtering.
        for prev, cur in zip(rows, rows[1:]):
            if int(cur[0]) <= int(prev[0]):
                raise VenueError(
                    f"non-monotonic timestamps from {self.name}: {prev[0]} then {cur[0]}"
                )
        return rows

    def _fetch_with_retry(
        self, symbol: str, timeframe: str, since_ms: int, limit: int
    ) -> list[OHLCVRow]:
        delay = self.backoff_seconds
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                self.request_count += 1
                return self.client.fetch_ohlcv(
                    symbol, timeframe, since=since_ms, limit=limit
                )
            except ccxt.NetworkError as exc:
                # DDoSProtection, RateLimitExceeded, ExchangeNotAvailable, OnMaintenance
                # and RequestTimeout all land here. Transient: back off and retry.
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(delay)
                    delay *= 2
            except ccxt.ExchangeError as exc:
                # BadRequest, BadSymbol, NotSupported, AuthenticationError. Retrying
                # cannot help and would just hammer the API with the same bad request.
                raise VenueError(f"{self.name} rejected the request: {exc}") from exc
        raise VenueError(
            f"{self.name} unreachable after {self.max_retries} attempts: {last_error}"
        ) from last_error
