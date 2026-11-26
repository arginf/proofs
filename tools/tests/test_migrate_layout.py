"""One-time move to the sharded layout with 20-digit hashes (scale design, test plan row 2).

User story: as a maintainer, the migration moves the statements and changes nothing else.
Statement bodies are identical apart from imports and namespace, attempt code is identical
apart from hash references, the attempt trees keep their shape, and an old-to-new map is
written for the site's redirects.
"""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools import arginf, layout, migrate_layout


def h12(material: str) -> str:
    return hashlib.sha256(material.encode()).hexdigest()[:12]


def legacy_statement(root: Path, prop: str, imports: str = "import Init") -> str:
    h = h12(prop)
    d = root / "Theorems" / h
    d.mkdir(parents=True)
    (d / "Statement.lean").write_text(
        f"{imports}\n\nnamespace Theorems.«{h}»\n\ndef statement : Prop :=\n  {prop}\n\n"
        f"end Theorems.«{h}»\n"
    )
    return h


def legacy_attempt(
    root: Path, h: str, code: str, parent: str | None = None, extra: str = ""
) -> str:
    material = "\n".join(([extra] if extra else []) + ["", code]).strip()
    a = h12(f"parent:{parent or ''}\n{' '.join(material.split())}")
    d = root / "Theorems" / h / "attempts" / a
    d.mkdir(parents=True)
    head = f"import Theorems.«{h}».Statement\n" + (f"{extra}\n" if extra else "")
    (d / "Attempt.lean").write_text(
        f"{head}\nnamespace Theorems.«{h}».Attempt_{a}\n\n{code}\n\n"
        f"end Theorems.«{h}».Attempt_{a}\n"
    )
    (d / "attempt.toml").write_text(f'parent = "{parent or ""}"\n')
    return a


def legacy_repo() -> tuple[Path, dict[str, str]]:
    root = Path(tempfile.mkdtemp())
    (root / "Theorems").mkdir()
    lemma = legacy_statement(root, "True")
    la = legacy_attempt(root, lemma, f"theorem proof : Theorems.«{lemma}».statement :=\n  trivial")
    nat = legacy_statement(root, "∀ n : Nat, n + 0 = n")
    p = legacy_attempt(
        root, nat, f"theorem proof : Theorems.«{nat}».statement := by\n  intro n\n  sorry"
    )
    c = legacy_attempt(
        root, nat, f"theorem proof : Theorems.«{nat}».statement := by\n  intro n\n  rfl", parent=p
    )
    # A statement whose proof uses the lemma's proof: a cross-statement import.
    uses = legacy_statement(root, "1 = 1")
    u = legacy_attempt(
        root,
        uses,
        f"theorem proof : Theorems.«{uses}».statement := by\n"
        f"  have := Theorems.«{lemma}».Attempt_{la}.proof\n  rfl",
        extra=f"import Theorems.«{lemma}».attempts.«{la}».Attempt",
    )
    unproved = legacy_statement(root, "2 = 2")
    return root, {
        "lemma": lemma,
        "la": la,
        "nat": nat,
        "p": p,
        "c": c,
        "uses": uses,
        "u": u,
        "unproved": unproved,
    }


