"""The decisive test: does the one surviving effect clear costs anywhere?

ma_pullback_long restricted to (high relative volume AND 06-12 ET) -- the only slice
that survived both Benjamini-Hochberg and the out-of-sample check -- swept across stop
width and timeframe, priced with MAKER entry and target exits and TAKER stop exits.
"""
from datetime import datetime, timezone
from tradedesk import store
from tradedesk.config import load_config
from tradedesk.frames import read_bars
from tradedesk.levels import compute_levels
from tradedesk.patterns import REGISTRY, detect
from tradedesk.patterns.regime import add_regime_columns
from tradedesk.backtest import (BacktestConfig, CostModel, IntrabarResolver,
                                make_split, partition_trades, run_backtest)
import polars as pl

cfg=load_config(); con=store.connect(cfg.data.db_path, read_only=True)
now=datetime.now(timezone.utc); SYM="BTC/USD"
MAKER,TAKER,SPREAD,SLIP,BPS=60.0,120.0,2.0,3.0,1e-4
SIX=6*3_600_000

print(f"ma_pullback_long | high rel-vol AND 06-12 ET | {SYM}")
print("maker entry + maker target exit + taker stop exit\n")
print(f"{'tf':4} {'stop':>5} {'n':>6} {'gross':>10} {'NET(taker)':>12} {'NET(maker)':>12} {'clears?':>9}")
any_pos=False
for tf in ["5m","15m","1h"]:
    df=add_regime_columns(compute_levels(read_bars(con,SYM,tf,as_of=now),cfg).to_polars())
    rv=IntrabarResolver.from_frame(read_bars(con,SYM,"1m",as_of=now).to_polars())
    costs=CostModel.for_symbol(cfg,SYM)
    split=make_split(df,in_sample_pct=70.0)
    base=run_backtest(df,detect(df,"ma_pullback_long"),is_long=True,timeframe=tf,
                      costs=costs,bt=BacktestConfig(stop_atr=1.0,target_r=2.0),resolver=rv)
    in_t,_=partition_trades(base.trades,split)
    rvs=sorted(t.rvol for t in in_t if t.rvol is not None)
    RV=rvs[len(rvs)//2] if rvs else 1.0
    for stop_atr in [1,2,4,8,16,32]:
        r=run_backtest(df,detect(df,"ma_pullback_long"),is_long=True,timeframe=tf,
                       costs=costs,bt=BacktestConfig(stop_atr=stop_atr,target_r=2.0,max_bars=48),
                       resolver=rv)
        sel=[t for t in r.trades if t.rvol is not None and t.rvol>=RV
             and t.tod_ms is not None and SIX<=t.tod_ms<2*SIX]
        if len(sel)<30: continue
        g=sum(t.r_gross for t in sel)/len(sel)
        n_taker=sum(t.r_net for t in sel)/len(sel)
        # maker pricing on the same trade set
        tot=0.0
        for t in sel:
            risk=abs(t.entry_price-t.stop)
            entry_fee=t.entry_price*MAKER*BPS
            exit_fee=(t.exit_price*MAKER*BPS if t.exit_reason=="target"
                      else t.exit_price*(TAKER+SPREAD/2+SLIP)*BPS)
            tot += t.r_gross - (entry_fee+exit_fee)/risk
        n_maker=tot/len(sel)
        clears = "YES" if n_maker>0 else "no"
        if n_maker>0: any_pos=True
        print(f"{tf:4} {stop_atr:>4}x {len(sel):>6,} {g:>+9.4f}R {n_taker:>+11.4f}R {n_maker:>+11.4f}R {clears:>9}")
print()
print("RESULT: at least one configuration clears costs" if any_pos else
      "RESULT: no configuration clears costs, on the only effect that survived both\n"
      "        multiple-testing correction and the out-of-sample check.")
