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

After testing 20 detectors across 3 symbols, 4 years, and ~50 configurations:

**No pattern in this library is profitable on BTC, ETH or SOL under any tested
combination of timeframe, stop width, target multiple, order type, or obtainable fee
tier.**

That is the tool working, not failing. The details are in **[FINDINGS.md](FINDINGS.md)**;
the three that matter most:

1. **Costs dominate by one to two orders of magnitude.** At Coinbase's base tier the
   round trip is 248 bps. A 1×ATR(5m) stop on BTC is 0.14% of price — so you pay roughly
   **19× your risk** in fees per trade. No plausible edge survives that.
2. **The edge and the cost never meet.** Widening the stop shrinks the cost drag, but it
   dilutes the edge *faster*: on 5m, gross edge falls 34× while drag falls 3.8×. The
   configurations cheap enough to trade are exactly the ones where the edge is gone.
3. **Lower fees don't rescue it.** The obtainable floor for a US retail account is ~40 bps
   (OKX US base, maker both sides), not the ~10 bps you might assume. Tested: still zero
   survivors.

A corollary worth internalising: **TradingView's paper broker is not charging you 248
bps.** The gap between paper results and live results on these setups exceeds any edge
plausibly present.

---

## Quickstart

```bash
make setup          # uv sync + create the DuckDB store
make fetch          # backfill (~85 min, resumable — safe to interrupt)
make quality        # is the data trustworthy?
make validate       # backtest every pattern vs a random baseline
make test           # 171 tests, no network required
```

Then, daily:

```bash
make brief          # before the session: regime, levels, validated setups, cost drag
make live           # during: where price sits, what triggered, any qualifying signals
make journal        # after: your live expectancy vs the backtested number
```

The full command list:

| command | what it does |
|---|---|
| `tradedesk init` | create the store |
| `tradedesk fetch [--symbol --timeframe --until]` | backfill, idempotent and resumable |
| `tradedesk quality [--symbol --timeframe]` | data-quality verdict per series |
| `tradedesk levels --symbol X [--timeframe --at]` | every level, sorted by distance in ATR |
| `tradedesk validate --symbol X [--pattern --stop-atr --target-r --draws --detail]` | backtest vs a random baseline; writes the qualification registry |
| `tradedesk brief [--symbol --timeframe]` | pre-session brief |
| `tradedesk live --symbol X [--timeframe] [--no-predict]` | live companion, predict-first by default |
| `tradedesk size --entry --stop --thesis [--account --risk-pct]` | position size; refuses without a thesis |
| `tradedesk journal add --symbol --direction --entry --stop --thesis [...]` | log a trade |
| `tradedesk journal report [--symbol]` | rolling stats, behavioural flags, live vs backtest |
| `tradedesk status` | what the store holds |

Requires [`uv`](https://docs.astral.sh/uv/). It pins its own Python 3.12.

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

**Alerts are local only** — terminal bell and macOS notification centre. `notify.py` has
no HTTP client at all; not disabled, absent. A trading tool that phones home leaks your
positions, and that failure mode is silent.

A handful of tests remain `pytest.mark.skip` with named reasons rather than being stubbed
into passing — a test that asserts nothing but reports green claims a guarantee that
doesn't exist.
