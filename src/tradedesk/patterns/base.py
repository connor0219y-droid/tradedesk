"""Pattern registry.

Same declared-depth trick as the level engine, for the same reason: a pattern that reads
bars `t-2, t-1, t` must not fire when those rows are not actually adjacent in time. On a
sparse 24/7 venue they frequently are not -- SOL/USD 1m alone has 3,450 gap-adjacent
bars, and a "three-bar reversal" spanning a six-hour hole is an artifact, not a pattern.

Detectors never apply the mask themselves. They declare `depth` and the engine applies
`BarFrame.window_mask(depth)`, so forgetting it is not possible.

Detectors are pure boolean expressions over bar `t` and earlier. Nothing here may read
`t+1` -- that is what `direction` is for: a bearish pattern is a short signal, not a
lookahead peek at what happened next.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import polars as pl

Direction = Literal["long", "short"]


class PatternError(Exception):
    """A pattern is misdeclared."""


@dataclass(frozen=True)
class RiskSpec:
    """The stop and target a SOURCE specified, carried by the detector itself.

    An imported strategy is an entry condition AND its exit rules. Validating it under
    whatever stop the command line happened to pass tests a different strategy that
    shares its entry, so a detector that came from a published rule pins its own risk
    parameters here and the validator uses them instead of the run-wide defaults.

    This is also a defence against the garden of forking paths. A strategy whose stop
    lives in its spec has one configuration; a strategy whose stop is a CLI flag has as
    many as you care to try, and the temptation is to quote the best.
    """

    stop_atr: float
    target_r: float
    #: The holding cap in DAYS, not bars. Every source states its cap in calendar terms
    #: -- "hold one month", "exit within two to six bars" on a daily chart, "day trade
    #: only" -- and a bar count would silently mean six days at 1d and one day at 4h.
    #: `bars_for` converts it against the timeframe actually being traded.
    max_hold_days: float
    atr_column: str = "atr_daily"
    hold_across_sessions: bool = True
    min_bars_between_entries: int = 0

    def __post_init__(self) -> None:
        if self.stop_atr <= 0:
            raise PatternError(f"stop_atr must be > 0, got {self.stop_atr}")
        if self.target_r <= 0:
            raise PatternError(f"target_r must be > 0, got {self.target_r}")
        if self.max_hold_days <= 0:
            raise PatternError(f"max_hold_days must be > 0, got {self.max_hold_days}")
        if self.atr_column not in ("atr_intraday", "atr_daily"):
            raise PatternError(f"unknown atr_column {self.atr_column!r}")

    def bars_for(self, tf_ms: int) -> int:
        """The holding cap in bars on a given timeframe, at least one bar."""
        return max(1, round(self.max_hold_days * 86_400_000 / tf_ms))


@dataclass(frozen=True)
class PatternSpec:
    name: str
    depth: int
    direction: Direction
    fn: Callable[[], pl.Expr]
    requires: tuple[str, ...] = ()
    doc: str = ""
    #: Set only for detectors imported from a published source. None means "use the
    #: run-wide stop and target", which is what every hand-written pattern does.
    risk: RiskSpec | None = None
    #: Pre-registered family this detector belongs to, e.g. "published". The multiple
    #: testing correction is applied across a family, so the family has to be a
    #: declared property of the detector rather than a filter invented at report time.
    family: str = "library"
    #: Citation for an imported strategy. Empty for hand-written patterns.
    source: str = ""
    #: Timeframes this detector may be evaluated on. Empty means "any", which is what
    #: every hand-written pattern declares.
    #:
    #: This exists to make a pre-registration executable. Declaring in a document that a
    #: strategy is evaluated at one horizon, and then running it at three because its
    #: columns happen to be present, is how a family of 36 tests quietly becomes 100 --
    #: and the correction that was sized for 36 stops controlling anything. Enforcing it
    #: here means the run cannot drift from the document without the declaration
    #: changing too.
    timeframes: tuple[str, ...] = ()

    @property
    def is_long(self) -> bool:
        return self.direction == "long"

    def runs_on(self, timeframe: str) -> bool:
        return not self.timeframes or timeframe in self.timeframes


REGISTRY: dict[str, PatternSpec] = {}


def pattern(
    *,
    name: str,
    depth: int,
    direction: Direction,
    requires: tuple[str, ...] = (),
    risk: RiskSpec | None = None,
    family: str = "library",
    source: str = "",
    timeframes: tuple[str, ...] = (),
):
    """Register a detector, forcing it to declare its lookback depth and direction.

    `depth` counts the signal bar itself: a two-bar pattern like engulfing has depth 2.
    """
    if depth < 1:
        raise PatternError(f"{name}: depth must be >= 1")
    if direction not in ("long", "short"):
        raise PatternError(f"{name}: direction must be 'long' or 'short'")
    # An imported strategy without its source is untraceable, and the whole point of
    # importing it is that someone else specified it. Refuse at import time.
    if risk is not None and not source:
        raise PatternError(f"{name}: a detector with a RiskSpec must cite its source")

    def decorator(fn: Callable[[], pl.Expr]):
        if name in REGISTRY:
            raise PatternError(f"duplicate pattern name {name!r}")
        REGISTRY[name] = PatternSpec(
            name=name, depth=depth, direction=direction, fn=fn,
            requires=tuple(requires), doc=(fn.__doc__ or "").strip(),
            risk=risk, family=family, source=source,
            timeframes=tuple(timeframes),
        )
        return fn

    return decorator


def detect(df: pl.DataFrame, name: str) -> pl.Series:
    """Boolean signal series for one pattern, with contiguity enforced.

    A signal is only emitted where the pattern's whole lookback window is genuinely
    consecutive in time AND the required level columns are present.
    """
    spec = REGISTRY.get(name)
    if spec is None:
        raise PatternError(f"unknown pattern {name!r}; registered: {sorted(REGISTRY)}")

    missing = [c for c in spec.requires if c not in df.columns]
    if missing:
        raise PatternError(f"{name}: needs level columns {missing}")

    raw = df.select(spec.fn().alias("sig"))["sig"].fill_null(False)

    # Contiguity: none of the `depth-1` preceding bars may have opened a gap.
    if spec.depth > 1:
        breaks = (
            df.select(
                pl.col("gap")
                .cast(pl.Int32)
                .rolling_sum(window_size=spec.depth - 1, min_samples=spec.depth - 1)
                .alias("b")
            )["b"]
        )
        contiguous = (breaks == 0).fill_null(False)
    else:
        contiguous = ~df["gap"].fill_null(True)

    # A pattern requiring a level cannot fire where that level is null.
    ok = raw & contiguous
    for col in spec.requires:
        ok = ok & df[col].is_not_null()
    return ok.rename(name)


def registered(
    direction: Direction | None = None, *, family: str | None = None
) -> list[str]:
    return sorted(
        n
        for n, s in REGISTRY.items()
        if (direction is None or s.direction == direction)
        and (family is None or s.family == family)
    )


def families() -> list[str]:
    return sorted({s.family for s in REGISTRY.values()})
