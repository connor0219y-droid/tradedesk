"""Level primitives and the registry that makes masking non-optional.

Two failure modes this module exists to prevent, both measured on the real store:

1. A ratio divides by zero and emits NaN or inf. Verified polars behaviour:
   1.0/0.0 -> inf, 0.0/0.0 -> NaN, and -- critically -- a NaN POISONS every rolling
   window it touches, silently, while a null at least yields null:

       [1,2,NaN,4,5] rolling_mean(3) -> [None,None,nan,nan,nan]
       [1,2,None,4,5] rolling_mean(3) -> [None,None,None,None,None]

   So the guard must sit BEFORE the division. A post-hoc fill_nan runs after the NaN
   has already spread through everything downstream.

2. A level is computed over a lookback window that spans a gap. Unmasked, 35,302
   SOL/USD 1m bars (1.68%) get an ATR(14) whose window crosses a hole, and the largest
   True Range in the store is 135x the median -- one gap bar inflates ATR roughly 10x
   for the next 14 bars.

The defence is three independent layers, because a convention in a docstring gets
violated during a refactor:

  * safe_div requires `when_zero` explicitly -- you cannot write a division without
    stating what happens at zero (the same trick as `as_of` in phase 1).
  * every level declares its lookback depth to @level, and the ENGINE applies the mask.
    Level authors never write masking code, so they cannot forget it.
  * assert_total refuses to let any frame containing NaN or inf leave the engine, even
    if a level bypassed safe_div entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal

import polars as pl

Kind = Literal["per_bar", "rolling", "session", "cross_session"]

#: Depth semantics differ by kind, and the engine dispatches on this:
#:   per_bar       -- no time mask; depth is always 1
#:   rolling       -- depth = bars of CONTIGUOUS history required (window_mask(depth))
#:   session       -- depth ignored; masked where session integrity broke at or before t
#:   cross_session -- depth = number of VALID PRIOR SESSIONS required
_KINDS: frozenset[str] = frozenset({"per_bar", "rolling", "session", "cross_session"})


class LevelError(Exception):
    """A level is misdeclared, or produced a value that must never escape."""


class NonFiniteError(LevelError):
    """A level produced NaN or inf. Nulls are permitted; these are not."""


@dataclass(frozen=True)
class LevelSpec:
    name: str
    kind: Kind
    depth: int
    outputs: tuple[str, ...]
    requires: tuple[str, ...]
    fn: Callable[["LevelContext"], pl.DataFrame]


REGISTRY: dict[str, LevelSpec] = {}


def level(
    *,
    name: str,
    kind: Kind,
    depth: int,
    outputs: tuple[str, ...],
    requires: tuple[str, ...] = (),
):
    """Register a level, forcing it to declare how much contiguous history it needs.

    Every argument is keyword-only and mandatory. Adding a level in six months without
    thinking about its lookback depth is a TypeError at import time, not a subtly wrong
    number in a backtest.
    """
    if kind not in _KINDS:
        raise LevelError(f"unknown level kind {kind!r}; expected one of {sorted(_KINDS)}")
    if depth < 1:
        raise LevelError(f"{name}: depth must be >= 1, got {depth}")
    if kind == "per_bar" and depth != 1:
        raise LevelError(f"{name}: per_bar levels have depth 1 by definition, got {depth}")
    if not outputs:
        raise LevelError(f"{name}: must declare at least one output column")

    def decorator(fn: Callable[["LevelContext"], pl.DataFrame]):
        if name in REGISTRY:
            raise LevelError(f"duplicate level name {name!r}")
        REGISTRY[name] = LevelSpec(
            name=name, kind=kind, depth=depth, outputs=tuple(outputs),
            requires=tuple(requires), fn=fn,
        )
        return fn

    return decorator


@dataclass
class LevelContext:
    """Everything a level function may read.

    `df` already carries the derived columns from levels.session: session_id, run_id,
    gap, session_broken, bar_idx_in_session, session_start_ms, and any outputs of
    levels this one declared in `requires`.
    """

    df: pl.DataFrame
    symbol: str
    timeframe: str
    tf_ms: int
    config: object  # tradedesk.config.Config

    @property
    def tf_minutes(self) -> float:
        return self.tf_ms / 60_000


# ---------------------------------------------------------------- total functions


def safe_div(num: pl.Expr, den: pl.Expr, *, when_zero: float | None) -> pl.Expr:
    """Division that is a total function.

    `when_zero` is keyword-only with NO default: you cannot write a division in this
    codebase without stating what the zero-denominator case means. Pass None to make
    it null (the usual answer -- an undefined quantity should stay undefined rather
    than being fabricated).

    The guard precedes the division deliberately. Dividing first and patching NaN
    afterwards is too late: the NaN has already propagated through any rolling window
    that touched it.
    """
    zero = pl.lit(when_zero, dtype=pl.Float64) if when_zero is not None else pl.lit(None, dtype=pl.Float64)
    return (
        pl.when(den.is_null() | num.is_null())
        .then(pl.lit(None, dtype=pl.Float64))
        .when(den == 0)
        .then(zero)
        .otherwise(num / den)
    )


def safe_sqrt(x: pl.Expr) -> pl.Expr:
    """Square root that cannot emit NaN.

    Weighted variance is non-negative by construction, but the cumulative-sums
    formulation can return about -1e-18 from floating-point cancellation, and sqrt of
    that is NaN. Clamping a tiny negative to zero corrects float error; it does not
    invent data. This is a real NaN source introduced by the numerically-stable VWAP
    formulation, not a hypothetical one.
    """
    return pl.when(x.is_null()).then(None).when(x < 0).then(pl.lit(0.0)).otherwise(x).sqrt()


def safe_ln(x: pl.Expr) -> pl.Expr:
    """Natural log that cannot emit NaN or -inf. Non-positive input yields null."""
    return pl.when(x.is_null() | (x <= 0)).then(None).otherwise(x.log())


def non_finite_columns(df: pl.DataFrame) -> dict[str, int]:
    """Float columns containing NaN or inf, with counts. Nulls are permitted."""
    offenders: dict[str, int] = {}
    for col, dtype in df.schema.items():
        if dtype not in (pl.Float32, pl.Float64):
            continue
        bad = df.select(
            (pl.col(col).is_nan() | pl.col(col).is_infinite()).fill_null(False).sum().alias("n")
        )["n"][0]
        if bad:
            offenders[col] = int(bad)
    return offenders


def assert_total(df: pl.DataFrame, *, context: str = "") -> pl.DataFrame:
    """Refuse to return a frame containing NaN or inf.

    The last line of defence: even a level that bypasses safe_div entirely cannot get
    a non-finite value past this. Nulls pass -- they are the designed representation
    for "undefined here".

    Note the .fill_null(False): is_nan() and is_infinite() both return NULL for a null
    input, and a bare .sum() would skip them. Without the fill, this check would be
    silently weaker than it looks.
    """
    offenders = non_finite_columns(df)
    if offenders:
        detail = ", ".join(f"{c}={n}" for c, n in sorted(offenders.items()))
        raise NonFiniteError(
            f"non-finite values escaped{' in ' + context if context else ''}: {detail}. "
            "Every ratio must route through safe_div with an explicit when_zero."
        )
    return df


def resolve_order(names: list[str]) -> list[LevelSpec]:
    """Topologically order levels so each runs after everything it requires."""
    ordered: list[LevelSpec] = []
    seen: set[str] = set()
    visiting: set[str] = set()

    def visit(n: str) -> None:
        if n in seen:
            return
        if n in visiting:
            raise LevelError(f"circular dependency involving level {n!r}")
        if n not in REGISTRY:
            raise LevelError(f"unknown level {n!r}; registered: {sorted(REGISTRY)}")
        visiting.add(n)
        for dep in REGISTRY[n].requires:
            visit(dep)
        visiting.discard(n)
        seen.add(n)
        ordered.append(REGISTRY[n])

    for name in names:
        visit(name)
    return ordered
