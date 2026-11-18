"""Candidates stop for manual reconciliation after uncertain side effects."""
import errno
import copy
import hashlib
import json
import os
import pathlib
import stat
import subprocess
import sys
import tempfile
import unittest
import shutil
from unittest import mock

import test_phase_candidate_commit as candidate_tests
ROOT = candidate_tests.ROOT
from devcrew_test_helpers import ExactPathSeed, copied_attributes, restore_attributes
import devcrew_phase
import devcrew_worktree


class CandidateSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._seed_builder = candidate_tests.CandidateCommitTests()
        cls._seed_builder.setUp()
        names = ("host", "base", "task", "verifier", "manifest", "receipt", "manifest_path", "bindings")
        cls._ready_attributes = copied_attributes(cls._seed_builder, names)
        cls._ready_seed = ExactPathSeed(cls._seed_builder.temp.name)
        cls._ready_seed_active = False

    @classmethod
    def tearDownClass(cls):
        try:
            if hasattr(cls, "_ready_seed"):
                cls._ready_seed.close()
            if hasattr(cls, "_seed_builder"):
                cls._seed_builder.doCleanups()
        finally:
            super().tearDownClass()

    def setUp(self):
        self.f = candidate_tests.CandidateCommitTests()
        if self._ready_seed.matches_environment() and not self.__class__._ready_seed_active:
            self._active_seed = self._ready_seed
            self._active_attributes = self._ready_attributes
            self._seed_temp = self._seed_builder.temp
            self._uses_shared_ready_seed = True
            self.__class__._ready_seed_active = True
            self.addCleanup(self._release_ready_seed)
        else:
            self.f.setUp()
            self._active_seed = ExactPathSeed(self.f.temp.name)
            names = ("host", "base", "task", "verifier", "manifest", "receipt", "manifest_path", "bindings")
            self._active_attributes = copied_attributes(self.f, names)
            self._seed_temp = self.f.temp
            self.addCleanup(self.f.doCleanups)
            self.addCleanup(self._active_seed.close)
            self._uses_shared_ready_seed = False
        self._restore_to_ready_seed()
        self.addCleanup(self._restore_to_ready_seed)
        self.state_path = devcrew_worktree.location(self.f.host, "T01")[-1]

    def _release_ready_seed(self):
        if getattr(self, "_uses_shared_ready_seed", False):
            self.__class__._ready_seed_active = False
            self._uses_shared_ready_seed = False

    def _restore_to_ready_seed(self):
        self._active_seed.restore()
        self.f.temp = self._seed_temp
        restore_attributes(self.f, self._active_attributes)

    def snapshot(self):
        return (self.f.manifest_path.read_bytes(), self.state_path.read_bytes(),
                self.f.git(self.f.task, "rev-parse", "HEAD"),
                self.f.git(self.f.task, "status", "--porcelain"),
                sorted(p.name for p in (self.f.host / ".codex/checks").iterdir()))

    def capture_ready_fixture(self):
        backup = tempfile.TemporaryDirectory()
        source = pathlib.Path(self.f.temp.name)
        snapshot = pathlib.Path(backup.name) / "tree"
        shutil.copytree(source, snapshot, symlinks=True)
        attrs = {name: copy.deepcopy(getattr(self.f, name))
                 for name in ("manifest", "receipt", "base", "host", "task", "manifest_path", "bindings")
                 if hasattr(self.f, name)}
        return backup, source, snapshot, attrs

    def restore_ready_fixture(self, source, snapshot, attrs):
        for child in source.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        shutil.copytree(snapshot, source, dirs_exist_ok=True, symlinks=True)
        for name, value in attrs.items():
            setattr(self.f, name, copy.deepcopy(value))

    @staticmethod
    def tree_contents(root):
        result = {}
        for path in pathlib.Path(root).rglob("*"):
            relative = str(path.relative_to(root))
            if path.is_symlink():
                result[relative] = ("symlink", os.readlink(path))
            elif path.is_file():
                result[relative] = ("file", path.read_bytes())
        return result

    def test_fault_snapshot_restores_git_and_durable_evidence_at_same_paths(self):
        backup, source, tree, attrs = self.capture_ready_fixture()
        self.addCleanup(backup.cleanup)
        initial = self.tree_contents(source)
        state_path = self.state_path
        check_record = next((self.f.host / ".codex/checks").glob("*.json"))
        check_payload = json.loads(check_record.read_text())
        log_path = check_record.parent / check_payload["log"]
        manifest_before = self.f.manifest_path.read_bytes()
        receipt_before = state_path.read_bytes()
        head_before = self.f.git(self.f.task, "rev-parse", "HEAD")
        index_path = pathlib.Path(self.f.git(self.f.task, "rev-parse", "--git-path", "index"))
        if not index_path.is_absolute():
            index_path = self.f.task / index_path
        index_before = index_path.read_bytes()

        (self.f.task / "owned.txt").write_text("fault leaked\n")
        self.f.git(self.f.task, "add", "owned.txt")
        self.f.git(self.f.task, "commit", "-qm", "fault leak")
        self.f.manifest_path.write_text("fault leak")
        state_path.write_text("fault leak")
        check_record.write_text("fault leak")
        log_path.write_text("fault leak")
        self.restore_ready_fixture(source, tree, attrs)

        self.assertEqual(self.tree_contents(source), initial)
        self.assertEqual(self.f.git(self.f.task, "rev-parse", "HEAD"), head_before)
        self.assertEqual(index_path.read_bytes(), index_before)
        self.assertEqual(self.f.manifest_path.read_bytes(), manifest_before)
        self.assertEqual(state_path.read_bytes(), receipt_before)
        self.assertTrue(log_path.is_file())

    def test_class_seed_restores_git_receipt_manifest_routes_and_check_evidence(self):
        expected_tree = self.tree_contents(self._ready_seed.snapshot)
        expected_manifest = copy.deepcopy(self._ready_attributes["manifest"])
        expected_receipt = copy.deepcopy(self._ready_attributes["receipt"])
        state_path = self.state_path
        manifest_path = self.f.manifest_path
        check_record = next((self.f.host / ".codex/checks").glob("*.json"))
        check_data = json.loads(check_record.read_text())
        log_path = check_record.parent / check_data["log"]
        route_path = self.f.host / ".codex/sessions/agent-dispatch-T01.json"

        (self.f.task / "owned.txt").write_text("uncommitted seed fault\n")
        self.f.git(self.f.task, "add", "owned.txt")
        self.f.git(self.f.task, "commit", "-qm", "seed fault")
        manifest_path.write_text("seed fault")
        state_path.write_text("seed fault")
        route_path.write_text("seed fault")
        check_record.write_text("seed fault")
        log_path.write_text("seed fault")
        self.f.manifest["phases"].clear()
        self.f.receipt.clear()

        self._restore_to_ready_seed()
        self.assertEqual(self.tree_contents(self._seed_builder.temp.name), expected_tree)
        self.assertEqual(self.f.manifest, expected_manifest)
        self.assertEqual(self.f.receipt, expected_receipt)
        self.assertEqual(self.f.git(self.f.task, "rev-parse", "HEAD"), self.f.base)

    def test_overlapping_safety_fixtures_do_not_restore_an_active_seed(self):
        other = CandidateSafetyTests("test_partial_candidate_never_replays_or_creates_another_commit")
        owned = self.f.task / "owned.txt"
        owned.write_text("active fixture remains intact\n", encoding="utf-8")
        original_root = self._active_seed.root
        try:
            other.setUp()
            self.assertNotEqual(other._active_seed.root, original_root)
            self.assertEqual(owned.read_text(encoding="utf-8"), "active fixture remains intact\n")
        finally:
            other.doCleanups()

    def test_changed_environment_uses_a_fresh_ready_fixture(self):
        fallback = CandidateSafetyTests("test_partial_candidate_never_replays_or_creates_another_commit")
        with mock.patch.dict(os.environ, {"DEVCREW_SEED_ENVIRONMENT_PROBE": "changed"}):
            fallback.setUp()
            try:
                self.assertIsNot(fallback._active_seed, self._ready_seed)
                self.assertNotEqual(fallback._active_seed.root, self._ready_seed.root)
            finally:
                fallback.doCleanups()

    def run_candidate(self):
        f = self.f
        def check(argv, _verification):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
                "run-check", "--project", str(f.host), "--execution-root", str(f.task),
                "--task-id", "T01", "--name", "safety", "--", *argv], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.splitlines()[0]
        return devcrew_phase.candidate_commit(f.host, f.task, f.manifest, f.receipt,
                                              f.bindings, f.verifier, check)

    def assert_stopped(self):
        f = self.f
        saved = json.loads(f.manifest_path.read_text())
        receipt = json.loads(self.state_path.read_text())
        before = self.snapshot()
        operations = [
            lambda: devcrew_worktree.resolve(f.host, "T01"),
            lambda: devcrew_worktree.resolve(f.host, "T01", worktree_request="changed request"),
            lambda: devcrew_worktree.create(f.host, "T01"),
            lambda: devcrew_worktree._atomic_json(self.state_path, receipt, expected=receipt),
            lambda: devcrew_worktree.advance_expected_head(f.host, "T01", f.base, "f" * 40,
                                                           before_advance=lambda: self.fail("must stop")),
            lambda: devcrew_worktree.validate_dispatch(f.host, "T01", str(f.task), f.base, f.receipt),
            lambda: devcrew_phase._atomic_json(f.manifest_path, f.manifest),
            lambda: devcrew_phase.save_manifest(f.manifest_path, saved, f.receipt),
            lambda: devcrew_phase.executor_input(saved, f.task),
            lambda: self.run_candidate(),
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                with self.assertRaisesRegex(ValueError, "CANDIDATE_RECONCILIATION_REQUIRED"):
                    operation()
                self.assertEqual(self.snapshot(), before)
        for operation in ("resume", "candidate-commit", "review-start", "local-fix-start", "replan-complete"):
            result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
                "phase", operation, "--project", str(f.host), "--task-id", "T01"],
                capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("CANDIDATE_RECONCILIATION_REQUIRED", result.stderr)
            self.assertIn("manual reconciliation", result.stderr)
            self.assertEqual(self.snapshot(), before)
        touched = f.task / "check-must-not-run"
        result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
            "run-check", "--project", str(f.host), "--execution-root", str(f.task), "--task-id", "T01",
            "--name", "must-not-run", "--", sys.executable, "-c",
            "from pathlib import Path; Path('check-must-not-run').touch()"], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("CANDIDATE_RECONCILIATION_REQUIRED", result.stderr)
        self.assertFalse(touched.exists())
        self.assertEqual(self.snapshot(), before)
        # Inspection remains available without rewriting JSON or advancing Git.
        self.assertEqual(json.loads(f.manifest_path.read_text()), saved)
        self.assertEqual(json.loads(self.state_path.read_text()), receipt)

    def test_partial_candidate_never_replays_or_creates_another_commit(self):
        for boundary in ("intent", "sha", "receipt", "manifest"):
            for after in (False, True):
                with self.subTest(boundary=boundary, after_replace=after):
                    self._restore_to_ready_seed()
                    saved, receipt = self.f._interrupt_candidate(boundary, after_replace=after)
                    if boundary == "intent" and not after:
                        self.assertNotIn("candidate_transition", saved)
                        self.assertEqual(receipt["expected_head"], self.f.base)
                        self.assertEqual(self.f.git(self.f.task, "rev-parse", "HEAD"), self.f.base)
                        continue
                    if boundary == "manifest" and after:
                        # A visible, consistent final snapshot is readable. A
                        # repeated candidate command still cannot make a commit.
                        self.assertNotIn("candidate_transition", saved)
                        self.f.manifest = saved
                        self.f.receipt = devcrew_worktree.resolve(self.f.host, "T01", read_only=True)
                        before = self.snapshot()
                        with self.assertRaisesRegex(ValueError, "PHASE_NOT_READY_FOR_CLOSE"):
                            self.run_candidate()
                        self.assertEqual(self.snapshot(), before)
                        continue
                    self.assertIn("candidate_transition", saved)
                    if boundary in ("receipt", "manifest"):
                        self.assertEqual(saved["candidate_transition"]["sha"],
                                         self.f.git(self.f.task, "rev-parse", "HEAD"))
                    self.assert_stopped()
                    if boundary == "intent":
                        fresh = devcrew_worktree.resolve(self.f.host, "T01", read_only=True)
                        self.assertEqual(fresh["expected_head"], self.f.base)

    def test_commit_tree_uncertain_result_keeps_intent_without_guessing_sha(self):
        original = devcrew_phase.subprocess.run
        created = []
        def run(argv, *args, **kwargs):
            result = original(argv, *args, **kwargs)
            if isinstance(argv, list) and "commit-tree" in argv:
                created.append(result.stdout.strip())
                raise OSError(errno.EIO, "commit-tree result lost")
            return result
        with mock.patch.object(devcrew_phase.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "CANDIDATE_RECONCILIATION_REQUIRED"):
                self.run_candidate()
        self.assertEqual(len(created), 1)
        saved = json.loads(self.f.manifest_path.read_text())
        self.assertNotIn("sha", saved["candidate_transition"])
        self.assertEqual(self.f.git(self.f.task, "rev-parse", "HEAD"), self.f.base)
        self.assert_stopped()

    def test_update_ref_cas_rejects_foreign_head_and_preserves_pending(self):
        original = devcrew_phase.subprocess.run
        foreign = []
        def run(argv, *args, **kwargs):
            if isinstance(argv, list) and "update-ref" in argv:
                result = original(["git", "-C", str(self.f.task), "commit-tree",
                    self.f.git(self.f.task, "rev-parse", "HEAD^{tree}"), "-p", self.f.base, "-m", "foreign"],
                    capture_output=True, text=True, check=True)
                foreign.append(result.stdout.strip())
                original(["git", "-C", str(self.f.task), "update-ref", argv[-3], foreign[-1], self.f.base],
                         capture_output=True, check=True)
            return original(argv, *args, **kwargs)
        with mock.patch.object(devcrew_phase.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "PHASE_COMMIT_CAS_FAILED"):
                self.run_candidate()
        self.assertEqual(self.f.git(self.f.task, "rev-parse", "HEAD"), foreign[0])
        self.assert_stopped()

    def test_storage_sync_failures_preserve_safe_stop(self):
        for stage in ("intent", "sha", "receipt", "manifest"):
            for directory in (False, True):
                with self.subTest(stage=stage, directory=directory):
                    self._restore_to_ready_seed()
                    active = []
                    original_replace = devcrew_phase._replace_json
                    original_write = devcrew_worktree._write_json
                    original_fsync = devcrew_phase.os.fsync
                    def replace(path, value):
                        marker = value.get("candidate_transition")
                        active[:] = ["sha" if marker and "sha" in marker else "intent" if marker else "manifest"]
                        return original_replace(path, value)
                    def write(path, value):
                        active[:] = ["receipt"]
                        return original_write(path, value)
                    def sync(fd):
                        if active == [stage] and stat.S_ISDIR(devcrew_phase.os.fstat(fd).st_mode) == directory:
                            raise OSError(errno.EIO, "injected fsync failure")
                        return original_fsync(fd)
                    with mock.patch.object(devcrew_phase, "_replace_json", side_effect=replace), \
                         mock.patch.object(devcrew_worktree, "_write_json", side_effect=write), \
                         mock.patch.object(devcrew_phase.os, "fsync", side_effect=sync):
                        with self.assertRaisesRegex(ValueError, "CANDIDATE_RECONCILIATION_REQUIRED"):
                            self.run_candidate()
                    saved = json.loads(self.f.manifest_path.read_text())
                    if "candidate_transition" in saved:
                        self.assert_stopped()
                    else:
                        self.assertTrue(stage == "intent" or (stage == "manifest" and directory))

    def test_legacy_recovery_states_are_preserved_even_if_acknowledged(self):
        for proof in ({}, {"completed_sha256": "a" * 64}, None):
            with self.subTest(proof=proof):
                value = json.loads(self.f.manifest_path.read_text())
                value["phases"][0]["candidate_commit"] = {"recovery_transition": proof}
                self.f.manifest_path.write_text(json.dumps(value))
                self.assert_stopped()

    def test_run_check_rejects_git_receipt_and_manifest_head_mismatches(self):
        self.run_candidate()
        saved_manifest = self.f.manifest_path.read_bytes()
        saved_receipt = self.state_path.read_bytes()
        for change in ("manifest", "receipt", "revision", "git"):
            with self.subTest(change=change):
                if change == "manifest":
                    manifest = json.loads(saved_manifest)
                    manifest["phases"][0]["candidate_commit"]["sha"] = self.f.base
                    self.f.manifest_path.write_text(json.dumps(manifest))
                elif change == "receipt":
                    receipt = json.loads(saved_receipt)
                    receipt["expected_head"] = self.f.base
                    self.state_path.write_text(json.dumps(receipt))
                elif change == "revision":
                    manifest = json.loads(saved_manifest)
                    manifest["manifest_revision"] = -1
                    self.f.manifest_path.write_text(json.dumps(manifest))
                else:
                    self.f.git(self.f.task, "commit", "--allow-empty", "-qm", "foreign")
                before = self.snapshot()
                result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
                    "run-check", "--project", str(self.f.host), "--execution-root", str(self.f.task),
                    "--task-id", "T01", "--name", "inconsistent", "--", sys.executable, "-c", "raise SystemExit(0)"],
                    capture_output=True, text=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)
                with self.assertRaises(ValueError):
                    devcrew_worktree.resolve(self.f.host, "T01", worktree_request="must not persist")
                self.assertEqual(self.snapshot(), before)
                with self.assertRaises(ValueError):
                    devcrew_phase._atomic_json(self.f.manifest_path, json.loads(self.f.manifest_path.read_text()))
                self.assertEqual(self.snapshot(), before)
                current = json.loads(self.state_path.read_text())
                with self.assertRaises(ValueError):
                    devcrew_worktree._atomic_json(self.state_path, current, expected=current)
                self.assertEqual(self.snapshot(), before)
                self.f.manifest_path.write_bytes(saved_manifest)
                self.state_path.write_bytes(saved_receipt)

    def test_evidence_changed_by_update_ref_hook_stops_before_receipt_save(self):
        original = devcrew_phase.subprocess.run
        def run(argv, *args, **kwargs):
            result = original(argv, *args, **kwargs)
            if isinstance(argv, list) and "update-ref" in argv:
                records = sorted((self.f.host / ".codex/checks").glob("*safety*.json"))
                record = records[-1]
                data = json.loads(record.read_text())
                log = record.parent / data["log"]
                log.write_text("replaced log\n")
                data["log_sha256"] = hashlib.sha256(log.read_bytes()).hexdigest()
                record.write_text(json.dumps(data))
            return result
        with mock.patch.object(devcrew_phase.subprocess, "run", side_effect=run):
            with self.assertRaisesRegex(ValueError, "CANDIDATE_RECONCILIATION_REQUIRED"):
                self.run_candidate()
        self.assertEqual(json.loads(self.state_path.read_text())["expected_head"], self.f.base)
        self.assert_stopped()


if __name__ == "__main__":
    unittest.main()
