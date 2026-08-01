# tradedesk -- common commands
# `just` is not installed on this machine, so this is a Makefile. GNU Make 3.81 (macOS).

.PHONY: help setup test fetch fetch-5m quality status clean check levels levels-sweep

help:
	@echo "setup     install dependencies and create the store"
	@echo "test      run the test suite"
	@echo "fetch     backfill all configured symbols and timeframes (resumable)"
	@echo "fetch-5m  backfill just the 5m timeframe (fast first pass)"
	@echo "quality   print the data-quality report"
	@echo "levels    print the level table for a symbol/date"
	@echo "levels-sweep  assert no level is ever NaN or inf across the whole store"
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

check: test levels-sweep quality

clean:
	rm -rf data/tradedesk.duckdb data/tradedesk.duckdb.wal
