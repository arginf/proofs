# tools/

Python 3.11+, standard library only, strict type hints. Tests: `python3 -m unittest discover -s tools/tests -t .` (PR CI runs them).

- `arginf.py`: hashing, scope checks, attempts, `index` (index.json, version 2).
- `readings.py`: pinned model + llama.cpp runner. `prepare()` returns an `Engine` (verified binary + weights); `complete()` runs one deterministic completion. `statement_lean()` is the shared view of a statement that every model prompt uses.
- `areas.py`: area labels. `AREAS` is the fixed list (arXiv codes). `parse_codes()` keeps only known codes, in reply order, capped at `MAX_AREAS`; `embed()` re-checks them, so nothing but fixed labels reaches the site. Published as `areas.json` on the `index` branch and as `index.areaList` plus `theorem.areas = {codes, model, modelSha256, runtime, cpu, promptSha256} | null`.
- Changing `AREAS` or `PROMPT` changes `promptSha256`, so CI relabels every statement on the next merge (one short completion per statement, but the prompt is reprocessed each time, so check run time before growing the catalog or list a lot).
- Model output is untrusted text: `raw` stays in areas.json for audit and is never embedded in index.json; the site renders only codes from `areaList`.
