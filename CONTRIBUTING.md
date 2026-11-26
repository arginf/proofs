# Contributing

By opening a pull request, you agree that your contribution is licensed under the [Apache License 2.0](LICENSE).

Everything happens through GitHub. There are no site accounts. You need `git`, `gh` (logged in) and Python 3.11+. To build locally, you also need [elan](https://github.com/leanprover/elan).

## Rules CI enforces
1. A PR changes exactly one folder `Theorems/<hash>/`: `Statement.lean`, `Proof.lean`, and at most one `attempts/<attempt>/` (`Attempt.lean`, `attempt.toml`).
2. Your branch is named `<hash>/<anything>`.
3. Folder and attempt names equal their hashes. A different statement is a different folder, and merged files never change.
4. Lean only: no comments, no string literals, no other files. `attempt.toml` holds only `parent`.
5. Complete proofs use only `propext`, `Classical.choice` and `Quot.sound`.

New statements and English readings need a maintainer's approval, because Lean can't tell whether a statement is the right one or a reading is faithful. Attempts and proofs merge once CI is green. `curation.toml` and anything outside `Theorems/` is maintainer-only.

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
When an attempt has no `sorry` left, promote it, in the same PR or a later one:
```bash
python3 tools/arginf.py promote "$H" "$A"        # writes Theorems/$H/Proof.lean
```

Statements that only import Lean core (`Init`, `Std`, `Lean`) are checked in seconds. Statements that import Mathlib or cslib-frontier use a prebuilt cache.

## Add an English reading (translation)
Statements stay Lean. A plain-English reading can sit beside one in `translations/<hash>.toml`, as long as it's labeled with the model that wrote it and links a **public shared chat** showing that model the exact Lean statement:
```toml
[[reading]]
model = "Model name and version"
chat = "https://claude.ai/share/…"          # or chatgpt.com/share, gemini.google.com/share, g.co/gemini/share, grok.com/share
text = "One plain paragraph: no links, markup or line breaks."
archive = "https://web.archive.org/web/…"   # optional snapshot of the chat
```
```bash
git switch -c "$H/reading" && $EDITOR "translations/$H.toml"
python3 tools/arginf.py check && git add translations && git commit -m "Reading for $H" && git push -u origin HEAD
gh pr create --repo arginf/proofs --fill
```
A maintainer opens the chat and confirms it contains the exact Lean of `Theorems/$H/Statement.lean` and that the text matches. The site shows readings as **unverified** and attributed to their model.

## Notes for agents
- `python3 tools/arginf.py index` prints the catalog as JSON: statements, status, attempt trees and dependencies. arginf.com renders the same data, published on the `index` branch.
- Keep one PR per folder and per attempt. CI fails anything broader.
- Explanations belong in the PR description, not in the files.
