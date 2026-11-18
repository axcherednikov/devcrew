import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
import shutil
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_worktree


class TaskWorktreeLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = pathlib.Path(self.temp.name) / "repo"
        self.project.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.project / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD").stdout.strip()

    def git(self, *args, cwd=None):
        return subprocess.run(
            ["git", "-C", str(cwd or self.project), *args],
            text=True,
            capture_output=True,
            check=True,
        )

    def cli(self, operation, task="T01", base=None):
        args = [
            sys.executable,
            str(CLI),
            "task-worktree",
            operation,
            "--project",
            str(self.project),
            "--task-id",
            task,
        ]
        if operation == "create":
            args += ["--worktree-request", "Use a worktree for this task"]
        if base is not None:
            args += ["--base-sha", base]
        result = subprocess.run(args, text=True, capture_output=True)
        if result.stdout:
            return result, json.loads(result.stdout)
        return result, None

    def expected_path(self, task):
        key = hashlib.sha256(str(self.project.resolve()).encode()).hexdigest()[:16]
        return self.project.parent / ".devcrew-task-worktrees" / f"repo-{key}" / task

    def create(self, task="T01"):
        result, receipt = self.cli("create", task, self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["status"], "TASK_WORKTREE_CREATED")
        self.assertEqual(receipt["owner"], "DEVCREW")
        self.assertEqual(receipt["expected_head"], self.base)
        self.assertTrue(receipt["repository_id"])
        self.assertNotIn("/.codex/worktrees/", receipt["task_worktree_root"])
        return pathlib.Path(receipt["task_worktree_root"])

    def test_dirty_main_checkout_is_untouched_and_not_copied(self):
        self.git("add", "tracked.txt")
        (self.project / "tracked.txt").write_text("staged plus dirty\n")
        (self.project / "untracked.txt").write_text("private main edit\n")
        before = self.git("status", "--porcelain", "--untracked-files=all").stdout
        path = self.create()
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all").stdout, before)
        self.assertEqual((path / "tracked.txt").read_text(), "base\n")
        self.assertFalse((path / "untracked.txt").exists())
        self.assertEqual(self.git("rev-parse", "HEAD", cwd=path).stdout.strip(), self.base)

    def test_resolve_reports_dirty_and_preserves_task_changes(self):
        path = self.create()
        changed = path / "tracked.txt"
        changed.write_text("task edit\n")
        (path / "new.txt").write_text("untracked task work\n")
        before = self.git("status", "--porcelain", "--untracked-files=all", cwd=path).stdout
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["status"], "DIRTY")
        self.assertTrue(receipt["dirty"])
        self.assertEqual(
            self.git("status", "--porcelain", "--untracked-files=all", cwd=path).stdout, before
        )

    def test_multiple_tasks_get_distinct_worktrees(self):
        one, two = self.create("T01"), self.create("T02")
        self.assertNotEqual(one, two)
        self.assertIn("codex/tasks/T01", self.git("branch", "--list", "codex/tasks/T01").stdout)
        self.assertIn("codex/tasks/T02", self.git("branch", "--list", "codex/tasks/T02").stdout)

    def test_repository_metadata_uses_one_rev_parse_for_normal_repository(self):
        original = devcrew_worktree._git
        calls = []

        def recording_git(project, *args, **kwargs):
            calls.append(args)
            return original(project, *args, **kwargs)

        with mock.patch.object(devcrew_worktree, "_git", side_effect=recording_git):
            root, common, identity, path, branch, state = devcrew_worktree.location(
                self.project, "T_META"
            )

        rev_parse_calls = [args for args in calls if args[:1] == ("rev-parse",)]
        self.assertEqual(len(rev_parse_calls), 1)
        self.assertEqual(
            rev_parse_calls[0][1:],
            ("--path-format=absolute", "--show-toplevel", "--git-common-dir", "--absolute-git-dir"),
        )
        self.assertEqual(root, self.project.resolve())
        self.assertEqual(common, self.project.resolve() / ".git")
        self.assertTrue(identity)

    def test_repository_metadata_falls_back_for_newline_in_path(self):
        newline_repo = pathlib.Path(self.temp.name) / "repo\nline"
        newline_repo.mkdir()
        subprocess.run(["git", "-C", str(newline_repo), "init", "-q"], check=True)
        original = devcrew_worktree._git
        calls = []

        def recording_git(project, *args, **kwargs):
            calls.append(args)
            return original(project, *args, **kwargs)

        with mock.patch.object(devcrew_worktree, "_git", side_effect=recording_git):
            result = devcrew_worktree.location(newline_repo, "T_NEWLINE")

        rev_parse_calls = [args for args in calls if args[:1] == ("rev-parse",)]
        self.assertEqual(len(rev_parse_calls), 4)
        self.assertEqual(result[0], newline_repo.resolve())

    def test_read_only_resolve_batches_linked_root_common_dir_and_head(self):
        path = self.create("T_RESOLVE_BATCH")
        original = devcrew_worktree._git
        calls = []

        def recording_git(project, *args, **kwargs):
            calls.append((pathlib.Path(project), args))
            return original(project, *args, **kwargs)

        with mock.patch.object(devcrew_worktree, "_git", side_effect=recording_git):
            receipt = devcrew_worktree.resolve(self.project, "T_RESOLVE_BATCH", read_only=True)

        linked_rev_parse = [
            args for project, args in calls if project == path and args[:1] == ("rev-parse",)
        ]
        self.assertEqual(
            linked_rev_parse,
            [
                (
                    "rev-parse",
                    "--path-format=absolute",
                    "--show-toplevel",
                    "--git-common-dir",
                    "HEAD",
                )
            ],
        )
        self.assertEqual(receipt["state"], "RESOLVED")

    def test_linked_metadata_falls_back_for_newline_path_and_git_overrides(self):
        newline_repo = pathlib.Path(self.temp.name) / "repo\nline"
        newline_repo.mkdir()
        subprocess.run(["git", "-C", str(newline_repo), "init", "-q"], check=True)
        subprocess.run(["git", "-C", str(newline_repo), "config", "user.name", "Test"], check=True)
        subprocess.run(
            ["git", "-C", str(newline_repo), "config", "user.email", "test@example.com"], check=True
        )
        (newline_repo / "tracked.txt").write_text("base\n")
        subprocess.run(["git", "-C", str(newline_repo), "add", "tracked.txt"], check=True)
        subprocess.run(["git", "-C", str(newline_repo), "commit", "-qm", "base"], check=True)
        base = subprocess.run(
            ["git", "-C", str(newline_repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        receipt = devcrew_worktree.create(
            newline_repo, "T_LINKED_NEWLINE", base, worktree_request="Use a worktree for this task"
        )
        linked = pathlib.Path(receipt["task_worktree_root"])
        original = devcrew_worktree._git
        calls = []

        def recording_git(project, *args, **kwargs):
            calls.append(args)
            return original(project, *args, **kwargs)

        with mock.patch.object(devcrew_worktree, "_git", side_effect=recording_git):
            linked_data = devcrew_worktree._linked_repository_details(linked, read_only=True)
        self.assertEqual(len([args for args in calls if args[:1] == ("rev-parse",)]), 4)
        self.assertEqual(linked_data[0], linked.resolve())

        calls.clear()
        git_dir = subprocess.run(
            ["git", "-C", str(linked), "rev-parse", "--absolute-git-dir"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        with mock.patch.dict(os.environ, {"GIT_DIR": git_dir, "GIT_WORK_TREE": str(linked)}):
            with mock.patch.object(devcrew_worktree, "_git", side_effect=recording_git):
                devcrew_worktree._linked_repository_details(linked, read_only=True)
        self.assertEqual(len([args for args in calls if args[:1] == ("rev-parse",)]), 3)

    def test_duplicate_create_never_resumes_or_mutates(self):
        path = self.create()
        (path / "tracked.txt").write_text("kept\n")
        before = (path / "tracked.txt").read_text()
        result, receipt = self.cli("create", base=self.base)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["status"], "TASK_WORKTREE_ALREADY_EXISTS")
        self.assertEqual((path / "tracked.txt").read_text(), before)

    def test_path_and_branch_collisions_fail_closed(self):
        path = self.expected_path("T01")
        path.mkdir(parents=True)
        (path / "keep").write_text("untouched")
        result, _ = self.cli("create", base=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TASK_WORKTREE_PATH_COLLISION", result.stderr)
        (path / "keep").unlink()
        path.rmdir()
        self.git("branch", "codex/tasks/T01")
        result, collision = self.cli("create", base=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(collision["status"], "TASK_WORKTREE_BRANCH_COLLISION")

    def test_invalid_base_is_rejected_without_worktree(self):
        result, _ = self.cli("create", base="f" * 40)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INVALID_BASE_SHA", result.stderr)
        self.assertFalse(self.expected_path("T01").exists())

    def test_invalid_git_branch_id_fails_before_parent_directory_creation(self):
        result, _ = self.cli("create", task="T..", base=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("INVALID_TASK_BRANCH", result.stderr)
        self.assertFalse(self.expected_path("T..").parent.exists())

    def test_registered_path_on_wrong_branch_is_collision_not_duplicate(self):
        path = self.expected_path("T01")
        path.parent.mkdir(parents=True)
        self.git("worktree", "add", "-b", "codex/tasks/OTHER", str(path), self.base)
        before = self.git("worktree", "list", "--porcelain").stdout
        result, _ = self.cli("create", base=self.base)
        self.assertNotEqual(result.returncode, 1)
        self.assertIn("TASK_WORKTREE_PATH_COLLISION", result.stderr)
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before)

    def test_resolve_recreates_missing_clean_worktree_at_same_identity(self):
        path = self.create()
        self.git("worktree", "remove", str(path))
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["status"], "TASK_WORKTREE_RECREATED")
        self.assertEqual(pathlib.Path(receipt["task_worktree_root"]), path)
        self.assertEqual(receipt["branch"], "codex/tasks/T01")
        self.assertEqual(receipt["head"], self.base)
        self.assertEqual(
            self.git("branch", "--show-current", cwd=path).stdout.strip(), "codex/tasks/T01"
        )
        result, resolved = self.cli("resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(resolved["state"], "RESOLVED")
        self.assertEqual(resolved["head"], self.base)

    def test_stale_expected_checkout_does_not_prune_unrelated_stale_worktree(self):
        path = self.create()
        unrelated = self.project.parent / "unrelated"
        self.git("worktree", "add", "-b", "codex/tasks/OTHER", str(unrelated), self.base)
        self.git("worktree", "lock", str(unrelated))
        shutil.rmtree(path)
        shutil.rmtree(unrelated)
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["status"], "TASK_WORKTREE_RECREATED")
        self.assertEqual(
            self.git("branch", "--show-current", cwd=path).stdout.strip(), "codex/tasks/T01"
        )
        entries = self.git("worktree", "list", "--porcelain").stdout
        self.assertIn(f"worktree {unrelated.resolve()}\n", entries)
        self.assertIn("locked", entries)

    def test_locked_expected_stale_registration_fails_without_removal(self):
        path = self.create()
        self.git("worktree", "lock", str(path))
        shutil.rmtree(path)
        before = self.git("worktree", "list", "--porcelain").stdout
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")
        after = self.git("worktree", "list", "--porcelain").stdout
        self.assertEqual(after, before)
        self.assertIn("locked", after)

    def test_resolve_rejects_task_branch_ref_that_differs_from_linked_head(self):
        path = self.create()
        newer = self.project / "later.txt"
        newer.write_text("later\n")
        self.git("add", "later.txt")
        self.git("commit", "-qm", "later")
        new_head = self.git("rev-parse", "HEAD").stdout.strip()
        common = pathlib.Path(
            self.git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        )
        (common / "refs" / "heads" / "codex" / "tasks" / "T01").write_text(new_head + "\n")
        result, _ = self.cli("resolve")
        self.assertNotEqual(result.returncode, 0)

    def test_dirty_main_remains_unchanged_and_not_copied_through_recreate(self):
        self.git("add", "tracked.txt")
        (self.project / "tracked.txt").write_text("main dirty\n")
        (self.project / "main-only.txt").write_text("main untracked\n")
        before = self.git("status", "--porcelain", "--untracked-files=all").stdout
        path = self.create()
        self.git("worktree", "remove", str(path))
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["status"], "TASK_WORKTREE_RECREATED")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all").stdout, before)
        self.assertEqual((path / "tracked.txt").read_text(), "base\n")
        self.assertFalse((path / "main-only.txt").exists())

    def test_missing_worktree_without_clean_proof_fails_closed(self):
        path = self.expected_path("T01")
        self.git("branch", "codex/tasks/T01", self.base)
        result, receipt = self.cli("resolve")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")

    def test_dirty_snapshot_writer_dispatch_and_changed_head_disable_recreation(self):
        path = self.create()
        (path / "tracked.txt").write_text("dirty task edit\n")
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(receipt["status"], "DIRTY")
        self.git("worktree", "remove", "--force", str(path))
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")

        path = self.create("T02")
        self.git("worktree", "remove", str(path))
        newer = self.project / "later.txt"
        newer.write_text("later\n")
        self.git("add", "later.txt")
        self.git("commit", "-qm", "later")
        new_head = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("update-ref", "refs/heads/codex/tasks/T02", new_head)
        result, receipt = self.cli("resolve", "T02")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")

    def test_symlink_occupied_and_corrupt_state_fail_closed(self):
        path = self.create()
        self.git("worktree", "remove", str(path))
        path.symlink_to(self.project, target_is_directory=True)
        result, _ = self.cli("resolve")
        self.assertNotEqual(result.returncode, 0)
        path.unlink()
        path.mkdir()
        (path / "keep").write_text("untouched")
        result, _ = self.cli("resolve")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((path / "keep").read_text(), "untouched")
        (path / "keep").unlink()
        path.rmdir()
        state = path.parent / ".devcrew-state" / "T01.json"
        state.write_text("not json")
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")

    def test_create_refuses_symlinked_durable_parent(self):
        namespace_parent = self.expected_path("T01").parent.parent
        outside = self.project.parent / "outside"
        outside.mkdir()
        namespace_parent.symlink_to(outside, target_is_directory=True)
        result, _ = self.cli("create", base=self.base)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TASK_WORKTREE_PATH_COLLISION", result.stderr)
        self.assertFalse((outside / self.expected_path("T01").parent.name / "T01").exists())

    def test_unwritable_durable_parent_fails_closed_for_create_and_recreate(self):
        path = self.create()
        self.git("worktree", "remove", str(path))
        durable_parent = path.parent
        original_mode = durable_parent.stat().st_mode & 0o777
        self.addCleanup(lambda: durable_parent.chmod(original_mode))
        durable_parent.chmod(0o500)
        probe = durable_parent / ".write-permission-probe"
        try:
            probe.write_text("probe")
        except PermissionError:
            pass
        else:
            probe.unlink()
            self.skipTest("current process can write despite removed owner write permission")

        create, _ = self.cli("create", task="T02", base=self.base)
        self.assertNotEqual(create.returncode, 0)
        self.assertIn("TASK_WORKTREE_PATH_COLLISION", create.stderr)

        before = self.git("worktree", "list", "--porcelain").stdout
        resolve, receipt = self.cli("resolve")
        self.assertEqual(resolve.returncode, 1)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")
        self.assertFalse(path.exists())
        self.assertEqual(self.git("worktree", "list", "--porcelain").stdout, before)

    def test_branch_attached_elsewhere_and_repository_record_mismatch_fail_closed(self):
        path = self.create()
        self.git("worktree", "remove", str(path))
        elsewhere = self.project.parent / "elsewhere"
        self.git("worktree", "add", str(elsewhere), "codex/tasks/T01")
        result, receipt = self.cli("resolve")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(receipt["status"], "UNSAFE_MISSING_WORKTREE")
        self.git("worktree", "remove", str(elsewhere))
        state = path.parent / ".devcrew-state" / "T01.json"
        record = json.loads(state.read_text())
        record["repository_id"] = "different"
        state.write_text(json.dumps(record))
        result, _ = self.cli("resolve")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
