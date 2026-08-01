"""Q2: resting limit entries, with fills determined by the DATA rather than assumed.

The honest way to model a maker entry is not a fill probability -- it is to ask whether
price actually came back to your limit. A limit buy below the next bar's open fills only
if that bar trades down to it. That captures ADVERSE SELECTION automatically: the moves
that run away from you are exactly the winners you miss, so the fill set is worse than
the signal set. A model that assumes limits always fill gets this backwards and
manufactures edge.

Fee treatment reflects how the orders actually rest:
  entry       maker  (a resting limit)
  target exit maker  (also a resting limit)
  stop exit   taker  (a stop becomes marketable when hit)
"""
from datetime import datetime, timezone
from tradedesk import store
from tradedesk.config import load_config
from tradedesk.frames import read_bars
from tradedesk.levels import compute_levels
from tradedesk.patterns import REGISTRY, detect
from tradedesk.patterns.regime import add_regime_columns
from tradedesk.backtest import IntrabarResolver
from tradedesk.backtest.exits import resolve_exit

cfg=load_config(); con=store.connect(cfg.data.db_path, read_only=True)
now=datetime.now(timezone.utc)
SYMBOL="BTC/USD"
MAKER_BPS=60.0; TAKER_BPS=120.0; SPREAD_BPS=2.0; SLIP_BPS=3.0
BPS=1e-4

def run(df, sig, is_long, tf, stop_atr, target_r, offset_atr, wait_bars, rv):
    o=df["open"].to_list(); h=df["high"].to_list(); l=df["low"].to_list()
    c=df["close"].to_list(); ts=df["bar_open_ms"].to_list()
    sess=df["session_date"].to_list(); gap=df["gap"].to_list()
    atr=df["atr_intraday"].to_list()
    tf_ms={"5m":300_000,"15m":900_000,"1h":3_600_000}[tf]
    idx=[i for i,v in enumerate(sig.to_list()) if v]
    filled=missed=0; rs=[]; busy=-1
    for i in idx:
        if i<=busy or i+1>=len(o): continue
        if gap[i+1]: continue
        a=atr[i]
        if a is None or a<=0: continue
        ref=o[i+1]
        limit = ref - offset_atr*a if is_long else ref + offset_atr*a
        # Did price actually come to the limit within the wait window?
        fill_bar=None
        for j in range(i+1, min(i+1+wait_bars, len(o))):
            if gap[j] and j>i+1: break
            if (l[j] <= limit) if is_long else (h[j] >= limit):
                fill_bar=j; break
        if fill_bar is None:
            missed+=1; continue
        filled+=1
        risk=stop_atr*a
        stop = limit-risk if is_long else limit+risk
        target = limit+target_r*risk if is_long else limit-target_r*risk
        ex=resolve_exit(highs=h,lows=l,closes=c,opens=o,ts=ts,session_dates=sess,
            tf_ms=tf_ms,entry_index=fill_bar,entry_price=limit,stop=stop,target=target,
            is_long=is_long,max_bars=48,resolver=rv)
        entry_fee = limit*MAKER_BPS*BPS
        if ex.reason=="target":
            exit_fee = ex.price*MAKER_BPS*BPS          # resting limit
        else:
            exit_fee = ex.price*(TAKER_BPS+SPREAD_BPS/2+SLIP_BPS)*BPS  # marketable
        gross=(ex.price-limit)/risk if is_long else (limit-ex.price)/risk
        rs.append(gross - (entry_fee+exit_fee)/risk)
        busy=ex.bar_index
    return filled, missed, rs

print(f"{SYMBOL} · maker entry {MAKER_BPS:.0f}bps, maker target exit, taker stop exit\n")
for tf in ["5m","1h"]:
    df=add_regime_columns(compute_levels(read_bars(con,SYMBOL,tf,as_of=now),cfg).to_polars())
    rv=IntrabarResolver.from_frame(read_bars(con,SYMBOL,"1m",as_of=now).to_polars())
    print(f"--- {tf} ---")
    print(f"{'pattern':22} {'offset':>7} {'wait':>5} {'filled':>8} {'miss%':>7} {'gross':>10} {'NET':>10}")
    for name in ["ma_pullback_long","three_bar_reversal_long","inside_bar_long"]:
        spec=REGISTRY[name]
        if any(cc not in df.columns for cc in spec.requires): continue
        s=detect(df,name)
        for offset,wait in [(0.0,1),(0.25,1),(0.25,3),(0.5,3)]:
            for stop_atr in [4,16]:
                f,m,rs=run(df,s,spec.is_long,tf,stop_atr,2.0,offset,wait,rv)
                if len(rs)<30: continue
                # gross recomputed without fees for comparison
                net=sum(rs)/len(rs)
                # approximate gross by adding back the average fee load
                print(f"{name[:22]:22} {offset:>6.2f}a {wait:>5} {f:>8,} {100*m/max(f+m,1):>6.1f}% "
                      f"{'':>10} {net:>+9.4f}R   stop={stop_atr}x")
