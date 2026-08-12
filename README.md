# tradedesk

A local research tool that answers one question honestly:

> **Does this chart pattern actually make money on this instrument — or does it just look
> like it does?**

It runs alongside TradingView and **never places orders**. TradingView stays the
execution surface; this is the research surface.

The premise is that textbook pattern lore is folklore until it has been tested against a
*random entry with the same stop, target and costs, on the same symbol and timeframe*. A
pattern that cannot beat coin-flip entries is not a pattern, it is a habit.

---

## The headline finding

After testing 26 hand-written detectors, **18 strategies imported from published
sources**, and **6 cross-sectional strategies** — all pre-registered before running (see
[PREREGISTRATION.md](PREREGISTRATION.md)) — across crypto and US equities, 3 + 681
instruments, and 8 years:

**Nothing survives. Not on BTC, ETH or SOL at any tested combination of timeframe, stop
width, target multiple, order type or fee tier; and not on the US equities most of these
papers actually studied, where the round trip is 2.9 bps instead of 248.**

That is the tool working, not failing. The details are in **[FINDINGS.md](FINDINGS.md)**;
the four that matter most:

1. **On crypto, costs dominate by one to two orders of magnitude.** At Coinbase's base
   tier the round trip is 248 bps. A 1×ATR(5m) stop on BTC is 0.14% of price — so you pay
   roughly **19× your risk** in fees per trade. No plausible edge survives that.
2. **The edge and the cost never meet.** Widening the stop shrinks the cost drag, but it
   dilutes the edge *faster*: on 5m, gross edge falls 34× while drag falls 3.8×. The
   configurations cheap enough to trade are exactly the ones where the edge is gone.
3. **Importing strategies from the literature does not rescue it.** 18 published
   strategies — Turtle, time-series momentum, Connors' RSI(2), Crabel, *Street Smarts*,
   the recent Zarattini ORB work — run as one pre-registered family produced **1 raw
   p < 0.05 out of 105 scored tests where chance produces ~5.3, and zero survivors**.
4. **Neither does removing costs entirely.** On equities the drag falls from −17.7 R to
   **−0.011 R** — from 1,770% of a risk unit to 1.1% — on a survivorship-free universe of
   681 names with real opening bells. **42 pre-registered tests, 2 survive the
   correction, and neither survives inspection.** The closest thing to an edge, a
   low-volatility quintile sort, has a mean of +0.57%/month and a **median of −0.15%**:
   remove its five best months out of ninety-six and the sign flips.

A corollary worth internalising: **TradingView's paper broker is not charging you 248
bps.** The gap between paper results and live results on these setups exceeds any edge
plausibly present.

---

## How to use this

Written for someone who has not spent much time in a terminal. Every output below is a
real capture from a real session, not an illustration.

A terminal is a window where you type one command, press Enter, and read what comes
back. On a Mac, open **Terminal** from Applications → Utilities. You'll see a line
ending in `%` or `$` — that's the prompt, waiting. When this README shows a command,
type everything *after* the prompt, not the prompt itself. `Ctrl-C` stops whatever is
running; nothing here is harmed by being interrupted.

Commands only work when the terminal is "inside" the project folder. `cd tradedesk`
puts you there, and it stays there until you close the window.

### First run, from a clean machine

Seven steps, in order. Nothing here needs an exchange account, an API key, or a `.env`
file — Coinbase publishes candle data publicly, and the tool never places orders, so it
has nothing to log in to.

```bash
# 1. install uv, the only prerequisite. It fetches its own Python 3.12,
#    so you do not need to install Python yourself.
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. download the code and move into the folder
git clone https://github.com/connor0219y-droid/tradedesk.git
cd tradedesk

# 3. install dependencies and create an empty database
make setup

# 4. prove the code works before trusting any number it prints
make test

# 5. download four years of candles for BTC, ETH and SOL
#    (~85 min. Safe to Ctrl-C — it resumes where it stopped, and re-running
#     it after a completed fetch does nothing rather than re-downloading.)
make fetch

# 6. ask whether that data is trustworthy enough to backtest on
make quality

# 7. backtest every pattern against random entries with the same stop,
#    target and costs. This writes the qualification registry that the
#    brief and the live companion read.
make validate
```

Steps 1–4 take a couple of minutes. Steps 5 and 7 are the long ones; start `make fetch`
and go do something else.

Step 4 should end like this, and takes about two seconds — the tests need no network:

```
171 passed, 5 skipped, 5 deselected in 1.78s
```

The skips are deliberate and each names its reason; the deselected five are the slow
whole-store checks, which `make levels-sweep` runs separately.

`make setup` finishes by printing the path it created:

```
initialised /Users/you/tradedesk/data/tradedesk.duckdb
```

To see what actually landed, `make status`:

