"""Reconstruct point-in-time S&P 500 membership from Wikipedia's revision history.

WHY THIS EXISTS. Taking today's index members and backfilling eight years is the single
most effective way to manufacture a momentum result that is not there. Every name in
such a universe survived, stayed liquid, and stayed large enough to remain in the index
-- so a cross-sectional momentum sort is being run on a sample selected for having gone
up. Delisted, acquired and demoted names are exactly the losers the strategy would have
held, and they are exactly the ones missing.

WHY REVISION HISTORY IS A LEGITIMATE SOURCE. The article as it stood on 2018-08-01 lists
the constituents as its editors understood them ON 2018-08-01. It cannot contain
knowledge of a 2023 acquisition, which is the property that matters: the reconstruction
is not forward-looking. Wikipedia's own "changes" table would have been easier, but it
has since been removed from the page; revision history is what remains and it is the
better source anyway, because it is a snapshot rather than a derived diff.

WHAT THIS IS NOT. It is not CRSP. Three limitations, all of which are recorded in the
generated file's header so they travel with the data:

  1. EDITOR LAG. An index change is reflected when someone edits the article, typically
     within days. Membership near a change date can therefore be off by a few days.
     The lag is backward, not forward, so it cannot leak future information.
  2. TICKER REUSE AND RENAMES. A ticker is not a permanent identifier. If a symbol was
     reassigned to a different company during the window, this reconstruction cannot
     tell them apart -- there is no CIK-level continuity here even though the modern
     revisions carry CIKs.
  3. SAMPLED MONTHLY. Membership is captured at month ends. A name added and removed
     inside a single month is invisible. For monthly-rebalanced cross-sectional
     strategies, which is what consumes this, month-end sampling is the natural grain.

Run: uv run python tools/build_universe.py
Writes: universe/sp500_membership.csv  (committed; regenerate deliberately, not on a
schedule -- the file is data, and a silent change to it changes every result built on it)
"""

from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

API = "https://en.wikipedia.org/w/api.php"
RAW = "https://en.wikipedia.org/w/index.php"
TITLE = "List of S&P 500 companies"
UA = "tradedesk/0.1 (research; contact via repository)"

START = date(2018, 8, 1)
END = date(2026, 8, 1)

#: Ticker-shaped tokens. Class-B share tickers legitimately carry a dot (BRK.B) or a
#: hyphen (BRK-B, the convention Alpaca uses), so both survive.
_TICKER = re.compile(r"^[A-Z][A-Z.\-]{0,6}$")

#: Wikitable cell separator: `||` between data cells, `!!` between header cells.
_CELL_SEP = re.compile(r"\|\||!!")

#: The symbol cell has worn several costumes over eight years: {{NyseSymbol|MMM}} and
#: {{NasdaqSymbol|ABMD}} in 2018, {{NYSE|MMM}} later, and a bare ticker today. Rather
#: than enumerate the templates, strip markup and take the first ticker-shaped token --
#: which is stable across every format the article has used.
_STRIP = [
    (re.compile(r"\{\{[^|}]*\|"), ""),      # template head: {{NyseSymbol|
    (re.compile(r"\}\}"), ""),
    (re.compile(r"\[\[(?:[^|\]]*\|)?"), ""),  # wiki link, keeping the display text
    (re.compile(r"\]\]"), ""),
    (re.compile(r"\[[^ \]]+ ?"), ""),        # external link
    (re.compile(r"\]"), ""),
    (re.compile(r"<[^>]+>"), ""),            # inline html
    (re.compile(r"'{2,}"), ""),              # bold/italic
]


def _get(url: str, params: dict[str, str] | None = None, *, retries: int = 6) -> str:
    """GET with backoff. Wikipedia rate-limits, and a 429 mid-run would otherwise
    abort a 200-request generation after several minutes of work."""
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    delay = 2.0
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                if attempt == retries - 1:
                    raise
                time.sleep(delay)
                delay *= 2
                continue
            raise
    raise RuntimeError("unreachable")


def revision_as_of(when: date) -> tuple[int, str] | None:
    """The latest revision at or before `when`. None if the article did not exist."""
    payload = json.loads(
        _get(API, {
            "action": "query", "prop": "revisions", "titles": TITLE,
            "rvlimit": "1", "rvdir": "older", "rvprop": "ids|timestamp",
            "rvstart": f"{when.isoformat()}T00:00:00Z", "format": "json",
        })
    )
    pages = payload.get("query", {}).get("pages", {})
    for page in pages.values():
        revs = page.get("revisions") or []
        if revs:
            return int(revs[0]["revid"]), revs[0]["timestamp"]
    return None


def _clean(cell: str) -> str:
    for pattern, repl in _STRIP:
        cell = pattern.sub(repl, cell)
    return cell.strip()


