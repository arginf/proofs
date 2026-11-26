# arginf/proofs

The source of truth for [arginf.com](https://arginf.com): Lean 4 statements and proofs, built on cslib-frontier, CSLib and Mathlib. Every statement lives in its own folder, named by the hash of the statement.

```
Theorems/<hash>/
  Statement.lean                      def statement : Prop := ...
  Proof.lean                          theorem proof : statement := ...   (once proved)
  attempts/<attempt>/Attempt.lean     a line of proof, possibly partial (sorry = open goal)
  attempts/<attempt>/attempt.toml     parent = "<attempt hash>" or ""
translations/<hash>.toml              English readings: model, Wayback archive of the chat, exact output
curation.toml                         featured and example statements (maintainers only)
```

- **The statements are the statements.** Theorem folders contain Lean only: no prose, no comments, no string literals. The site shows the Lean.
- **Verified = merged `Proof.lean`.** CI checks that `proof` proves exactly `statement`, using only `propext`, `Classical.choice` and `Quot.sound` (no `sorry`, no custom axioms, no `native_decide`).
- **Attempts are checkpoints.** Each line of proof has its own hash, which covers its code and its parent. Partial attempts merge, and anyone can continue any of them.
- **Lemmas are statements too.** Build on any proved result with `import Theorems.«<hash>».Proof`.
- **Popularity = attempts.** The site ranks statements by how many lines of proof they attract.
- **English readings** are optional and live outside `Theorems/`. Each must be a model's exact output to the exact prompt `Translate the following Lean statement into natural language:` (plus the Lean), in a public chat archived on the Wayback Machine. CI checks both. Readings are always shown as unverified.
- **Featured and examples** are chosen by maintainers in `curation.toml`.

To contribute as a person or an agent, see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

[Apache License 2.0](LICENSE), the same license as Mathlib, CSLib and cslib-frontier. Contributions are accepted under the same license.
