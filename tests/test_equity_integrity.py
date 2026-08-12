"""Equity bar integrity, tested against the real pathologies that motivated it.

The fixtures reproduce what Alpaca actually returns for SBNY and CA -- a frozen
zero-volume tail, and a reused ticker with a live second act at a different price. These
are not hypotheticals; both were observed while probing the account before backfilling,
and both would have corrupted results silently.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl
import pytest

from tradedesk.equity_integrity import (
    clean,
    clip_to_tenure,
    drop_zero_volume,
    find_discontinuities,
    longest_frozen_run,
)


def _bars(rows):
    """rows: (date, close, volume) or (date, o, h, l, c, volume)."""
    out = []
    for r in rows:
        if len(r) == 3:
            d, c, v = r
            o = h = l_ = c
        else:
            d, o, h, l_, c, v = r
        out.append({"session_date": d, "open": o, "high": h, "low": l_,
                    "close": c, "volume": v})
    return pl.DataFrame(out)


def _series(start: date, n: int, price: float, vol: float, step: float = 0.0):
    return [(start + timedelta(days=i), price + i * step, vol) for i in range(n)]


def test_sbny_frozen_tail_is_removed():
    """Signature Bank: real bars to 2023-03-10, then 509 zero-volume bars at 70.00.

    Kept, they turn a total loss into a flat position -- a strategy holding SBNY through
    the seizure books 0% instead of -100%. That is the survivorship bias the
    point-in-time universe exists to remove, arriving through the bars instead.
    """
    real = [(date(2023, 3, 6) + timedelta(days=i), 90.0 - i * 5, 1e6) for i in range(5)]
    frozen = [(date(2023, 3, 13) + timedelta(days=i), 70.0, 0.0) for i in range(509)]
    df, report = clean(_bars(real + frozen), "SBNY")

    assert report.zero_volume_dropped == 509
    assert df.height == 5
    assert df["session_date"].max() == date(2023, 3, 10)


def test_ca_ticker_reuse_is_excluded_by_tenure():
    """CA Technologies: acquired 2018-11-05 at 44.44; the ticker later carried a
    different security at ~25 on a few hundred shares a day.

    Spliced together the series shows a 43% single-day drop that happened to nobody, and
    every momentum and reversal detector fires on it. Clipping to index tenure removes
    the second company entirely, because whatever traded under `CA` in 2024 is not the
    company the universe was referring to.
    """
    original = _series(date(2018, 8, 1), 60, 44.0, 5e6)
    frozen = [(date(2018, 11, 5) + timedelta(days=i), 44.44, 0.0) for i in range(1323)]
    reused = _series(date(2023, 12, 15), 500, 25.0, 800.0)

    df, report = clean(
        _bars(original + frozen + reused), "CA",
        tenure=(date(2018, 8, 31), date(2018, 10, 31)),
    )
    assert report.zero_volume_dropped == 1323
    assert report.outside_tenure_dropped > 0
    assert df.height > 0
    # Nothing from the second company survives.
    assert df["session_date"].max() < date(2019, 1, 1)
    assert float(df["close"].min()) > 30.0, "a ~25 print means the reused ticker leaked"


def test_the_fabricated_return_never_exists_after_cleaning():
    """The concrete harm, stated as a return.

    Uncleaned, the last frozen bar at 44.44 is followed by a 25.22 bar: a -43% return
    on a day nothing happened. After cleaning there is no such adjacency.
    """
    frozen = [(date(2023, 12, 1) + timedelta(days=i), 44.44, 0.0) for i in range(10)]
    reused = _series(date(2023, 12, 15), 20, 25.22, 500.0)
    raw = _bars(frozen + reused)

    dirty = raw.sort("session_date").with_columns(
        (pl.col("close") / pl.col("close").shift(1) - 1).alias("ret")
    )
    assert float(dirty["ret"].min()) < -0.40, "fixture should contain the fake drop"

    df, _ = clean(raw, "CA", tenure=(date(2018, 1, 1), date(2018, 12, 31)))
    assert df.height == 0  # nothing in this window belongs to CA Technologies


def test_zero_volume_is_the_criterion_not_flat_ohlc():
    """A genuinely illiquid name can trade all day at one price. That bar is data.

    Keying the rule on flat OHLC instead of on volume would delete real trades in thin
    names -- and thin names are exactly where a cost or liquidity result would matter.
    """
    rows = [
        (date(2024, 1, 2), 10.0, 10.0, 10.0, 10.0, 500.0),   # real, flat, kept
        (date(2024, 1, 3), 10.0, 10.0, 10.0, 10.0, 0.0),     # phantom, dropped
    ]
    kept, dropped = drop_zero_volume(_bars(rows))
    assert dropped == 1
    assert kept.height == 1
    assert float(kept["volume"][0]) == 500.0


def test_the_tenure_buffer_is_asymmetric():
    """Formation windows reach backwards; ticker reuse happens forwards.

    A 12-month formation with a one-month skip needs ~400 calendar days of history
    BEFORE a name joins. Clipping tightly at the join date leaves every recently-added
    name unrankable for its first year -- which biases the universe toward long-tenured
    names, reintroducing the exact survivorship problem the point-in-time universe
    removes. Measured on real SBNY data, a symmetric 10-day buffer cut it from 1,145
    usable bars to 306.

    The trailing side stays tight, because that is the side ticker reuse arrives on.
    """
    rows = _series(date(2019, 1, 1), 900, 50.0, 1e6)
    kept, _ = clip_to_tenure(
        _bars(rows), (date(2020, 6, 1), date(2020, 9, 1)),
        lead_days=420, trail_days=10,
    )
    assert kept["session_date"].min() <= date(2019, 5, 1), "lead buffer too tight"
    assert kept["session_date"].max() <= date(2020, 9, 12), "trail buffer too loose"


def test_no_tenure_means_no_clipping():
    """A name with no membership record keeps its history; the caller decides."""
    rows = _series(date(2020, 1, 1), 100, 50.0, 1e6)
    kept, dropped = clip_to_tenure(_bars(rows), None)
    assert dropped == 0 and kept.height == 100


def test_discontinuities_are_flagged_not_stitched():
    """A month-long hole in a large cap has no innocent explanation, but the right
    response depends on the name -- so it surfaces as a flag rather than a guess."""
    first = _series(date(2020, 1, 1), 30, 50.0, 1e6)
    later = _series(date(2020, 6, 1), 30, 80.0, 1e6)
    gaps = find_discontinuities(_bars(first + later))
    assert len(gaps) == 1
    lo, hi, days = gaps[0]
    assert days > 30 and lo < hi


def test_a_clean_series_is_left_alone():
    """The regression that matters most: normal data must pass through untouched."""
    rows = _series(date(2024, 1, 2), 250, 100.0, 2e6, step=0.05)
    df, report = clean(_bars(rows), "AAPL")
    assert report.bars_out == report.bars_in == 250
    assert report.zero_volume_dropped == 0
    assert report.outside_tenure_dropped == 0
    assert report.clean


def test_frozen_run_with_volume_is_reported_as_a_note():
    """After rule 1 there should be no frozen runs. One remaining means a venue emitted
    frozen prices WITH volume -- a different and more alarming problem, which should
    surface as a number rather than be cleaned away."""
    rows = [(date(2024, 1, 2) + timedelta(days=i), 10.0, 10.0, 10.0, 10.0, 100.0)
            for i in range(8)]
    df, report = clean(_bars(rows), "WEIRD")
    assert report.frozen_run_max >= 3
    assert any("frozen" in n for n in report.notes)
    assert not report.clean


def test_longest_frozen_run_counts_consecutive_identical_bars():
    rows = (_series(date(2024, 1, 2), 3, 10.0, 1e5, step=0.1)
            + [(date(2024, 1, 10) + timedelta(days=i), 12.0, 1e5) for i in range(4)]
            + _series(date(2024, 2, 1), 3, 15.0, 1e5, step=0.1))
    assert longest_frozen_run(_bars(rows)) == 3   # bars 2..4 of the frozen block


def test_empty_input_is_handled():
    empty = pl.DataFrame(schema={
        "session_date": pl.Date, "open": pl.Float64, "high": pl.Float64,
        "low": pl.Float64, "close": pl.Float64, "volume": pl.Float64,
    })
    df, report = clean(empty, "NONE")
    assert df.is_empty() and report.bars_in == 0 and report.bars_out == 0