```
                                  candle store
┏━━━━━━━━━┳━━━━━┳━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ symbol  ┃ tf  ┃ bars      ┃ first (UTC)    ┃ last (UTC)     ┃ coverage ranges┃
┡━━━━━━━━━╇━━━━━╇━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━┩
│ BTC/USD │ 5m  │ 420,589   │ 2022-08-01     │ 2026-08-01     │ 1              │
│         │     │           │ 00:00          │ 04:45          │                │
│ ETH/USD │ 5m  │ 420,585   │ 2022-08-01     │ 2026-08-01     │ 1              │
…12 rows, one per symbol × timeframe
```

`coverage ranges` is `1` when the history is a single unbroken span. A number above 1
means there is a hole — usually an interrupted fetch that hasn't been re-run.

### The daily loop

Three commands. That's the whole routine.

| command | when | what it is for, in plain English |
|---|---|---|
| `make brief` | before you trade | One screen that answers "is today worth trading, and what has earned the right to be traded?" It shows how volatile the instrument is right now, every price level worth watching, what a round trip costs you, and which setups passed the gate. |
| `make live` | while you're watching | Asks for your read *first*, then shows where price sits relative to those levels and whether any pattern fired on the last closed bar. It only tells you to take a trade if a setup passed the gate. |
| `make journal` | after | Compares the trades you actually took against what the backtest said to expect. This is the part that tells you whether your problem is the setup or your execution of it. |
| `make score` | after | Grades the predict-first reads `make live` recorded against what price actually did. Works even if you never place a trade. |

`make brief` covers all three configured symbols. `make live` follows one instrument —
BTC/USD 5m as configured in the `Makefile`.

### What `make live` actually looks like

It is **predict-first**: you are shown the price and the time, and nothing else, until
you've committed to a read. The point is to train your eye against a measured baseline
rather than react to the tool's opinion. Here is a real session, top to bottom.

First it asks. Only price and clock are visible — no levels, no patterns, no numbers:

```
╭─────────────────────────────── predict-first ────────────────────────────────╮
│ BTC/USD 5m · bar closed 00:45 ET · price 63,025.29                           │
│                                                                              │
│ Your read first. Nothing else is shown until you answer.                     │
╰──────────────────────────────────────────────────────────────────────────────╯
  direction [long/short/none]: long
  where would your stop go: 62980
  one-line reasoning (optional): reclaim of VWAP after the 15:05 flush
```

Three questions, in that order. `none` is a legitimate answer and skips the stop
question. Press Enter on the reasoning line to leave it blank. Your answer is written to
the database before anything else is revealed, so you cannot revise it after the fact.

Only then does it show you the levels — sorted by distance from price, measured in ATR
rather than dollars so the number means the same thing on any instrument:

```
BTC/USD 5m · last closed bar 00:45 ET · 63,025.29
level                price  distance  unit
VWAP −2σ         62,910.32     +4.74  5m ATR
OR-15 low        62,931.36     +3.87  5m ATR
OR-30 low        62,931.36     +3.87  5m ATR
OR-5 low         62,933.34     +3.79  5m ATR
VWAP −1σ         62,939.24     +3.55  5m ATR
POC              62,948.49     +3.17  5m ATR
OR-5 high        62,950.63     +3.08  5m ATR
OR-15 high       62,954.53     +2.92  5m ATR
VWAP             62,968.17     +2.35  5m ATR
OR-30 high       62,985.00     +1.66  5m ATR
VWAP +1σ         62,997.09     +1.16  5m ATR
prior day low    62,358.96     +0.40  daily ATR
prior day close  62,938.54     +0.05  daily ATR
VWAP +2σ         63,026.01     -0.03  5m ATR
prior day high   64,429.15     -0.84  daily ATR
```

A **positive** distance means the level is *below* price, a negative one *above*. So
here price is pinned to VWAP +2σ (0.03 ATR away) with everything else underneath it —
extended, with the prior day's high 0.84 daily ATR overhead. Cross-session levels are
measured in *daily* ATR on purpose: in intraday ATR a prior-day level reads ~20 units
away on 1m and ~2 on 1h purely because of the timescale, which tells you nothing.

Then what fired, and what the tool is willing to say about it:

```
no patterns triggered on the last closed bar

╭──────────────────────────── NO QUALIFYING SETUPS ────────────────────────────╮
│ No setup on this instrument has passed the gate:                             │
│   • survived Benjamini-Hochberg correction, AND                              │
│   • held its expectancy sign out of sample, AND                              │
│   • positive net expectancy at your actual fee tier, AND                     │
│   • n ≥ 100                                                                  │
│                                                                              │
│ No signal cards will be emitted. This is the measured answer, not a missing  │
│ feature.                                                                     │
╰──────────────────────────────────────────────────────────────────────────────╯

your read: long · stop 62,980.00 · reclaim of VWAP after the 15:05 flush
```

