"""Causal data-quality checks.

Every check here reads bar `t` and bars strictly before it. None reads the future, so
any of them may be turned into a feature later without introducing lookahead.

Findings are logged, never silently repaired. A "fixed" bad tick is a fabricated
price, and a backtest built on fabricated prices reports an edge that cannot be
traded.
"""

from __future__ import annotations

import math

import polars as pl

from ..config import Config
from ..coverage import Interval
from ..timeutil import bar_grid, tf_ms

ISSUE_SCHEMA = {
    "venue": pl.String,
    "symbol": pl.String,
    "timeframe": pl.String,
    "bar_open_ms": pl.Int64,
    "check_name": pl.String,
    "severity": pl.String,
    "detail": pl.String,
    "causal": pl.Boolean,
    "detected_at_ms": pl.Int64,
}

# MAD * this constant is a consistent estimator of sigma under a Gaussian reference.
MAD_TO_SIGMA = 1.4826


def empty_issues() -> pl.DataFrame:
    return pl.DataFrame(schema=ISSUE_SCHEMA)


def _issues_from(
    df: pl.DataFrame,
    mask: pl.Series,
    *,
    check_name: str,
    severity: str,
    detail: str,
    venue: str,
    symbol: str,
    timeframe: str,
    detected_at_ms: int,
) -> pl.DataFrame:
    hits = df.filter(mask)
    if hits.is_empty():
        return empty_issues()
    return pl.DataFrame(
        {
            "venue": [venue] * hits.height,
            "symbol": [symbol] * hits.height,
            "timeframe": [timeframe] * hits.height,
            "bar_open_ms": hits["bar_open_ms"].to_list(),
            "check_name": [check_name] * hits.height,
            "severity": [severity] * hits.height,
            "detail": [detail] * hits.height,
            "causal": [True] * hits.height,
            "detected_at_ms": [detected_at_ms] * hits.height,
        },
        schema=ISSUE_SCHEMA,
    )


def run_causal_checks(
    df: pl.DataFrame,
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    detected_at_ms: int,
    cfg: Config,
) -> pl.DataFrame:
    """All per-bar checks over one freshly fetched batch."""
    if df.is_empty():
        return empty_issues()

    step = tf_ms(timeframe)
    q = cfg.quality
    out: list[pl.DataFrame] = []

    def add(mask, name, severity, detail):
        out.append(
            _issues_from(
                df,
                mask,
                check_name=name,
                severity=severity,
                detail=detail,
                venue=venue,
                symbol=symbol,
                timeframe=timeframe,
                detected_at_ms=detected_at_ms,
            )
        )

    # --- OHLC invariants. A violation means the venue sent something impossible. ---
    max_oc = df.select(pl.max_horizontal("open", "close").alias("v")).to_series()
    min_oc = df.select(pl.min_horizontal("open", "close").alias("v")).to_series()
    add(df["high"] < df["low"], "ohlc.high_lt_low", "ERROR", "high < low")
    add(
        df["high"] < max_oc,
        "ohlc.high_lt_max_oc",
        "ERROR",
        "high below max(open, close)",
    )
    add(
        df["low"] > min_oc,
        "ohlc.low_gt_min_oc",
        "ERROR",
        "low above min(open, close)",
    )
    add(
        (df["open"] <= 0) | (df["high"] <= 0) | (df["low"] <= 0) | (df["close"] <= 0),
        "ohlc.nonpositive_price",
        "ERROR",
        "non-positive price",
    )
    add(df["volume"] < 0, "volume.negative", "ERROR", "negative volume")

    # Legal and real, but any pattern feature of the form (x - low) / (high - low)
    # divides by zero here and emits inf/NaN that propagates through rolling windows
    # without raising. The pattern layer must handle it explicitly.
    add(df["high"] == df["low"], "ohlc.zero_range", "INFO", "high == low")

    # Coinbase omits no-trade buckets entirely, so a *returned* bar with zero volume
    # is anomalous rather than routine. On a venue that zero-fills instead, this
    # threshold would need to be venue-specific.
    add(df["volume"] == 0, "volume.zero", "WARN", "zero volume on a returned bar")

    # --- Timestamp integrity ---
    add(
        (df["bar_open_ms"] % step) != 0,
        "ts.misaligned",
        "ERROR",
        f"bar_open_ms not a multiple of {step}",
    )
    if df.height > 1:
        diffs = df["bar_open_ms"].diff()
        add(
            (diffs <= 0).fill_null(False),
            "ts.non_monotonic",
            "ERROR",
            "timestamps not strictly increasing",
        )

    # --- Robust outlier detection on log returns ---
    out.append(
        _return_outliers(
            df,
            venue=venue,
            symbol=symbol,
            timeframe=timeframe,
            detected_at_ms=detected_at_ms,
            k=q.outlier_mad_k,
            sigma_floor=q.sigma_floor_bps / 10_000.0,
            abs_limit=q.outlier_abs_return,
        )
    )

    non_empty = [d for d in out if not d.is_empty()]
    return pl.concat(non_empty) if non_empty else empty_issues()


