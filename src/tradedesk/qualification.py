"""THE GATE.

One rule governs whether the tool is allowed to tell you to take a trade:

    A setup may be signalled ONLY if it
      1. survived Benjamini-Hochberg correction across the family it was tested in,
      2. held the SIGN of its expectancy out of sample, and
      3. has positive NET expectancy at the ACTUAL fee tier in config.toml,
      4. on a sample of at least `provisional_n` trades.

As of the Phase 3 study, **nothing qualifies**. That is the honest output, not an empty
state to design around: `tradedesk live` will say "no qualifying setups" and emit no
cards. A tool that manufactures a card because the screen would otherwise look bare is
the exact failure this project exists to prevent.

The gate is enforced structurally, not by convention. `qualifying_setups()` is the only
way to obtain a signallable setup, and it filters on a stored `qualifies` column that is
computed by the single predicate below. There is a test asserting that a setup failing
ANY criterion cannot produce a card, and a companion test that injects a synthetic
qualifying row and asserts a card IS produced -- because "no qualifying setups" is only
meaningful if the code path that emits one demonstrably works.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import duckdb

DDL = """
CREATE TABLE IF NOT EXISTS setup_validation (
    setup            VARCHAR NOT NULL,
    symbol           VARCHAR NOT NULL,
    timeframe        VARCHAR NOT NULL,
    direction        VARCHAR NOT NULL,
    stop_atr         DOUBLE  NOT NULL,
    target_r         DOUBLE  NOT NULL,
    max_bars         INTEGER NOT NULL,
    round_trip_bps   DOUBLE  NOT NULL,
    n_in             INTEGER NOT NULL,
    n_out            INTEGER NOT NULL,
    gross_in         DOUBLE,
    gross_out        DOUBLE,
    net_in           DOUBLE,
    ci_low           DOUBLE,
    ci_high          DOUBLE,
    p_value          DOUBLE,
    survives_bh      BOOLEAN NOT NULL,
    oos_sign_held    BOOLEAN NOT NULL,
    qualifies        BOOLEAN NOT NULL,
    disqualifiers    VARCHAR NOT NULL,
    validated_at_ms  BIGINT  NOT NULL,
    config_hash      VARCHAR NOT NULL,
    PRIMARY KEY (setup, symbol, timeframe, stop_atr, target_r, round_trip_bps)
)
"""


@dataclass(frozen=True)
class Criteria:
    """The gate's thresholds. Stored alongside results so a change invalidates them."""

    min_n: int = 100
    require_bh: bool = True
    require_oos_sign: bool = True
    require_positive_net: bool = True

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(self.__dict__, sort_keys=True).encode()
        ).hexdigest()[:12]


@dataclass
class SetupStats:
    """Everything the gate needs to judge one setup at one configuration."""

    setup: str
    symbol: str
    timeframe: str
    direction: str
    stop_atr: float
    target_r: float
    max_bars: int
    round_trip_bps: float
    n_in: int
    n_out: int
    gross_in: float | None
    gross_out: float | None
    net_in: float | None
    ci_low: float | None
    ci_high: float | None
    p_value: float | None
    survives_bh: bool
    disqualifiers: list[str] = field(default_factory=list)

    @property
    def oos_sign_held(self) -> bool:
        """Did the out-of-sample expectancy keep the sign of the in-sample one?

        A sign flip is the clearest evidence of overfitting available without new data.
        Both must be positive: an in-sample edge that goes negative out of sample is not
        a weaker edge, it is a different conclusion.
        """
        if self.gross_in is None or self.gross_out is None:
            return False
        return self.gross_in > 0 and self.gross_out > 0


