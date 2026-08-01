"""Q3: test the ma_pullback_long context slices properly.

The slices in the Phase 3 report were in-sample, uncorrected, and compared against
nothing. Testing them properly needs three things the earlier table lacked:

  1. A null CONDITIONED ON THE SAME CONTEXT. Comparing "pattern on high rel-vol bars"
     against "random on all bars" attributes the context's own effect to the pattern.
     The random entries must be drawn from high rel-vol bars too.
  2. Benjamini-Hochberg across the whole family of slices tested -- 9 slices at
     alpha=0.05 produces ~0.45 hits by chance.
  3. An out-of-sample check, since the slice boundaries were chosen by looking at
     in-sample results.
"""
import random
from collections import Counter, defaultdict
from datetime import datetime, timezone
from tradedesk import store
from tradedesk.config import load_config
from tradedesk.frames import read_bars
from tradedesk.levels import compute_levels
from tradedesk.patterns import REGISTRY, detect
from tradedesk.patterns.regime import add_regime_columns
from tradedesk.backtest import (BacktestConfig, CostModel, IntrabarResolver,
                                make_split, partition_trades, run_backtest)
from tradedesk.backtest.engine import precompute_outcomes

cfg=load_config(); con=store.connect(cfg.data.db_path, read_only=True)
now=datetime.now(timezone.utc)
SYM,TF,PATTERN="BTC/USD","5m","ma_pullback_long"
DRAWS=2000; TOD_BUCKET=3_600_000

df=add_regime_columns(compute_levels(read_bars(con,SYM,TF,as_of=now),cfg).to_polars())
rv=IntrabarResolver.from_frame(read_bars(con,SYM,"1m",as_of=now).to_polars())
costs=CostModel.for_symbol(cfg,SYM); bt=BacktestConfig(stop_atr=1.0,target_r=2.0)
spec=REGISTRY[PATTERN]
res=run_backtest(df,detect(df,PATTERN),is_long=True,timeframe=TF,costs=costs,bt=bt,resolver=rv)
split=make_split(df,in_sample_pct=70.0)
in_t,out_t=partition_trades(res.trades,split)