def _return_outliers(
    df: pl.DataFrame,
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    detected_at_ms: int,
    k: float,
    sigma_floor: float,
    abs_limit: float,
) -> pl.DataFrame:
    """Flag implausible bar-to-bar moves.

    Two independent tests, because each covers the other's blind spot:

    * A robust z-score using MAD. Sigma is floored -- MAD collapses to zero in a
      dead-flat window, which would make the z-score infinite and flag every
      one-tick move.
    * An absolute threshold. If a whole window is corrupt, the robust estimator is
      fooled by it entirely, and only a threshold-free backstop catches that.

    Both use only prior bars, so both are causal.
    """
    if df.height < 3:
        return empty_issues()

    closes = df["close"].to_list()
    rets = [
        math.log(c / p) if (p > 0 and c > 0) else 0.0
        for p, c in zip(closes[:-1], closes[1:])
    ]
    if not rets:
        return empty_issues()

    med = _median(rets)
    mad = _median([abs(r - med) for r in rets])
    sigma = max(mad * MAD_TO_SIGMA, sigma_floor)

    flags = [False] + [
        (abs(r - med) > k * sigma) or (abs(r) > abs_limit) for r in rets
    ]
    mask = pl.Series("outlier", flags)
    return _issues_from(
        df,
        mask,
        check_name="ret.outlier",
        severity="WARN",
        detail=f"log return beyond {k} robust sigma or {abs_limit:.0%} absolute",
        venue=venue,
        symbol=symbol,
        timeframe=timeframe,
        detected_at_ms=detected_at_ms,
    )


def _median(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    return ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0


def classify_absent_bars(
    present_ms: set[int],
    covered: list[Interval],
    target: Interval,
    timeframe: str,
) -> tuple[int, int, int]:
    """Split the expected grid into (present, absent_no_trades, unknown).

    The whole reason the coverage table exists. Inside a covered range, a missing bar
    is market information: no trades occurred. Outside one, it means only that we
    have not looked yet. Conflating them would make the report describe an outage and
    a quiet market identically.
    """
    grid = bar_grid(target[0], target[1], timeframe)
    if not grid:
        return 0, 0, 0

    present = absent = unknown = 0
    for ts in grid:
        if ts in present_ms:
            present += 1
        elif _in_any(ts, covered):
            absent += 1
        else:
            unknown += 1
    return present, absent, unknown


def absent_runs(
    present_ms: set[int],
    covered: list[Interval],
    target: Interval,
    timeframe: str,
) -> list[tuple[int, int]]:
    """Contiguous stretches of covered-but-absent bars, as (start_ms, n_bars).

    An isolated absent bar and a five-hour block of them are different events, and
    the difference is not visible in a percentage. On a liquid instrument a long run
    is a venue outage, not a quiet market -- BTC/USD does not stop trading for five
    hours. Measured on the real 4-year 1h backfill: three runs of 3, 5 and 5 bars,
    on 2023-03-04, 2025-10-25 and 2026-05-08.

    This matters downstream because a pattern window spanning an outage is garbage.
    The contiguity mask already refuses those windows; this is what tells the trader
    the outage was there at all.
    """
    grid = bar_grid(target[0], target[1], timeframe)
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    run_len = 0
    for ts in grid:
        is_absent = ts not in present_ms and _in_any(ts, covered)
        if is_absent:
            if run_start is None:
                run_start = ts
            run_len += 1
        elif run_start is not None:
            runs.append((run_start, run_len))
            run_start, run_len = None, 0
    if run_start is not None:
        runs.append((run_start, run_len))
    return runs


def _in_any(ts: int, intervals: list[Interval]) -> bool:
    for start, end in intervals:
        if start <= ts < end:
            return True
        if start > ts:
            break
    return False