On a bar where a pattern *had* fired, it would be listed above that panel with its
measured expectancy attached — and if that expectancy is negative, inside a red
`⚠ NEGATIVE EXPECTANCY` box. The setups that tempt you are shown, not hidden, because
hiding them removes exactly the information that teaches you what they cost.

`make live` prints one snapshot and exits. Run it again when a bar closes. It reads the
**last closed bar**, never the one currently forming — the forming bar's high, low and
close all still change, and it is exactly the bar a live signal would fire on.

Add `--no-predict` to skip the questions:

```bash
uv run tradedesk live --symbol "BTC/USD" --timeframe 5m --no-predict
```

### How to read the brief

`make brief` opens with the regime and the cost drag:

```
BTC/USD 5m · 2026-08-01 19:10 ET · pre-session brief
regime                      LOW volatility · stable
ATR percentile (60d)        10
ATR(5m)                     24.26
expected daily range        1,669.68  (2.65% of price)
rel vol (same time of day)  0.53×

cost drag at 248 bps round trip
stop width  cost per round trip  gross edge needed to break even
1×ATR                    64.44R                          +64.44R
4×ATR                    16.11R                          +16.11R
8×ATR                     8.05R                           +8.05R
16×ATR                    4.03R                           +4.03R
```

**What R means.** R is one unit of *your* risk: the distance from your entry to your
stop. If you enter at 63,025.29 and put your stop 1×ATR away — today, 24.26 below — then
1R is $24.26 per unit of BTC. A trade that makes 2R made twice what it would have lost;
a trade that loses 1R hit its stop. Everything in this tool is measured in R rather than
dollars, so a result on BTC at $63k and a result on SOL at $80 are directly comparable,
and neither depends on how big your account is.

**What cost drag means.** A "basis point" is one hundredth of a percent, so 248 bps is
2.48%. That is what getting in and back out costs in total at Coinbase's base fee tier:
124 bps on each side, made up of the taker fee (120), slippage (3), and half the spread
(1). On a $63,025 position that is about $1,563. Your risk on that same trade, with a
1×ATR stop, is $24.26. So:

```
$1,563 of cost  ÷  $24.26 of risk  =  64.44R
```

You pay 64 times your risk in fees to take the trade. The right-hand column is the
consequence stated as a target: the pattern must earn **+64.44R per trade before fees**
just to break even. Nothing in the library earns a hundredth of that.

Widening the stop makes 1R bigger, so the same fee is a smaller multiple of it — 16×ATR
brings the drag down to 4.03R. That looks like the escape route. It was measured, and it
isn't: widening the stop dilutes the edge *faster* than it shrinks the drag, which is
the second headline finding above.

Today's drag is 64R rather than the ~19R quoted in the headline finding because ATR is
at its **10th percentile** — a quiet session. Same fee, smaller risk, bigger multiple.
This is why the number is recomputed and shown every session rather than memorised.

**Why the setups table says NOT VALIDATED.** Every setup that has been tested is listed
with the reasons it failed, because a rejected setup and an untested one look identical
to a reader and are not the same thing:

```
setups
setup                    status         net exp       n  why not
doji_long                NOT VALIDATED  -18.26R  10,706  failed
                                                         Benjamini-Hochberg
                                                         correction (p=0.608);
                                                         out-of-sample sign not
                                                         held (in -0.0069R, out
                                                         -0.0323R); net
                                                         expectancy -18.2643R at
                                                         248 bps is not positive
inside_bar_short         NOT VALIDATED  -18.34R  16,177  failed
                                                         Benjamini-Hochberg
                                                         correction (p=0.628);
                                                         net expectancy
                                                         -18.3434R at 248 bps is
                                                         not positive
…18 more rows, every one NOT VALIDATED

NO QUALIFYING SETUPS — nothing has passed the gate on this instrument. The live
companion will emit no signal cards today.
```

`n` is how many times that setup occurred in four years of history — 10,706 for
`doji_long`, so this is not a small-sample problem. `net exp` is the average result per
trade *after* costs, in R.

There are three reasons a setup can be disqualified, and a setup can collect more than
one. `doji_long` above has all three; `inside_bar_short` has two.

**1. `failed Benjamini-Hochberg correction (p=0.608)`** — the pattern is compared against
thousands of random entries taking the same stop, target and costs on the same symbol.
The p-value is the share of those random draws that did **at least as well as the
pattern did**. A conventional cutoff is 0.05. But 20 patterns were tested, and at 0.05
roughly one in twenty clears the bar on luck alone — so testing 20 things produces about
one false winner on average. Benjamini-Hochberg raises the bar to account for how many
tests were run. At p=0.608, `doji_long` is nowhere near either bar: coin-flip entries
matched or beat it 61% of the time.

