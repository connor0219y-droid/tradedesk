"""Q1: does ANY (timeframe, stop width, target) clear the 248bps round trip?

Screens on gross expectancy per pattern per config -- cheap, no Monte Carlo -- then
reports the best NET cell. The null is only worth running where net is positive.
"""
from datetime import datetime, timezone
from collections import Counter
import polars as pl
from tradedesk import store
from tradedesk.config import load_config
from tradedesk.frames import read_bars
from tradedesk.levels import compute_levels
from tradedesk.patterns import REGISTRY, detect
from tradedesk.patterns.regime import add_regime_columns
from tradedesk.backtest import CostModel, BacktestConfig, IntrabarResolver, run_backtest

cfg=load_config(); con=store.connect(cfg.data.db_path, read_only=True)
now=datetime.now(timezone.utc)
SYMBOL="BTC/USD"
costs=CostModel.for_symbol(cfg,SYMBOL)
print(f"{SYMBOL} · round trip {2*costs.per_side_bps:.0f} bps\n")

frames={}
for tf in ["5m","15m","1h"]:
    d=add_regime_columns(compute_levels(read_bars(con,SYMBOL,tf,as_of=now),cfg).to_polars())
    m1=read_bars(con,SYMBOL,"1m",as_of=now).to_polars()
    frames[tf]=(d, IntrabarResolver.from_frame(m1))
    med_atr=d["atr_intraday"].drop_nulls().median()
    med_px=d["close"].median()
    print(f"  {tf:4} median ATR {med_atr:8.2f} = {100*med_atr/med_px:.3f}% of price")
print()

rows=[]
for tf,(d,rv) in frames.items():
    for stop_atr in [1,2,4,8,16]:
        for target_r in [1.0,2.0,3.0]:
            best=None
            agg=Counter(); tot_n=0
            for name in sorted(REGISTRY):
                spec=REGISTRY[name]
                if any(c not in d.columns for c in spec.requires): continue
                r=run_backtest(d, detect(d,name), is_long=spec.is_long, timeframe=tf,
                    costs=costs, bt=BacktestConfig(stop_atr=stop_atr,target_r=target_r,max_bars=48),
                    resolver=rv)
                if r.n<30: continue
                g=sum(t.r_gross for t in r.trades)/r.n
                nn=sum(t.r_net for t in r.trades)/r.n
                agg.update(t.exit_reason for t in r.trades); tot_n+=r.n
                if best is None or nn>best[1]: best=(name,nn,g,r.n)
            if best:
                drag=best[1]-best[2]
                rows.append((tf,stop_atr,target_r,best[0],best[3],best[2],best[1],drag,
                             100*agg['session_close']/max(tot_n,1), 100*agg['bar_cap']/max(tot_n,1)))

print(f"{'tf':4} {'stop':>5} {'tgt':>5} {'best pattern':24} {'n':>7} {'gross':>10} {'NET':>10} {'drag':>9} {'sess%':>6} {'cap%':>6}")
best_overall=None
for r in rows:
    mark = "  <== NET POSITIVE" if r[6]>0 else ""
    print(f"{r[0]:4} {r[1]:>4}x {r[2]:>4.1f}R {r[3]:24} {r[4]:>7,} {r[5]:>+9.4f}R {r[6]:>+9.4f}R {r[7]:>+8.3f}R {r[8]:>5.1f}% {r[9]:>5.1f}%{mark}")
    if best_overall is None or r[6]>best_overall[6]: best_overall=r
print()
pos=[r for r in rows if r[6]>0]
print(f"{len(rows)} configurations tested · {len(pos)} with positive net expectancy")
print(f"best cell: {best_overall[0]} {best_overall[1]}xATR {best_overall[2]}R -> "
      f"{best_overall[3]} net {best_overall[6]:+.4f}R (gross {best_overall[5]:+.4f}R, drag {best_overall[7]:+.3f}R)")
