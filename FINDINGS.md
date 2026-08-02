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

## 5. Lower-fee venues: what is actually obtainable, and it still does not clear

### The fee schedules, verified rather than assumed

A 10 bps assumption turned out to be unobtainable. Base-tier US-accessible spot fees:

| venue | maker | taker | maker-maker round trip | US access |
|---|---|---|---|---|
| Coinbase Advanced | 0.60% | 1.20% | 240 bps | yes |
| Kraken Pro | 0.25% | 0.40% | 50 bps | yes |
| **OKX US** | **0.20%** | **0.35%** | **40 bps** | yes, except NY/TX/NV |
| Gemini ActiveTrader | ~0.20% | ~0.40% | ~40 bps | yes |

**Maker rebates require OKX VIP 7+**, i.e. very high volume. Not obtainable at retail size.
The realistic floor is **~40 bps**, not 10 — a 6× improvement over Coinbase base, against a
measured shortfall of ~5×. Close enough to be worth testing, not close enough to assume.

**Global OKX is not the same venue as OKX US.** Phase 1 probed `www.okx.com` and got HTTP
200 for market data, but the global platform blocks US users for *trading*. Public data
access is not trading access.

### Execution quality: thin 24h volume, but tight books at retail size

| venue | 24h notional | top-of-book spread | cost to fill $9,000 |
|---|---|---|---|
| Coinbase | $215M | — | — |
| Kraken | $37M | 0.02 bps | 0.00 bps |
| OKX US | $7.7M | 0.02 bps | 0.40 bps |
| Gemini | $4.4M | 0.00 bps | 0.00 bps |

Thin 24h volume does **not** translate into bad execution at a $9k position — spreads are
tight and the books are deep enough. Thinness hurts *data quality*, not fills at this size.

### Why the venue switch was not worth a backfill

Comparing 299 overlapping 5m bars, Coinbase vs OKX US:

- closes track within **2.1 bps median** (the venues are arbitraged)
- but OKX US has **22/299 zero-range bars (7.4%) against Coinbase's 0**, plus 51 missing bars
- and OKX US has **no data before 2025-08-01** — it launched in 2025, so ~1 year, not 4

Every candlestick detector reads bar *shape*. A venue with 7.4% zero-range bars has a
materially different pattern population from the same underlying market, so Phase 1's rule
("measure the venue you trade") is not satisfied by using Coinbase bars as a proxy.

### The upper-bound test

Rather than backfill a shorter, dirtier history, the design was inverted: pair Coinbase's
clean 4-year bars with OKX US's fee schedule. That is deliberately optimistic — best
available data quality AND best obtainable fees, a combination no real venue offers. If
nothing clears there, nothing clears on a thinner venue with a quarter the history.

**All 20 patterns, Monte Carlo null (1,000 draws), BH correction, 70/30 split:**

| config | round trip | positive net | survive BH |
|---|---|---|---|
| 5m 8×ATR | 40 bps | 0 / 20 | 0 |
| 5m 8×ATR | 70 bps | 0 / 20 | 0 |
| 15m 8×ATR | 40 bps | 0 / 20 | 0 |
| 15m 32×ATR | 40 bps | 0 / 20 | 0 |
| 1h 16×ATR | 40 bps | 0 / 16 | 0 |
| 1h 32×ATR | 40 bps | 0 / 16 | 0 |

At 40 bps, **not one pattern reaches even raw p < 0.05** — fewer than the ~1.0 chance alone
produces.

### ETH and SOL: the same answer

Full validation on the other two symbols — all 20 patterns, 1,000-draw Monte Carlo null,
BH correction, 70/30 split, at the same 40 bps upper bound:

