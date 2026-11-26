# Security

## Reporting a problem

Please report security problems privately through GitHub's [private vulnerability reporting](https://github.com/arginf/proofs/security/advisories/new), not in a public issue or PR. This includes any way to get a proof accepted that does not really prove its statement, to merge something outside the rules in [CONTRIBUTING.md](CONTRIBUTING.md), or to change what arginf.com shows without a merge.

## How a proof is trusted

1. **Scope** (`tools/arginf.py check`): a PR adds files to one `Theorems/<ab>/<hash>/` folder only, at a fixed depth and in the shard its hash names, and names match hashes.
2. **Plain Lean** (`tools/safety.py`): nothing in a theorem folder can run code while Lean compiles or imports it, extend the parser, add axioms, or change kernel options. This runs before any Lean is compiled, and `automerge` re-runs it on the PR's files before merging.
3. **Kernel re-check** (`leanchecker`, shipped with the toolchain): every declaration is replayed in the kernel, without running initializers.
4. **Axiom audit**: the proof has exactly the type `statement` and depends only on `propext`, `Classical.choice` and `Quot.sound`.
5. **main** runs all of the above again on every merge before the site is updated, and opens an issue if verification fails.

## Repository settings maintainers keep

- Actions: require approval to run workflows for all outside contributors; default `GITHUB_TOKEN` permission read-only; actions pinned by commit SHA.
- Rulesets: `main` allows no force pushes or deletions and requires the "PR checks" status and code-owner review for everything outside `Theorems/`; only GitHub Actions may push `index`.
- Private vulnerability reporting enabled.
