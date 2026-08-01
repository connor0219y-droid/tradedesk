"""Full Phase 3 validation on ETH and SOL -- all 20 patterns, MC null, BH, OOS split.

Run at the 40bps upper bound (OKX US base, maker both sides). The trade set is
fee-independent -- stop and target are market levels set from mid, so costs move fill
prices but not which bars trigger -- so gross, p and BH are identical at any fee level,
and net at the real 248bps tier is strictly worse than what is shown here.
"""
from datetime import datetime, timezone
from rich.console import Console
from tradedesk import store
from tradedesk.config import load_config
from tradedesk.backtest import CostModel, validate_series, render

cfg=load_config(); con=store.connect(cfg.data.db_path, read_only=True)
now=datetime.now(timezone.utc); c=Console(width=150)
LOW=CostModel(spread_bps=0.02, slippage_bps=0.4, taker_fee_bps=19.8)   # 40 bps rt

summary=[]
for symbol in ["ETH/USD","SOL/USD"]:
    for tf,sa in [("5m",1),("5m",8),("15m",8),("1h",16)]:
        print(f"\n{'='*104}\n{symbol} {tf} · {sa}xATR stop · 40 bps round trip (upper bound)\n{'='*104}")
        reps=validate_series(con,cfg,symbol,tf,as_of=now,stop_atr=sa,target_r=2.0,
                             baseline_draws=1000,bootstrap_iterations=1500,costs=LOW)
        if not reps:
            print("  no data"); continue
        render(c,reps)
        pos=[r for r in reps if (r.in_sample.expectancy_r or -9)>0]
        surv=[r for r in reps if r.survives_correction]
        raw=[r for r in reps if r.baseline and r.baseline.p_value<0.05]
        summary.append((symbol,tf,sa,len(reps),len(raw),len(surv),len(pos),
                        max((r.gross_in or -9) for r in reps),
                        max((r.in_sample.expectancy_r or -9) for r in reps)))
        print(f">>> {len(raw)} raw p<0.05 · {len(surv)} survive BH · {len(pos)} positive NET")
        for r in surv:
            print(f"    BH SURVIVOR {r.pattern}: gross {r.gross_in:+.4f}R  net {r.in_sample.expectancy_r:+.4f}R  "
                  f"OOS {r.gross_out if r.gross_out is None else f'{r.gross_out:+.4f}R'}  p={r.baseline.p_value:.4f}  n={r.in_sample.n:,}")

print(f"\n\n{'='*104}\nSUMMARY (40 bps upper bound; at the real 248 bps tier every net is strictly worse)\n{'='*104}")
print(f"{'symbol':9} {'tf':4} {'stop':>5} {'pats':>5} {'raw p<.05':>10} {'BH':>4} {'net>0':>6} {'best gross':>12} {'best net':>11}")
for s in summary:
    print(f"{s[0]:9} {s[1]:4} {s[2]:>4}x {s[3]:>5} {s[4]:>10} {s[5]:>4} {s[6]:>6} {s[7]:>+11.4f}R {s[8]:>+10.4f}R")
tot_bh=sum(s[5] for s in summary); tot_pos=sum(s[6] for s in summary)
print(f"\nacross {len(summary)} configurations: {tot_bh} BH survivors, {tot_pos} with positive net expectancy")