| symbol | tf | stop | raw p<0.05 | survive BH | net > 0 | best gross | best net |
|---|---|---|---|---|---|---|---|
| ETH/USD | 5m | 1× | 0 | 0 | 0 | +0.0353R | −2.3286R |
| ETH/USD | 5m | 8× | 0 | 0 | 0 | +0.0139R | −0.2860R |
| ETH/USD | 15m | 8× | 0 | 0 | 0 | +0.0212R | −0.1427R |
| ETH/USD | 1h | 16× | 0 | 0 | 0 | +0.0175R | −0.0193R |
| SOL/USD | 5m | 1× | 1 | 0 | 0 | +0.0608R | −1.2314R |
| SOL/USD | 5m | 8× | 0 | 0 | 0 | +0.0210R | −0.1475R |
| SOL/USD | 15m | 8× | 1 | 0 | 0 | +0.0293R | −0.0611R |
| SOL/USD | 1h | 16× | 0 | 0 | 0 | +0.0119R | −0.0088R |

**0 BH survivors, 0 positive net across 8 configurations.**

Two observations worth recording:

- **Only 2 raw p<0.05 hits across 160 tests** (8 configs × 20 patterns), where chance
  alone would produce ~8. The library generates *fewer* apparent findings than noise
  would — a strong null, and a sign the one-sided Monte Carlo p-value is conservative.
- **`ma_pullback_long` is the best gross on all three symbols.** That consistency is
  mildly suggestive of a real effect rather than noise, but it reaches significance on
  none of them outside BTC 5m, and net is negative everywhere.

The closest approach to break-even anywhere in the entire study is **SOL/USD 1h 16×ATR at
−0.0088R** (gross +0.0119R against 0.0207R of drag, p=0.179) — still negative, and not
statistically distinguishable from random.

A prediction made before this run was **wrong** and is recorded as such: SOL's sparse bars
(19,852 zero-range, 3,450 gap-adjacent) were expected to be the most likely source of a
spurious edge. SOL was instead the weakest of the three. The Phase 2 contiguity masking,
which refuses to let a pattern fire across a gap, is the plausible reason.

### The structural reason the two never meet

Section 4 showed edge decays faster than drag as stops widen. Section 5 shows the
consequence: **the configurations where drag is small enough to matter are exactly the
configurations where the edge has already decayed to nothing.** They do not overlap. At
1×ATR the edge exists (+0.0222R) and drag is 19R; at 8×ATR drag is 0.37R and the edge is
gone. Lowering fees moves the drag but does not move that crossing point.

## 6. An imported TradingView strategy: the losing sample was luck, the losing system is not

An EMA 9/21 cross, filtered by the 200 EMA and confirmed by RSI(14), with a 2×ATR stop,
3×ATR target and 6 bars minimum between entries — reconstructed from a chart showing
**124 trades, profit factor 0.721, −660.74 on 100k over two months (Bitstamp)**. The
entry condition was not visible in the settings; the reconstruction and its assumptions
are documented in `patterns/trend.py`.

```
tradedesk validate --symbol BTC/USD --timeframe 15m \
  --stop-atr 2.0 --target-r 1.5 --max-bars 96 --min-bars-between 6 --draws 1000
```

**The timeframe identifies itself.** Over 201 overlapping two-month windows, this
setup produces a median of **120 trades** on 15m (range 100–145) and **355** on 5m. The
chart's 124 is a 15m figure.

| BTC/USD, 4 years | 5m | 15m |
|---|---|---|
| trades (long + short) | 8,574 | 2,891 |
| gross expectancy | +0.0063R | +0.0405R |
| gross profit factor | 1.011 | 1.080 |
| Monte Carlo p (long / short) | 0.305 / 0.689 | 0.134 / 0.238 |
| survives BH across 26 patterns | no | no |
| out-of-sample gross | +0.0079R / +0.0364R | +0.0445R / +0.0752R |
| **net expectancy @ 248 bps** | **−10.21R** | **−5.22R** |

**Two months is far too short to conclude anything.** 37% of two-month windows on 15m
have negative gross expectancy even though the four-year gross is positive, and window
profit factor ranges 0.753 to 1.720 around a median of 1.079. A losing two-month sample
is unremarkable — it is roughly a coin flip.