The instructive case is `ma_pullback_long`, which came in at **p=0.027** — under the
naive 0.05 cutoff, and still rejected. That is the correction doing its job. One
apparent winner out of twenty tries is what chance looks like.

**2. `out-of-sample sign not held (in -0.0069R, out -0.0323R)`** — the four years are
split 70/30 by time. The pattern is measured on the first 70%, then checked on the last
30%, which it was never selected against. To pass, it must be **profitable in both
windows**. `doji_long` lost money in both, so it fails; `ma_pullback_long` made +0.0222R
in-sample and lost −0.0092R out-of-sample, which is the sign flip that is the clearest
evidence of overfitting available without going and getting new data. An in-sample edge
that goes negative out of sample is not a weaker edge, it is a different conclusion.

Note that `inside_bar_short` does **not** list this reason — it was positive in both
windows and genuinely passed this check. It still fails the other two.

**3. `net expectancy -18.2643R at 248 bps is not positive`** — this is the cost drag
arriving. Gross, `doji_long` is worth about −0.007R per trade; the fees turn that into
−18.26R. This reason appears on all twenty setups, and it would still appear on all
twenty if every one of them had a real edge, because none of the edges are within two
orders of magnitude of the fee.

### The journal, and why it's the point

Everything above is measurement. The journal is the part that closes the loop on *you*.

TradingView has no public API for reading your fills, so trades are logged by hand:

```bash
uv run tradedesk journal add \
  --symbol "BTC/USD" --direction long \
  --entry 63025.29 --stop 62980 \
  --setup vwap_reclaim_long \
  --thesis "reclaim of VWAP after the 15:05 flush" \
  --exit 63140 --exit-reason target
```

`--thesis` is mandatory, here and in `tradedesk size`. A position size computed from a
stop you cannot justify is a number that makes a bad trade feel rigorous.

`make journal` then reports on what you've logged. Before you log anything it says so
rather than printing an empty table:

```
no trades logged — `tradedesk journal add` to start
```

Once there are trades it prints three things: **rolling stats** (n, win rate,
expectancy, profit factor, max drawdown, risk of ruin), **behavioural flags** on
individual trades, and the report that matters most — *live vs backtest*, per setup,
with your live expectancy beside the backtested number and the **gap** between them.

That gap is the whole point. A setup can disappoint for two completely different
reasons, and they call for opposite responses: either the setup never worked (the
backtest was already negative, and you should stop taking it), or the setup measured
positive and your executed version of it did not — in which case the problem is entry
timing, stop placement, or discipline, and the fix is you rather than the setup. A
single blended P&L number cannot tell those apart. This one localises the problem.

### `make score` — grading your own reads

`make journal` scores the trades you took. **`make score` scores the trades you didn't**
— every predict-first read that `make live` recorded, replayed against what price
actually did. You don't have to place a single order for this to start teaching you
something, which makes it the fastest feedback loop in the tool.

Each read is replayed through the backtest's own rules: entry at the next bar's open,
your stated stop as the risk unit, a 2R target, a 48-bar horizon, and costs at your fee
tier. Using the same machinery is the point — "your expectancy" and "the backtested
expectancy" have to be the same measurement or comparing them is meaningless.

Here are the two reads from the session above, graded:

```
predict-first scorecard · 2 recorded · 2 graded · 0 pending
entry at the next bar's open, your stated stop, target 2R, 48-bar horizon, 248
bps round trip — the same rules the backtest uses

graded reads (most recent 20)
   bar (ET)  said  your stop  exit   gross      net  ok  your note
08-01 00:45  long  62,928.00  stop  -1.00R  -17.05R  ✓   price holding abo…
08-01 00:45  long  62,980.00  stop  -1.00R  -35.50R  ✓   reclaim of VWAP a…
ok = price was on your side at the 48-bar horizon. A read can be ✓ on a trade
that still stopped out — that gap is your stop, not your eye. gross is before
costs, net is after.

what your reads were worth
question                          answer  n
were you right about direction?  REFUSED  2  under the 30-prediction minimum — 2
                                             of 2 so far, which is not a rate
how far, in your own R?          REFUSED  2  n below the 30 minimum
would the trade have worked?     REFUSED  2  n below the 30 minimum
would it have made money?        REFUSED  2  n below the 30 minimum
```

Read the first row across and you get the whole argument of this project in one line.
**The direction call was right** — four hours later price was above the entry, so the
`ok` column is a tick. **The trade still lost**, because the stop at 62,928 was taken
before price came back. And **the loss was 17× worse than the trade itself**: −1.00R
became −17.05R once the 248 bps round trip was charged.

