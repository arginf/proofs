"""Auto-merge: only a PR that adds one complete attempt to an existing statement qualifies."""

import unittest

from tools import automerge, layout

H = "0123456789abcdef0123"
A = "ba98765432100123abcd"
COMPLETE = (
    f"import {layout.statement_module(H)}\n\nnamespace X\n\n"
    f"theorem proof : Theorems.«{H}».statement :=\n  trivial\n\nend X\n"
)
PARTIAL = COMPLETE.replace("trivial", "by\n  sorry")


def files(*paths: str, status: str = "added") -> list[dict[str, str]]:
    return [{"filename": p, "status": status} for p in paths]


ATTEMPT = files(
    layout.attempt_path(H, A, "Attempt.lean"), layout.attempt_path(H, A, "attempt.toml")
)


STATEMENT = (
    f"import Mathlib\n\nnamespace Theorems.«{H}»\n\n"
    f"def statement : Prop :=\n  True\n\nend Theorems.«{H}»\n"
)


class _DecideBase(unittest.TestCase):
    def ok(
        self,
        fs: list[dict[str, str]],
        lean: str = COMPLETE,
        exists: bool = True,
        recent: int = 0,
        recent_checkpoints: int = 0,
        recent_proofs: int = 0,
        changed_files: int | None = None,
        account_age_days: float = 365.0,
    ) -> automerge.Decision:
        return automerge.decide(
            fs,
            statement_exists=lambda h: exists,
            attempt_lean=lambda path: lean,
            recent_statements=lambda: recent,
            recent_checkpoints=lambda: recent_checkpoints,
            recent_proofs=lambda: recent_proofs,
            changed_files=len(fs) if changed_files is None else changed_files,
            account_age_days=lambda: account_age_days,
        )


class DecideTests(_DecideBase):
    def test_complete_attempt_on_existing_statement_merges(self) -> None:
        d = self.ok(ATTEMPT)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual((d.theorem, d.attempt), (H, A))

    def test_partial_attempt_merges_as_a_checkpoint(self) -> None:
        d = self.ok(ATTEMPT, PARTIAL)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual(d.kind, "checkpoint")

    def test_checkpoint_daily_cap(self) -> None:
        d = self.ok(ATTEMPT, PARTIAL, recent_checkpoints=automerge.DAILY_CHECKPOINTS)
        self.assertFalse(d.merge)
        self.assertTrue(self.ok(ATTEMPT, COMPLETE, recent_checkpoints=99).merge)  # separate cap

    def test_complete_proof_daily_cap(self) -> None:
        self.assertFalse(self.ok(ATTEMPT, COMPLETE, recent_proofs=automerge.DAILY_PROOFS).merge)
        self.assertTrue(self.ok(ATTEMPT, COMPLETE, recent_proofs=automerge.DAILY_PROOFS - 1).merge)

    def test_sorry_in_comments_is_not_a_checkpoint_but_real_sorry_is(self) -> None:
        self.assertEqual(self.ok(ATTEMPT, COMPLETE + "-- sorry\n").kind, "proof")
        self.assertEqual(self.ok(ATTEMPT, COMPLETE.replace("trivial", "sorry")).kind, "checkpoint")

    def test_other_files_never_auto_merge(self) -> None:
        self.assertFalse(self.ok(ATTEMPT + files(".github/workflows/pr.yml")).merge)
        self.assertFalse(self.ok(ATTEMPT + files("curation.toml")).merge)
        self.assertFalse(self.ok(ATTEMPT + files(layout.statement_path(H))).merge)

    def test_only_additions_of_one_attempt(self) -> None:
        self.assertFalse(self.ok(files(*[f["filename"] for f in ATTEMPT], status="modified")).merge)
        other = files(
            layout.attempt_path(H, H, "Attempt.lean"), layout.attempt_path(H, H, "attempt.toml")
        )
        self.assertFalse(self.ok(ATTEMPT + other).merge)
        self.assertFalse(self.ok(files(layout.attempt_path(H, A, "Attempt.lean"))).merge)  # no toml

    def test_statement_must_already_exist_on_main(self) -> None:
        self.assertFalse(self.ok(ATTEMPT, exists=False).merge)

    def test_new_statement_merges_on_its_own(self) -> None:
        d = self.ok(files(layout.statement_path(H)), lean=STATEMENT, exists=False)
        self.assertTrue(d.merge, d.reason)
        self.assertEqual(d.kind, "statement")

    def test_statement_guardrails(self) -> None:
        stmt = files(layout.statement_path(H))
        self.assertFalse(self.ok(stmt, lean=STATEMENT.replace("True", "sorry"), exists=False).merge)
        bad_import = STATEMENT.replace("import Mathlib", "import Mathlib\nimport SomeOther.Lib")
        self.assertFalse(self.ok(stmt, lean=bad_import, exists=False).merge)
        for ok_import in (
            "Init",
            "Std",
            "Lean",
            "Mathlib.Data.Nat.Basic",
            "Cslib.Crypto",
            "CslibFrontier.X",
        ):
            lean = STATEMENT.replace("import Mathlib", f"import {ok_import}")
            self.assertTrue(self.ok(stmt, lean=lean, exists=False).merge, ok_import)
        self.assertFalse(
            self.ok(stmt, lean=STATEMENT, exists=False, recent=automerge.DAILY_STATEMENTS).merge
        )
        self.assertFalse(self.ok(stmt, lean=STATEMENT, exists=True).merge)  # already on main

    def test_statement_and_attempt_together_wait_for_a_maintainer(self) -> None:
        both = files(layout.statement_path(H)) + ATTEMPT
        self.assertFalse(self.ok(both, exists=False).merge)

    def test_path_tricks(self) -> None:
        bad = files(
            f"Theorems/{H[:2]}/{H}/attempts/{A[:2]}/{A}/../../../../../lakefile.toml",
            layout.attempt_path(H, A, "attempt.toml"),
        )
        self.assertFalse(self.ok(bad).merge)


