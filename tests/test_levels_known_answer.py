"""Known-answer tests: values worked out by hand, not blessed from the output.

A test that asserts whatever the code currently produces is a change-detector, not a
correctness test. Every expected number below is derived in the docstring so a reader
can check the arithmetic independently of the implementation.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal, getcontext

import polars as pl
import pytest

from tradedesk.config import load_config
from tradedesk.frames import BarFrame
from tradedesk.levels import compute_levels
from tradedesk.levels.profile import value_area
from tradedesk.timeutil import et_day_bounds

STEP = 60_000
# ET MIDNIGHT of the session date, not UTC midnight. A fixture whose bars sit
# outside the ET window its own `session_date` names is internally inconsistent:
# `ms_since_open` goes negative, the opening-range window never closes, and
# `or5_high` silently becomes a running max over the whole frame. Real data cannot
# hit this -- ingest derives `session_date` FROM the bar time -- but the fixture
# could, and did.
BASE = et_day_bounds(date(2025, 6, 1))[0]


def _frame(bars, sess=date(2025, 6, 1), start=None):
    start = et_day_bounds(sess)[0] if start is None else start
    rows = [
        {
            "venue": "coinbase", "symbol": "X/USD", "timeframe": "1m",
            "bar_open_ms": start + i * STEP,
            "open": o, "high": h, "low": l, "close": c, "volume": v,
            "session_date": sess, "calendar_version": 1, "revision": 0,
            "ingested_at_ms": 0,
        }
        for i, (o, h, l, c, v) in enumerate(bars)
    ]
    df = pl.DataFrame(rows)
    return BarFrame(df=df, venue="coinbase", symbol="X/USD", timeframe="1m",
                    calendar_version=1, as_of_ms=int(df["bar_open_ms"].max()) + STEP)


@pytest.fixture
def cfg():
    return load_config()


def test_vwap_and_sigma_hand_computed(cfg):
    """Three bars, worked out in exact fractions.

        bar 0: h=102 l=98  c=101 v=10  -> tp = (102+98+101)/3 = 301/3
        bar 1: h=103 l=100 c=102 v=20  -> tp = (103+100+102)/3 = 305/3
        bar 2: h=104 l=101 c=103 v=30  -> tp = (104+101+103)/3 = 308/3

    VWAP after bar 1 = (10*301/3 + 20*305/3) / 30 = (3010 + 6100)/90 = 9110/90
    VWAP after bar 2 = (3010 + 6100 + 9240)/180   = 18350/180 = 1835/18

    Weighted variance after bar 1, about VWAP = 9110/90:
        tp0 - vwap = 301/3 - 9110/90 = (9030 - 9110)/90 = -8/9
        tp1 - vwap = 305/3 - 9110/90 = (9150 - 9110)/90 =  4/9
        var = (10*(8/9)^2 + 20*(4/9)^2) / 30 = (640/81 + 320/81)/30 = 32/81
        sigma = sqrt(32)/9
    """
    bf = _frame([
        (100.0, 102.0, 98.0, 101.0, 10.0),
        (101.0, 103.0, 100.0, 102.0, 20.0),
        (102.0, 104.0, 101.0, 103.0, 30.0),
    ])
    df = compute_levels(bf, cfg).to_polars()

    assert df["vwap"][0] == pytest.approx(301 / 3)
    assert df["vwap"][1] == pytest.approx(9110 / 90)
    assert df["vwap"][2] == pytest.approx(1835 / 18)

    assert df["vwap_sigma"][0] is None            # n == 1, no dispersion
    assert df["vwap_sigma"][1] == pytest.approx(math.sqrt(32) / 9)


def test_opening_range_hand_computed(cfg):
    """5-minute opening range on 1m bars, three bars, all inside the window.

        highs 102, 103, 104 -> running max 102, 103, 104
        lows   98, 100, 101 -> running min  98,  98,  98
        mid after bar 2 = (104 + 98)/2 = 101
        or5_pos at bar 2 = (close - low)/(high - low) = (103 - 98)/(104 - 98) = 5/6
    """
    bf = _frame([
        (100.0, 102.0, 98.0, 101.0, 10.0),
        (101.0, 103.0, 100.0, 102.0, 20.0),
        (102.0, 104.0, 101.0, 103.0, 30.0),
    ])
    df = compute_levels(bf, cfg).to_polars()

    assert df["or5_high"].to_list() == [102.0, 103.0, 104.0]  # RUNNING, not final
    assert df["or5_low"].to_list() == [98.0, 98.0, 98.0]
    assert df["or5_mid"][2] == pytest.approx(101.0)
    assert df["or5_pos"][2] == pytest.approx(5 / 6)


def test_atr_hand_computed(cfg):
    """15 identical bars: o=100 h=101 l=99 c=100.

        TR = max(h-l, |h-prev_c|, |l-prev_c|) = max(2, 1, 1) = 2 for every bar.
        Bar 0 is the start of the series, so its TR is null (no previous close).
        Bars 1..14 give 14 clean TRs, so ATR is first defined at bar 14 and equals
        the SMA seed = mean of fourteen 2.0s = 2.0 exactly.
    """
    bf = _frame([(100.0, 101.0, 99.0, 100.0, 1.0)] * 15)
    df = compute_levels(bf, cfg).to_polars()

    assert df["true_range"][0] is None
    assert df["true_range"][1] == pytest.approx(2.0)
    assert df["atr_intraday"][13] is None          # only 13 clean TRs so far
    assert df["atr_intraday"][14] == pytest.approx(2.0)


def test_value_area_hand_computed():
    """Volume profile over a hand-built histogram.

    Bucket width = atr_daily/100 = 100/100 = 1.0. Reference price is the first typical
    price. Volumes: 10 at the reference bucket, 50 one bucket up, 10 two up.
    Total 70; POC is the 50 bucket. The 70% target is 49, which the POC alone already
    exceeds, so the value area is the POC bucket only -- vah == val == poc.
    """
    rows = []
    for i, (tp_off, vol) in enumerate([(0.0, 10.0), (1.0, 50.0), (2.0, 10.0)]):
        p = 100.0 + tp_off
        rows.append({
            "bar_open_ms": BASE + i * STEP, "session_date": date(2025, 6, 1),
            "typical_price": p, "volume": vol, "atr_daily": 100.0,
        })
    df = pl.DataFrame(rows)
    poc, vah, val = value_area(df, at_ms=BASE + 2 * STEP, buckets_per_atr=100,
                              tick_size=0.01, area_pct=70.0)
    assert poc == pytest.approx(101.5)   # bucket 1 spans [101,102), centre 101.5
    assert vah == val == poc             # one bucket already exceeds 70%


def test_vwap_is_numerically_stable_over_a_full_session(cfg):
    """The textbook variance formula catastrophically cancels at crypto prices.

    With typical price near 60,000, tp^2 is about 3.6e9 and the two terms of
    `E[x^2] - E[x]^2` agree to roughly ten significant figures. What survives is noise,
    and it goes negative -- at which point sqrt() returns NaN.

    This compares the shifted-cumsum implementation against an exact Decimal reference
    over a full 1,440-bar session.
    """
    getcontext().prec = 50
    n = 1440
    bars = []
    for i in range(n):
        base = 60000.0 + math.sin(i / 50.0) * 30.0
        bars.append((base, base + 1.0, base - 1.0, base + 0.5, 1.0 + (i % 7)))
    bf = _frame(bars)
    df = compute_levels(bf, cfg).to_polars()

    tps = [Decimal(str(h)) + Decimal(str(l)) + Decimal(str(c)) for _, h, l, c, _ in bars]
    tps = [t / 3 for t in tps]
    vols = [Decimal(str(v)) for *_, v in bars]

    sw = sum(vols)
    swx = sum(v * t for v, t in zip(vols, tps))
    ref_vwap = swx / sw
    ref_var = sum(v * (t - ref_vwap) ** 2 for v, t in zip(vols, tps)) / sw
    ref_sigma = ref_var.sqrt()

    got_vwap = Decimal(str(df["vwap"][-1]))
    got_sigma = Decimal(str(df["vwap_sigma"][-1]))

    assert abs(got_vwap - ref_vwap) / ref_vwap < Decimal("1e-12")
    assert abs(got_sigma - ref_sigma) / ref_sigma < Decimal("1e-9")
    # And the thing the clamp exists to prevent:
    assert not df["vwap_sigma"].is_nan().any()


def test_naive_variance_formula_would_have_failed():
    """Demonstrate the cancellation this design avoids, so the guard is not cargo-cult.

    Computed in float64 exactly as the textbook formula would, at crypto prices.
    """
    prices = [60000.0 + math.sin(i / 50.0) * 30.0 for i in range(1440)]
    w = [1.0] * len(prices)
    sw = sum(w)
    sx = sum(wi * p for wi, p in zip(w, prices))
    sxx = sum(wi * p * p for wi, p in zip(w, prices))
    mean = sx / sw
    naive_var = sxx / sw - mean * mean

    shifted = [p - prices[0] for p in prices]
    sd = sum(wi * d for wi, d in zip(w, shifted))
    sdd = sum(wi * d * d for wi, d in zip(w, shifted))
    md = sd / sw
    stable_var = sdd / sw - md * md

    # The shifted form is the trustworthy one; assert they disagree materially, which is
    # the whole reason the implementation does not use the naive form.
    rel = abs(naive_var - stable_var) / stable_var
    assert rel > 1e-9, (
        "cancellation was not reproduced; if this ever fails the naive formula may be "
        "safe on this platform, but the shifted form is still correct"
    )
