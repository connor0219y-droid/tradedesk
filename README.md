# tradedesk

A local intraday research companion that runs alongside TradingView. **It never places
orders.** TradingView stays the execution surface; this is the research surface.

The premise: TradingView shows you charts and gives you a paper broker, but tells you
nothing about whether the patterns you trade have any measurable edge *on the
instrument you trade them on*. Textbook pattern lore is mostly folklore until it has
been tested against a random entry with the same stop, target and costs, on the same
symbol and timeframe.

**Phase 1 (this release) is the data layer.** No signals, no patterns, no backtests
yet — those are phases 2–6. What exists now is the candle store they will all stand
on, and the causality contract that makes lookahead bias hard to write later.

---

## Setup

```bash
make setup        # uv sync + create the DuckDB store
make test         # full suite, no network required
make fetch        # backfill (resumable — safe to interrupt)
make quality      # the report that tells you whether to trust the data
make status       # what the store currently holds
```

Requires `uv`. It pins its own Python 3.12; the system Python 3.9 is not used.

Configuration lives in `config.toml`. Secrets live in `.env`, are read from the
environment, and are never printed — `redact()` in `config.py` reduces anything whose
name looks like a key or secret to `<set>` / `<unset>`.

---

## The one invariant

> A bar is identified by its **open** time, in UTC epoch milliseconds.
> Bar `t` covers the half-open interval `[t, t + timeframe)`.
> A bar may be read only once `t + timeframe + settle <= now`.

Everything else follows from this. Reads go through `frames.read_bars`, whose `as_of`
parameter is **keyword-only with no default** — you cannot accidentally read the
future, because you cannot call the function without stating what time you are
pretending it is.

```python
from tradedesk.frames import read_bars

frame = read_bars(con, "BTC/USD", "5m", as_of=some_datetime)
frame.contiguous_windows(3)   # only genuinely consecutive 3-bar windows
```

---

## Why the design looks the way it does

Each of these was measured, not assumed.

**Coinbase, not Binance.** From a US IP, Binance.com returns 451 and Bybit 403. Kraken
is the dangerous one: it answers HTTP 200 and looks perfectly healthy, but *ignores*
`since` — ask it for August 2024 and it hands back the most recent 720 bars. A
backfill loop written against it fills the database with one recent week labelled as
years of history, and every downstream check passes because the data is internally
consistent. `SinceIgnoredError` guards against this for *any* venue, not just Kraken.

> If you chart `BINANCE:BTCUSDT` on TradingView while this tool measures Coinbase
> bars, every base rate it reports describes a different instrument. Chart
> `COINBASE:BTCUSD`, `COINBASE:ETHUSD`, `COINBASE:SOLUSD`.

**Storage is sparse; absence is data.** Coinbase publishes no candle for an interval
with no ticks. On liquid majors that is rare, but on thin alts it is routine — in one
12-hour window, BTC-USD and SOL-USD returned all 145 possible 5m bars while AERGO-USD
returned 72 and ACS-USD 78. We never synthesise a row, because a forward-filled bar is
a price that never traded, and a backtest built on invented prices reports an edge you
cannot take.

The consequence is that rows `t-2, t-1, t` are **not necessarily adjacent in time**. A
three-bar detector handed such a triple is detecting an artifact. That is what
`BarFrame.window_mask(n)` is for.

**The coverage table is mandatory, not bookkeeping.** Because a missing bar is
ambiguous between *no trades occurred* and *we never fetched this*, idempotency is
impossible to derive from the bars table alone. A fetcher that infers its resume point
from missing timestamps re-fetches the same holes on every run and never converges.
Recording what was **requested**, separately from what came back, is the only way to
tell the two apart:

```
covered   + no bar  ->  ABSENT_NO_TRADES   (market information)
uncovered + no bar  ->  UNKNOWN            (we have not looked yet)
```

**Bars and coverage commit together.** A crash between them leaves coverage claiming
bars that were never stored, silently promoting `UNKNOWN` to `ABSENT_NO_TRADES`. Over
a ~7,000-request backfill, partial failure is the normal case.

Relatedly, when a response comes back full it may have been truncated by the venue's
300-row cap, so coverage is clamped to the last bar actually seen. Over-claiming there
is the most corrupting bug available in this design: it converts a fetch truncation
into a permanent, invisible "no trades occurred" assertion.

