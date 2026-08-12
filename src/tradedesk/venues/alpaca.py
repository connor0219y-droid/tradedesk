"""Alpaca market data v2, for US equities.

THE SINGLE MOST CONSEQUENTIAL SETTING IN THIS FILE IS `adjustment`, and its default is
wrong for our purposes. Alpaca defaults to `raw`: the prices that printed, uncorrected
for splits. Over eight years that is not a subtle problem. AAPL split 4:1 in 2020 and
NVDA 10:1 in 2024; on a raw series both look like a 75% and a 90% single-day crash, and
every momentum, breakout and 52-week-high detector in the library would fire on an event
that never happened. A raw eight-year backfill would not be slightly noisy, it would be
systematically fabricated.

So this adapter requests `split` adjustment, and deliberately NOT `all`:

  * SPLIT adjustment is a pure change of units. A 4:1 split re-denominates the shares
    and carries no economic information, so correcting for it recovers the price series
    that actually traded, in consistent units.
  * DIVIDEND adjustment is economic, and it rewrites history. Under `all`, the 2019
    prices you download today are not the prices that traded in 2019 -- every past bar
    has been scaled down by every dividend paid since. For a study of price PATTERNS
    (opening ranges, 52-week highs, VWAP reclaims) that is a quiet form of lookahead:
    the level a detector fires on is a level no one could have seen at the time.

The cost of that choice is stated rather than hidden: returns measured here are price
returns, not total returns, so they understate the momentum literature's numbers by
roughly the dividend yield. For CROSS-SECTIONAL ranking, which is what the quintile
sorts do, a near-uniform ~2%/yr shortfall barely moves the ordering. For a level claim
about absolute performance it would matter, and this file is where to look when it does.

EXTENDED HOURS. Minute bars from this endpoint include pre- and post-market prints.
That is what we want -- the session model has a premarket segment and `prior.py` has had
null `premarket_high`/`premarket_low` columns waiting for it since phase 2 -- but it
means the raw feed contains 20:00 bars that belong to no segment this project models.
Filtering happens at ingest against the calendar, not here: a venue adapter's job is to
return what the venue said.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .base import OHLCVRow, SinceIgnoredError, VenueError

DATA_HOST = "https://data.alpaca.markets"

#: Our timeframe names to Alpaca's. Alpaca accepts `5Min`, `1Hour`, `1Day`; it rejects
#: our lowercase spellings outright rather than guessing, which is the good failure.
_TIMEFRAMES: dict[str, str] = {
    "1m": "1Min",
    "5m": "5Min",
    "15m": "15Min",
    "1h": "1Hour",
    "1d": "1Day",
}

#: The most bars Alpaca returns per page. Requests above this are silently capped, so
#: the pagination loop must key off `next_page_token` rather than off page size.
MAX_PAGE = 10_000


class AlpacaAuthError(VenueError):
    """Keys are missing, wrong, or lack entitlement for the requested feed."""


@dataclass
class Capability:
    """What this account can actually pull. Discovered, never assumed.

    Alpaca's docs are ambiguous about whether historical SIP bars are available on the
    free tier when the window is old enough, and the answer decides whether the intraday
    half of this study means anything -- a 5-minute bar built from IEX alone is ~2.5% of
    the tape. Rather than reason about the documentation, `probe` asks the API.
    """

    feed: str
    earliest_bar: str | None
    sip_available: bool
    iex_available: bool
    notes: list[str] = field(default_factory=list)


@dataclass
class AlpacaVenue:
    """Read-only market data client. This project never places orders."""

    key_id: str
    secret_key: str
    feed: str = "sip"
    adjustment: str = "split"
    name: str = "alpaca"
    max_bars_per_request: int = MAX_PAGE
    request_timeout_ms: int = 30_000
    max_retries: int = 5
    retry_backoff_seconds: float = 1.0
    user_agent: str = "tradedesk/0.1"

    def supported_timeframes(self) -> frozenset[str]:
        return frozenset(_TIMEFRAMES)

    # ------------------------------------------------------------------ transport

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret_key,
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }

    def _get(self, path: str, params: dict[str, str]) -> dict:
        """One GET, with backoff on 429 and 5xx.

        429 is retried because Alpaca rate-limits by the minute and a backfill will hit
        it; 403 is NOT, because it means the account lacks entitlement for the feed and
        retrying just burns the quota while the answer stays no.
        """
        url = f"{DATA_HOST}{path}?{urllib.parse.urlencode(params)}"
        delay = self.retry_backoff_seconds
        last: Exception | None = None

        for attempt in range(self.max_retries):
            req = urllib.request.Request(url, headers=self._headers())
            try:
                with urllib.request.urlopen(
                    req, timeout=self.request_timeout_ms / 1000
                ) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:400]
                if exc.code in (401, 403):
                    # Never echo the key itself; `config.redact` exists for this reason.
                    raise AlpacaAuthError(
                        f"HTTP {exc.code} from Alpaca for {path} "
                        f"(feed={params.get('feed')}): {body}"
                    ) from None
                if exc.code == 429 or exc.code >= 500:
                    last = exc
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise VenueError(f"HTTP {exc.code} from Alpaca for {path}: {body}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                last = exc
                time.sleep(delay)
                delay *= 2

        raise VenueError(f"Alpaca request failed after {self.max_retries} attempts: {last}")

    # ------------------------------------------------------------------ capability

    def probe(self, symbol: str = "AAPL") -> Capability:
        """Ask the account what it can see, instead of trusting the pricing page.

        Three questions, one call each: does SIP answer, does IEX answer, and how far
        back do bars actually go. The last matters because the study wants eight years
        and a plan that silently truncates to two would produce a backfill that looks
        complete and is not.
        """
        notes: list[str] = []
        available: dict[str, bool] = {}
        for feed in ("sip", "iex"):
            try:
                self._get(
                    f"/v2/stocks/{symbol}/bars",
                    {"timeframe": "1Day", "limit": "1", "feed": feed,
                     "start": "2024-01-02", "end": "2024-01-05"},
                )
                available[feed] = True
            except AlpacaAuthError as exc:
                available[feed] = False
                notes.append(f"{feed}: not entitled ({str(exc)[:120]})")

        best = "sip" if available.get("sip") else ("iex" if available.get("iex") else "")
        if not best:
            raise AlpacaAuthError(
                "neither SIP nor IEX returned data; check ALPACA_API_KEY_ID / "
                "ALPACA_API_SECRET_KEY"
            )

        earliest = None
        try:
            page = self._get(
                f"/v2/stocks/{symbol}/bars",
                {"timeframe": "1Day", "limit": "1", "feed": best,
                 "start": "2000-01-01", "adjustment": self.adjustment},
            )
            bars = page.get("bars") or []
            if bars:
                earliest = bars[0]["t"]
        except VenueError as exc:
            notes.append(f"earliest-bar probe failed: {str(exc)[:120]}")

        return Capability(
            feed=best, earliest_bar=earliest,
            sip_available=available.get("sip", False),
            iex_available=available.get("iex", False),
            notes=notes,
        )

    # ------------------------------------------------------------------ bars

    def fetch_ohlcv(
        self, symbol: str, timeframe: str, *, since_ms: int, limit: int
    ) -> list[OHLCVRow]:
        """Bars with open times in [since_ms, since_ms + limit * timeframe).

        The window is closed HERE rather than trusted from the response, and the result
        is checked against the request. Alpaca honours `start`, but the contract in
        `venues/base.py` exists because Kraken did not -- it answered 200 and returned
        the most recent bars regardless, filling a database with one recent week
        labelled as years of history. That check is cheap and it is the difference
        between a wrong backfill and an exception.
        """
        tf = _TIMEFRAMES.get(timeframe)
        if tf is None:
            raise VenueError(
                f"Alpaca does not serve {timeframe!r}; known: {sorted(_TIMEFRAMES)}"
            )

        step = _tf_ms(timeframe)
        end_ms = since_ms + limit * step
        params = {
            "timeframe": tf,
            "start": _rfc3339(since_ms),
            "end": _rfc3339(end_ms),
            "limit": str(min(limit, MAX_PAGE)),
            "feed": self.feed,
            "adjustment": self.adjustment,
            "sort": "asc",
        }

        rows: list[OHLCVRow] = []
        raw_seen = 0
        raw_min: int | None = None
        token: str | None = None
        while True:
            page = dict(params)
            if token:
                page["page_token"] = token
            payload = self._get(f"/v2/stocks/{symbol}/bars", page)
            for bar in payload.get("bars") or []:
                ms = _parse_ms(bar["t"])
                # Counted BEFORE clipping. The since-ignored guard has to see what the
                # venue actually sent: measured on the clipped rows it is dead code,
                # because a venue that ignored `since` returns bars that are all clipped
                # away, leaving an empty list that looks exactly like a quiet window.
                raw_seen += 1
                raw_min = ms if raw_min is None else min(raw_min, ms)
                # Drop anything outside the requested window rather than returning it,
                # so callers never reason about a venue's boundary convention.
                if since_ms <= ms < end_ms:
                    rows.append(
                        [ms, float(bar["o"]), float(bar["h"]), float(bar["l"]),
                         float(bar["c"]), float(bar["v"])]
                    )
            token = payload.get("next_page_token")
            if not token:
                break

        if raw_seen and raw_min is not None and raw_min >= end_ms:
            raise SinceIgnoredError(
                f"Alpaca returned {raw_seen} bars starting {raw_min}, entirely after "
                f"the requested window ending {end_ms} -- `start` was disregarded"
            )
        rows.sort(key=lambda r: r[0])
        # Strictly monotonic: a duplicate timestamp would violate the store's primary
        # key and, worse, would mean two different bars claim the same instant.
        out: list[OHLCVRow] = []
        for row in rows:
            if out and row[0] == out[-1][0]:
                continue
            out.append(row)
        return out


def _rfc3339(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _parse_ms(stamp: str) -> int:
    """RFC3339 to epoch ms. Alpaca returns UTC with a Z suffix and variable precision."""
    text = stamp.replace("Z", "+00:00")
    return int(datetime.fromisoformat(text).timestamp() * 1000 + 0.5)


def _tf_ms(timeframe: str) -> int:
    from ..timeutil import tf_ms

    return tf_ms(timeframe)


def from_env(feed: str | None = None) -> AlpacaVenue:
    """Build a client from the environment. Keys are never returned or logged."""
    import os

    from ..config import load_dotenv

    load_dotenv()
    key = os.environ.get("ALPACA_API_KEY_ID") or os.environ.get("APCA_API_KEY_ID")
    secret = os.environ.get("ALPACA_API_SECRET_KEY") or os.environ.get(
        "APCA_API_SECRET_KEY"
    )
    if not key or not secret:
        raise AlpacaAuthError(
            "set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY (in .env or the "
            "environment); this client is read-only and never places orders"
        )
    return AlpacaVenue(key_id=key, secret_key=secret, feed=feed or "sip")
