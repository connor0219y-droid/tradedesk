"""Time, timezone and session handling.

The single most important module in the project. Every downstream statistic inherits
its correctness from here.

Invariant (assert this, put it in the README, never break it):

    A bar is identified by its OPEN time, in UTC epoch milliseconds.
    Bar `t` covers the half-open interval [t, t + timeframe).
    A bar may be read only once `t + timeframe + settle <= now`.

All arithmetic on bar grids is integer arithmetic on UTC milliseconds. We never do
calendar arithmetic in local time -- an ET "day" is 23 or 25 hours twice a year, and
a grid built by stepping through local wall clock is wrong on those days.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# Canonicalised once. `US/Eastern` and `America/New_York` are the same zone but are
# DIFFERENT STRINGS inside a polars dtype, so a frame tagged one way fails frame
# equality against the other. Everything in this project uses this constant.
ET = ZoneInfo("America/New_York")
ET_NAME = "America/New_York"
UTC = timezone.utc

# Bumped whenever the session definition changes. Stored on every bar so a later
# redefinition cannot silently blend two incompatible definitions in one result set.
CALENDAR_VERSION = 1

MS = 1000
TIMEFRAME_MS: dict[str, int] = {
    "1m": 60 * MS,
    "5m": 300 * MS,
    "15m": 900 * MS,
    "1h": 3600 * MS,
    # Coinbase serves neither 4h nor a usable 1d (its daily candles are UTC-anchored
    # and would silently disagree with the ET session model). Both are DERIVED by
    # aggregation from stored 1h bars -- see resample.py. They appear here because
    # this table is the bar-grid arithmetic, not a statement about what is fetchable;
    # the venue keeps its own SUPPORTED set and still rejects them.
    "4h": 14400 * MS,
    "6h": 21600 * MS,
    "1d": 86400 * MS,
}

# Timeframes whose length divides the Unix epoch evenly, so that
# `floor(ms / tf) * tf` lands on a true bucket boundary. '1w' would NOT be safe
# (the epoch fell on a Thursday), nor would calendar months.
_EPOCH_ALIGNED = frozenset({"1m", "5m", "15m", "1h", "4h", "6h", "1d"})


class NonExistentTimeError(ValueError):
    """A local wall-clock time that does not exist (spring-forward gap)."""


class AmbiguousTimeError(ValueError):
    """A local wall-clock time that occurs twice (fall-back overlap)."""


def tf_ms(timeframe: str) -> int:
    """Milliseconds in one bar of `timeframe`."""
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"unsupported timeframe {timeframe!r}; known: {sorted(TIMEFRAME_MS)}"
        ) from None


def assert_epoch_aligned(timeframe: str) -> None:
    """Guard the floor-division watermark.

    Only meaningful for timeframes that tile the epoch evenly. If the timeframe set is
    ever widened to weekly or monthly bars, this fires instead of silently producing
    misaligned buckets.
    """
    if timeframe not in _EPOCH_ALIGNED:
        raise ValueError(
            f"timeframe {timeframe!r} does not tile the Unix epoch evenly; "
            "floor-division alignment is invalid for it"
        )


def align_down(ms: int, timeframe: str) -> int:
    """Floor `ms` to the start of its bucket."""
    assert_epoch_aligned(timeframe)
    step = tf_ms(timeframe)
    return (ms // step) * step


def align_up(ms: int, timeframe: str) -> int:
    """Ceil `ms` to the start of the next bucket (identity if already aligned)."""
    assert_epoch_aligned(timeframe)
    step = tf_ms(timeframe)
    return -((-ms) // step) * step


def now_ms() -> int:
    """Wall clock in UTC epoch milliseconds."""
    return int(datetime.now(UTC).timestamp() * MS)


def to_ms(dt: datetime) -> int:
    """UTC epoch milliseconds for a timezone-aware datetime."""
    if dt.tzinfo is None:
        raise ValueError("refusing to convert a naive datetime; attach a timezone")
    return int(dt.timestamp() * MS)


def from_ms(ms: int) -> datetime:
    """Timezone-aware UTC datetime for UTC epoch milliseconds."""
    return datetime.fromtimestamp(ms / MS, tz=UTC)


def localize_strict(naive: datetime, tz: ZoneInfo = ET) -> datetime:
    """Attach `tz` to a naive local datetime, refusing anything ambiguous.

    Python's `zoneinfo` does NOT raise on a non-existent local time. Verified:
    `datetime(2025, 3, 9, 2, 30, tzinfo=ET)` silently yields 07:30Z at fold=0 and
    06:30Z at fold=1 -- an hour apart, neither an error. Any code path that builds a
    bar grid by localising wall-clock times is therefore a silent-corruption path
    unless it goes through this function.
    """
    if naive.tzinfo is not None:
        raise ValueError("localize_strict expects a naive datetime")

    early = naive.replace(tzinfo=tz, fold=0)
    late = naive.replace(tzinfo=tz, fold=1)

    if early.utcoffset() == late.utcoffset():
        return early

    # The two folds disagree, so this instant is either ambiguous or non-existent.
    # Round-tripping through UTC distinguishes them: an ambiguous time survives the
    # trip unchanged, a non-existent one does not.
    round_tripped = early.astimezone(UTC).astimezone(tz).replace(tzinfo=None)
    if round_tripped == naive:
        raise AmbiguousTimeError(
            f"{naive.isoformat()} occurs twice in {tz.key} (fall-back overlap)"
        )
    raise NonExistentTimeError(
        f"{naive.isoformat()} does not exist in {tz.key} (spring-forward gap)"
    )


def session_date_et(ms: int) -> date:
    """The ET calendar date a bar belongs to.

    UTC -> ET is always unambiguous, so no strict localisation is needed here.
    """
    return datetime.fromtimestamp(ms / MS, tz=UTC).astimezone(ET).date()


def et_day_bounds(day: date) -> tuple[int, int]:
    """[start, end) of an ET calendar day, in UTC ms.

    Midnight ET always exists and is never ambiguous because US transitions happen at
    02:00 local -- so an ET-day boundary is always well defined. The day itself is 23
    or 25 hours long across a transition, which is exactly why this returns real
    instants rather than a fixed 24-hour offset.
    """
    start = localize_strict(datetime.combine(day, time(0, 0)))
    end = localize_strict(datetime.combine(day + timedelta(days=1), time(0, 0)))
    return to_ms(start), to_ms(end)


def et_session_bounds(day: date, start_hm: str, end_hm: str) -> tuple[int, int]:
    """[start, end) of an intraday ET session window, in UTC ms.

    Used for equity RTH (09:30-16:00) and premarket (04:00-09:30) in phase 2+.
    Both boundaries sit outside the 02:00 transition hour, so they always exist.
    """
    sh, sm = (int(x) for x in start_hm.split(":"))
    eh, em = (int(x) for x in end_hm.split(":"))
    start = localize_strict(datetime.combine(day, time(sh, sm)))
    end = localize_strict(datetime.combine(day, time(eh, em)))
    return to_ms(start), to_ms(end)


def bar_grid(start_ms: int, end_ms: int, timeframe: str) -> list[int]:
    """Every bar-open timestamp in [start_ms, end_ms), half-open.

    Integer arithmetic on UTC ms. Half-open deliberately: polars' `datetime_range`
    defaults to `closed='both'`, which yields one extra bar and then reads as a
    phantom gap or a duplicate at every window boundary.
    """
    step = tf_ms(timeframe)
    first = align_up(start_ms, timeframe)
    return list(range(first, end_ms, step))


def expected_bar_count(start_ms: int, end_ms: int, timeframe: str) -> int:
    """Number of bars the grid [start_ms, end_ms) should contain."""
    step = tf_ms(timeframe)
    first = align_up(start_ms, timeframe)
    if end_ms <= first:
        return 0
    return (end_ms - first + step - 1) // step


def is_bar_final(bar_open_ms: int, timeframe: str, now: int, settle_ms: int) -> bool:
    """Whether a bar has closed AND settled, and may therefore be stored or read.

    Two distinct hazards, both confirmed live against Coinbase:

    1. The forming bar. Exchanges return the currently-open bucket. Writing it puts a
       mutable, wrong bar in the database permanently -- and it is the exact bar a
       live signal would fire on.
    2. Publication lag. A just-closed bucket is not published immediately (~140s
       observed). Without a settle margin, an incremental run records a false gap for
       the newest bar and then never revisits it, because coverage says that window
       was already fetched.
    """
    return bar_open_ms + tf_ms(timeframe) + settle_ms <= now


def readable_watermark(timeframe: str, now: int, settle_ms: int) -> int:
    """Exclusive upper bound on bar-open timestamps that are safe to fetch or read.

    Everything at or after this instant is either still forming or not yet settled.
    """
    return align_down(now - tf_ms(timeframe) - settle_ms, timeframe) + tf_ms(timeframe)
