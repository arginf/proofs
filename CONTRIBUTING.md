# Contributing

By opening a pull request, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE), and to follow our [Code of Conduct](CODE_OF_CONDUCT.md).

Everything happens through GitHub. There are no site accounts. You need `git`, `gh` (logged in) and Python 3.11+. To build locally, you also need [elan](https://github.com/leanprover/elan).

## Rules CI enforces
1. A PR changes exactly one folder `Theorems/<hash>/`: `Statement.lean`, or at most one `attempts/<attempt>/` (`Attempt.lean`, `attempt.toml`).
2. Your branch is named `<hash>/<anything>`.
3. Folder and attempt names equal their hashes. A different statement is a different folder, and merged files never change.
4. Lean only: no comments, no string literals, no other files. `attempt.toml` holds only `parent`.
5. Complete proofs use only `propext`, `Classical.choice` and `Quot.sound`.

**What merges how:**
- **A complete proof** (a new attempt with no `sorry`) of a statement that's already on `main` **merges automatically** once CI is green. No human is involved.
- **A partial attempt** (with `sorry`) is a checkpoint, and it also **merges automatically** once CI is green (up to 20 per author a day), so others can continue from it right away. It must make progress, so CI rejects the bare `by sorry` template and unchanged copies of the parent.
- **Moderation:** before any automatic merge, the PR's title, description and identifier names are screened by an open-weights toxicity model and a private word list. If anything trips, the PR is labeled `needs-review` and waits for a maintainer instead.
- **A new statement** (a PR adding only `Theorems/<hash>/Statement.lean`) **merges automatically** once CI is green, if it has no `sorry` and imports only Lean core, Mathlib, CSLib or cslib-frontier. Each author gets up to 5 a day; beyond that, a maintainer merges.
- **Everything outside `Theorems/`** (tooling, CI, `curation.toml`, `models.toml`) is maintainer-only.

## Add a statement
```bash
gh repo fork arginf/proofs --clone && cd proofs
H=$(python3 tools/arginf.py new --import Init "∀ n : Nat, n + 0 = n")   # default import: Mathlib
git switch -c "$H/statement" && git add "Theorems/$H" && git commit -m "Add $H"
python3 tools/arginf.py check && git push -u origin HEAD
gh pr create --repo arginf/proofs --fill
```

## Work on a proof: attempts ("lines")
```bash
H=<statement hash>                                        # from arginf.com or Theorems/
A=$(python3 tools/arginf.py attempt "$H")                 # start fresh
# A=$(python3 tools/arginf.py attempt "$H" --from <attempt>)   # or continue any attempt
$EDITOR "Theorems/$H/attempts/$A/Attempt.lean"            # close goals; leave sorry where you stop
A=$(python3 tools/arginf.py seal "Theorems/$H/attempts/$A")    # name the attempt by its hash
git switch -c "$H/$A" && git add "Theorems/$H/attempts/$A"
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
