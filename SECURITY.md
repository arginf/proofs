# Security

## Reporting a problem

Please report security problems privately through GitHub's [private vulnerability reporting](https://github.com/arginf/proofs/security/advisories/new), not in a public issue or PR. This includes any way to get a proof accepted that does not really prove its statement, to merge something outside the rules in [CONTRIBUTING.md](CONTRIBUTING.md), or to change what arginf.com shows without a merge.

## How a proof is trusted

1. **Scope** (`tools/arginf.py check`): a PR adds files to one `Theorems/<ab>/<hash>/` folder only, at a fixed depth and in the shard its hash names, and names match hashes.
2. **Plain Lean** (`tools/safety.py`): nothing in a theorem folder can run code while Lean compiles or imports it, extend the parser, add axioms, or change kernel options. This runs before any Lean is compiled, and `automerge` re-runs it on the PR's files before merging.
3. **Kernel re-check** (`leanchecker`, shipped with the toolchain): every declaration is replayed in the kernel, without running initializers.
4. **Axiom audit**: the proof has exactly the type `statement` and depends only on `propext`, `Classical.choice` and `Quot.sound`.
5. **Merge queue**: automerge never merges directly. It enqueues the exact commit CI checked, and the queue re-runs the checks on that commit combined with `main` before merging.
6. **main** runs all of the above again on every merge before the site is updated, and opens an issue if verification fails.

## Who merged what

Daily caps and earned credit come from the ledger (`tools/ledger.py`), which main builds from GitHub's own records: each automatically merged PR's `arginf:<kind>` label, its author and its merge time. Commit authors and commit titles are never trusted for this. Automerge reads the ledger from main's latest artifact through the Actions API, never from the public copy.

## Repository settings maintainers keep

- Actions: require approval to run workflows for all outside contributors; default `GITHUB_TOKEN` permission read-only; actions pinned by commit SHA.
- Rulesets: `main` allows no force pushes or deletions, requires the merge queue (merge method: merge; "only merge non-failing PRs"), the "PR checks" jobs `scope` and `lean`, and code-owner review for everything outside `Theorems/`; only GitHub Actions may push `index`.
- Labels `arginf:statement`, `arginf:checkpoint` and `arginf:proof` are applied only by automerge. Maintainers don't add them by hand: they are the ledger's record.
- The arginf GitHub App (variable `ARGINF_APP_ID`, secret `ARGINF_APP_KEY`) is installed on this repository only, with contents and pull requests write and actions read. Automerge enqueues with its token, because GitHub doesn't start the merge queue's checks for events caused by `GITHUB_TOKEN`.
- Private vulnerability reporting enabled.