class HardeningTests(_DecideBase):
    """Defense in depth: automerge re-checks what CI checked, and trusts only its own trigger."""

    def test_unsafe_lean_never_merges_even_if_ci_passed(self) -> None:
        for extra in ("#eval 1", "run_cmd pure ()", "set_option debug.skipKernelTC true in", "`x"):
            with self.subTest(extra=extra):
                lean = COMPLETE.replace("theorem proof", f"{extra}\ntheorem proof")
                self.assertFalse(self.ok(ATTEMPT, lean).merge)
                self.assertFalse(
                    self.ok(
                        ATTEMPT, PARTIAL.replace("theorem proof", f"{extra}\ntheorem proof")
                    ).merge
                )

    def test_statement_with_helper_never_merges(self) -> None:
        stmt = files(layout.statement_path(H))
        lean = STATEMENT.replace("def statement", "def helper : Prop := False\n\ndef statement")
        self.assertFalse(self.ok(stmt, lean=lean, exists=False).merge)

    def test_statement_namespace_must_match_its_folder(self) -> None:
        stmt = files(layout.statement_path(A))
        self.assertFalse(self.ok(stmt, lean=STATEMENT, exists=False).merge)

    def test_truncated_file_list_never_merges(self) -> None:
        self.assertFalse(self.ok(ATTEMPT, changed_files=3).merge)
        self.assertFalse(self.ok(ATTEMPT, changed_files=0).merge)

    def test_new_accounts_wait_for_a_maintainer(self) -> None:
        young = automerge.MIN_ACCOUNT_AGE_DAYS - 0.5
        self.assertFalse(self.ok(ATTEMPT, account_age_days=young).merge)
        stmt = files(layout.statement_path(H))
        self.assertFalse(self.ok(stmt, lean=STATEMENT, exists=False, account_age_days=young).merge)

    def test_only_our_own_pr_workflow_counts(self) -> None:
        self.assertTrue(automerge.trusted_run(".github/workflows/pr.yml", "pull_request"))
        for path, event in (
            (".github/workflows/other.yml", "pull_request"),
            (".github/workflows/pr.yml", "push"),
            (".github/workflows/pr.yml@refs/heads/x", "pull_request"),
            ("", "pull_request"),
        ):
            with self.subTest(path=path, event=event):
                self.assertFalse(automerge.trusted_run(path, event))

    def test_pages_are_all_fetched(self) -> None:
        pages = {1: list(range(100)), 2: list(range(100, 200)), 3: [200]}
        seen: list[int] = []

        def fetch(page: int) -> list[int]:
            seen.append(page)
            return pages.get(page, [])

        self.assertEqual(automerge.all_pages(fetch), list(range(201)))
        self.assertEqual(seen, [1, 2, 3])

    def test_pages_have_a_ceiling(self) -> None:
        with self.assertRaises(RuntimeError):
            automerge.all_pages(lambda page: list(range(100)), max_pages=5)


