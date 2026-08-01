"""Causal level engine.

Every level is a total function (never NaN, never inf) and every level declares the
contiguous history it requires, which the engine enforces. See levels/base.py.
"""

from .base import LevelSpec, NonFiniteError, assert_total, safe_div, safe_sqrt
from .engine import DEFAULT_LEVELS, LevelFrame, compute_levels

__all__ = [
    "compute_levels", "LevelFrame", "DEFAULT_LEVELS",
    "safe_div", "safe_sqrt", "assert_total", "LevelSpec", "NonFiniteError",
]