def strip_ns(text: str) -> list[str]:
    return [
        ln
        for ln in text.splitlines()
        if ln.strip() and not ln.startswith(("import ", "namespace ", "end "))
    ]


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root, self.old = legacy_repo()
        self.before = {
            p.relative_to(self.root).as_posix(): p.read_text()
            for p in sorted((self.root / "Theorems").rglob("*"))
            if p.is_file()
        }
        self.map = migrate_layout.migrate(self.root)

    def test_map_covers_every_statement_and_attempt(self) -> None:
        o = self.old
        self.assertEqual(
            set(self.map["statements"]), {o["lemma"], o["nat"], o["uses"], o["unproved"]}
        )
        self.assertEqual(set(self.map["attempts"]), {o["la"], o["p"], o["c"], o["u"]})
        for new in [*self.map["statements"].values(), *self.map["attempts"].values()]:
            self.assertTrue(layout.HASH_RE.match(new), new)

    def test_statement_hashes_extend_the_old_ones(self) -> None:
        for old, new in self.map["statements"].items():
            self.assertTrue(new.startswith(old), (old, new))

    def test_only_the_new_layout_remains_and_passes_every_check(self) -> None:
        self.assertEqual(arginf.check_all(self.root), [])
        for p in (self.root / "Theorems").rglob("*"):
            if p.is_file():
                self.assertIsInstance(
                    layout.parse_path(p.relative_to(self.root).as_posix()), layout.Parsed
                )

    def test_statement_bodies_unchanged_apart_from_imports_and_namespace(self) -> None:
        for old, new in self.map["statements"].items():
            before = self.before[f"Theorems/{old}/Statement.lean"]
            after = (self.root / layout.statement_path(new)).read_text()
            self.assertEqual(strip_ns(before), strip_ns(after))
            self.assertEqual(arginf.extract_statement(before), arginf.extract_statement(after))
            self.assertIn(f"namespace Theorems.«{new}»", after)

    def test_attempt_code_unchanged_apart_from_hash_references(self) -> None:
        rename = {**self.map["statements"], **self.map["attempts"]}
        for (h_old, a_old), (h_new, a_new) in migrate_layout.attempt_pairs(self.map):
            before = self.before[f"Theorems/{h_old}/attempts/{a_old}/Attempt.lean"]
            for old, new in rename.items():
                before = before.replace(f"«{old}»", f"«{new}»").replace(
                    f"Attempt_{old}", f"Attempt_{new}"
                )
            after = (self.root / layout.attempt_path(h_new, a_new)).read_text()
            self.assertEqual(strip_ns(before), strip_ns(after), a_old)

    def test_attempt_trees_keep_their_shape(self) -> None:
        o, m = self.old, self.map
        by = {t["hash"]: t for t in arginf.build_index(self.root)["theorems"]}
        tree = {a["hash"]: a for a in by[m["statements"][o["nat"]]]["attempts"]}
        child, parent = m["attempts"][o["c"]], m["attempts"][o["p"]]
        self.assertEqual(tree[child]["parent"], parent)
        self.assertEqual(tree[parent]["children"], [child])
        self.assertTrue(tree[child]["complete"])
        self.assertEqual(tree[parent]["openGoals"], 1)
        self.assertEqual(
            by[m["statements"][o["uses"]]]["dependencies"], [m["statements"][o["lemma"]]]
        )
        self.assertEqual(by[m["statements"][o["unproved"]]]["status"], "open")
        self.assertEqual(by[m["statements"][o["lemma"]]]["status"], "proved")

    def test_map_file_for_site_redirects(self) -> None:
        out = self.root / "migrations" / "map.json"
        migrate_layout.write_map(self.map, out)
        data = json.loads(out.read_text())
        self.assertEqual(data["hashLength"], {"from": 12, "to": 20})
        self.assertEqual(data["statements"], self.map["statements"])
        self.assertEqual(data["attempts"], self.map["attempts"])
        self.assertEqual(
            data["attemptStatement"][self.map["attempts"][self.old["c"]]],
            self.map["statements"][self.old["nat"]],
        )

    def test_refuses_to_run_twice(self) -> None:
        with self.assertRaises(migrate_layout.MigrationError):
            migrate_layout.migrate(self.root)


class GuardTests(unittest.TestCase):
    def test_refuses_a_folder_whose_name_does_not_match_its_legacy_hash(self) -> None:
        root, o = legacy_repo()
        (root / "Theorems" / o["unproved"]).rename(root / "Theorems" / "000000000000")
        with self.assertRaises(migrate_layout.MigrationError):
            migrate_layout.migrate(root)

    def test_leaves_the_tree_untouched_when_it_refuses(self) -> None:
        root, o = legacy_repo()
        bad = root / "Theorems" / o["nat"] / "attempts" / o["c"] / "attempt.toml"
        bad.write_text('parent = "ffffffffffff"\n')
        before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
        with self.assertRaises(migrate_layout.MigrationError):
            migrate_layout.migrate(root)
        self.assertEqual(before, sorted(p.relative_to(root).as_posix() for p in root.rglob("*")))


if __name__ == "__main__":
    unittest.main()
