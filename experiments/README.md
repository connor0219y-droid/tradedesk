# Experiments

One-off research scripts behind FINDINGS.md. Kept because the results cost ~30 minutes
of compute and the reasoning is only checkable if the code is readable.

- `sweep.py`   Q1: timeframe x stop width x target, net expectancy per cell
- `maker.py`   Q2: resting limit entries with fills determined by the data, not assumed
- `slices.py`  Q3: context slices vs a context-matched null, with BH correction
- `final.py`   the decisive test: does the one surviving effect clear costs anywhere

Run with `uv run python experiments/<name>.py`. All read-only against the store.