def evaluate(stats: SetupStats, criteria: Criteria = Criteria()) -> tuple[bool, list[str]]:
    """The single qualification predicate. Returns (qualifies, reasons_it_did_not).

    Deliberately returns the REASONS rather than a bare boolean, so the brief and the
    live view can say *why* a setup is not signallable instead of silently omitting it.
    Silence and disqualification look identical to a user, and they should not.
    """
    reasons: list[str] = []

    if criteria.require_bh and not stats.survives_bh:
        p = "—" if stats.p_value is None else f"{stats.p_value:.3f}"
        reasons.append(f"failed Benjamini-Hochberg correction (p={p})")

    if criteria.require_oos_sign and not stats.oos_sign_held:
        gi = "—" if stats.gross_in is None else f"{stats.gross_in:+.4f}R"
        go = "—" if stats.gross_out is None else f"{stats.gross_out:+.4f}R"
        reasons.append(f"out-of-sample sign not held (in {gi}, out {go})")

    if criteria.require_positive_net and (stats.net_in is None or stats.net_in <= 0):
        n = "—" if stats.net_in is None else f"{stats.net_in:+.4f}R"
        reasons.append(
            f"net expectancy {n} at {stats.round_trip_bps:.0f} bps is not positive"
        )

    if stats.n_in < criteria.min_n:
        reasons.append(f"n={stats.n_in:,} below the {criteria.min_n} minimum")

    return (not reasons), reasons


def record(
    con: duckdb.DuckDBPyConnection,
    stats: SetupStats,
    *,
    validated_at_ms: int,
    criteria: Criteria = Criteria(),
) -> bool:
    """Persist one validation result with its verdict. Returns whether it qualifies."""
    ok, reasons = evaluate(stats, criteria)
    con.execute(
        """
        INSERT INTO setup_validation VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (setup, symbol, timeframe, stop_atr, target_r, round_trip_bps)
        DO UPDATE SET
            n_in=excluded.n_in, n_out=excluded.n_out,
            gross_in=excluded.gross_in, gross_out=excluded.gross_out,
            net_in=excluded.net_in, ci_low=excluded.ci_low, ci_high=excluded.ci_high,
            p_value=excluded.p_value, survives_bh=excluded.survives_bh,
            oos_sign_held=excluded.oos_sign_held, qualifies=excluded.qualifies,
            disqualifiers=excluded.disqualifiers,
            validated_at_ms=excluded.validated_at_ms, config_hash=excluded.config_hash
        """,
        [
            stats.setup, stats.symbol, stats.timeframe, stats.direction,
            stats.stop_atr, stats.target_r, stats.max_bars, stats.round_trip_bps,
            stats.n_in, stats.n_out, stats.gross_in, stats.gross_out, stats.net_in,
            stats.ci_low, stats.ci_high, stats.p_value, stats.survives_bh,
            stats.oos_sign_held, ok, "; ".join(reasons) or "",
            validated_at_ms, criteria.fingerprint(),
        ],
    )
    return ok


def qualifying_setups(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    round_trip_bps: float | None = None,
    criteria: Criteria = Criteria(),
) -> list[dict]:
    """THE ONLY WAY to obtain a signallable setup.

    Filters on the stored verdict AND re-checks the criteria fingerprint, so tightening
    the gate invalidates previously-stored passes rather than grandfathering them in.
    """
    sql = ["SELECT * FROM setup_validation WHERE qualifies = TRUE AND config_hash = ?"]
    params: list[object] = [criteria.fingerprint()]
    if symbol:
        sql.append("AND symbol = ?")
        params.append(symbol)
    if timeframe:
        sql.append("AND timeframe = ?")
        params.append(timeframe)
    if round_trip_bps is not None:
        sql.append("AND round_trip_bps = ?")
        params.append(round_trip_bps)
    sql.append("ORDER BY net_in DESC")
    return con.execute(" ".join(sql), params).pl().to_dicts()


def all_setups(
    con: duckdb.DuckDBPyConnection,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
) -> list[dict]:
    """Every validated setup, qualifying or not, with its disqualifiers.

    The brief uses this: a setup that was tested and rejected is more informative than
    one that is simply absent.
    """
    sql = ["SELECT * FROM setup_validation WHERE 1=1"]
    params: list[object] = []
    if symbol:
        sql.append("AND symbol = ?")
        params.append(symbol)
    if timeframe:
        sql.append("AND timeframe = ?")
        params.append(timeframe)
    sql.append("ORDER BY qualifies DESC, net_in DESC")
    return con.execute(" ".join(sql), params).pl().to_dicts()
