"""Timezone, DST and bar-grid correctness.

The bugs guarded here are the silent kind: nothing raises, the numbers are merely
wrong, and they are wrong for about eight months of the year in a way that looks fine
in casual testing.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from tradedesk.timeutil import (
    ET,
    ET_NAME,
    AmbiguousTimeError,
    NonExistentTimeError,
    align_down,
    align_up,
    bar_grid,
    et_day_bounds,
    et_session_bounds,
    expected_bar_count,
    is_bar_final,
    localize_strict,
    readable_watermark,
    tf_ms,
)

# Real US DST transitions. All fall on Sundays, all at 02:00 local.
SPRING_FORWARD = [date(2023, 3, 12), date(2024, 3, 10), date(2025, 3, 9),
                  date(2026, 3, 8), date(2027, 3, 14)]
FALL_BACK = [date(2023, 11, 5), date(2024, 11, 3), date(2025, 11, 2),
             date(2026, 11, 1), date(2027, 11, 7)]


@pytest.mark.parametrize("day", SPRING_FORWARD + FALL_BACK)
def test_rth_is_always_78_five_minute_bars(day):
    """RTH is 09:30-16:00 ET -- 6.5 hours, i.e. 78 five-minute bars, always.

    A DST transition shifts the UTC offset but not the length of the session. If this
    ever returns 66 or 90, the session is being built by stepping through wall-clock
    time instead of through real instants.
    """
    start, end = et_session_bounds(day, "09:30", "16:00")
    assert (end - start) == 6.5 * 3600 * 1000
    assert expected_bar_count(start, end, "5m") == 78


@pytest.mark.parametrize("day", SPRING_FORWARD + FALL_BACK)
def test_premarket_is_always_66_five_minute_bars(day):
    start, end = et_session_bounds(day, "04:00", "09:30")
    assert expected_bar_count(start, end, "5m") == 66


def test_et_calendar_day_length_across_dst():
    """An ET day is 23 or 25 hours across a transition. A UTC day is always 24."""
    assert expected_bar_count(*et_day_bounds(date(2025, 3, 9)), "5m") == 276  # 23h
    assert expected_bar_count(*et_day_bounds(date(2025, 11, 2)), "5m") == 300  # 25h
    assert expected_bar_count(*et_day_bounds(date(2025, 6, 1)), "5m") == 288  # 24h


def test_localize_strict_rejects_nonexistent_time():
    """zoneinfo silently invents an answer here; we must not.

    datetime(2025,3,9,2,30) does not exist -- the clock jumps 02:00 -> 03:00. Plain
    zoneinfo yields 07:30Z at fold=0 and 06:30Z at fold=1, an hour apart, with no
    error either way.
    """
    naive = datetime(2025, 3, 9, 2, 30)
    with pytest.raises(NonExistentTimeError):
        localize_strict(naive)

    # Demonstrate the footgun this guards against, so the test documents the why.
    a = naive.replace(tzinfo=ET, fold=0).astimezone(ZoneInfo("UTC"))
    b = naive.replace(tzinfo=ET, fold=1).astimezone(ZoneInfo("UTC"))
    assert a != b and abs((a - b).total_seconds()) == 3600


def test_localize_strict_rejects_ambiguous_time():
    """01:30 ET occurs twice on the November transition."""
    with pytest.raises(AmbiguousTimeError):
        localize_strict(datetime(2025, 11, 2, 1, 30))


def test_midnight_et_is_always_unambiguous():
    """Why the ET calendar day is a safe session anchor: transitions are at 02:00."""
    for day in SPRING_FORWARD + FALL_BACK:
        localize_strict(datetime.combine(day, time(0, 0)))  # must not raise


def test_fall_back_duplicates_twelve_five_minute_labels():
    """ET wall clock is not a usable key: 01:00-01:55 happens twice each November.

    Any join, group_by, dict key or filename built on local wall clock silently drops
    or double-counts these 12 bars once a year.
    """
    start, end = et_day_bounds(date(2025, 11, 2))
    grid = bar_grid(start, end, "5m")
    local_labels = [
        datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("UTC")).astimezone(ET).strftime("%H:%M")
        for ms in grid
    ]
    duplicated = {lbl for lbl in local_labels if local_labels.count(lbl) > 1}
    assert len(duplicated) == 12
    assert "01:30" in duplicated


def test_polars_and_zoneinfo_agree_on_offsets():
    """polars carries its own compiled-in tz database, separate from Python's.

    Two independent tzdbs in one process can disagree after a release. If they ever
    do, every ET-labelled column silently diverges from every Python-computed bound.
    """
    probes = [
        datetime(2025, 3, 9, 6, 30),
        datetime(2025, 3, 9, 7, 30),
        datetime(2025, 11, 2, 5, 30),
        datetime(2025, 11, 2, 6, 30),
        datetime(2025, 6, 1, 12, 0),
    ]
    df = pl.DataFrame({"ts": probes}).with_columns(
        pl.col("ts").dt.replace_time_zone("UTC").dt.convert_time_zone(ET_NAME).alias("et")
    )
    for probe, got in zip(probes, df["et"].to_list()):
        expected = probe.replace(tzinfo=ZoneInfo("UTC")).astimezone(ET)
        assert got == expected, f"tzdb disagreement at {probe}"


def test_bar_grid_is_half_open():
    """polars' datetime_range defaults to closed='both'; ours must not.

    An extra boundary bar reads as a phantom gap or a duplicate at every window edge.
    """
    step = tf_ms("5m")
    start = align_down(1_700_000_000_000, "5m")
    grid = bar_grid(start, start + 3 * step, "5m")
    assert grid == [start, start + step, start + 2 * step]
    assert start + 3 * step not in grid


def test_alignment_helpers():
    step = tf_ms("5m")
    aligned = align_down(1_700_000_123_456, "5m")
    assert aligned % step == 0
    assert align_up(aligned, "5m") == aligned  # identity when already aligned
    assert align_up(aligned + 1, "5m") == aligned + step


def test_epoch_alignment_guard_rejects_weekly():
    """Floor division only lands on true buckets for timeframes tiling the epoch.

    The Unix epoch fell on a Thursday, so '1w' would be silently misaligned.
    """
    with pytest.raises(ValueError):
        align_down(1_700_000_000_000, "1w")


def test_is_bar_final_requires_close_plus_settle():
    step = tf_ms("5m")
    open_ms = align_down(1_700_000_000_000, "5m")
    settle = 600_000

    assert not is_bar_final(open_ms, "5m", open_ms + step - 1, settle)   # still forming
    assert not is_bar_final(open_ms, "5m", open_ms + step, settle)       # closed, unsettled
    assert not is_bar_final(open_ms, "5m", open_ms + step + settle - 1, settle)
    assert is_bar_final(open_ms, "5m", open_ms + step + settle, settle)


def test_readable_watermark_excludes_unsettled_tail():
    step = tf_ms("5m")
    settle = 600_000
    now = align_down(1_700_000_000_000, "5m") + 123_456
    wm = readable_watermark("5m", now, settle)
    assert wm <= now - settle
    # Every bar below the watermark is final; the first one at or above it is not.
    assert is_bar_final(wm - step, "5m", now, settle)
    assert not is_bar_final(wm, "5m", now, settle)
