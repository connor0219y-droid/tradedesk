"""Full Phase 3 validation at obtainable low-fee levels -- ALL 20 patterns.

UPPER-BOUND DESIGN. This pairs Coinbase's clean 4-year bars with OKX US's fee schedule.
Neither venue offers both. It is deliberately optimistic: the best available data quality
AND the best obtainable fees. If no pattern clears here, none clears on a thinner venue
with a shorter history, and the OKX US backfill is unnecessary.

Fees are the MEASURED base-tier schedules, not assumptions:
  OKX US   0.200% maker / 0.350% taker  -> 40 bps maker-maker, 70 bps taker-taker
  Kraken   0.250% maker / 0.400% taker  -> 50 / 80
Spread and walk cost measured live: <=0.02 bps spread, <=0.40 bps walk for a $9k fill.
"""
from datetime import datetime, timezone
from rich.console import Console
from tradedesk import store
from tradedesk.config import load_config
from tradedesk.backtest import CostModel, validate_series, render

cfg=load_config(); con=store.connect(cfg.data.db_path, read_only=True)
now=datetime.now(timezone.utc); c=Console(width=150)

SCENARIOS=[
  ("OKX US base, maker both sides (40 bps rt)", CostModel(spread_bps=0.02, slippage_bps=0.4, taker_fee_bps=19.8)),
  ("OKX US base, taker both sides (70 bps rt)", CostModel(spread_bps=0.02, slippage_bps=0.4, taker_fee_bps=34.8)),
]
for tf, stop_atr in [("5m", 8), ("15m", 8), ("1h", 16)]:
    for label, cm in SCENARIOS:
        print(f"\n{'='*100}\n{label}  ·  BTC/USD {tf}  ·  {stop_atr}xATR stop  "
              f"(round trip {2*cm.per_side_bps:.1f} bps)\n{'='*100}")
        reps=validate_series(con,cfg,"BTC/USD",tf,as_of=now,stop_atr=stop_atr,target_r=2.0,
                             baseline_draws=1000,bootstrap_iterations=1500,costs=cm)
        render(c,reps)
        pos=[r for r in reps if (r.in_sample.expectancy_r or -9)>0]
        surv=[r for r in reps if r.survives_correction]
        print(f">>> {len(pos)} patterns with positive NET · {len(surv)} survive BH")
        for r in surv:
            print(f"    {r.pattern}: net {r.in_sample.expectancy_r:+.4f}R  "
                  f"OOS gross {r.gross_out:+.4f}R  p={r.baseline.p_value:.4f}")
