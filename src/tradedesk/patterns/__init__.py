"""Pattern detection.

Detectors are pure boolean expressions over bar `t` and earlier. Each declares its
lookback depth, and the engine applies the contiguity mask -- so a three-bar pattern can
never fire across a six-hour venue outage.
"""

from . import candles, regime, structures, trend  # noqa: F401  (registers the detectors)
from .base import REGISTRY, PatternError, PatternSpec, detect, pattern, registered

__all__ = ["detect", "pattern", "registered", "REGISTRY", "PatternSpec", "PatternError"]
