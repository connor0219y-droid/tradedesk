# Findings

Research output, as distinct from code. Every number here was measured on the local
4-year Coinbase store (8,092,151 bars, BTC/ETH/SOL, 1m/5m/15m/1h) and is reproducible
from the commands noted.

---

## 1. No pattern has a demonstrated edge on BTC/USD 5m

`tradedesk validate --symbol BTC/USD --timeframe 5m`

Twenty detectors, one position at a time, 1×ATR stop / 2R target, against a
time-of-day-matched Monte Carlo null of 1,000 draws.

Gross expectancies span **−0.040R to +0.034R** — indistinguishable from random. One
pattern reached raw p=0.025, which across 20 tests is precisely the ~1.0 false positive
chance produces. It failed Benjamini-Hochberg, and independently its out-of-sample gross
flipped sign (+0.0222R → −0.0092R).

**0 of 20 patterns survive correction.**

## 2. Costs dominate everything, by one to two orders of magnitude

Coinbase Advanced Trade base tier (verified 2026-08): **0.60% maker / 1.20% taker**.
With spread and slippage the round trip is **248 bps**.

A 1×ATR(5m) stop on BTC is ~0.140% of price. The cost is 2.48%. You pay roughly **19×
your risk** in fees per trade.

Drag is analytic: `2 × per_side × price / (stop_atr × ATR)`. So it halves each time the
stop doubles — and that is the entire lever available.

### The sweep: 45 configurations, none profitable

3 timeframes × 5 stop widths × 3 targets, best pattern per cell:

| | 5m | 15m | 1h |
|---|---|---|---|
| median ATR (% of price) | 0.140% | 0.267% | 0.592% |
| best net @ 1×ATR | −18.79R | −9.82R | −4.35R |
| best net @ 4×ATR | −4.56R | −2.33R | −1.09R |
| best net @ 16×ATR | −1.17R | −0.61R | **−0.27R** |

**0 of 45 configurations have positive net expectancy.** Best cell: 1h, 16×ATR →
−0.2724R (gross +0.0172R, drag −0.290R).

For the drag to fall to the best gross expectancy observed anywhere (+0.0759R), risk per
trade would need to be **32.7% of price**. That is not an intraday trade — and at that
width 99% of trades already exit at session close rather than at a stop or target.

## 3. Maker fills halve the drag and change nothing

Modelled honestly: a resting limit fills only if price actually came back to it, using
the subsequent bar rather than an assumed fill probability. Fees follow how the orders
rest — maker entry, maker target exit, taker stop exit.

Best result across all configurations: **−0.198R** (1h, 16×ATR).

**Adverse selection is visible and behaves as theory predicts.** Requiring a 0.25 ATR
better entry price means 35–38% of signals never fill — and net gets *slightly worse*,
not better. The fills you miss are disproportionately the winners. Any backtest assuming
limit orders always fill has this backwards and will manufacture edge.

## 4. One conditioned effect is real, and still does not clear costs

The Phase 3 context slices were in-sample, uncorrected, and compared against nothing.
Retested with a null **conditioned on the same context** (random entries drawn from
high-rel-vol bars too, not all bars), plus BH across the 9-slice family, plus a holdout:

| slice | n (IS) | gross IS | null | p | gross OOS |
|---|---|---|---|---|---|
| time 06-12 ET | 2,650 | +0.0748R | −0.0127R | 0.0005 | **−0.0362R** ⚠ flip |
| trending | 4,608 | +0.0487R | +0.0061R | 0.021 | **−0.0325R** ⚠ flip |
| rel vol ≥ 0.92× | 4,512 | +0.0699R | +0.0132R | 0.004 | +0.0144R |
| **hi rel-vol AND 06-12** | 1,332 | **+0.1372R** | −0.0071R | 0.0005 | **+0.0769R** |

4 of 9 survive BH (chance alone: ~0.45). Two flip sign out of sample. **One holds both
tests**: `ma_pullback_long` restricted to high relative volume during 06-12 ET, retaining
56% of its in-sample magnitude on the holdout.

Swept across 18 configurations with maker pricing, that surviving effect still clears
costs **nowhere**:

```
tf    stop      n      gross   NET(taker)   NET(maker)
5m      1x  1,894   +0.1193R    -14.1744R     -0.9781R
15m     8x    135   +0.0848R     -0.9313R     -0.3766R
1h     32x    129   +0.0105R     -0.1072R     -0.0712R
```

**The edge and the cost do not converge.** Widening the stop reduces drag, but it dilutes
the edge *faster* — on 5m, gross falls 34× (+0.119R → +0.0035R) while maker drag falls
only 3.8×. The best ratio achieved anywhere is ~0.18, meaning roughly **5× more edge**
would be needed than the strongest effect measured.

---

## The conclusion, stated plainly

**On BTC/USD at Coinbase base-tier fees, no intraday pattern in this library is
profitable under any tested combination of timeframe, stop width, target multiple, or
order type.** That is not a failure of the search; it is the search returning an answer.

There is one genuine, statistically defensible effect — `ma_pullback_long` in high
relative volume during 06-12 ET — that survives multiple-testing correction and holds
out of sample. It is roughly 5× too small to pay for its own execution.

### What would actually change this

- **A better fee tier.** Drag scales linearly with fees. Advanced 2 (≥$50k 30-day volume,
  0.25% taker) cuts it ~5×, which is the same order as the shortfall. This is the only
  lever measured here that could plausibly close the gap.
- **Not wider stops.** Measured, not assumed: they dilute edge faster than cost.
- **Not maker orders alone.** ~2× improvement against a ~5× shortfall.

### What this says about paper trading

TradingView's paper broker is not charging 248 bps round trip. The gap between paper
results and live results on these setups would exceed any edge plausibly present — which
is worth knowing before funding anything.

### Caveats worth keeping

- BTC/USD only. ETH and SOL are unswept.
- The *choice* of which slices to test was informed by an earlier in-sample table, which
  BH does not fully correct for. The out-of-sample survival is the stronger evidence.
- n for the surviving slice at wide stops is 129–135 — above the n≥100 line, but only just.
- Session-close exits dominate at wide stops, so those configurations are no longer
  testing an intraday setup.