rvs=sorted(t.rvol for t in in_t if t.rvol is not None); RV=rvs[len(rvs)//2]
efs=sorted(t.eff_ratio for t in in_t if t.eff_ratio is not None); EF=efs[len(efs)//2]
outcomes=precompute_outcomes(df,is_long=True,timeframe=TF,costs=costs,bt=bt,resolver=rv)

tod=df["ms_since_open"].to_list(); gap=df["gap"].to_list()
atr=df["atr_intraday"].to_list(); rvol=df["rvol_tod"].to_list(); eff=df["eff_ratio"].to_list()
bar_ms=df["bar_open_ms"].to_list()

SIX=6*3_600_000
SLICES={
 "time 00-06 ET": (lambda t: t.tod_ms is not None and t.tod_ms<SIX,
                   lambda i: tod[i] is not None and tod[i]<SIX),
 "time 06-12 ET": (lambda t: t.tod_ms is not None and SIX<=t.tod_ms<2*SIX,
                   lambda i: tod[i] is not None and SIX<=tod[i]<2*SIX),
 "time 12-18 ET": (lambda t: t.tod_ms is not None and 2*SIX<=t.tod_ms<3*SIX,
                   lambda i: tod[i] is not None and 2*SIX<=tod[i]<3*SIX),
 "time 18-24 ET": (lambda t: t.tod_ms is not None and t.tod_ms>=3*SIX,
                   lambda i: tod[i] is not None and tod[i]>=3*SIX),
 f"rel vol >= {RV:.2f}x": (lambda t: t.rvol is not None and t.rvol>=RV,
                   lambda i: rvol[i] is not None and rvol[i]>=RV),
 f"rel vol <  {RV:.2f}x": (lambda t: t.rvol is not None and t.rvol<RV,
                   lambda i: rvol[i] is not None and rvol[i]<RV),
 f"trending eff>={EF:.3f}": (lambda t: t.eff_ratio is not None and t.eff_ratio>=EF,
                   lambda i: eff[i] is not None and eff[i]>=EF),
 f"ranging  eff< {EF:.3f}": (lambda t: t.eff_ratio is not None and t.eff_ratio<EF,
                   lambda i: eff[i] is not None and eff[i]<EF),
 "hi relvol AND 06-12": (lambda t: t.rvol is not None and t.rvol>=RV and t.tod_ms is not None and SIX<=t.tod_ms<2*SIX,
                   lambda i: rvol[i] is not None and rvol[i]>=RV and tod[i] is not None and SIX<=tod[i]<2*SIX),
}

def conditioned_p(trades, pool_pred, seed=0):
    """Null drawn from bars satisfying the SAME condition, matched on time of day."""
    if len(trades)<30: return None,None,None
    obs=sum(t.r_gross for t in trades)/len(trades)
    hist=Counter(int(t.tod_ms//TOD_BUCKET) for t in trades if t.tod_ms is not None)
    pool=defaultdict(list)
    for i in range(len(bar_ms)-1):
        if gap[i+1]: continue
        a=atr[i]
        if a is None or a<=0: continue
        if not pool_pred(i): continue
        if not split.is_in_sample(bar_ms[i]): continue
        pool[int(tod[i]//TOD_BUCKET)].append(i)
    rng=random.Random(seed); null=[]
    for _ in range(DRAWS):
        picks=[]
        for b,cnt in hist.items():
            cand=pool.get(b)
            if cand: picks.extend(rng.choice(cand) for _ in range(cnt))
        if not picks: continue
        picks.sort(); s=0.0; k=0; busy=-1
        for i in picks:
            if i<=busy: continue
            o=outcomes.get(i)
            if o is None: continue
            s+=o[1]; k+=1; busy=o[0]
        if k: null.append(s/k)
    if not null: return None,None,None
    p=(sum(1 for v in null if v>=obs)+1)/(len(null)+1)
    return obs, sum(null)/len(null), p

print(f"{PATTERN} · {SYM} {TF} · slices tested against a CONTEXT-MATCHED null "
      f"({DRAWS:,} draws each)\n")
print(f"{'slice':24} {'n(IS)':>7} {'gross IS':>10} {'null':>10} {'p':>7} {'n(OOS)':>7} {'gross OOS':>11}")
results=[]
for name,(tpred,ipred) in SLICES.items():
    sel_in=[t for t in in_t if tpred(t)]
    sel_out=[t for t in out_t if tpred(t)]
    obs,null,p = conditioned_p(sel_in, ipred)
    g_out = (sum(t.r_gross for t in sel_out)/len(sel_out)) if len(sel_out)>=30 else None
    results.append((name,len(sel_in),obs,null,p,len(sel_out),g_out))
    print(f"{name:24} {len(sel_in):>7,} "
          f"{obs if obs is None else f'{obs:+.4f}R':>10} "
          f"{null if null is None else f'{null:+.4f}R':>10} "
          f"{'—' if p is None else f'{p:.3f}':>7} {len(sel_out):>7,} "
          f"{'—' if g_out is None else f'{g_out:+.4f}R':>11}")

scored=[r for r in results if r[4] is not None]
m=len(scored)
ordered=sorted(scored,key=lambda r:r[4])
largest=0
for k,r in enumerate(ordered,1):
    if r[4]<=(k/m)*0.05: largest=k
print(f"\n{m} slices tested · {sum(1 for r in scored if r[4]<0.05)} with raw p<0.05 "
      f"(chance alone: ~{0.05*m:.2f}) · {largest} survive Benjamini-Hochberg at FDR 5%")
if largest:
    for r in ordered[:largest]: print(f"  SURVIVES: {r[0]} p={r[4]:.4f}")
