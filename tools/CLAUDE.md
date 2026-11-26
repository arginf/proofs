# tools

- Layout (2026-11-27): `layout.py` owns every path and module name. Statements live at `Theorems/<ab>/<hash>/Statement.lean`, attempts at `Theorems/<ab>/<hash>/attempts/<cd>/<attempt>/{Attempt.lean,attempt.toml}`, where `<ab>`/`<cd>` are the first two hex digits and hashes are 20 digits (`HASH_LEN`). `parse_path` accepts exactly those two shapes (fixed depth 4 and 7, shard checked); `check_scope`, `automerge.decide` and `check_all` all go through it. Never build a `Theorems/...` path or module by hand.
- Module paths are sharded (`Theorems.«ab».«hash».Statement`); namespaces are not (`Theorems.«hash»`, `Theorems.«hash».Attempt_<a>`). `safety.py` checks theorem imports with `layout.THEOREM_MODULE_RE`, which ties the shard to the hash.
- `migrate_layout.py` did the one-time move from 12-digit flat folders and wrote `migrations/2026-11-hash20.json` (old→new statements and attempts, plus each attempt's statement). Automerge's earned-credit parser and provenance read that map, so old merge titles and original merge dates still count. Keep the map; the site uses it for redirects.
- Shard fill is random: one directory passes GitHub's 3,000 entries at about 700k items per level, not 768k.
- `arginf.py path <hash> [<attempt>]` prints a folder; CONTRIBUTING and pr.yml use it instead of shell substrings.
- Run `python3 -m unittest discover -s tools/tests -t .` and `python3 tools/arginf.py check-all`.
