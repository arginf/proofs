# Contributing

By opening a pull request, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), and to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Everything happens through GitHub. There are no site accounts. You need `git`, `gh` (logged in) and Python 3.11+. To build locally, you also need [elan](https://github.com/leanprover/elan).

## Rules CI enforces
1. A PR changes exactly one statement folder `Theorems/<ab>/<hash>/`: `Statement.lean`, or at most one `attempts/<cd>/<attempt>/` (`Attempt.lean`, `attempt.toml`). `<ab>` and `<cd>` are the first two digits of the hash; `arginf.py` writes the folders for you, and `arginf.py path <hash> [<attempt>]` prints them.
2. Your branch is named `<hash>/<anything>`.
3. Folder and attempt names equal their hashes (20 hex digits), in the shard their first two digits name. A different statement is a different folder, and merged files never change.
4. Lean only: no comments, no string literals, no other files. `attempt.toml` holds only `parent`.
5. Complete proofs use only `propext`, `Classical.choice` and `Quot.sound`, and pass a kernel re-check (`leanchecker`).
6. **Plain Lean only** (`tools/safety.py`). Nothing that runs code while Lean compiles or imports a file: no `#` commands (`#eval`, `#print`, …; write `# s` with a space for `Finset.card`), `run_cmd`/`run_tac`, `initialize`, `macro`/`elab`/`syntax`/`notation`, `simproc`, `axiom`, `opaque`, `unsafe`, `partial`, `attribute`, `native_decide`, no `` ` `` quotations, and no names under `IO`, `Lean.Elab`, `Lean.Meta` and similar. Attributes: only `simp`, `local`, `reducible`, `irreducible`, `instance`. Options: only `maxHeartbeats`, `maxRecDepth`, `synthInstance.maxHeartbeats` and `synthInstance.maxSize`, with caps. Imports: Lean core, Mathlib, CSLib, cslib-frontier, or other statements and attempts. Files up to 512 KiB, numerals up to 20 digits, no invisible or bidirectional Unicode characters. If a proof truly needs something else, open the PR anyway and explain why: a maintainer can merge it after review.
7. A `Statement.lean` is its imports, then `namespace Theorems.«<hash>»`, `def statement : Prop := …` and `end Theorems.«<hash>»`, with nothing else. The hash covers only the statement, so helper definitions are not allowed: put them in their own statement, or inline them.

**What merges how:**
- **A complete proof** (a new attempt with no `sorry`) of a statement that's already on `main` **merges automatically** once CI is green. No human is involved. The daily cap is earned: 10 a day to start, plus 5 for every statement someone else added that you proved first, up to 200.
- **A partial attempt** (with `sorry`) is a checkpoint, and it also **merges automatically** once CI is green (up to 20 per author a day), so others can continue from it right away. It must make progress, so CI rejects the bare `by sorry` template and unchanged copies of the parent.
- **Moderation:** before any automatic merge, the PR's title, description and identifier names are screened by an open-weights toxicity model and a private word list. If anything trips, the PR is labeled `needs-review` and waits for a maintainer instead.
- **A new statement** (a PR adding only `Theorems/<ab>/<hash>/Statement.lean`) **merges automatically** once CI is green, if it has no `sorry` and imports only Lean core, Mathlib, CSLib or cslib-frontier. Each author gets up to 5 a day; beyond that, a maintainer merges.
- **Daily caps** (5 statements, 20 checkpoints, and the earned proof cap) can be raised or lowered for one author by a maintainer in `limits.toml`. Ask in an issue if you regularly hit them.
- **New GitHub accounts** (younger than 7 days) always wait for a maintainer.
- **Everything outside `Theorems/`** (tooling, CI, `curation.toml`, `models.toml`) is maintainer-only.

## Add a statement
```bash
gh repo fork arginf/proofs --clone && cd proofs
H=$(python3 tools/arginf.py new --import Init "∀ n : Nat, n + 0 = n")   # default import: Mathlib
git switch -c "$H/statement" && git add "$(python3 tools/arginf.py path "$H")" && git commit -m "Add $H"
python3 tools/arginf.py check && git push -u origin HEAD   # checks against upstream/main
gh pr create --repo arginf/proofs --fill
```
In a fork clone, run `git fetch upstream` first so `check` compares against the current `main`.

## Work on a proof: attempts ("lines")
```bash
H=<statement hash>                                        # from arginf.com or Theorems/
A=$(python3 tools/arginf.py attempt "$H")                 # start fresh
# A=$(python3 tools/arginf.py attempt "$H" --from <attempt>)   # or continue any attempt
D=$(python3 tools/arginf.py path "$H" "$A")               # Theorems/<ab>/<hash>/attempts/<cd>/<attempt>
$EDITOR "$D/Attempt.lean"                                 # close goals; leave sorry where you stop
A=$(python3 tools/arginf.py seal "$D")                    # name the attempt by its hash
git switch -c "$H/$A" && git add "$(python3 tools/arginf.py path "$H" "$A")"
python3 tools/arginf.py check && git commit -m "Attempt $A on $H" && git push -u origin HEAD
gh pr create --repo arginf/proofs --fill
```
An attempt with no `sorry` left is a **proof**, and CI audits it on merge. A statement can collect any number of proofs: a long one, a short one, different approaches. Nothing else needs promoting.

Statements that only import Lean core (`Init`, `Std`, `Lean`) are checked in seconds. Statements that import Mathlib or cslib-frontier use a prebuilt cache.

## English readings
Don't write them. CI generates one per statement after it merges (see `models.toml` and `tools/readings.py`). The prompt is fixed (see `PROMPT` in `tools/readings.py`): it asks for the statement in the language of a research paper, as prose with inline LaTeX, followed by your `Statement.lean`'s imports and `def statement : Prop := …` in a code block. To see what the model will be asked:
```bash
python3 -c "from tools import readings; from pathlib import Path; print(readings.user_message(Path('.'), '$H'))"
```

## Notes for agents
- `python3 tools/arginf.py index` prints the catalog as JSON: statements, status, attempt trees and dependencies. arginf.com renders the same data, published on the `index` branch.
- Keep one PR per folder and per attempt. CI fails anything broader.
- Explanations belong in the PR description, not in the files.
