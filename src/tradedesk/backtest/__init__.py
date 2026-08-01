"""Honest backtesting.

Entry at the next bar's open, one position at a time, costs on both sides, exits
resolved intrabar where the 1m data allows, and every result compared against a
time-of-day-matched random baseline.
"""

from .baseline import BaselineResult, run_baseline
from .costs import CostModel
from .engine import BacktestConfig, BacktestResult, Trade, run_backtest
from .exits import IntrabarResolver
from .report import (PatternReport, apply_multiple_testing_correction,
                     render, render_detail)
from .split import LeakageError, Split, make_split, partition_trades
from .stats import Stats, compute
from .validate import validate_series

__all__ = [
    "run_backtest", "BacktestConfig", "BacktestResult", "Trade",
    "CostModel", "IntrabarResolver",
    "compute", "Stats",
    "run_baseline", "BaselineResult",
    "make_split", "partition_trades", "Split", "LeakageError",
    "PatternReport", "render", "render_detail", "validate_series",
    "apply_multiple_testing_correction",
]