The second row is the same read with a tighter stop, and it is worse in exactly the way
the cost drag predicts. A stop 45 away instead of 97 makes 1R smaller, so the same fixed
fee is a bigger multiple of it: −35.50R instead of −17.05R. Being right about direction
did not save either one.

That is why the report is **three questions, never collapsed into a score**:

| the question | what it isolates |
|---|---|
| *were you right about direction?* | your read alone — measured at the horizon, ignoring your stop and ignoring costs |
| *would the trade have worked?* | your read **plus** your stop placement, gross |
| *would it have made money?* | all of that **plus** what your fee tier takes |

A single number cannot distinguish "you can't read the chart" from "you can read it fine
and your stop is too tight" from "both were fine and the fees ate it". Those need three
different responses, so they get three different rows.

**Everything says REFUSED here because n=2.** The same rule that governs every other
base rate in this tool applies to you: nothing under n=30 is shown as a rate, because a
2-from-2 hit rate is not evidence that you can read a chart. The individual reads are
still listed — those are observations, and an observation is not an inference. Keep
using `make live` and the rows accumulate until the gate opens.

#### Your hit rate against a coin flip

A hit rate on its own is uninterpretable, in exactly the way a pattern's win rate is
uninterpretable until it has been compared to random entries. So `score` runs the same
Monte Carlo the validator runs, with one thing randomised: **the direction**. Same bars,
same risk, same horizon, same costs — a thousand coin flips reading the bars you read.

Under n=30 it says so and stops:

```
vs random direction: not run. It needs 30 graded reads and you have 2. Until
then a hit rate has nothing to be compared against.
```

Past the gate you get the comparison. Below is that table on a **deliberately
uninformative sample** — the 40-read fixture from `tests/test_predictions.py`, which
alternates long and short on a series that only rises, so it is a coin flip by
construction:

```
vs random direction — 500 coin flips over the same 40 bars, same risk, same horizon
measure               you   random    random 95% band      p  reading
direction accuracy  50.0%    49.5%      35.0% … 62.5%  0.551  inside the noise band
horizon R         +0.000R  -0.030R  -0.900R … +0.750R  0.551  inside the noise band
gross R           +0.500R  +0.485R  +0.050R … +0.875R  0.551  inside the noise band
random pays the same 248 bps and nets -2.522R, so the p-value is computed on gross —
the costs are identical either way and cancel.

Your 50.0% is not evidence you can read this chart: a coin flip on the same bars
manages 49.5% and does at least as well as you 55% of the time.

Nothing here is distinguishable from reading these bars at random. That is the
finding — not a reason to look at a different statistic.
```

Look at the `gross R` row. **+0.500R per read looks like an edge.** The null says coin
flips on those same bars averaged +0.485R, and beat it 55% of the time. The number was
never a measure of skill; it was a measure of what those particular bars did, and the
baseline is what tells the two apart.

Three properties worth knowing:

- **The bars are held fixed, so nothing needs correcting for afterwards.** The pattern
  validator has to match its random entries to the pattern's time-of-day histogram,
  because a setup that fires in the first 90 minutes compared against randomness spread
  over the whole day is being compared against a different volatility regime. Here that
  problem cannot arise: the null reads the same bars you did.
- **It needs no independence assumption.** Two reads of the same bar — which is exactly
  what the real log above contains — are perfectly correlated, and averaging them as
  independent observations would understate the interval. But the null draws twice from
  that same bar too, so both samples carry identical correlation structure and the
  comparison stays like for like.
- **The p-value is computed on gross**, as it is in `validate`. Costs at a given bar are
  identical whichever way you read it, so net is gross shifted by a constant and the
  ordering — hence the p-value — is unchanged.

The verdict line at the bottom is gated on the null. `score` will not tell you your
reads worked on a gross number the baseline just placed inside the noise band, because
a report that contradicts the table printed directly above it is a report that tells
you what you want to hear.

Two more things it refuses to do:

- **A read whose horizon has not closed is `pending`, not scored.** Grading whatever
  bars happen to exist would score fast resolutions early and slow ones late, biasing
  the sample toward whatever resolves quickest. Run `make score` on a prediction you
  made ten minutes ago and it will tell you how many bars are left to run.
- **A read it cannot grade is listed with the reason**, never dropped — `stood aside`,
  `no stop stated`, a stop on the wrong side of the entry, or an entry bar that sat
  across a data gap. A scorecard that silently discards the reads it can't handle
  flatters you by construction.

### Why NO QUALIFYING SETUPS is the correct output

It is the answer, not an empty state.

Nothing in the pattern library has passed the gate on BTC, ETH or SOL, so the live
companion emits no signal cards. A tool that manufactured a card because the screen
would otherwise look bare is the exact failure this project exists to prevent — and
"there is no edge here" is a genuinely useful thing to know before you spend a year
finding it out at 248 bps a round trip.

