"""Configuration loading.

Secrets come from the environment, never from config.toml, and are never rendered.
`.env` is git-ignored and is read only for keys that later phases need (Alpaca).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPO_ROOT / "config.toml"

# Any env var whose name matches one of these is a secret: it may be read, but must
# never be printed, logged, or included in a report or exception message.
_SECRET_HINTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


@dataclass(frozen=True)
class VenueConfig:
    name: str
    ccxt_id: str
    max_bars_per_request: int
    settle_seconds: int
    user_agent: str
    request_timeout_ms: int
    max_retries: int
    retry_backoff_seconds: float

    @property
    def settle_ms(self) -> int:
        return self.settle_seconds * 1000


@dataclass(frozen=True)
class DataConfig:
    db_path: Path
    symbols: list[str]
    timeframes: list[str]
    history_start: date


@dataclass(frozen=True)
class SessionConfig:
    timezone: str
    calendar_version: int
    crypto_day_anchor: str
    opening_range_minutes: list[int]
    equity_rth: list[str]
    equity_premarket: list[str]


@dataclass(frozen=True)
class QualityConfig:
    min_coverage_pct: float
    absent_pct_warn: float
    absent_pct_block: float
    outlier_mad_k: float
    sigma_floor_bps: float
    outlier_abs_return: float


@dataclass(frozen=True)
class Config:
    venue: VenueConfig
    data: DataConfig
    session: SessionConfig
    quality: QualityConfig
    costs: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    backtest: dict[str, Any] = field(default_factory=dict)
    path: Path = DEFAULT_CONFIG_PATH

    def cost_for(self, symbol: str) -> dict[str, float]:
        """Cost assumptions for a symbol, with per-symbol overrides layered on."""
        base = dict(self.costs.get("default", {}))
        base.update(self.costs.get("overrides", {}).get(symbol, {}))
        return base


def load_config(path: Path | str | None = None) -> Config:
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)

    venue = VenueConfig(**raw["venue"])

    d = raw["data"]
    db_path = Path(d["db_path"])
    if not db_path.is_absolute():
        db_path = path.parent / db_path
    history_start = d["history_start"]
    if not isinstance(history_start, date):
        history_start = date.fromisoformat(str(history_start))
    data = DataConfig(
        db_path=db_path,
        symbols=list(d["symbols"]),
        timeframes=list(d["timeframes"]),
        history_start=history_start,
    )

    session = SessionConfig(**raw["session"])
    quality = QualityConfig(**raw["quality"])

    cfg = Config(
        venue=venue,
        data=data,
        session=session,
        quality=quality,
        costs=raw.get("costs", {}),
        risk=raw.get("risk", {}),
        backtest=raw.get("backtest", {}),
        path=path,
    )
    _validate(cfg)
    return cfg


def _validate(cfg: Config) -> None:
    from .timeutil import TIMEFRAME_MS, assert_epoch_aligned

    unknown = [tf for tf in cfg.data.timeframes if tf not in TIMEFRAME_MS]
    if unknown:
        raise ValueError(f"unknown timeframes in config: {unknown}")
    for tf in cfg.data.timeframes:
        assert_epoch_aligned(tf)
    if cfg.session.calendar_version < 1:
        raise ValueError("session.calendar_version must be >= 1")
    if cfg.venue.max_bars_per_request < 1:
        raise ValueError("venue.max_bars_per_request must be >= 1")


def load_dotenv(path: Path | str | None = None) -> None:
    """Load `.env` into os.environ without overwriting anything already set.

    Deliberately minimal -- no third-party dependency, and no code path that echoes a
    value back. Nothing here ever returns or logs a secret.
    """
    path = Path(path) if path else REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        os.environ.setdefault(key, value)


def is_secret_name(name: str) -> bool:
    return any(hint in name.upper() for hint in _SECRET_HINTS)


def redact(name: str, value: str) -> str:
    """Render an env var for display. Secrets become a presence indicator only."""
    if is_secret_name(name):
        return "<set>" if value else "<unset>"
    return value
