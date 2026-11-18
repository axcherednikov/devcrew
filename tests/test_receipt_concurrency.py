import json
import multiprocessing
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_worktree
from test_phase_candidate_commit import delayed_lifecycle_resolve


def receipt_writer(path, snapshot, first, gates, results):
    paused, attempted, release, finished = gates
    write_json = devcrew_worktree._write_json
    updated = dict(snapshot)
    if first:
        updated["last_known_clean"] = False

        def paused_write(*args, **kwargs):
            paused.set()
            if not release.wait(timeout=15):
                raise TimeoutError("first receipt writer was not released")
            return write_json(*args, **kwargs)

        devcrew_worktree._write_json = paused_write
    else:
        updated["writer_dispatched_since_snapshot"] = True
        if not paused.wait(timeout=15):
            raise TimeoutError("first receipt writer did not compare its snapshot")
        attempted.set()
    try:
        devcrew_worktree._atomic_json(path, updated, expected=snapshot)
        results.put({"first": first, "outcome": "SUCCESS"})
    except ValueError as exc:
        results.put({"first": first, "outcome": str(exc)})
    finally:
        devcrew_worktree._write_json = write_json
        if not first:
            finished.set()


class ReceiptConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = pathlib.Path(self.temp.name).resolve() / "repo"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.project / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "fixture")
        self.receipt = devcrew_worktree.create(self.project, "T01")
        self.path = devcrew_worktree.location(self.project, "T01")[-1]

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.project), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    def test_delayed_resolve_cannot_undo_writer_dispatch_or_nonce(self):
        (self.project / "tracked.txt").write_text("writer changes\n")
        ctx = multiprocessing.get_context("spawn")
        paused, resume, results = ctx.Event(), ctx.Event(), ctx.Queue()
        resolver = ctx.Process(
            target=delayed_lifecycle_resolve, args=(self.project, paused, resume, results)
        )
        try:
            resolver.start()
            self.assertTrue(paused.wait(timeout=15))
            devcrew_worktree.validate_dispatch(
                self.project, "T01", str(self.project), self.receipt["expected_head"], self.receipt
            )
            dispatched = json.loads(self.path.read_text())
            self.assertTrue(dispatched["writer_dispatched_since_snapshot"])
            self.assertFalse(dispatched["last_known_clean"])
            self.assertNotEqual(dispatched["receipt_nonce"], self.receipt["receipt_nonce"])
            resume.set()
            result = results.get(timeout=15)
            resolver.join(timeout=15)
            self.assertEqual(resolver.exitcode, 0)
            self.assertTrue(result["outcome"].startswith("TASK_WORKTREE_STATE_CONFLICT"), result)
            self.assertEqual(json.loads(self.path.read_text()), dispatched)
            with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"):
                devcrew_worktree.validate_dispatch(
                    self.project,
                    "T01",
                    str(self.project),
                    self.receipt["expected_head"],
                    self.receipt,
                )
        finally:
            resume.set()
            if resolver.is_alive():
                resolver.terminate()
                resolver.join(timeout=5)
            results.close()
            results.join_thread()

    def test_delayed_resolve_rejects_false_true_false_receipt_aba(self):
        self.assert_resolve_aba_conflict(legacy=False)

    def test_legacy_receipt_aba_is_rejected_after_upgrade(self):
        self.assert_resolve_aba_conflict(legacy=True)

    def assert_resolve_aba_conflict(self, *, legacy):
        tracked = self.project / "tracked.txt"
        tracked.write_text("initial dirty state\n")
        devcrew_worktree.resolve(self.project, "T01")
        initial = json.loads(self.path.read_text())
        if legacy:
            initial.pop("receipt_revision")
            self.path.write_text(json.dumps(initial))
        self.assertFalse(initial["last_known_clean"])
        # The delayed resolver reads false, observes a clean checkout and prepares
        # true. Another process completes false -> true -> false before its save.
        tracked.write_text("base\n")
        ctx = multiprocessing.get_context("spawn")
        paused, resume, results = ctx.Event(), ctx.Event(), ctx.Queue()
        resolver = ctx.Process(
            target=delayed_lifecycle_resolve, args=(self.project, paused, resume, results)
        )
        try:
            resolver.start()
            self.assertTrue(paused.wait(timeout=15))
            devcrew_worktree.resolve(self.project, "T01")
            clean = json.loads(self.path.read_text())
            self.assertTrue(clean["last_known_clean"])
            tracked.write_text("dirty again\n")
            devcrew_worktree.resolve(self.project, "T01")
            restored = json.loads(self.path.read_text())
            self.assertFalse(restored["last_known_clean"])
            initial_payload, restored_payload = dict(initial), dict(restored)
            initial_payload.pop("receipt_revision", None)
            restored_payload.pop("receipt_revision", None)
            self.assertEqual(initial_payload, restored_payload)
            self.assertEqual(clean["receipt_nonce"], initial["receipt_nonce"])
            resume.set()
            result = results.get(timeout=15)
            resolver.join(timeout=15)
            self.assertEqual(resolver.exitcode, 0)
            self.assertTrue(result["outcome"].startswith("TASK_WORKTREE_STATE_CONFLICT"), result)
            self.assertEqual(json.loads(self.path.read_text()), restored)
            self.assertEqual(restored["receipt_revision"], initial.get("receipt_revision", 0) + 2)
        finally:
            resume.set()
            if resolver.is_alive():
                resolver.terminate()
                resolver.join(timeout=5)
            results.close()
            results.join_thread()

    def test_snapshot_comparison_and_replace_share_one_process_lock(self):
        ctx = multiprocessing.get_context("spawn")
        gates = tuple(ctx.Event() for _ in range(4))
        paused, attempted, release, finished = gates
        results = ctx.Queue()
        snapshot = json.loads(self.path.read_text())
        processes = [
            ctx.Process(target=receipt_writer, args=(self.path, snapshot, first, gates, results))
            for first in (True, False)
        ]
        try:
            for process in processes:
                process.start()
            self.assertTrue(paused.wait(timeout=15))
            self.assertTrue(attempted.wait(timeout=15))
            self.assertFalse(finished.wait(timeout=0.2), "receipt writer escaped the held lock")
            release.set()
            outcomes = {
                result["first"]: result["outcome"]
                for result in (results.get(timeout=15), results.get(timeout=15))
            }
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            self.assertEqual(outcomes[True], "SUCCESS")
            self.assertTrue(outcomes[False].startswith("TASK_WORKTREE_STATE_CONFLICT"), outcomes)
            self.assertEqual(
                json.loads(self.path.read_text()),
                dict(
                    snapshot,
                    last_known_clean=False,
                    receipt_revision=snapshot["receipt_revision"] + 1,
                ),
            )
        finally:
            release.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
            results.join_thread()

    def test_creation_cannot_replace_an_existing_receipt(self):
        snapshot = json.loads(self.path.read_text())
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_STATE_CONFLICT"):
            devcrew_worktree._atomic_json(
                self.path, dict(snapshot, expected_head="a" * 40), expected=None
            )
        self.assertEqual(json.loads(self.path.read_text()), snapshot)

    def test_legacy_read_only_resolve_preserves_json_and_first_write_adds_revision(self):
        legacy = json.loads(self.path.read_text())
        legacy.pop("receipt_revision")
        self.path.write_text(json.dumps(legacy))
        original = self.path.read_bytes()
        read_only = devcrew_worktree.resolve(self.project, "T01", read_only=True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(read_only["receipt_nonce"], legacy["receipt_nonce"])
        fresh = devcrew_worktree.resolve(self.project, "T01")
        stored = json.loads(self.path.read_text())
        self.assertEqual(stored, dict(legacy, receipt_revision=1))
        self.assertEqual(fresh["receipt_nonce"], legacy["receipt_nonce"])
        self.assertNotIn("receipt_revision", fresh)
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_STATE_CONFLICT"):
            devcrew_worktree._atomic_json(self.path, dict(legacy), expected=legacy)
        self.assertEqual(json.loads(self.path.read_text()), stored)

    def test_invalid_receipt_revisions_fail_closed(self):
        snapshot = json.loads(self.path.read_text())
        for revision in (True, -1, 1.0, "1", None, [], {}):
            with self.subTest(revision=revision):
                invalid = dict(snapshot, receipt_revision=revision)
                self.path.write_text(json.dumps(invalid))
                original = self.path.read_bytes()
                with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_STATE_INVALID"):
                    devcrew_worktree.resolve(self.project, "T01")
                with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_STATE_INVALID"):
                    devcrew_worktree._atomic_json(self.path, dict(snapshot), expected=snapshot)
                self.assertEqual(self.path.read_bytes(), original)

    def test_creation_resolve_dispatch_and_git_cas_advance_revisions(self):
        original = json.loads(self.path.read_text())
        self.assertEqual(original["receipt_revision"], 1)
        devcrew_worktree.resolve(self.project, "T01")
        clean = json.loads(self.path.read_text())
        self.assertEqual(clean["receipt_revision"], 2)
        self.assertEqual(clean["receipt_nonce"], original["receipt_nonce"])
        devcrew_worktree.resolve(self.project, "T01", read_only=True)
        self.assertEqual(json.loads(self.path.read_text()), clean)
        tracked = self.project / "tracked.txt"
        tracked.write_text("dirty state\n")
        devcrew_worktree.resolve(self.project, "T01")
        dirty = json.loads(self.path.read_text())
        self.assertEqual(dirty["receipt_revision"], 3)
        self.assertEqual(dirty["receipt_nonce"], original["receipt_nonce"])
        tracked.write_text("base\n")
        devcrew_worktree.validate_dispatch(
            self.project, "T01", str(self.project), self.receipt["expected_head"], self.receipt
        )
        dispatched = json.loads(self.path.read_text())
        # Dispatch refreshes clean state first, then consumes the nonce.
        self.assertEqual(dispatched["receipt_revision"], 5)
        self.assertNotEqual(dispatched["receipt_nonce"], original["receipt_nonce"])
        devcrew_worktree.resolve(self.project, "T01")
        self.assertEqual(json.loads(self.path.read_text()), dispatched)
        tracked.write_text("candidate state\n")
        self.git("add", "tracked.txt")
        tree = self.git("write-tree")
        base = self.receipt["expected_head"]
        sha = self.git("commit-tree", tree, "-p", base, "-m", "candidate fixture")
        ref = "refs/heads/" + self.receipt["branch"]
        advanced = devcrew_worktree.advance_expected_head(
            self.project,
            "T01",
            base,
            sha,
            before_advance=lambda: self.git("update-ref", ref, sha, base),
        )
        self.assertEqual(advanced["receipt_revision"], 6)
        self.assertNotEqual(advanced["receipt_nonce"], dispatched["receipt_nonce"])
        self.assertEqual(json.loads(self.path.read_text()), advanced)
        self.assertEqual(self.git("rev-parse", "HEAD"), sha)
        with self.assertRaises(subprocess.CalledProcessError):
            devcrew_worktree.advance_expected_head(
                self.project,
                "T01",
                sha,
                sha,
                before_advance=lambda: self.git("update-ref", ref, sha, base),
            )
        self.assertEqual(json.loads(self.path.read_text()), advanced)
        self.assertEqual(self.git("rev-parse", "HEAD"), sha)

    def test_linked_creation_and_request_refresh_advance_revisions(self):
        receipt = devcrew_worktree.create(
            self.project, "T_LINK", worktree_request="Use a worktree for this task"
        )
        path = devcrew_worktree.location(self.project, "T_LINK")[-1]
        original = json.loads(path.read_text())
        self.assertEqual(original["receipt_revision"], 1)
        refreshed = devcrew_worktree.resolve(
            self.project, "T_LINK", worktree_request="Keep this requested worktree"
        )
        stored = json.loads(path.read_text())
        self.assertEqual(stored["receipt_revision"], 3)
        self.assertEqual(stored["worktree_request"], "Keep this requested worktree")
        self.assertEqual(refreshed["receipt_nonce"], receipt["receipt_nonce"])


if __name__ == "__main__":
    unittest.main()
