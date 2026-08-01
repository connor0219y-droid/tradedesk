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
class PatternSpec:
    name: str
    depth: int
    direction: Direction
    fn: Callable[[], pl.Expr]
    requires: tuple[str, ...] = ()
    doc: str = ""

    @property
    def is_long(self) -> bool:
        return self.direction == "long"


REGISTRY: dict[str, PatternSpec] = {}


def pattern(
    *,
    name: str,
    depth: int,
    direction: Direction,
    requires: tuple[str, ...] = (),
):
    """Register a detector, forcing it to declare its lookback depth and direction.

    `depth` counts the signal bar itself: a two-bar pattern like engulfing has depth 2.
    """
    if depth < 1:
        raise PatternError(f"{name}: depth must be >= 1")
    if direction not in ("long", "short"):
        raise PatternError(f"{name}: direction must be 'long' or 'short'")

    def decorator(fn: Callable[[], pl.Expr]):
        if name in REGISTRY:
            raise PatternError(f"duplicate pattern name {name!r}")
        REGISTRY[name] = PatternSpec(
            name=name, depth=depth, direction=direction, fn=fn,
            requires=tuple(requires), doc=(fn.__doc__ or "").strip(),
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


def registered(direction: Direction | None = None) -> list[str]:
    return sorted(
        n for n, s in REGISTRY.items() if direction is None or s.direction == direction
    )
