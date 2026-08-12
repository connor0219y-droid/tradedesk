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

## 7. Swing horizon: costs stop dominating, and the edge still is not there

The first six findings all end the same way — a gross edge near +0.03R against a cost
drag of −5R to −19R. That ratio is a property of the *horizon*, not of the patterns: a
1×ATR(5m) stop is 0.14% of price while the round trip is 2.48%. So the obvious question
is what happens when the stop is a whole daily ATR and the trade is allowed to run for
days. This finding answers it.

```
tradedesk validate --symbol BTC/USD --timeframe 4h \
  --stop-atr 2.0 --target-r 3.0 --max-bars 42 \
  --hold-across-sessions --risk-scale atr_daily --draws 1000
```

### What had to be built, and what was assumed

- **4h and 1d bars are derived**, by UTC-anchored aggregation from stored 1h
  (`resample.py`). Coinbase serves no 4h, and its 1d candles are UTC-anchored in a way
  that would silently disagree with the ET session model. Buckets are UTC-anchored
  because an ET-anchored grid produces a 3- or 5-hour "4h" bar at each DST transition,
  which the contiguity machinery would correctly read as a gap. Incomplete buckets are
  dropped rather than emitted with the wrong open — **4 buckets over four years**.
- **Positions now carry across midnight ET** (`hold_across_sessions`, default off). The
  engine previously closed every trade at the session boundary, which made a multi-day
  hold structurally impossible: the boundary fired on the first midnight regardless of
  the bar cap.
- **Stops are in daily ATR at both timeframes.** On 1d that is the timeframe's own
  ATR(14) (median **3.507%** of price); on 4h it is `atr_daily` (**3.492%**). Verified
  equal, which is what makes the two rows comparable.
- **Six session-anchored detectors are excluded** — opening-range breaks, failed
  breaks, VWAP reclaims. `or30_high` is null at every 4h and 1d bar, because a
  30-minute opening range cannot exist inside a 4-hour bar. 20 detectors remain.

### Costs really do stop dominating

| stop (BTC) | risk, % of price | cost drag |
|---|---|---|
| 1×ATR(5m) | 0.140% | **−17.7R** |
| 0.5×daily ATR | 1.75% | −1.42R |
| 1×daily ATR | 3.49% | −0.71R |
| 2×daily ATR | 6.98% | **−0.36R** |

That is a ~50× improvement on the worst intraday cell, and it is the whole point of the
horizon change. Best net anywhere: **−0.020R** (SOL/USD 4h, 1×ATR, 2R) against the
previous project best of −0.27R.

### And one cell finally went positive — it does not survive contact

BTC/USD 1d, 2×ATR stop, 3R target: **net +0.024R**, on `inside_bar_long`. The first
positive net expectancy in the project. Every check kills it:

| | |
|---|---|
| in-sample n | 45 — PROVISIONAL, below the n≥100 line |
| out-of-sample n | 18 — **REFUSED**, below n≥30 |
| gross in → out | +0.3714R → **−0.1045R**, sign flipped |
| 95% CI on net | [−0.379, +0.459] — straddles zero |
| vs its own null | +0.3714R against a null of +0.1551R, **p = 0.129** |
| survives BH | no |

It is also the best of 18 grid cells × ~10 eligible detectors. One +0.024R out of ~180
tests is what chance produces.

### The large gross numbers are drift, not edge

Swing gross expectancies look ~10× the intraday ones (+0.27R vs +0.03R), which is the
result most likely to be misread. Splitting pattern against its own time-matched null,
by direction, shows where it comes from:

| | mean gross | mean null | difference |
|---|---|---|---|
| BTC 4h long (10 detectors) | +0.1916R | +0.0510R | +0.1406R |
| BTC 4h short (10 detectors) | −0.0663R | −0.0507R | −0.0156R |
| ETH 4h long | −0.0106R | +0.0503R | −0.0609R |
| SOL 4h long | +0.1141R | +0.0211R | +0.0930R |

**Random entries are strongly directional at this horizon** — positive for longs,
negative for shorts — because a four-year window of crypto has a large upward drift and
a multi-day hold captures it. The pattern-minus-null differences are small and change
sign across instruments, and every one sits inside the null's own 95% width (±0.4R on
1d). Across all six symbol × timeframe families, **1 raw p<0.05 out of 93 scored tests,
where chance alone produces ~4.7**, and **0 survive Benjamini-Hochberg anywhere**.

