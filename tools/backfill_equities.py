"""Backfill US equity bars from Alpaca, through the integrity guard.

TWO SELECTION DECISIONS, BOTH MADE CAUSALLY, because both are places a survivorship
bias would walk straight back in after all the work spent removing it.

  WHICH NAMES EXIST is point-in-time index membership (`universe.py`): every ticker that
  was ever an S&P 500 constituent between 2018-08 and 2026-07, 681 of them, including
  the ones that were later acquired, demoted or seized.

  WHICH 50 GET INTRADAY DATA is ranked on liquidity measured STRICTLY BEFORE the study
  window opens -- 2018-01-01 to 2018-07-31, entirely prior to the 2018-08-01 start. The
  obvious alternative, ranking by today's liquidity or by liquidity over the whole
  sample, picks the names that went on to become heavily traded. That is the same
  mistake as picking today's index members, applied to the intraday universe, and it
  would matter most for exactly the strategies the 5m data exists to test.

EVERY BAR PASSES THROUGH `equity_integrity.clean` BEFORE IT IS STORED. Alpaca serves
delisted symbols -- which is what makes this possible at all -- but for some of them it
also serves fabrications: SBNY has 509 zero-volume bars frozen at 70.00 after the bank
was seized, and CA has 1,323 frozen at its buyout price followed by a different company
wearing the same ticker. Storing those and cleaning later would mean every consumer has
to remember to clean; the store holds bars that were real trades.

Run:
    uv run python tools/backfill_equities.py --pick-intraday   # writes the 50-name list
    uv run python tools/backfill_equities.py --daily
    uv run python tools/backfill_equities.py --intraday
"""

from __future__ import annotations

import argparse
import csv
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import polars as pl

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from tradedesk import store  # noqa: E402
from tradedesk.calendars import EquityCalendar  # noqa: E402
from tradedesk.config import load_config, load_dotenv  # noqa: E402
from tradedesk.equity_integrity import clean  # noqa: E402
from tradedesk.ingest import rows_to_frame  # noqa: E402
from tradedesk.timeutil import CALENDAR_VERSION, to_ms  # noqa: E402
from tradedesk.universe import default as default_universe  # noqa: E402
from tradedesk.venues.alpaca import from_env  # noqa: E402
from tradedesk.venues.base import VenueError  # noqa: E402

VENUE = "alpaca"
STUDY_START = date(2018, 8, 1)
STUDY_END = date(2026, 8, 1)

#: Liquidity is measured over this window ONLY, which ends before the study begins.
LIQ_START, LIQ_END = date(2018, 1, 1), date(2018, 8, 1)
INTRADAY_NAMES = 50
INTRADAY_LIST = REPO_ROOT / "universe" / "intraday50_2018_liquidity.csv"

#: History fetched before the study window, so formation windows are warm on day one.
#: 420 calendar days matches the tenure lead buffer in equity_integrity.
WARMUP_DAYS = 420


def _ms(d: date) -> int:
    return to_ms(datetime(d.year, d.month, d.day, tzinfo=timezone.utc))


def _frame(rows, symbol: str, timeframe: str) -> pl.DataFrame:
    return rows_to_frame(
        rows, venue=VENUE, symbol=symbol, timeframe=timeframe,
        calendar_version=CALENDAR_VERSION,
        ingested_at_ms=int(time.time() * 1000),
    )


