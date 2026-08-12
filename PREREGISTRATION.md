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