### Two structural limits worth more than the numbers

**Daily bars cannot answer this question with four years of data.** One position at a
time plus a multi-day hold bounds the achievable sample:

| | bars | n≥30 in-sample | n≥100 in-sample | n≥30 out-of-sample |
|---|---|---|---|---|
| 4h (any symbol) | 8,761 | 20 / 20 | 12 / 20 | 14 / 20 |
| 1d (any symbol) | 1,458 | 10–12 / 20 | **0 / 20** | **0 / 20** |

No daily detector on any symbol clears the out-of-sample gate. The daily timeframe
cannot return a verdict here at all, and that is a fact about the data budget, not
about the setups.

**The widest stops stop testing the setup.** Exit mix on BTC, pooled over 20 detectors:

| config | trades | median hold | stop | target | bar cap |
|---|---|---|---|---|---|
| 4h, 1×ATR / 2R | 3,304 | 3.7 days | 50.5% | 21.5% | 27.7% |
| 4h, 2×ATR / 3R | 2,515 | 7.0 days | 22.6% | 2.1% | **74.6%** |
| 1d, 2×ATR / 3R | 672 | 18 days | 45.8% | 7.3% | **45.5%** |

At 2×ATR the target is hit 2.1% of the time and three quarters of trades die at the bar
cap. Those cells are measuring "hold for seven days" — which is exactly the cell that
produced the positive net. Same trap as the intraday sweep's session-close exits, in a
new place.

---

## 8. Eighteen published strategies, pre-registered: nine testable, none survive

Findings 1–7 test setups this project wrote down itself. The obvious objection is that
the library is the problem — that these are folk patterns, and real strategies live in
the literature. Finding 8 answers that directly: 18 strategies taken from published
sources, each with an entry, a stop and a target specified by its author rather than by
me, registered as 36 detectors and run in a single pass.

**The family was fixed in writing before anything was run** — see
[PREREGISTRATION.md](PREREGISTRATION.md), which names the strategies, the horizon each
is evaluated at, the decision rule, and the Monte Carlo resolution. That document is what
makes the Benjamini-Hochberg correction below mean anything, and it carries its own
amendment log: two changes were made after writing it and before running it (Momentum
Pinball moved to 5m because a one-hour opening range does not exist inside a 4h bar, and
Turtle System 1's "skip the breakout after a winner" filter turned out to be
inexpressible as a per-bar detector). Both are recorded in place rather than quietly
applied.

### What was tested

| Group | Strategies | Sources |
|---|---|---|
| Momentum / trend | 7 | Moskowitz-Ooi-Pedersen (2012); Turtle Systems 1 & 2; George-Hwang (2004); Liu-Tsyvinski (2021); Gao-Han-Li-Zhou (2018); Zarattini-Aziz-Barbon (2024) |
| Mean reversion | 6 | Connors-Alvarez RSI(2); Raschke-Connors *Street Smarts* — Turtle Soup, Turtle Soup Plus One, 80-20's, Momentum Pinball; Bollinger band reversion |
| Volatility | 5 | Crabel NR7, ID/NR4, stretch-ORB; Lundström (2013) ρ-ORB; Bollinger squeeze |

Session-anchored strategies ran on 5m, swing strategies on 4h and 1d — each at one
horizon only, enforced by a declaration on the detector rather than by my remembering.
Three instruments × three timeframes, 4,000 Monte Carlo draws per test.

### The headline

**186 tested cells, 105 of them scored against a random baseline. One raw p < 0.05, where
chance alone produces ~5.3. Zero survive Benjamini-Hochberg on any series. Two cells
have positive net expectancy, and both fail on other gates.**

Getting *fewer* raw hits than chance is itself the result. A family of 105 tests with no
edge anywhere should throw about five p < 0.05 results; this one threw one.

### Only half the family could be tested at all

The more useful finding, and the one that constrains what any of this can conclude:

| Outcome | Strategies | Which |
|---|---|---|
| Testable (in- and out-of-sample) | **9 / 18** | Connors RSI(2), Crabel ID/NR4, Crabel NR7, Crabel stretch, crypto weekly momentum, 80-20's, Gao intraday, Lundström ORB, Momentum Pinball, noise breakout |
| In-sample only (no cell reaches n≥30 out of sample) | 4 / 18 | Bollinger reversion, Turtle System 1, Turtle Soup |
| Insufficient sample anywhere | **5 / 18** | 12-month TSMOM, 52-week high, Turtle System 2, Turtle Soup Plus One, Bollinger squeeze |

The five that cannot be tested are exactly the ones with the longest lookbacks or the
rarest triggers. A 12-month return crosses zero 1–5 times in four years on BTC; the
Bollinger squeeze plus a band break fires 2–5 times. **Four years of history cannot
evaluate a strategy whose signal is annual**, and no amount of care in the harness
changes that. Finding 7 hit the same wall from the other side.

### The one cell that went positive, and why it is not an edge

`crypto_wk_mom_long` — Liu & Tsyvinski's weekly time-series momentum — on SOL/USD:

| | 4h | 1d |
|---|---|---|
| net expectancy | **+0.050R** | **+0.084R** |
| in-sample n | 68 (provisional, < 100) | 60 (provisional) |
| out-of-sample n | 33 | 26 — **below the n≥30 line** |
| gross in → out | +0.229R → +0.026R (decays 9×) | +0.272R → **−0.013R, sign flips** |
| 95% CI on net | [−0.167, +0.276] | [−0.168, +0.352] |
| p vs its own null | 0.104 | 0.070 |
| survives BH | no | no |

And the exit mix says what the trade actually was:

| | trades | median hold | bar cap | stop | target |
|---|---|---|---|---|---|
| SOL 4h | 101 | 7.0 days | **82.2%** | 13.9% | 3.0% |
| SOL 1d | 86 | 7.0 days | **79.1%** | 17.4% | 3.5% |

The target is hit 3% of the time and four trades in five die at the seven-day cap. This
cell is not measuring a stop-and-target strategy; it is measuring **"hold SOL for seven
days"**, on the instrument with the strongest drift in the store. That is the same trap
finding 7 documented, and the time-of-day-matched null already prices it — which is
precisely why p is 0.10 rather than significant. It is also SOL-only: the same strategy
on BTC and ETH is negative net at both timeframes.

The single raw p < 0.05 — `noise_breakout_long` on BTC 5m, p = 0.0497 — fails the
out-of-sample gate for a different reason: gross **+0.132R in-sample, −0.085R out**, sign
flipped.

### Costs stop dominating here too, and it does not help

The imported intraday strategies specify stops on a *daily* ATR scale, so 5m inherits
swing-like drag rather than finding 1's −17.7R:

| timeframe | median cost drag | median gross expectancy |
|---|---|---|
| 5m | −0.72R | +0.0038R |
| 4h | −0.40R | +0.0053R |
| 1d | −0.62R | −0.0338R |

Gross edge is two orders of magnitude below the drag at every horizon. This is finding 2
in a new setting: the configurations cheap enough to trade are the ones where there is
nothing left to trade.

### What this does and does not show

It does **not** show these strategies do not work. Every one was implemented under three
engine limits that all cut against it — entry at the next bar's open rather than on a
stop order at the named price, ATR stops where a source specified a percentage or a
price, and no trailing stops or indicator exits where Turtle, Connors and Zarattini all
use them. Nine of the eighteen were also tested on instruments and a venue their authors
never studied; five could not be tested at all.

What it does show is narrower and still worth having: **on BTC, ETH and SOL, at
obtainable costs, over four years, the published literature's best-known time-series
rules do no better than the setups this project wrote itself — which is to say, no better
than random entries with the same stop, target and holding rule.**

---

## 9. Equities: costs stop mattering entirely, and still nothing survives

Findings 1-8 all ended at the same place, and the same objection applied to all of them:
crypto costs 248 bps a round trip, most of those papers studied equities, and the
substitution was made because crypto was the data on hand. Finding 9 removes the
objection. Same 18 published strategies, on US equities, plus the 6 cross-sectional
strategies that three crypto symbols could not support.

