"""In-sample / out-of-sample discipline.

Every expectancy Phase 3 produces is in-sample by construction -- the patterns and their
thresholds were chosen by looking at this data. So the split exists from the first
report rather than arriving in Phase 5: fit on the older 70%, and print the held-out 30%
beside it. A rule whose in-sample expectancy is +0.30R and out-of-sample is -0.05R is
overfit, and that should be visible while deciding what to keep.

THE SUBTLE LEAK THIS MODULE OWNS: context bucket boundaries. Splitting trades by
"above/below median relative volume" with a median taken over all four years leaks the
holdout's distribution into the in-sample labels. It is small, it is invisible, and it
is exactly the kind of thing that makes an out-of-sample number quietly optimistic. So
thresholds are fitted on the in-sample slice ONLY, then applied unchanged to both.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl


class LeakageError(Exception):
    """The fit window and the reporting window overlap."""


@dataclass(frozen=True)
class Split:
    boundary_ms: int
    in_sample_pct: float

    def is_in_sample(self, bar_open_ms: int) -> bool:
        return bar_open_ms < self.boundary_ms

    def assert_no_overlap(self, in_ms: list[int], out_ms: list[int]) -> None:
        """Fail loudly on a single shared bar.

        The brief: "Fail the build if the fit window and the reporting window overlap by
        a single bar."
        """
        if not in_ms or not out_ms:
            return
        if max(in_ms) >= self.boundary_ms:
            raise LeakageError(
                f"in-sample contains a bar at {max(in_ms)} at or after the boundary "
                f"{self.boundary_ms}"
            )
        if min(out_ms) < self.boundary_ms:
            raise LeakageError(
                f"out-of-sample contains a bar at {min(out_ms)} before the boundary "
                f"{self.boundary_ms}"
            )
        overlap = set(in_ms) & set(out_ms)
        if overlap:
            raise LeakageError(f"{len(overlap)} bars appear in both windows")


def make_split(df: pl.DataFrame, *, in_sample_pct: float = 70.0) -> Split:
    """Split by TIME, not by row count.

    A row-count split would put a disproportionate share of quiet, sparse sessions on
    one side; splitting on the time axis keeps each window a contiguous stretch of
    market history, which is what "out of sample" is supposed to mean.
    """
    if df.is_empty():
        return Split(boundary_ms=0, in_sample_pct=in_sample_pct)
    lo = int(df["bar_open_ms"].min())
    hi = int(df["bar_open_ms"].max())
    boundary = lo + int((hi - lo) * (in_sample_pct / 100.0))
    return Split(boundary_ms=boundary, in_sample_pct=in_sample_pct)


def partition_trades(trades: list, split: Split) -> tuple[list, list]:
    """Assign trades by their SIGNAL time.

    Signal time, not exit time: a trade signalled just before the boundary but exiting
    after it belongs to the window in which the decision was made. Assigning by exit
    would let in-sample decisions be scored with out-of-sample outcomes.
    """
    in_s = [t for t in trades if split.is_in_sample(t.signal_ms)]
    out_s = [t for t in trades if not split.is_in_sample(t.signal_ms)]
    return in_s, out_s
