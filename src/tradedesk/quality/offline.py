"""NON-CAUSAL data-quality checks.

Everything in this module reads bar `t+1`. That is legitimate for an offline audit of
stored history and catastrophic if it ever reaches a feature, a signal, or a
backtest: a check that knows the next bar's price can "detect" things no live system
could.

The separation is physical rather than a `causal=False` column because a column is
easy to ignore during a refactor and an import is not. Nothing in `tradedesk` outside
the quality report may import this module -- there is a test asserting exactly that.
"""

from __future__ import annotations

import math

import polars as pl

from .checks import ISSUE_SCHEMA, MAD_TO_SIGMA, _median, empty_issues


def run_offline_checks(
    df: pl.DataFrame,
    *,
    venue: str,
    symbol: str,
    timeframe: str,
    detected_at_ms: int,
    reversion_fraction: float = 0.75,
    k: float = 8.0,
    sigma_floor: float = 1e-4,
) -> pl.DataFrame:
    """Checks that need hindsight.

    `tick.reversion` is the classic bad-tick signature: a large move that almost
    entirely reverses on the very next bar. A genuine move tends to persist; a bad
    print does not. Distinguishing them requires seeing what happened next, which is
    precisely why this cannot be a feature.
    """
    if df.height < 3:
        return empty_issues()

    closes = df["close"].to_list()
    rets = [
        math.log(c / p) if p > 0 and c > 0 else 0.0
        for p, c in zip(closes[:-1], closes[1:])
    ]
    med = _median(rets)
    mad = _median([abs(r - med) for r in rets])
    sigma = max(mad * MAD_TO_SIGMA, sigma_floor)

    hits: list[int] = []
    for i in range(len(rets) - 1):
        move, next_move = rets[i], rets[i + 1]
        if abs(move - med) <= k * sigma:
            continue
        # Opposite sign and at least `reversion_fraction` of the move undone.
        if move * next_move < 0 and abs(next_move) >= reversion_fraction * abs(move):
            hits.append(int(df["bar_open_ms"][i + 1]))

    if not hits:
        return empty_issues()
    return pl.DataFrame(
        {
            "venue": [venue] * len(hits),
            "symbol": [symbol] * len(hits),
            "timeframe": [timeframe] * len(hits),
            "bar_open_ms": hits,
            "check_name": ["tick.reversion"] * len(hits),
            "severity": ["WARN"] * len(hits),
            "detail": [
                f"outsized move reversed >={reversion_fraction:.0%} on the next bar"
            ]
            * len(hits),
            "causal": [False] * len(hits),
            "detected_at_ms": [detected_at_ms] * len(hits),
        },
        schema=ISSUE_SCHEMA,
    )
