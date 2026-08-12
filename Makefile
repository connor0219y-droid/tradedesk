# tradedesk -- common commands
# `just` is not installed on this machine, so this is a Makefile. GNU Make 3.81 (macOS).

.PHONY: help setup test fetch fetch-5m quality status clean check levels levels-sweep validate brief live journal score

help:
	@echo "setup     install dependencies and create the store"
	@echo "test      run the test suite"
	@echo "fetch     backfill all configured symbols and timeframes (resumable)"
	@echo "fetch-5m  backfill just the 5m timeframe (fast first pass)"
	@echo "quality   print the data-quality report"
	@echo "levels    print the level table for a symbol/date"
	@echo "levels-sweep  assert no level is ever NaN or inf across the whole store"
	@echo "validate  backtest every pattern vs a random baseline"
	@echo "brief     pre-session brief: regime, levels, validated setups, cost drag"
	@echo "live      live companion (signal cards only for setups that passed the gate)"
	@echo "journal   trade journal report"
	@echo "score     grade your predict-first reads against what price did"
	@echo "status    show what the store currently holds"
	@echo "check     test + quality, the pre-flight before trusting a result"
	@echo "clean     delete the candle store (destructive)"

setup:
	uv sync
	uv run tradedesk init

test:
	uv run pytest -q

fetch:
	uv run tradedesk fetch

fetch-5m:
	uv run tradedesk fetch --timeframe 5m

quality:
	uv run tradedesk quality

status:
	uv run tradedesk status

levels:
	uv run tradedesk levels --symbol "BTC/USD" --timeframe 5m

levels-sweep:
	uv run pytest -m store -q -s

validate:
	uv run tradedesk validate --symbol "BTC/USD" --timeframe 5m

# The pre-registered published-strategy family (FINDINGS.md finding 8), exactly as it
# was run. Every parameter here is fixed by PREREGISTRATION.md -- the 4,000 draws in
# particular, so the p-value can resolve below the Benjamini-Hochberg threshold rather
# than being clipped by it. Changing any of it means the correction no longer describes
# the family that was declared.
validate-published:
	@for tf in 5m 4h 1d; do \
	  for sym in "BTC/USD" "ETH/USD" "SOL/USD"; do \
	    echo "=== $$sym $$tf ==="; \
	    uv run tradedesk validate --symbol "$$sym" --timeframe $$tf \
	      --family published --draws 4000; \
	  done; \
	done

brief:
	uv run tradedesk brief --timeframe 5m

live:
	uv run tradedesk live --symbol "BTC/USD" --timeframe 5m

journal:
	uv run tradedesk journal report

score:
	uv run tradedesk journal score

check: test levels-sweep quality

clean:
	rm -rf data/tradedesk.duckdb data/tradedesk.duckdb.wal
