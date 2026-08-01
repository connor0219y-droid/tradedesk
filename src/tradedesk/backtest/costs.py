"""Transaction costs, applied on both sides of every trade.

A strategy that is profitable gross and negative net is a losing strategy, so costs are
not an afterthought applied to the summary -- they move the actual fill prices, which
also changes which trades hit their stop.

Three components, all in basis points of notional:

  spread     you cross it, so you pay half on entry and half on exit
  slippage   adverse fill beyond the quote, both sides
  fee        taker fee on notional, both sides

Defaults in config.toml are deliberately conservative for Coinbase retail. Understating
costs is the single easiest way to manufacture an edge that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass

BPS = 1e-4


@dataclass(frozen=True)
class CostModel:
    spread_bps: float = 2.0
    slippage_bps: float = 3.0
    taker_fee_bps: float = 60.0

    @classmethod
    def for_symbol(cls, cfg, symbol: str) -> "CostModel":
        c = cfg.cost_for(symbol)
        return cls(
            spread_bps=float(c.get("spread_bps", 2.0)),
            slippage_bps=float(c.get("slippage_bps", 3.0)),
            taker_fee_bps=float(c.get("taker_fee_bps", 60.0)),
        )

    @property
    def per_side_bps(self) -> float:
        """Half the spread, plus slippage, plus the fee -- charged on each side."""
        return self.spread_bps / 2.0 + self.slippage_bps + self.taker_fee_bps

    def fill_price(self, mid: float, *, is_long: bool, is_entry: bool) -> float:
        """Adverse fill: you always trade at the worse side of the quoted price.

        Entering long you pay up; exiting long you sell down. Both directions of both
        sides move against you, which is what makes the round trip cost 2x per_side.
        """
        adverse_up = is_long if is_entry else not is_long
        factor = 1.0 + self.per_side_bps * BPS if adverse_up else 1.0 - self.per_side_bps * BPS
        return mid * factor

    def round_trip_in_r(self, entry: float, stop: float) -> float:
        """Approximate round-trip cost expressed in R, for reporting.

        The backtest applies costs to prices rather than subtracting this, but the
        number is worth showing: on a tight stop it is often a large fraction of the
        edge being claimed.
        """
        risk = abs(entry - stop)
        if risk <= 0:
            return float("inf")
        return (2.0 * self.per_side_bps * BPS * entry) / risk