**And the strategy is still not tradeable**, for the reason everything else in this
document is not: gross edge of +0.04R against a cost drag of −5.22R. The sample size
question and the profitability question have different answers, and only the second one
matters.

Worth noting: the observed 0.721 sits below *every* one of the 201 windows (minimum
0.753). Some of that gap is venue and some is the reconstructed entry condition, but it
also means the chart's two months were a bad draw even for a strategy whose long-run
gross is marginally positive.

### The RSI threshold was the weakest inference, and it does not matter

RSI's role was the least-constrained assumption, so all three live readings are
registered as their own detectors and corrected together — 26 patterns in the family,
not a best-of-three. Combined long + short, four years:

| RSI reading | 5m gross | 5m net | 15m gross | 15m net | 15m trades/2mo |
|---|---|---|---|---|---|
| >50 / <50 (midline) | +0.0063R | −10.21R | +0.0405R | −5.22R | 120 |
| >55 / <45 (band) | +0.0015R | −10.20R | +0.0423R | −5.20R | 90 |
| 50–70 / 30–50 (veto) | +0.0067R | −10.25R | +0.0387R | −5.25R | 118 |

**Net expectancy is identical to two decimal places across all three.** Tightening to
55/45 discards 25% of trades and buys nothing; the overbought veto removes ~1% of
entries, because RSI above 70 at the moment of a 9/21 cross is rare. On 5m the band is
actively *worse* (long gross +0.0032R → −0.0096R). Raw p ranges 0.134–0.810 across the
six detectors; **0 survive BH on either timeframe**, and 15m produced no raw p<0.05 at
all where chance alone gives ~1.3.

The prettiest cell — 15m band-short, gross +0.0448R in-sample and +0.0993R out — is
exactly the one to distrust: it is the best of six after the fact, and it fails
correction. The variants are nested and highly correlated, which makes BH conservative
here, so this is not the correction being harsh.

The midline reading also reproduces the chart's trade count best (120 vs 124 per two
months), so it stands as the reconstruction.

Caveats specific to this test: the engine closes any open position at the midnight-ET
session boundary, which the TradingView strategy does not — that affects 18.2% of 15m
trades (5.9% on 5m) and is worth **−0.031R** on 15m, i.e. it makes this result slightly
*pessimistic*, by about 0.6% of the cost drag. The 96/288-bar holding cap never binds;
median holding period is 9–10 bars.

---

## The conclusion, stated plainly

**On BTC/USD, ETH/USD and SOL/USD, no intraday pattern in this library is profitable under any tested
combination of timeframe, stop width, target multiple, order type, or obtainable fee
tier -- including a deliberately optimistic upper bound pairing the best available data
with the best obtainable fees.** That is not a failure of the search; it is the search
returning an answer.

There is one genuine, statistically defensible effect — `ma_pullback_long` in high
relative volume during 06-12 ET — that survives multiple-testing correction and holds
out of sample. It is roughly 5× too small to pay for its own execution.

### What would actually change this

- **Not a better fee tier.** This was the leading candidate and it has now been tested.
  At the obtainable floor (~40 bps, OKX US base, maker both sides) zero patterns clear,
  because the low-drag configurations are exactly the ones where the edge has decayed.
- **Not wider stops.** Measured, not assumed: they dilute edge faster than cost.
- **Not maker orders alone.** ~2× improvement against a ~5× shortfall.
- **Not a rebate venue.** Negative maker fees require OKX VIP 7+, unobtainable at retail size.
- **Not a different instrument.** ETH and SOL now tested across 4 configurations each:
  0 survivors, 0 positive net, and only 2 raw p<0.05 hits across 160 tests where chance
  produces ~8.
- **Not an imported strategy from elsewhere.** The first one tested (finding 6, an EMA
  cross with a trend filter and RSI confirmation) lands in the same place as the native
  library: marginal gross edge, no BH survivor, net −5.22R.
- What is left untested: other instruments beyond these three, and setups not in this
  pattern library. The apparatus is instrument-agnostic and ready for them.

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