def pick_intraday(venue, members) -> list[str]:
    """The 50 most liquid names of the 2018 index, by median dollar volume in H1 2018.

    Median rather than mean: a single earnings day or an index-rebalance print can
    multiply a name's mean dollar volume, and the question here is which names are
    routinely liquid, not which had one busy session.
    """
    asof = members.as_of(LIQ_END)
    print(f"ranking {len(asof)} constituents as of {LIQ_END} on {LIQ_START}..{LIQ_END}")
    scored: list[tuple[float, str]] = []
    for i, sym in enumerate(sorted(asof), 1):
        try:
            rows = venue.fetch_ohlcv(
                sym, "1d", since_ms=_ms(LIQ_START),
                limit=(_ms(LIQ_END) - _ms(LIQ_START)) // 86_400_000,
            )
        except VenueError as exc:
            print(f"  {sym}: {str(exc)[:70]}", file=sys.stderr)
            continue
        real = [r for r in rows if r[5] > 0]
        if len(real) < 60:
            continue
        dollar = sorted(r[4] * r[5] for r in real)
        scored.append((dollar[len(dollar) // 2], sym))
        if i % 100 == 0:
            print(f"  ...{i}/{len(asof)}")

    scored.sort(reverse=True)
    top = [sym for _, sym in scored[:INTRADAY_NAMES]]

    INTRADAY_LIST.parent.mkdir(parents=True, exist_ok=True)
    with INTRADAY_LIST.open("w", newline="") as fh:
        fh.write(
            f"# 50 most liquid S&P 500 names by MEDIAN DAILY DOLLAR VOLUME over\n"
            f"# {LIQ_START}..{LIQ_END} -- strictly before the {STUDY_START} study start,\n"
            f"# so the selection uses no information from the period being tested.\n"
            f"# Ranking on today's liquidity, or on the whole sample, would pick the\n"
            f"# names that went on to become heavily traded -- the same survivorship\n"
            f"# mistake as using today's index members.\n"
            f"# generated {datetime.now(timezone.utc):%Y-%m-%dT%H:%M:%SZ}\n"
        )
        w = csv.writer(fh)
        w.writerow(["rank", "ticker", "median_daily_dollar_volume"])
        for rank, (dv, sym) in enumerate(scored[:INTRADAY_NAMES], 1):
            w.writerow([rank, sym, f"{dv:.0f}"])
    print(f"\nwrote {INTRADAY_LIST}")
    print("top 10:", ", ".join(top[:10]))
    return top


def load_intraday_list() -> list[str]:
    if not INTRADAY_LIST.exists():
        raise SystemExit(f"{INTRADAY_LIST} missing -- run with --pick-intraday first")
    with INTRADAY_LIST.open() as fh:
        rows = csv.DictReader(line for line in fh if not line.startswith("#"))
        return [r["ticker"] for r in rows]


def backfill(venue, con, members, symbols: list[str], timeframe: str) -> None:
    """Fetch, clean and store one timeframe for a list of symbols."""
    cal = EquityCalendar()
    step_ms = 86_400_000 if timeframe == "1d" else 300_000
    total_in = total_out = total_written = 0
    flagged: list[str] = []

    for i, sym in enumerate(symbols, 1):
        tenure = members.tenure(sym)
        # Fetch from the warmup point so formation windows are warm at the study start.
        start = _ms(STUDY_START) - WARMUP_DAYS * 86_400_000
        end = _ms(STUDY_END)
        try:
            rows = venue.fetch_ohlcv(
                sym, timeframe, since_ms=start, limit=(end - start) // step_ms
            )
        except VenueError as exc:
            print(f"  [{i}/{len(symbols)}] {sym}: FETCH FAILED {str(exc)[:70]}",
                  file=sys.stderr)
            continue
        if not rows:
            print(f"  [{i}/{len(symbols)}] {sym}: no bars")
            continue

        df = _frame(rows, sym, timeframe)
        cleaned, report = clean(df, sym, tenure=tenure)
        total_in += report.bars_in
        total_out += report.bars_out
        if not report.clean or report.zero_volume_dropped or report.outside_tenure_dropped:
            flagged.append(report.summary())

        written = store.insert_bars(con, cleaned) if not cleaned.is_empty() else 0
        total_written += written
        if i % 25 == 0 or i == len(symbols):
            print(f"  [{i}/{len(symbols)}] {sym}: {report.bars_in}->{report.bars_out} "
                  f"({written} new) · running total {total_written:,} bars")

    print(f"\n{timeframe}: {total_in:,} fetched -> {total_out:,} kept -> "
          f"{total_written:,} written")
    print(f"{len(flagged)} symbols had bars removed or flagged:")
    for line in flagged[:40]:
        print(f"    {line}")
    if len(flagged) > 40:
        print(f"    ... and {len(flagged) - 40} more")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pick-intraday", action="store_true")
    ap.add_argument("--daily", action="store_true")
    ap.add_argument("--intraday", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="first N symbols (smoke test)")
    args = ap.parse_args()

    load_dotenv()
    cfg = load_config()
    venue = from_env()
    members = default_universe()
    con = store.connect(cfg.data.db_path)
    store.init_schema(con)

    if args.pick_intraday:
        pick_intraday(venue, members)

    if args.daily:
        syms = sorted(members.all_tickers())
        if args.limit:
            syms = syms[: args.limit]
        print(f"\n=== DAILY: {len(syms)} names, {STUDY_START}..{STUDY_END} ===")
        backfill(venue, con, members, syms, "1d")

    if args.intraday:
        syms = load_intraday_list()
        if args.limit:
            syms = syms[: args.limit]
        print(f"\n=== 5m: {len(syms)} names, {STUDY_START}..{STUDY_END} ===")
        backfill(venue, con, members, syms, "5m")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
