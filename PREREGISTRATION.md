# Pre-registration: the published-strategy batch

This file fixes a family of hypotheses **before** any of them is run. It exists because
a Benjamini-Hochberg correction is only meaningful over a family declared in advance: if
detectors are added after seeing results, or a threshold is nudged and re-run, the
correction is being applied to a family that was itself chosen by the data, and the false
discovery rate it claims to control is fiction.

Written 2026-08-11. Nothing below was run before this file was committed.

---

## What is being tested

18 strategies from published sources, each with an entry condition, a stop and a target
that were specified by the source rather than by me. Registered as **36 detectors** (long
and short separately, so their expectancies cannot cancel into a meaningless combined
number — the same convention the rest of the library uses).

Selection rule, stated so it is auditable: I searched for strategies that are (a)
specified concretely enough to implement without inventing the entry, (b) from a named
published source, and (c) time-series rules on a single instrument. Every candidate
meeting all three was included. Nothing was dropped after implementation.

**Two exclusion classes, and why.** *Cross-sectional* strategies — Jegadeesh & Titman
(1993) momentum, the cross-sectional leg of George & Hwang (2004), Liu & Tsyvinski's
quintile sorts — rank a universe. Three instruments cannot support a quintile sort, so
they are excluded rather than degraded into something that shares only a name with the
published rule. *Monthly-rebalanced* strategies are kept only where their signal can be
evaluated on every bar; a literal monthly rebalance over four years yields ~48
observations, which cannot clear the n≥30 out-of-sample gate after a 70/30 split.

## Where each strategy is evaluated

Session-anchored intraday strategies run on **5m**; swing strategies run on **4h** and
**1d**. Each strategy is evaluated at exactly one horizon class — the one its source
specifies. No strategy is run at several horizons and the best one reported.

Calendar lookbacks are expressed in **calendar units, not bars**: a "20-day high" is the
high of the last 20 days at every timeframe, computed as 20 × (bars per day). So the
Turtle breakout on 4h bars is the same economic event as on 1d bars, detected up to four
hours earlier rather than at the daily close. Indicators whose period is intrinsically a
*bar* count — RSI(2), NR7, the 80-20 bar — are that bar count on the timeframe being
traded, and are literal only on 1d. This is flagged per detector below.

## Three fidelity limits that apply to the whole family

These are properties of the existing backtest engine, not choices made for this batch.
They are stated once here rather than repeated 36 times, and they all cut the same way —
against the strategies.