The obvious worry is that "nothing qualifies" and "the feature is broken" produce
identical output. That is why three tests exist: one breaks each gate criterion in
isolation and asserts no card appears, one injects a synthetic *qualifying* setup and
asserts a card **is** produced, and one asserts that tightening the criteria invalidates
previously-stored passes instead of grandfathering them in. The emitting path
demonstrably works. It has nothing to emit.

If you want to see what the tool looks like when something does qualify, that second
test is `test_a_qualifying_setup_does_produce_a_card` in `tests/test_qualification.py`.
It builds a passing setup and asserts the full signal card renders.

### Every command

The three `make` targets above cover the daily routine. Underneath, each is a
`tradedesk` command you can call directly with more options:

| command | what it does |
|---|---|
| `tradedesk init` | create the store |
| `tradedesk fetch [--symbol --timeframe --until]` | backfill, idempotent and resumable |
| `tradedesk quality [--symbol --timeframe]` | data-quality verdict per series |
| `tradedesk levels --symbol X [--timeframe --at]` | every level, sorted by distance in ATR |
| `tradedesk validate --symbol X [--pattern --stop-atr --target-r --max-bars --draws --detail]` | backtest vs a random baseline; writes the qualification registry |
| `tradedesk validate ... --timeframe 4h\|1d --hold-across-sessions --risk-scale atr_daily` | swing horizon: derived bars, multi-day holds, stops in daily ATR |
| `tradedesk brief [--symbol --timeframe]` | pre-session brief |
| `tradedesk live --symbol X [--timeframe] [--no-predict]` | live companion, predict-first by default |
| `tradedesk size --entry --stop --thesis [--account --risk-pct]` | position size; refuses without a thesis |
| `tradedesk journal add --symbol --direction --entry --stop --thesis [...]` | log a trade |
| `tradedesk journal report [--symbol]` | rolling stats, behavioural flags, live vs backtest |
| `tradedesk journal score [--symbol --timeframe --target-r --max-bars --draws --all]` | grade your predict-first reads vs a random-direction null |
| `tradedesk status` | what the store holds |

**Stored vs derived timeframes.** The store holds 1m, 5m, 15m and 1h — what Coinbase
serves. `4h` and `1d` are built on read by aggregating 1h bars (`resample.py`), on UTC
boundaries, dropping any bucket missing a sub-bar rather than emitting one with the
wrong open. Nothing needs fetching to use them. Coinbase's own daily candles are
deliberately not used: they are UTC-anchored in a way that would disagree with the ET
session model everything else here is built on.