Pre-registered in [PREREGISTRATION.md](PREREGISTRATION.md) Part 2 and committed before
the bars finished downloading: **42 tests — 36 time-series detectors and 6
cross-sectional strategies — with ONE Benjamini-Hochberg correction across all of them.**

### The setup

| | |
|---|---|
| universe | point-in-time S&P 500 membership, 681 names ever, ~503 at any moment |
| intraday | 50 names by median dollar volume over 2018-01..07, strictly before the window |
| data | 1.2M daily bars, 13.3M 5m bars, 2018-08 to 2026-07, split-adjusted |
| costs | commission zero; spread estimated per name (Corwin-Schultz), median 0.9 bps |
| round trip | **2.9 bps**, against crypto's 248 |

### Costs stop being the binding constraint

This is the structural result, and it is what makes the rest of the finding worth
anything:

| study | median cost drag |
|---|---|
| crypto 5m, 1×ATR stop (finding 1) | **−17.7 R** |
| crypto 4h/1d, daily-ATR stops (findings 7-8) | −0.36 to −0.72 R |
| **equities (this finding)** | **−0.011 R** |

The drag falls from 1,770% of a risk unit to **1.1%**. Eleven of 36 time-series tests
now have positive net expectancy, which never happened once in findings 1-8. Every
"but the fees" explanation for the earlier results is gone.

### Time-series: 36 tests, 0 survivors

**2 raw p < 0.05 where chance alone produces ~1.6.** Not one survives Benjamini-Hochberg,
and both fail elsewhere anyway:

| detector | tf | n in/out | gross in → out | p |
|---|---|---|---|---|
| `crypto_wk_mom_short` | 1d | 2687 / 1157 | −0.0156 → −0.0418 | 0.030 |
| `momentum_pinball_short` | 5m | 704 / 302 | +0.0480 → **−0.0551** | 0.043 |

The first is negative in both halves; the second flips sign out of sample. On the asset
class these rules were written for, with real sessions and costs 85× lower, the answer
is the same one crypto gave.

### Cross-sectional: 2 survive the correction, 0 survive contact

| strategy | n | gross/mo | net/mo | in → out | turnover | p | BH |
|---|---|---|---|---|---|---|---|
| `xs_low_volatility` | 96 | +0.57% | +0.57% | +0.37% → +1.05% | 0.08 | **0.00025** | **survives** |
| `xs_reversal_1m` | 96 | +0.35% | +0.34% | +0.55% → −0.09% | 0.58 | **0.0020** | **survives** |
| `xs_momentum_12_1` | 96 | +0.24% | +0.23% | −0.06% → +0.91% | 0.24 | 0.026 | no |
| `xs_momentum_6_1` | 96 | +0.20% | +0.19% | −0.11% → +0.91% | 0.32 | 0.048 | no |
| `xs_reversal_long_term` | 72 | +0.14% | +0.14% | +0.72% → −1.18% | 0.15 | 0.158 | no |
| `xs_52w_high` | 96 | −0.17% | −0.18% | −0.33% → +0.19% | 0.35 | 0.924 | no |

`xs_reversal_1m` fails the out-of-sample sign gate: +0.55% in-sample becomes −0.09% out.

`xs_low_volatility` is the only thing in nine findings to clear both the baseline and the
correction with its sign intact. **It still is not an edge, for two separate reasons.**

**It misses the sample gate by one period.** 96 monthly rebalances, split 70/30, gives 29
out-of-sample — and the declared gate is n ≥ 30. That gate is not relaxed here, but it
should be read as a flaw in the pre-registration rather than as evidence: a
monthly-rebalanced strategy on eight years of data can *never* clear a 30-period holdout
under a 70/30 split, because 96 × 0.30 = 28.8. The gate was written for trade counts and
applied to rebalance periods without noticing.

**It is five months out of ninety-six.** This check was NOT pre-registered, and it is
reported as post-hoc — but it cuts against the result, which is the safe direction for a
post-hoc test to cut:

| | |
|---|---|
| mean monthly return | **+0.57%** |
| median monthly return | **−0.15%** |
| positive months | 47 / 96 — worse than a coin flip |
| mean excluding the 5 best months | **−0.44%** — the sign flips |

