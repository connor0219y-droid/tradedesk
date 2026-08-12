"""Equity bar integrity: what a delisted ticker's data actually looks like.

DISCOVERED BY PROBING ALPACA BEFORE BACKFILLING, and the reason this module exists at
all. Alpaca serves bars for delisted symbols, which is what makes a point-in-time
universe possible. But for some of them it keeps serving bars long after the company
stopped existing, and those bars are not data:

    SBNY  Signature Bank, seized 2023-03-12. Real bars stop 2023-03-10. Alpaca then
          emits 509 further daily bars -- through 2025-03-21 -- every one of them
          zero volume with open == high == low == close == 70.00.

    CA    CA Technologies, acquired by Broadcom 2018-11-05 at 44.44. Real bars stop
          there. 1,323 zero-volume bars at exactly 44.44 follow, and then, from
          2023-12-15, bars WITH volume at around 25.00 on a few hundred shares a day
          -- a different security that inherited the ticker.

Three separate ways that corrupts a result, none of which announce themselves:

  1. A frozen price makes a total loss look like a flat position. A strategy holding
     SBNY through the seizure books 0% instead of -100%. That is the survivorship bias
     the point-in-time universe was built to remove, sneaking back in through the bars
     -- and it is worse than the original, because the name IS in the universe, so the
     problem looks handled.
  2. A flat bar has a true range of zero. Feed enough of them to ATR and the stop
     distance collapses toward zero, at which point every R-multiple is divided by
     something near nothing.
  3. A reused ticker splices two companies into one price series. CA shows a 43% drop
     on 2023-12-15 that happened to no one, and any momentum or reversal detector will
     fire on it.

THE RULES, in the order they apply. Each is conservative in the same direction: when in
doubt, treat data as absent rather than as a price, because this project already knows
how to handle absence and has no way to detect a plausible fiction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

import polars as pl

from .calendars import MS_PER_DAY, CalendarError

#: THE BUFFER IS ASYMMETRIC, and the asymmetry is load-bearing.
#:
#: BEFORE a name joins the index it still needs history, because a 12-month formation
#: window with a one-month skip reaches 273 trading days back -- roughly 400 calendar
#: days. Clipping tightly at the join date would leave every recently-added name
#: unrankable for its first year, which silently biases the universe toward long-tenured
#: names: precisely the survivorship problem the point-in-time universe was built to
#: remove, reintroduced by the cleaning step. Measured on SBNY, a 10-day lead buffer cut
#: it from 1,145 usable bars to 306.
#:
#: AFTER it leaves, a few days is enough. That buffer only covers the lag between an
#: index change and the Wikipedia edit recording it; extending it further is what lets a
#: reused ticker back in.
TENURE_LEAD_DAYS = 420
TENURE_TRAIL_DAYS = 10

#: A gap longer than this inside a name's kept history means the two sides may not be
#: the same instrument. Flagged rather than silently stitched.
DISCONTINUITY_DAYS = 30


@dataclass
class IntegrityReport:
    symbol: str
    bars_in: int
    bars_out: int
    zero_volume_dropped: int = 0
    outside_tenure_dropped: int = 0
    frozen_run_max: int = 0
    discontinuities: list[tuple[date, date, int]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.discontinuities and not self.notes

    def summary(self) -> str:
        bits = [f"{self.symbol}: {self.bars_in} -> {self.bars_out} bars"]
        if self.zero_volume_dropped:
            bits.append(f"{self.zero_volume_dropped} zero-volume dropped")
        if self.outside_tenure_dropped:
            bits.append(f"{self.outside_tenure_dropped} outside tenure")
        if self.frozen_run_max:
            bits.append(f"longest frozen run {self.frozen_run_max}")
        for lo, hi, days in self.discontinuities:
            bits.append(f"GAP {lo}..{hi} ({days}d)")
        return "; ".join(bits + self.notes)


def drop_zero_volume(df: pl.DataFrame) -> tuple[pl.DataFrame, int]:
    """Rule 1: a bar with no volume is not a trade, so it is ABSENT, not flat.

    This is the same convention the crypto side already uses -- on a 24/7 venue a
    missing bar means no trades occurred -- applied to a case where the venue emits a
    row anyway. Dropping the row rather than keeping a zero-volume price is what lets
    the existing contiguity machinery see the hole and refuse to compute across it.

    Deliberately keyed on VOLUME rather than on flat OHLC. A genuinely illiquid name can
    print a real trade at one price all day, and that bar is data; a zero-volume bar is
    not, whatever its OHLC says.
    """
    if df.is_empty():
        return df, 0
    kept = df.filter(pl.col("volume") > 0)
    return kept, df.height - kept.height


def clip_to_tenure(
    df: pl.DataFrame,
    tenure: tuple[date, date] | None,
    *,
    lead_days: int = TENURE_LEAD_DAYS,
    trail_days: int = TENURE_TRAIL_DAYS,
) -> tuple[pl.DataFrame, int]:
    """Rule 2: keep only bars from the window in which the ticker was the company.

    This is the defence against ticker reuse, and it works because the point-in-time
    universe already knows when each name was a member. CA Technologies was in the index
    until November 2018; whatever traded under `CA` in 2024 is irrelevant to this study
    regardless of what it was, so it never enters the store.

    See the note on the buffer constants for why the two sides differ by a factor of
    forty. Short version: formation windows reach backwards, ticker reuse happens
    forwards.
    """
    if tenure is None or df.is_empty():
        return df, 0
    lo, hi = tenure
    lo -= timedelta(days=lead_days)
    hi += timedelta(days=trail_days)
    kept = df.filter(
        (pl.col("session_date") >= lo) & (pl.col("session_date") <= hi)
    )
    return kept, df.height - kept.height


def longest_frozen_run(df: pl.DataFrame) -> int:
    """Longest run of consecutive bars with an identical, zero-range price.

    Reported rather than acted on. After rule 1 there should be none, so a non-zero
    value here means a venue emitted frozen prices WITH volume attached -- which would
    be a different and more alarming problem than the one this module was written for,
    and it should surface as a number rather than be silently cleaned away.
    """
    if df.is_empty():
        return 0
    flat = df.select(
        (
            (pl.col("high") == pl.col("low"))
            & (pl.col("open") == pl.col("close"))
            & (pl.col("close") == pl.col("close").shift(1))
        ).alias("f")
    )["f"].fill_null(False)

    best = run = 0
    for v in flat:
        run = run + 1 if v else 0
        best = max(best, run)
    return best


def find_discontinuities(
    df: pl.DataFrame, *, max_gap_days: int = DISCONTINUITY_DAYS
) -> list[tuple[date, date, int]]:
    """Calendar gaps large enough that the two sides may be different instruments.

    Rule 3, and it is a FLAG rather than a fix. A one-month hole in a large-cap's daily
    history has no innocent explanation, but the right response depends on which name it
    is -- so this surfaces it for the quality report instead of guessing.
    """
    if df.height < 2:
        return []
    days = df["session_date"].to_list()
    out: list[tuple[date, date, int]] = []
    for prev, cur in zip(days, days[1:]):
        delta = (cur - prev).days
        if delta > max_gap_days:
            out.append((prev, cur, delta))
    return out


def drop_after_hours(
    df: pl.DataFrame, calendar, *, timeframe_ms: int
) -> tuple[pl.DataFrame, int]:
    """Keep only bars inside the DECLARED session: premarket open through the close.

    Alpaca's minute bars include the 16:00-20:00 post-market, which is a quarter of the
    series -- 97,758 bars of AAPL's 407,635. PREREGISTRATION.md declares a session model
    of premarket 04:00-09:30 plus RTH 09:30-16:00; after-hours is not part of it, so
    those bars are removed before anything computes over them rather than being carried
    along and filtered by every consumer that remembers to.

    Bars at or coarser than a day are returned untouched: a daily bar spans the session
    by definition, and there is no intraday window for it to sit outside of.
    """
    if df.is_empty() or timeframe_ms >= MS_PER_DAY:
        return df, 0

    lo_map: dict[object, int | None] = {}
    hi_map: dict[object, int | None] = {}
    for day in df["session_date"].unique().to_list():
        try:
            w = calendar.window(day)
        except CalendarError:
            lo_map[day] = hi_map[day] = None
            continue
        lo_map[day] = (
            w.premarket_open_ms if w.premarket_open_ms is not None else w.open_ms
        )
        hi_map[day] = w.close_ms

    marked = df.with_columns(
        pl.col("session_date").replace_strict(lo_map, return_dtype=pl.Int64).alias("_lo"),
        pl.col("session_date").replace_strict(hi_map, return_dtype=pl.Int64).alias("_hi"),
    )
    kept = marked.filter(
        pl.col("_lo").is_not_null()
        & (pl.col("bar_open_ms") >= pl.col("_lo"))
        & (pl.col("bar_open_ms") < pl.col("_hi"))
    ).drop("_lo", "_hi")
    return kept, df.height - kept.height


def clean(
    df: pl.DataFrame,
    symbol: str,
    *,
    tenure: tuple[date, date] | None = None,
    lead_days: int = TENURE_LEAD_DAYS,
    trail_days: int = TENURE_TRAIL_DAYS,
) -> tuple[pl.DataFrame, IntegrityReport]:
    """Apply every rule, in order, and report what each one removed.

    The report is the point as much as the cleaning is. A backfill that silently
    discarded a third of a name's history would be indistinguishable from one that found
    a third of its history missing, and those call for different responses.
    """
    report = IntegrityReport(symbol=symbol, bars_in=df.height, bars_out=df.height)
    if df.is_empty():
        return df, report

    df = df.sort("session_date")
    df, report.zero_volume_dropped = drop_zero_volume(df)
    df, report.outside_tenure_dropped = clip_to_tenure(
        df, tenure, lead_days=lead_days, trail_days=trail_days
    )
    report.frozen_run_max = longest_frozen_run(df)
    report.discontinuities = find_discontinuities(df)
    report.bars_out = df.height

    if report.frozen_run_max >= 3:
        report.notes.append(
            f"{report.frozen_run_max} consecutive frozen bars WITH volume -- inspect"
        )
    if report.bars_in and report.bars_out == 0:
        report.notes.append("every bar removed")
    return df, report