def _cells(row: str) -> list[str]:
    """Split one wikitable row into cells.

    Wikitable permits both `| a || b || c` on one line and one `| a` per line, and the
    article has used both. Handling only the first silently returns a single cell per
    row, which then looks like a parse that worked.
    """
    out: list[str] = []
    for line in row.split("\n"):
        line = line.strip()
        if not line.startswith(("|", "!")):
            continue
        line = line.lstrip("|!")
        # Header cells are separated by `!!` and data cells by `||`. Splitting on only
        # one of them collapses the header into a single cell, and the symbol-column
        # lookup then silently falls back to column 0 -- which is how company names got
        # read as tickers in the first place.
        out.extend(_CELL_SEP.split(line))
    return out


def _symbol_column(table: str) -> int:
    """Which column holds the ticker, read from the header rather than assumed.

    THE BUG THIS EXISTS TO PREVENT. The article swapped its first two columns in early
    2019: `Ticker symbol !! Security` became `Security !! Symbol`. Code that took the
    first cell then read COMPANY NAMES as tickers -- and because a name like "3M" or
    "Aflac" is ticker-shaped, it did not fail loudly. It returned 50 plausible-looking
    symbols out of 505, which is exactly the kind of half-success that reaches a result
    table intact.
    """
    for row in table.split("\n|-"):
        if "!" not in row:
            continue
        headers = [_clean(c).lower() for c in _cells(row)]
        for idx, name in enumerate(headers):
            if "symbol" in name or "ticker" in name:
                return idx
        if headers:
            break  # first header row seen and it named no symbol column
    raise ValueError("no symbol/ticker column found in the constituents table header")


def tickers_from_wikitext(text: str) -> list[str]:
    """Every ticker in the constituents table of one revision.

    Reads the first table in the article -- the constituents table in every revision
    across this window -- locates the symbol column by name, and takes the ticker-shaped
    token from that column of each row.
    """
    start = text.find("{|")
    if start < 0:
        return []
    end = text.find("|}", start)
    table = text[start : end if end > 0 else len(text)]

    col = _symbol_column(table)
    out: list[str] = []
    seen: set[str] = set()
    for row in table.split("\n|-"):
        body = row.strip()
        # Header rows use `!`; skip them rather than parsing "Ticker symbol" as a ticker.
        if not body or body.lstrip("|").startswith("!"):
            continue
        cells = _cells(body)
        if len(cells) <= col:
            continue
        token = _clean(cells[col]).split()
        if not token:
            continue
        cand = token[0].rstrip(",.")
        if _TICKER.match(cand) and cand not in seen:
            seen.add(cand)
            out.append(cand)
    return out


def month_ends(start: date, end: date) -> list[date]:
    out, cur = [], date(start.year, start.month, 1)
    while cur <= end:
        nxt = date(cur.year + (cur.month == 12), (cur.month % 12) + 1, 1)
        out.append(nxt - timedelta(days=1))
        cur = nxt
    return [d for d in out if start <= d <= end]


def main() -> int:
    dest = Path(__file__).resolve().parents[1] / "universe" / "sp500_membership.csv"
    dest.parent.mkdir(parents=True, exist_ok=True)

    rows: list[tuple[str, str, str]] = []
    for when in month_ends(START, END):
        rev = revision_as_of(when)
        if rev is None:
            print(f"{when}: no revision", file=sys.stderr)
            continue
        revid, stamp = rev
        tickers = tickers_from_wikitext(_get(RAW, {"oldid": str(revid), "action": "raw"}))
        if len(tickers) < 400:
            # The article has always listed ~500 names. A short parse means the format
            # changed and the extractor silently half-worked -- refuse it rather than
            # writing a truncated universe that looks plausible.
            print(
                f"{when}: parsed only {len(tickers)} tickers from rev {revid} "
                f"({stamp}) -- refusing to write a partial snapshot",
                file=sys.stderr,
            )
            return 1
        for t in tickers:
            rows.append((when.isoformat(), t, str(revid)))
        print(f"{when}: {len(tickers)} constituents (rev {revid} @ {stamp})")
        time.sleep(1.0)  # be a polite API citizen; ~200 requests per run

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with dest.open("w", newline="") as fh:
        fh.write(
            f"# S&P 500 point-in-time membership, month-end snapshots\n"
            f"# generated {generated} by tools/build_universe.py\n"
            f"# source: en.wikipedia.org revision history of '{TITLE}'\n"
            f"# LIMITS: editor lag of a few days around index changes; no CIK-level\n"
            f"#   continuity, so a reused ticker is indistinguishable; month-end\n"
            f"#   sampling, so a name added and dropped within one month is invisible.\n"
            f"# This is NOT survivorship-free in the CRSP sense, but it does contain\n"
            f"#   the names that were later delisted, acquired or demoted -- which is\n"
            f"#   the bias that matters for momentum.\n"
        )
        writer = csv.writer(fh)
        writer.writerow(["as_of", "ticker", "revid"])
        writer.writerows(rows)

    uniq = {t for _, t, _ in rows}
    print(f"\nwrote {dest}: {len(rows)} rows, {len(uniq)} distinct tickers")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