The five months are 2020-11 (+26.1%), 2020-05 (+21.8%), 2025-04 (+20.3%), 2021-02
(+13.8%) and 2023-01 (+13.3%). The largest is the week of the COVID vaccine
announcement; the second is the March-2020 recovery. This is not a strategy with an
edge, it is a position that pays off enormously during violent factor rotations and
loses slowly the rest of the time. Five events is not a sample, and a p-value of 0.00025
against a rank-shuffled null does not become one — the null correctly reports that the
low-volatility ranking really did align with those rotations, which is true and is not
the same as being tradable.

### What this finding actually establishes

Findings 1-8 could always be answered with "crypto is expensive to trade and these are
equity strategies". That answer is now closed off. At 2.9 bps round trip, on a
survivorship-free universe of the exact instruments these papers studied, with real
opening bells and real sessions: **42 pre-registered tests, 2 survive the correction,
and neither survives inspection.**

The one that comes closest is a well-documented factor whose entire measured return sits
in five months of the sample.

---

## The conclusion, stated plainly

**On BTC/USD, ETH/USD and SOL/USD, no pattern in this library is profitable under any tested
combination of timeframe, stop width, target multiple, order type, holding horizon, or
obtainable fee tier -- including a deliberately optimistic upper bound pairing the best
available data with the best obtainable fees.** That is not a failure of the search; it
is the search returning an answer.

**The reason changes with the horizon, and finding 7 is where it changes.** Intraday,
costs dominate by one to two orders of magnitude and nothing else gets a chance to
matter. At swing horizon -- daily-ATR stops, multi-day holds -- the drag falls to
-0.36R and stops being the binding constraint. What is left is simply no demonstrable
edge: random entries with the same stop, target and holding rule do as well, and the
apparently large swing gross numbers are the market's upward drift, which the null
captures in full. Cheaper execution was never going to fix that, and now there is a
regime where cheap execution has been tested directly.

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
- **Not an imported strategy from elsewhere.** This was the most substantive remaining
  objection — that the pattern library, not the market, was the problem. It has now been
  tested twice. Finding 6 imported one TradingView strategy; finding 8 imported
  **eighteen from published sources**, pre-registered as a fixed family, with each
  author's own stop and target. Result: 1 raw p < 0.05 out of 105 scored tests where
  chance produces ~5.3, and 0 BH survivors anywhere. The literature's best-known
  time-series rules land where the native library did.
- **Not a longer horizon.** This was the strongest remaining candidate, because it is
  the only lever that moves the cost ratio by 50× rather than 2×. Tested in finding 7:
  the drag duly collapses to −0.36R, and 0 of 20 detectors survive BH on any of six
  symbol × timeframe families. The constraint stops being cost and becomes the absence
  of edge.
- What is left untested: other instruments beyond these three, and cross-sectional
  strategies, which need a universe rather than three symbols and are the single largest
  category finding 8 had to exclude. The apparatus is instrument-agnostic and ready for
  more symbols. Note that a daily-bar study of anything will need more than four years of
  history — at one position at a time, 1,458 daily bars cannot fill an out-of-sample
  window (finding 7), and finding 8 showed five of eighteen published strategies are
  untestable on this budget for the same reason.

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
  testing an intraday setup. The swing sweep hits the same wall in a different place:
  bar-cap exits reach 74.6% at 2×daily ATR (finding 7).
- The swing study rests on derived bars. 4h and 1d are aggregated from stored 1h rather
  than fetched, so any error in `resample.py` propagates to every number in finding 7.
  It is covered by hand-checked known-answer tests, which is not the same as having the
  venue's own bars to compare against.
- Finding 7's daily rows are in-sample only, by necessity — no daily detector on any
  symbol reaches n≥30 out of sample.
- **Finding 8 tests implementations, not the strategies themselves.** Entry at the next
  bar's open, ATR-only stops, and no trailing or indicator exits are all engine limits
  that make each imported strategy weaker than the rule its author published. A negative
  result there is evidence about these rules *on this venue through this harness*, and
  not a refutation of the source.
- Finding 8 returns a verdict on 9 of 18 strategies. Four more are in-sample only, and
  five cannot be evaluated at all on four years of data. Reading it as "18 published
  strategies fail" overstates it; the accurate reading is "9 fail, 9 are unanswerable
  here".