**The forming bar is never stored.** Exchanges return the currently-open bucket, and
Coinbase additionally lags publication by ~140s (measured). "Not the current bucket"
is therefore not a sufficient test for finality, hence `settle_seconds`. Storing a
forming bar puts a mutable, wrong row in the database permanently — and it is exactly
the bar a live signal would fire on.

**Causal and non-causal checks are physically separated.** `quality/checks.py` reads
only bar `t` and earlier, so anything in it may become a feature. `quality/offline.py`
reads bar `t+1` — correct for auditing stored history, catastrophic in a signal. The
boundary is an import, not a `causal=False` column, because a column gets ignored
during a refactor and an import does not. A test asserts nothing outside the quality
package imports it.

**Timezones.** Everything is stored in UTC and displayed in US/Eastern. Bars are
labelled by **ET calendar day** — the trader is US-based, and midnight ET always exists
and is never ambiguous because US transitions happen at 02:00. Two traps are guarded
explicitly:

- Python's `zoneinfo` does **not** raise on a non-existent local time.
  `datetime(2025,3,9,2,30, tzinfo=ET)` silently yields 07:30Z at `fold=0` and 06:30Z at
  `fold=1` — an hour apart, neither an error. `localize_strict()` refuses both.
- ET wall clock 01:00–01:55 occurs **twice** every November, so local time is not a
  usable key. All grids are built in UTC.

The 4-year BTC/USD 1h backfill confirms the arithmetic exactly: hour 01 ET appears
**+4** times versus a normal hour (four fall-back Novembers) and hour 02 ET **−4**
(four spring-forward Marches). The report's ET-hour sparkline is that check made
visual — a timezone bug shows up as a notch rather than as a silent offset.

---

## Reading the quality report

```
BTC/USD 5m  ·  420,835 expected  ·  420,589 present  ·  100.00% covered  ·  VERDICT: USABLE
    ! 6 venue-outage run(s) of >=30m, longest 385m -- downtime, not quiet markets
range               2022-08-01 00:00 → 2026-08-01 04:45 UTC
no-trade bars       237 (0.06% of covered grid)
longest absent run  77 bars
bars by ET hour     ▇█▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇▇  (00→23, DST canary)

from (UTC)        bars  duration
2023-03-04 17:00    55      275m
2023-05-19 07:45     8       40m
2024-05-31 22:20    11       55m
2024-10-26 16:10    13       65m
2025-10-25 15:15    69      345m
2026-05-08 01:20    77      385m
```

The verdict is on the first line. A report that buries "this data is not usable" under
three tables has failed at its only job.

`USABLE` / `PROVISIONAL` / `BLOCKED` come from coverage, absent-bar share and
ERROR-severity issues — thresholds in `config.toml`.

**Absent bars are not all alike, and the difference is measured in minutes.** An
isolated absent bar is a quiet market; a long contiguous run is venue downtime.
`outage_minutes` (default 30) is deliberately a *duration* rather than a bar count,
because a bar count cannot classify the same event consistently: three absent bars is
3 minutes at 1m — an ordinary quiet stretch, and SOL/USD has 3,434 of them — but 45
minutes at 15m, which is unambiguously downtime.

The 4-year backfill justifies the threshold rather than assuming it. Absent-run
durations fall into two clearly separated populations, with only 13 runs across all
nine series landing in the 15–30 minute band between them. And the ≥30m runs appear
**simultaneously across BTC, ETH and SOL with matching durations** — the same event
measured at 1m/5m/15m yields 391/385/375, 349/345/345, 277/275/270 minutes. Six
outages, three of the deepest USD books, identical windows: that is the exchange being
down, not three instruments independently going quiet.

This matters for Phase 3 because a pattern window spanning an outage is garbage.
`window_mask` already refuses those windows; the report is what tells you they existed.

---

## What is deliberately not here

Phases 2–6 are unbuilt: the level engine (VWAP, ATR, opening range, volume profile),
pattern detection and honest validation against a random baseline, the pre-session
brief, the live signal engine with predict-first mode, and the trade journal.

Five tests in `tests/test_deferred.py` are `skip`, not `xfail`, and not stubbed into
passing — a test that asserts nothing but reports green claims a guarantee that does
not exist. Each names the phase that will make it real.

Equities via Alpaca are deferred too, but the session model and the `Venue` protocol
already accommodate RTH 09:30–16:00 ET and premarket 04:00–09:30 ET.