[`uv`](https://docs.astral.sh/uv/) is the only prerequisite; it pins its own Python 3.12.

Configuration is `config.toml`. Secrets live in `.env`, are read from the environment,
and are never printed.

---

## What it measures, and what it refuses to

Every number the tool prints carries `n` and a confidence interval, and several things
are refused outright rather than shown with a caveat:

| Guarantee | How it's enforced |
|---|---|
| No lookahead | `read_bars(as_of=…)` — `as_of` is keyword-only with **no default**, so you cannot read the future without stating your clock |
| No pattern fires across a data gap | Every detector declares a lookback `depth`; the engine applies the contiguity mask, so authors can't forget |
| No statistic is ever NaN or inf | `safe_div(…, when_zero=…)` is mandatory; `assert_total` blocks any frame containing NaN/inf on the way out |
| No base rate under n=30 | Refused entirely. Under n=100 it's labelled provisional |
| No chance findings | Benjamini-Hochberg across every pattern tested — 20 patterns at α=0.05 produces ~1 false positive by chance |
| No in-sample-only claims | 70/30 split by time, both windows reported side by side |
| Costs are never optional | Spread + slippage + fees on both sides, moving actual fill prices |

**What it does not do:** place orders, read your TradingView fills (there's no public API
— log trades manually or paste a CSV), predict prices, or tell you what to trade.

---

## The gate: when the tool is allowed to tell you to trade

This is the load-bearing rule of the whole system. A signal card is emitted **only** for
a setup that:

1. survived **Benjamini-Hochberg** correction across the family it was tested in, **and**
2. **held the sign** of its expectancy out of sample, **and**
3. has **positive net expectancy at your actual fee tier**, **and**
4. was measured on **n ≥ 100** trades.

**As of the current study, nothing qualifies.** So `tradedesk live` prints:

```
╭──────────────────── NO QUALIFYING SETUPS ────────────────────╮
│ No setup on this instrument has passed the gate.             │
│ No signal cards will be emitted. This is the measured        │
│ answer, not a missing feature.                               │
╰──────────────────────────────────────────────────────────────╯
```

That message is only meaningful if the code path that *does* emit a card demonstrably
works — otherwise "nothing qualifies" is indistinguishable from a bug. So there are two
complementary tests: one breaks each criterion in isolation and asserts no card appears,
and one injects a synthetic qualifying setup and asserts a card **is** produced. A third
asserts that tightening the criteria invalidates previously-stored passes rather than
grandfathering them in.

**Rejection is shown, not hidden.** The brief lists every tested setup with its
disqualifiers, because absence and rejection look identical to a reader and are not the
same thing:

```
doji_long   NOT VALIDATED   failed Benjamini-Hochberg (p=0.608); out-of-sample sign
                            not held (in −0.0069R, out −0.0323R); net −18.26R at
                            248 bps is not positive
```

**Patterns that trigger are still shown**, with their measured expectancy attached —
including negative ones, under a `NEGATIVE EXPECTANCY` banner. Hiding the setups that
tempt you removes exactly the information that would teach you what they cost.

**The cost drag is permanently on screen.** It moves with volatility: at the 10th ATR
percentile a 1×ATR(5m) stop costs ~64R per round trip, against ~19R at typical
volatility. Same fee, smaller risk, larger multiple.

Two smaller refusals in the same spirit: **position sizing requires a thesis** (a size
computed from a stop you can't justify is a number that makes a bad trade feel rigorous),
and **predict-first is on by default** — you're shown price and time only, and prompted
for your own read, before anything measured is revealed. `--no-predict` turns it off.

---

## Pointing it at a new instrument

The apparatus is instrument-agnostic. For another crypto pair on the same venue:

```bash
# 1. add the symbol to config.toml
#    [data]
#    symbols = ["BTC/USD", "ETH/USD", "SOL/USD", "LINK/USD"]

make fetch                                        # backfills only what's missing
make quality                                      # CHECK THIS FIRST — see below
uv run tradedesk validate --symbol LINK/USD --timeframe 5m
```

**Read the quality report before trusting any result.** It gives a `USABLE` /
`PROVISIONAL` / `BLOCKED` verdict per series. The gate that matters most is the
**no-trade bar rate**: on a thin instrument, half the bars can be missing, and a
"three-bar pattern" spanning a six-hour hole is an artifact. Measured examples from
Coinbase over one 12-hour window (145 possible 5m bars): BTC-USD 145, SOL-USD 145,
ALEPH-USD 143, ACS-USD 78, AERGO-USD 72. The last two are not research-grade.

For a **different venue**, implement the `Venue` protocol in `src/tradedesk/venues/base.py`
(one method, `fetch_ohlcv`). Two things to know before you do:

- **Public data access is not trading access.** Global OKX serves market data to US IPs
  but blocks US users from trading. Verify you can actually trade where you measure.
- **`SinceIgnoredError` exists for a reason.** Kraken returns HTTP 200 and looks healthy
  while silently ignoring the requested start date — a naive backfill fills the database
  with one recent week labelled as years of history. The guard is generic, not
  Kraken-specific.

For **equities or futures**, see the architecture notes at the end of
[FINDINGS.md](FINDINGS.md). Short version: the causality seam, level engine, detectors
and backtest are all venue-agnostic; the session model (currently the ET calendar day)
and the cost model (currently bps-of-notional, which futures aren't) are the real work.

---

## Adding a detector

A detector is a pure boolean expression over bar `t` and earlier. Add it to
`src/tradedesk/patterns/candles.py` or `structures.py`:

```python
from .base import pattern
import polars as pl

@pattern(name="my_setup", depth=3, direction="long", requires=("vwap",))
def _my_setup() -> pl.Expr:
    """Two lower closes, then a reclaim of VWAP."""
    return (
        (pl.col("close").shift(2) > pl.col("close").shift(1))
        & (pl.col("close") > pl.col("vwap"))
        & (pl.col("close").shift(1) <= pl.col("vwap").shift(1))
    )
```

Every argument is mandatory and each one buys you something:

- **`depth`** — bars of contiguous history needed (counting the signal bar). The engine
  refuses to fire the signal where those bars aren't genuinely consecutive in time. You
  never write masking code, so you can't forget it.
- **`direction`** — `"long"` or `"short"`. Registering a directionless pattern would make
  its expectancy meaningless, because the long and short cases cancel.
- **`requires`** — level columns the detector reads. The signal is suppressed wherever
  those are null, so a VWAP-keyed setup can't fire in a session whose VWAP was
  invalidated by a venue outage.

It's picked up automatically — no registration list to update:

```bash
uv run tradedesk validate --symbol BTC/USD --timeframe 5m --pattern my_setup --detail
```

**Write a known-answer test.** The convention is in `tests/test_patterns.py`: a hand-built
fixture where you can verify by eye that the detector finds *exactly* the bars it should
and no others. A detector that fires twice on a fixture containing one instance is not
measuring what it claims to.

---

## Reproducing the findings

| Finding | Command |
|---|---|
| No pattern beats random on BTC 5m | `uv run tradedesk validate --symbol BTC/USD --timeframe 5m` |
| Per-pattern detail with context slices | `... --pattern ma_pullback_long --detail` |
| Stop width × timeframe sweep (45 configs) | `uv run python experiments/sweep.py` |
| Maker fills with data-determined fills | `uv run python experiments/maker.py` |
| Context slices vs a context-matched null | `uv run python experiments/slices.py` |
| Does the surviving effect ever clear costs? | `uv run python experiments/final.py` |
| Full validation at low-fee venues | `uv run python experiments/lowfee.py` |
| ETH and SOL | `uv run python experiments/eth_sol.py` |
| No level is ever NaN/inf across 8.1M bars | `make levels-sweep` |

The `validate` output reads gross, net and drag side by side on purpose: **gross** answers
"does the pattern have edge", **net** answers "would it have made money", and **drag** is
what the fee tier costs. Collapsing them into one number hides both questions.

Verdicts are three-way, because "no edge" and "real edge that costs more than it makes"
call for different responses: `NO DEMONSTRATED EDGE`, `EDGE, BUT COSTS EXCEED IT`,
`TRADEABLE EDGE`.

---

## Why the design looks the way it does

Every decision below was forced by something measured, not chosen on taste. Details in
the module docstrings.

**Coinbase, not Binance.** From a US IP, Binance.com returns 451 and Bybit 403. Kraken
returns 200 but ignores `since`. Binance.US is reachable but 120–175× thinner and
synthesises flat zero-volume bars — backtesting there measures its gap-filling algorithm.

> Chart `COINBASE:BTCUSD` on TradingView. Measuring one venue while trading another means
> every base rate describes a different instrument.

**Storage is sparse; absence is data.** Coinbase publishes no candle when no trades
occurred, so rows `t-2, t-1, t` are not necessarily adjacent in time. Nothing is ever
forward-filled — a fabricated bar is a price that never traded.

**A separate coverage table is mandatory.** A missing bar is ambiguous between "no trades"
and "we never fetched this", so idempotency is impossible to derive from the bars alone.
Recording what was *requested* is the only way to tell them apart. Proven by SIGKILL:
interrupted at 24%, resumed, byte-identical content hash to an uninterrupted run, zero
redundant re-fetches.

**The forming bar is never stored.** Exchanges return the currently-open bucket, and
Coinbase lags publication ~140s. Storing it puts a mutable, wrong row in the database —
and it's exactly the bar a live signal would fire on.

**One position at a time.** 50–64% of pattern signals overlap. Bootstrapping 36,000
correlated trades as independent yields a CI near ±0.01R — tight, and false.

**Entry at the next bar's open, never the signal bar's close** — and if that next bar sits
after a gap, the signal is *skipped*, not filled at a stale price hours later.

**Timezones.** Stored UTC, displayed US/Eastern, sessions anchored to the ET calendar day.
Python's `zoneinfo` does **not** raise on a non-existent local time — `datetime(2025,3,9,2,30)`
silently yields answers an hour apart depending on `fold` — so all grids are built in UTC.
Verified against real bars: hour 01 ET appears +4 times over four years, hour 02 ET −4.

---

## What isn't built

All six phases are complete: the candle store, the causal level engine, the pattern
validator, the pre-session brief, the live companion, and the journal.

**Equities and futures are deferred.** `timeutil` already handles RTH 09:30–16:00 and
premarket 04:00–09:30, and the session model is versioned so redefining it is a migration
rather than a silent corruption — but the session grouping is currently the ET calendar
day, and the cost model is bps-of-notional, which futures aren't. See the architecture
notes in [FINDINGS.md](FINDINGS.md).

**TradingView fills are not read automatically.** There's no public API for it, and
scraping it is out of scope by design. Log trades with `tradedesk journal add`.

**Prediction scoring has no multiple-testing correction.** `tradedesk journal score`
compares your reads to a random-direction null, but unlike `validate` it does not
correct across a family — it tests one hypothesis, so there is no family yet. If
per-symbol or per-session-hour breakdowns are ever added, each slice is another test and
Benjamini-Hochberg has to arrive with them.

**Alerts are local only** — terminal bell and macOS notification centre. `notify.py` has
no HTTP client at all; not disabled, absent. A trading tool that phones home leaks your
positions, and that failure mode is silent.

A handful of tests remain `pytest.mark.skip` with named reasons rather than being stubbed
into passing — a test that asserts nothing but reports green claims a guarantee that
doesn't exist.
