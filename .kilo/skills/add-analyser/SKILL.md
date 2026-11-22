---
name: add-analyser
description: "Add or modify an analysis-engine analyser in StockAnalysis. USE FOR: new indicator, new signal, analyser, composite setup, signal weights, Telegram signal formatting, confluence families. DO NOT USE FOR: paper-trading exit rules (use add-strategy), data feed producers (use options-data-contract)."---

# Adding an analyser

## Where things live

- Analysers: `services/analysis_engine/analyser/<Name>Analyser.py`, subclass
  `Analyser` (`analyser/Analyser.py`).
- Registration: `services/analysis_engine/main.py` — order matters; analyzers
  that read earlier results (PanicMode, OptionSellerComposite) go last.
- Weights: `common/constants.py` (analysis weight map + priority bands).
- Signal formatting: `services/analysis_engine/analyser/MessageFormatter.py`.
- Confluence families: `lib/intelligence/correlator.py` → `signal_family()`.
- Tests: `tests/analyser/` with fixtures from `tests/analyser/conftest.py`.

## Checklist

1. **Mode decorators** decide when it runs: `@index_intraday`,
   `@index_positional`, `@index_both`, or the stock equivalents. Methods are
   discovered with `dir()` — within a class they run alphabetically.
2. **Write into exactly one bucket**:
   `stock.analysis["BULLISH"|"BEARISH"|"NEUTRAL"][KEY] = {...}`.
   Only BULLISH/BEARISH reach the correlator and the score. NEUTRAL is for
   setups/gates/context (composites live here and use `PRIORITY_OVERRIDE`).
3. **Register the weight** for `KEY` in `common/constants.py`. Weights are
   looked up case-insensitively now, but the key must match what you write.
4. **If another analyser reads your signal, the key must be the emitted one.**
   This exact bug class shipped before: a composite checked
   `FUTURE_ACTION_LONG_BUILDUP`, which nothing emits. Read the producer's
   payload shape, not its conceptual name.
5. **Composite hygiene**: if your signal feeds a composite, decide explicitly
   whether it means "move direction" or just "activity". Require it to be
   reachable for indices (index spot has no volume — use futures volume).
6. **Formatter**: add a case in `MessageFormatter`. Formatters must accept
   dict payloads (composites emit `dict`), or the card renders
   "(format error)". Add a formatting test.
7. **Correlator family**: add the source to `signal_family()` so confluence
   counts distinct families (HIGH needs ≥2 layers and ≥2 families).
8. **Data**: read existing Redis state via the shared loader; do not add new
   fetches inside an analyser.
9. **Thresholds** are named constants with a comment stating why; DEBUG log
   the gate decision, INFO log the emitted signal.

## Tests to write

- Fires for a synthetic context that should trigger it (assert key, direction,
  strength).
- Does not fire for the inverse/insufficient context.
- Boundary cases (missing data, None fields, stale timestamps).
- If it feeds a composite: a test that the composite can actually fire.

Run: `PYTHONPATH=. .venv/bin/python -m pytest tests/analyser -q` then the full
suite before finishing.
