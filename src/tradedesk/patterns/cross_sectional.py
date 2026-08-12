"""The six cross-sectional strategies, as declared in PREREGISTRATION.md Part 2.

These are the strategies Part 1 had to exclude. Every one of them ranks a universe and
holds the extremes, which three crypto symbols cannot support: a quintile of 0.6 names
is not a quintile. A 500-name point-in-time equity universe can, so the exclusion is
lifted here rather than worked around.

WHY THEY LIVE IN THEIR OWN MODULE RATHER THAN THE REGISTRY. A `PatternSpec` is a boolean
expression over one instrument's bar `t`, resolved into a single position with an ATR
stop and an R-multiple target. None of that applies. There is no stop, no target, no R,
and the decision is not "does this bar qualify" but "where does this name rank against
499 others today". Registering these as patterns would require lying about all four.

ONE CONFIGURATION EACH, fixed in advance. Quintiles rather than deciles, monthly
rebalance rather than weekly, and the formation windows the sources specify. Running
both quintiles and deciles and reporting the better is the forking path the
pre-registration exists to close, and with six strategies it would quietly become twelve.

THE SKIP MONTH IS NOT OPTIONAL where a source specifies it. Jegadeesh & Titman skip the
most recent month so that short-term reversal -- which is strategy 21 here, a distinct
and opposing effect -- does not contaminate the momentum signal. A 12-month momentum
strategy without the skip is measuring both at once and is not the published strategy.
"""

from __future__ import annotations

from ..backtest.cross_section import DAYS_PER_MONTH, DAYS_PER_YEAR, CrossSectionalSpec

#: Long the top quintile, short the bottom. With ~500 eligible names that is ~100 per
#: leg, which is the regime the source papers operate in.
QUANTILES = 5

CROSS_SECTIONAL: tuple[CrossSectionalSpec, ...] = (
    CrossSectionalSpec(
        name="xs_momentum_12_1",
        source="Jegadeesh & Titman (1993), Journal of Finance 48:65",
        lookback_days=12 * DAYS_PER_MONTH,
        skip_days=DAYS_PER_MONTH,
        quantiles=QUANTILES,
        rebalance_days=DAYS_PER_MONTH,
        sign=1,
    ),
    CrossSectionalSpec(
        name="xs_momentum_6_1",
        source="Jegadeesh & Titman (1993), the 6-month formation leg",
        lookback_days=6 * DAYS_PER_MONTH,
        skip_days=DAYS_PER_MONTH,
        quantiles=QUANTILES,
        rebalance_days=DAYS_PER_MONTH,
        sign=1,
    ),
    CrossSectionalSpec(
        # Jegadeesh (1990) and Lehmann (1990): last month's losers outperform next
        # month. Same ranking as momentum, opposite sign and no skip -- the skip is
        # precisely what momentum uses to AVOID this effect, so including one here
        # would delete the strategy.
        name="xs_reversal_1m",
        source="Jegadeesh (1990), Journal of Finance 45:881",
        lookback_days=DAYS_PER_MONTH,
        skip_days=0,
        quantiles=QUANTILES,
        rebalance_days=DAYS_PER_MONTH,
        sign=-1,
    ),
    CrossSectionalSpec(
        # De Bondt & Thaler's 3-5 year losers. 36 months of formation plus a 1-month
        # skip needs ~757 trading days of history before a name is rankable at all,
        # which on an 8-year sample costs three of those years. That is a real limit on
        # what this can conclude and it is stated in the writeup, not discovered later.
        name="xs_reversal_long_term",
        source="De Bondt & Thaler (1985), Journal of Finance 40:793",
        lookback_days=36 * DAYS_PER_MONTH,
        skip_days=DAYS_PER_MONTH,
        quantiles=QUANTILES,
        rebalance_days=DAYS_PER_MONTH,
        sign=-1,
    ),
    CrossSectionalSpec(
        # George & Hwang rank on nearness to the 52-week high -- a different ranking
        # variable from a trailing return, declared on the spec so it cannot be run
        # against a variable the source never used.
        name="xs_52w_high",
        source="George & Hwang (2004), Journal of Finance 59:2145",
        signal_kind="nearness_52w",
        lookback_days=DAYS_PER_YEAR,
        skip_days=0,
        quantiles=QUANTILES,
        rebalance_days=DAYS_PER_MONTH,
        sign=1,
    ),
    CrossSectionalSpec(
        # Ang, Hodrick, Xing & Zhang: high idiosyncratic volatility earns LOW returns,
        # so the low-vol leg is the long. Ranked on total realised volatility rather
        # than idiosyncratic, because decomposing it needs a factor model this project
        # does not have -- a documented simplification, and the reason this detector is
        # named for volatility rather than for the paper's exact variable.
        name="xs_low_volatility",
        source="Ang, Hodrick, Xing & Zhang (2006), Journal of Finance 61:259",
        signal_kind="realised_vol",
        lookback_days=DAYS_PER_YEAR,
        skip_days=0,
        quantiles=QUANTILES,
        rebalance_days=DAYS_PER_MONTH,
        sign=-1,
    ),
)


def by_name(name: str) -> CrossSectionalSpec:
    for spec in CROSS_SECTIONAL:
        if spec.name == name:
            return spec
    raise KeyError(f"unknown cross-sectional strategy {name!r}")


def names() -> list[str]:
    return [s.name for s in CROSS_SECTIONAL]