1. **Entry is the next bar's open.** Eight of these strategies enter on a *stop order* at
   a named price (the breakout level, the prior 20-day low, yesterday's low). The engine
   enters at the open of the bar after the signal bar closes. On a breakout this is
   usually a worse fill than the published rule; on a failed-breakout reversal it can be
   better. It is a real deviation and it is not small.
2. **Stops are in ATR.** Several sources specify a stop at a fixed percentage (Lundström's
   opposite threshold, at 2ρ) or at a price (one tick under today's low). The engine only
   places stops at k × ATR. Where a source's stop is not an ATR multiple, the nearest ATR
   equivalent is used and the deviation is named in the detector's docstring.
3. **No trailing stops and no indicator exits.** Turtle exits on a 10-day Donchian
   channel; Connors exits on a 5-day MA cross; Zarattini trails. The engine exits on
   stop, target, bar cap, or session close. Where the source's exit is unavailable, a
   fixed target and bar cap approximate it, and the docstring says so.

A strategy that fails here has not been refuted at its source. It has failed *this*
implementation of it, on *these* three instruments, under *these* costs.

---

## The family

`R` is the risk unit (the distance from entry to stop). "daily ATR" is ATR(14) on the ET
day, so a stop is comparable across timeframes.

### Momentum and trend (7 strategies, 14 detectors) — 4h and 1d

| # | Detector | Source | Entry (bar `t`, close) | Stop | Target |
|---|---|---|---|---|---|
| 1 | `tsmom_12m_{long,short}` | Moskowitz, Ooi & Pedersen (2012), *JFE* 104:228 | trailing 12-month return crosses zero | 2× daily ATR | 3R |
| 2 | `turtle_s1_{long,short}` | Original Turtle Rules (Dennis/Eckhardt, 1983) System 1 | close exceeds the prior 20-day high | 2N = 2× ATR | 3R |
| 3 | `turtle_s2_{long,short}` | Original Turtle Rules, System 2 | close exceeds the prior 55-day high | 2N = 2× ATR | 3R |
| 4 | `high_52w_{long,short}` | George & Hwang (2004), *JF* 59:2145 | close makes a new 52-week high | 2× daily ATR | 3R |
| 5 | `crypto_wk_mom_{long,short}` | Liu & Tsyvinski (2021), *RFS* 34:2689 | trailing 1-week return crosses zero | 2× daily ATR | 3R |
| 6 | `gao_intraday_{long,short}` | Gao, Han, Li & Zhou (2018), *JFE* 129:394 | at the start of the last 30 min, sign of the first 30-min return | 1× daily ATR | 10R |
| 7 | `noise_breakout_{long,short}` | Zarattini, Aziz & Barbon (2024), SSRN 4824172 | at a 30-min boundary, close leaves the noise band | 0.5× daily ATR | 10R |

Notes. **(1)** MOP's rule is `sign(r[t-12m])` held one month with 40%/σ vol scaling; the
sizing is a portfolio construction and is not a per-trade rule, so only the sign rule is
tested. Fired on the *crossing* rather than as a standing condition, or it signals on
every bar of a trend. **(2,3)** N is Wilder's ATR(20) in the source; the engine's ATR(14)
is used, a 14-vs-20 difference that is named here rather than hidden. The System 1
"skip if the last breakout won" filter is **not** implemented, and neither is the 55-day
failsafe entry that depends on it — see the second amendment below. **(6)** Paper's strategy is unstopped and closes at the session close;
the 1× daily ATR stop exists only because the engine requires one and is wide enough
that the session-close exit dominates. **(7)** Noise band = session open × (1 ± the
14-session average absolute move from the open at this time of day). Trailing stop
replaced by a fixed stop; see limit 3.

### Mean reversion (6 strategies, 12 detectors) — 4h and 1d

| # | Detector | Source | Entry (bar `t`, close) | Stop | Target |
|---|---|---|---|---|---|
| 8 | `connors_rsi2_{long,short}` | Connors & Alvarez (2008), *Short Term Trading Strategies That Work* | close > 200-day SMA and RSI(2) crosses below 5 | 2× daily ATR | 1R |
| 9 | `turtle_soup_{long,short}` | Raschke & Connors (1995), *Street Smarts* ch. 4 | new 20-day low, prior 20-day low ≥4 sessions earlier | 0.5× daily ATR | 2R |
| 10 | `turtle_soup_p1_{long,short}` | Raschke & Connors (1995) ch. 5 | as above, ≥3 sessions, and close ≤ the prior 20-day low | 0.5× daily ATR | 2R |
| 11 | `eighty_twenty_{long,short}` | Raschke & Connors (1995) ch. 6 | prior bar opened in the top 20% and closed in the bottom 20% of its range | 0.5× daily ATR | 2R |
| 12 | `momentum_pinball_{long,short}` † | Raschke & Connors (1995) ch. 7 | prior session's LBR/RSI < 30, then close breaks the first hour's high | 0.5× daily ATR | 2R |
| 13 | `bollinger_revert_{long,short}` | Bollinger (2001), *Bollinger on Bollinger Bands* | close crosses below the lower band (20, 2σ) | 2× daily ATR | 1R |

Notes. **(8)** RSI(2) is intrinsically a bar count; literal only on 1d. Connors exits on a
close above the 5-day SMA and specifies no stop — the 1R target and the bar cap stand in,
per limit 3. **(9,10)** "The previous 20-day low must have occurred at least four
trading sessions earlier" is encoded exactly: the prior 20-day low must be strictly below
the lowest low of the last 3 sessions. **(11)** Reads the prior *bar*, so it is the
published daily rule on 1d and its bar-scale analogue elsewhere. **(12)** LBR/RSI is a
3-period RSI of the 1-period close change, taken at the prior session's last bar.

† **Amendment, 2026-08-11, before any run.** `momentum_pinball` was listed here as a 4h
and 1d strategy. It cannot be: its entry is a break of *the first hour's* range, and a
one-hour opening range does not exist inside a 4-hour bar (the column is null there, so
the detector would silently never fire). It moves to the intraday group and is evaluated
on **5m**, like the other session-anchored strategies. This is a data-availability fact
known before seeing any result, not a reaction to one. It is recorded rather than
silently corrected because an amended pre-registration that hides its amendments is
worth less than none.

**Amendment 2, 2026-08-11, before any run.** The table above claimed Turtle System 1's
"ignore this breakout if the last one would have won" filter would be implemented. It
cannot be. A detector in this library is a pure boolean expression over bar `t` and
earlier; that filter requires simulating each previous breakout forward to a 2N stop or
a 10-day exit and carrying the verdict as state. The 55-day failsafe entry, which exists
only to catch signals the filter skipped, goes with it. `turtle_s1_*` is therefore the
**unfiltered** 20-day Donchian breakout — System 1's entry without its selectivity — and
`turtle_s2_*` is unaffected, since System 2 takes every breakout by design. This makes
System 1 a weaker test of the published system than intended, and it is disclosed rather
than left for a reader to discover in the source.

### Volatility (5 strategies, 10 detectors) — 5m for 15/16, 4h and 1d for 14/17/18

| # | Detector | Source | Entry (bar `t`, close) | Stop | Target |
|---|---|---|---|---|---|
| 14 | `crabel_nr7_{long,short}` | Crabel (1990), *Day Trading with Short Term Price Patterns and ORB* | prior bar was the narrowest range of 7; close breaks its range | 1× daily ATR | 2R |
| 15 | `crabel_stretch_{long,short}` | Crabel (1990) | close crosses session open ± stretch | 1× daily ATR | 2R |
| 16 | `lundstrom_orb_{long,short}` | Lundström (2013), Umeå UES 861 | close crosses session open × (1 ± 1.0%) | 0.6× daily ATR | 2R |
| 17 | `crabel_id_nr4_{long,short}` | Crabel (1990) | prior bar was an inside bar and NR4; close breaks its range | 1× daily ATR | 2R |
| 18 | `squeeze_breakout_{long,short}` | Bollinger (2001), "The Squeeze" | band width at a 125-day low on the prior bar; close breaks the band | 1× daily ATR | 2R |

Notes. **(15)** Stretch = the 10-session average of `min(high − open, open − low)`, from
strictly prior sessions. **(16)** Lundström's ρ ∈ {0.5, 1.0, 1.5, 2.0}%; **ρ = 1.0% is
fixed in advance** — testing all four and reporting the best is the exact failure this
file exists to prevent. His stop is the opposite threshold (risk = 2ρ = 2% of price);
0.6× daily ATR is the nearest ATR equivalent on BTC (daily ATR ≈ 3.5% of price) and is
the deviation named in limit 2.

---

## The decision rule, fixed in advance

A strategy is called a finding only if **all** of the following hold. These are the
gates the existing library already applies; they are restated so that no gate can be
relaxed after seeing a number.

1. In-sample n ≥ 30 (the engine refuses to display below this) and out-of-sample n ≥ 30.
2. Gross expectancy beats its own time-of-day-matched random baseline at p < 0.05.
3. It survives Benjamini-Hochberg at FDR 0.05 **across the whole family tested on that
   symbol × timeframe series**.
4. Net expectancy after costs is positive at an obtainable fee tier.
5. The sign of the gross edge is the same in-sample and out-of-sample.

Gate 3 is the reason this file exists. With 36 detectors, roughly two raw p < 0.05
results are expected from chance alone on every series.

**The family size per series.** Because each strategy is evaluated at one horizon class,
the family tested on a given symbol × timeframe series is the horizon-appropriate subset,
not all 36: **m = 10** on 5m (the five session-anchored strategies) and **m = 26** on 4h
and on 1d (the other thirteen). That partition is declared on the detectors themselves,
so a run cannot quietly evaluate a swing strategy at 5m and enlarge the family the
correction was sized for.

**Monte Carlo resolution.** BH's smallest threshold over m tests is `0.05/m`: 0.005 at
m = 10 and 0.00192 at m = 26. The baseline p-value is `(k+1)/(draws+1)`, so 1,000 draws
bottoms out at 0.000999 — the same order as the threshold it has to be compared against.
Runs use **4,000 draws**, fixed here, so the p-value can resolve below the BH line rather
than being clipped by it.

## What would count as a null result

If no strategy clears all five gates, the finding is that 18 published strategies, run
faithfully enough to be worth arguing with, do not produce a demonstrated edge on BTC,
ETH or SOL under obtainable costs. That is a real answer and it will be written up as
one — not as an invitation to widen the family until something passes.

---

# Part 2: the equity study

Written 2026-08-12, **before any equity backtest was run**. The bars were still being
fetched when this section was committed; nothing below was informed by a result.

Part 1 tested 18 published strategies on BTC, ETH and SOL. That was a substitution: most
of those papers studied equities, and crypto was the data on hand. Part 2 runs the same
family on the asset class the sources actually studied, and adds the strategies Part 1
had to exclude.

## What changes, and why each change matters

**Costs fall by two orders of magnitude.** Part 1 lived under a 248 bps round trip that
buried every edge by a factor of 10–100. US equities are commission-free; the round trip
is spread plus slippage, estimated per name from its own bars (Corwin-Schultz 2012, see
`backtest/equity_costs.py`) and typically single-digit basis points. This is the first
time in this project that a real edge would have room to survive its own execution.

**Cross-sectional strategies become possible.** Three crypto symbols cannot support a
quintile sort. A 500-name universe can. The exclusion recorded in Part 1 is lifted.

**The sessions are real.** "The first half-hour return" now means the first half hour
after an actual opening bell, not the first half hour of a synthetic ET day. Part 1's
intraday detectors were testing the shape of a rule; here they test the rule.

## The universe, and the bias it is built to avoid

Point-in-time S&P 500 membership, 96 month-end snapshots. **681 distinct tickers against
~503 at any one moment** — so a today's-constituents universe would omit 26% of the
sample, and not a random 26%: it is the acquired, the demoted and the failed. SIVB and
SBNY leave after February 2023, FRC after April 2023, TWTR after September 2022.

Intraday coverage is the 50 names with the highest median daily dollar volume over
**2018-01-01 to 2018-07-31 — strictly before the study window opens.** Ranking on
today's liquidity, or on the full sample, selects the names that went on to be heavily
traded, which is the same error as using today's index members.

Three limits, restated here rather than left in a docstring: editor lag of a few days
around index changes; no CIK-level continuity, so a reused ticker is indistinguishable
by identifier alone; month-end sampling, so a name added and dropped inside one month is
invisible.

## Data integrity, decided before the data was stored

Alpaca serves delisted symbols, and for some of them it serves fabrications: SBNY has
509 zero-volume bars frozen at 70.00 for two years after the bank was seized, and CA has
1,323 frozen at its buyout price followed by a different company on the same ticker.
The rules, fixed in advance:

1. A zero-volume bar is **absent, not flat**. Kept, a frozen price turns a total loss
   into a flat position — SBNY would book 0% instead of −100%, which is the survivorship
   bias re-entering through the bars after being removed from the universe.
2. Each ticker is **clipped to its index tenure**, which is what defeats ticker reuse.
   The buffer is asymmetric — 420 days leading, 10 trailing — because formation windows
   reach backwards and reuse arrives forwards.
3. Residual discontinuities are **flagged, not stitched**.

Verified on live data: SIVB keeps all 1,159 bars and its real −60.4% collapse; CA's
fabricated −43% day disappears.

## The added family: cross-sectional strategies

Long the top quintile, short the bottom, equal-weighted, monthly rebalance, on the
point-in-time universe. Registered **before** running, as Part 1's family was.

| # | Strategy | Source | Formation | Skip | Sign |
|---|---|---|---|---|---|
| 19 | `xs_momentum_12_1` | Jegadeesh & Titman (1993), *JF* 48:65 | 12 months | 1 month | long winners |
| 20 | `xs_momentum_6_1` | Jegadeesh & Titman (1993), the 6-month leg | 6 months | 1 month | long winners |
| 21 | `xs_reversal_1m` | Jegadeesh (1990), *JF* 45:881 | 1 month | 0 | long losers |
| 22 | `xs_reversal_long_term` | De Bondt & Thaler (1985), *JF* 40:793 | 36 months | 1 month | long losers |
| 23 | `xs_52w_high` | George & Hwang (2004), *JF* 59:2145 | nearness to the 52-week high | 0 | long nearest |
| 24 | `xs_low_volatility` | Ang, Hodrick, Xing & Zhang (2006), *JF* 61:259 | 12-month realised vol | 0 | long lowest |

Six strategies, one configuration each. Quintiles (not deciles) fixed in advance: with
~500 names a quintile is ~100 per leg, and testing both and reporting the better is the
forking path this document exists to close.

**The null is random-rank portfolios** — same eligible names, same rebalance dates, same
holding period, same costs, ranks shuffled. A long-short book of 200 diversified
positions produces a smooth equity curve whether or not the signal means anything, so
"it looks like a real strategy" is not evidence.

## Horizons, universes, and how the 18 time-series strategies are scored

**Horizons.** Session-anchored strategies on **5m**, swing strategies on **1d**. Part 1's
4h leg has no equity analogue — an equity session is 6.5 hours, so a 4h bar is neither
intraday nor daily — and is dropped rather than reinterpreted. That leaves **10 detectors
at 5m** and **26 at 1d**, the same partition Part 1 declared.

**Two universes, for two different reasons.**

- *Time-series strategies* run on the **50 names selected by 2018 liquidity**, at both
  horizons. These are per-instrument rules; breadth adds symbols, not information, and
  using the same 50 at 5m and 1d is what makes the two horizons comparable to each other.
- *Cross-sectional strategies* run on the **full 681-name point-in-time universe**,
  daily. They need breadth: that is the entire point of a quintile sort, and it is the
  thing three crypto symbols could not provide.

**Trades are POOLED ACROSS SYMBOLS, one test per strategy.** This is the significant
departure from Part 1 and it is a decision about what is being claimed. Part 1 tested
three symbols and treated each as its own family, which was tractable. Here, scoring
26 detectors against 50 symbols separately would be 1,300 tests at 1d alone, and a
correction sized for that has no power left — the smallest Benjamini-Hochberg threshold
would be 4×10⁻⁵, below what any affordable number of Monte Carlo draws can resolve.

More importantly, per-symbol scoring answers a question nobody asked. Connors does not
claim RSI(2) works on Cisco; he claims it works on equities. So every detector produces
ONE pooled sample — all trades from all 50 names — and one test. The null is pooled the
same way: random entries drawn per symbol, matched to that symbol's own time-of-day
histogram and trade count, then pooled into a single null mean per draw. The strategy and
its null therefore face identical symbol composition, identical trade counts and
identical hours.

**What pooling costs, stated rather than discovered later.** Part 1's one-position-at-a-
time rule kept trades close enough to independent for a bootstrap interval to mean
something. Pooling across 50 names breaks that: positions overlap in time and equities
are cross-sectionally correlated through market beta, so the effective sample is smaller
than the trade count suggests and a naive interval is too tight. The p-value comes from
a matched null rather than from a parametric interval, and the bootstrap CI is reported
but is optimistic and is not a gate.

> **Correction, made after the run (2026-08-12).** The sentence that stood here claimed
> "the null is pooled identically, so the correlation is present on both sides of the
> comparison." **That is wrong**, and it overstated the method in the strategy's favour.
> The null is *aggregated* identically, but it is *sampled* independently per symbol and
> uniformly over each symbol's history within its time-of-day buckets — so it does not
> reproduce the calendar clustering of the real signals, which fire on many names at once
> during market-wide moves. The null's variance is therefore understated and the
> time-series p-values are anti-conservative: too small, not too large.
>
> Scope, and why it does not change a verdict here. It applies **only to the 36
> time-series tests**, none of which survived; if anything they should be read as weaker
> still. It does **not** apply to the 6 cross-sectional tests, whose null shuffles ranks
> within fixed rebalance dates and therefore preserves the calendar and cross-sectional
> structure exactly — and both strategies that survived the correction were
> cross-sectional. Empirically no inflation is visible either: 2 raw p<0.05 out of 36
> where chance gives ~1.6.
>
> The fix would be a block bootstrap or date-matched sampling for the time-series null.
> It is recorded here rather than quietly repaired because the original sentence was a
> claim about rigour, and a claim about rigour that turns out to be false is worth more
> as a correction than as a deletion.

## The full family, and the correction across it

**42 tests. One Benjamini-Hochberg correction across all 42.**

| family | tests | what each test is |
|---|---|---|
| time-series, 5m | 10 | one pooled sample per detector, 50 symbols |
| time-series, 1d | 26 | one pooled sample per detector, 50 symbols |
| cross-sectional, 1d | 6 | one long-short portfolio per strategy, 681 symbols |

The two families are **reported separately**, because a per-trade R-multiple and a
per-month portfolio return are not the same quantity and putting them in one column
would invite exactly the wrong comparison. But the correction is applied **across all 42
together**, because 42 is the number of tests actually run and looked at. Correcting
within each family separately would be the garden of forking paths wearing a lab coat:
two corrections at FDR 0.05 do not control the error rate over the union.

Benjamini-Hochberg at FDR 0.05 over m = 42 gives a smallest threshold of
**0.05/42 = 0.00119**. The baseline p-value is `(k+1)/(draws+1)`, so runs use **4,000
draws**, fixed here, and 1/4001 = 0.00025 resolves comfortably below the line.

**Anything that scores goes in the family.** If a detector produces no trades, or fails
the sample gate, it is reported as such and still counted in m — dropping empty tests
from the denominator after the fact would inflate every surviving p-value.

## The decision rule, unchanged from Part 1

All five gates, restated so none can be relaxed after seeing a number: in- and
out-of-sample n ≥ 30; beats its own matched random baseline at p < 0.05; survives
Benjamini-Hochberg at FDR 0.05 **across all 42 tests**; positive net expectancy after
costs; and the same sign in-sample and out-of-sample.

For the cross-sectional strategies the gates read across with one substitution: the
sample unit is a rebalance period rather than a trade, so n is the number of periods and
the out-of-sample split is chronological on the same 70/30 boundary. Cross-sectional
results are reported in **per-period portfolio returns, not R-multiples** — there is no
stop, so there is no R.

## What would count as a null result

Same standard as Part 1. If the strategies fail on the asset class their authors studied,
with realistic costs, a survivorship-free universe and real sessions, that is a stronger
negative than Part 1 produced and it will be written up as one. And if something does
survive here, the honest reading is that Part 1's negative was about crypto and costs
rather than about the strategies — which is exactly why this part is worth running.