class QueueTests(unittest.TestCase):
    """Merges go through GitHub's merge queue; the kind label is the record."""

    def test_enqueue_labels_then_enqueues_at_the_checked_commit(self) -> None:
        calls: list[tuple[str, str, object]] = []

        def api(method: str, url: str, body: object = None) -> object:
            calls.append((method, url, body))
            return {"data": {"enqueuePullRequest": {"mergeQueueEntry": {"position": 1}}}}

        automerge.enqueue(api, "https://api.github.com/repos/o/r", 7, "PR_node", "c" * 40, "proof")
        self.assertEqual(calls[0][:2], ("POST", "https://api.github.com/repos/o/r/issues/7/labels"))
        self.assertEqual(calls[0][2], {"labels": ["arginf:proof"]})
        method, url, body = calls[1]
        self.assertEqual((method, url), ("POST", "https://api.github.com/graphql"))
        assert isinstance(body, dict)
        self.assertIn("enqueuePullRequest", body["query"])
        self.assertEqual(body["variables"], {"id": "PR_node", "sha": "c" * 40})
        self.assertEqual(len(calls), 2)

    def test_enqueue_fails_loudly_on_graphql_errors(self) -> None:
        def api(method: str, url: str, body: object = None) -> object:
            return {"errors": [{"message": "nope"}]} if url.endswith("/graphql") else None

        with self.assertRaises(RuntimeError):
            automerge.enqueue(api, "https://api.github.com/repos/o/r", 7, "n", "c" * 40, "proof")

    def test_recent_counts_ledger_gap_and_queued(self) -> None:
        from tools import ledger

        def en(pr: int, who: str, kind: str, t: str) -> ledger.Entry:
            return ledger.Entry(pr, who, kind, H, A, t)

        ledger_entries = [
            en(1, "bob", "proof", "2026-11-27T01:00:00Z"),
            en(2, "bob", "proof", "2026-11-25T01:00:00Z"),
        ]  # too old
        gap = [
            en(1, "bob", "proof", "2026-11-27T01:00:00Z"),  # also in the ledger: once
            en(3, "bob", "proof", "2026-11-27T02:00:00Z"),
            en(4, "amy", "proof", "2026-11-27T02:00:00Z"),
        ]
        open_prs = [
            {"number": 5, "user": {"login": "Bob"}, "labels": [{"name": "arginf:proof"}]},
            {"number": 6, "user": {"login": "bob"}, "labels": [{"name": "arginf:checkpoint"}]},
            {"number": 7, "user": {"login": "bob"}, "labels": []},
            {"number": 8, "user": {"login": "amy"}, "labels": [{"name": "arginf:proof"}]},
        ]
        since = "2026-11-26T03:00:00Z"
        self.assertEqual(
            automerge.recent_count(ledger_entries, gap, open_prs, "bob", "proof", since), 3
        )
        self.assertEqual(
            automerge.recent_count(ledger_entries, gap, open_prs, "bob", "checkpoint", since), 1
        )

    def test_ledger_artifact(self) -> None:
        import io
        import zipfile

        from tools import ledger

        entries = [ledger.Entry(1, "al", "statement", "1" * 20, None, "2026-11-27T01:00:00Z")]
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("ledger.json", ledger.dumps(entries))
        responses = {
            "runs": {"workflow_runs": [{"id": 9, "conclusion": "success", "head_branch": "main"}]},
            "artifacts": {
                "artifacts": [
                    {"name": "ledger", "expired": False, "archive_download_url": "https://x/zip"}
                ]
            },
        }

        def get_json(url: str) -> object:
            return responses["runs"] if "/runs?" in url else responses["artifacts"]

        got, started = ledger.read_ledger(
            get_json, lambda url: buf.getvalue(), "https://api.github.com/repos/o/r", "main.yml"
        )
        self.assertEqual(got, entries)
        self.assertIsNone(started)  # this fake run has no timestamps
        responses["artifacts"] = {"artifacts": []}
        with self.assertRaises(ledger.LedgerError):
            ledger.read_ledger(
                get_json, lambda url: buf.getvalue(), "https://api.github.com/repos/o/r", "main.yml"
            )
        responses["runs"] = {"workflow_runs": []}
        with self.assertRaises(ledger.LedgerError):
            ledger.read_ledger(
                get_json, lambda url: b"", "https://api.github.com/repos/o/r", "main.yml"
            )


class StatusTests(unittest.TestCase):
    def test_stage_for_each_workflow_event(self) -> None:
        self.assertEqual(automerge.status_stage("requested", None), "checks")
        self.assertEqual(automerge.status_stage("in_progress", None), None)
        self.assertEqual(automerge.status_stage("completed", "failure"), "failed")
        self.assertEqual(automerge.status_stage("completed", "timed_out"), "failed")
        self.assertIsNone(automerge.status_stage("completed", "success"))  # the merge job's
        self.assertIsNone(automerge.status_stage("completed", "cancelled"))  # superseded push
        self.assertIsNone(automerge.status_stage("completed", "skipped"))

    def test_only_theorem_prs_get_status_comments(self) -> None:
        self.assertTrue(automerge.eligible([f["filename"] for f in ATTEMPT]))
        self.assertTrue(automerge.eligible([f"Theorems/{H[:2]}/{H}/Statement.lean"]))
        self.assertFalse(automerge.eligible([]))
        self.assertFalse(automerge.eligible([f["filename"] for f in ATTEMPT] + ["README.md"]))
        self.assertFalse(automerge.eligible([".github/workflows/pr.yml"]))


if __name__ == "__main__":
    unittest.main()
