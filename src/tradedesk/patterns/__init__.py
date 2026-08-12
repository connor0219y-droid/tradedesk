"""Pattern detection.

Detectors are pure boolean expressions over bar `t` and earlier. Each declares its
lookback depth, and the engine applies the contiguity mask -- so a three-bar pattern can
never fire across a six-hour venue outage.
"""

from . import (  # noqa: F401  (registers the detectors)
    candles,
    published,
    regime,
    structures,
    trend,
)
from .base import (
    REGISTRY,
    PatternError,
    PatternSpec,
    RiskSpec,
    detect,
    families,
    pattern,
    registered,
)

__all__ = [
    "detect", "pattern", "registered", "families", "REGISTRY", "PatternSpec",
    "RiskSpec", "PatternError",
]
