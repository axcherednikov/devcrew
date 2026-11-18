import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_worktree
import test_phase_candidate_commit as candidate
import test_phase_review_lifecycle as review
import test_capacity as capacity
import devcrew_phase


class CurrentCheckoutLifecycle(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = pathlib.Path(self.temp.name) / "repo"
        self.project.mkdir()
        self.git("init", "-q", "-b", "master")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.com")
        (self.project / "tracked.txt").write_text("base\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "base")
        self.base = self.git("rev-parse", "HEAD")

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.project), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    def cli(self, command="task-checkout", operation="create", *extra):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                command,
                operation,
                "--project",
                str(self.project),
                "--task-id",
                "T01",
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def test_default_reuses_current_feature_branch_and_head(self):
        self.git("switch", "-c", "feature")
        (self.project / "feature.txt").write_text("feature only\n")
        self.git("add", "feature.txt")
        self.git("commit", "-qm", "feature change")
        feature_head = self.git("rev-parse", "HEAD")
        before = self.git("worktree", "list", "--porcelain")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["task_worktree_root"], str(self.project.resolve()))
        self.assertEqual(receipt["expected_head"], feature_head)
        self.assertEqual(receipt["branch"], "feature")
        self.assertEqual(self.git("branch", "--show-current"), "feature")
        self.assertEqual(self.git("rev-parse", "HEAD"), feature_head)
        self.assertTrue((self.project / "feature.txt").exists())
        self.assertEqual(
            self.git("worktree", "list", "--porcelain").count("worktree "),
            before.count("worktree "),
        )
        self.assertFalse((self.project.parent / ".devcrew-task-worktrees").exists())
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual(devcrew_worktree.resolve(self.project, "T01")["state"], "RESOLVED")

    def test_main_primary_is_supported(self):
        self.git("branch", "-m", "master", "main")
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)

    def test_dirty_primary_branches_without_losing_any_files_or_index(self):
        (self.project / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt")
        (self.project / "tracked.txt").write_text("unstaged\n")
        (self.project / "private.txt").write_text("keep\n")
        before = self.git("status", "--porcelain", "--untracked-files=all")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "DIRTY")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), before)
        self.assertEqual(self.git("show", ":tracked.txt"), "staged")
        self.assertEqual((self.project / "tracked.txt").read_text(), "unstaged\n")
        self.assertEqual((self.project / "private.txt").read_text(), "keep\n")
        self.assertEqual(self.git("branch", "--show-current"), "codex/tasks/T01")
        self.assertEqual(devcrew_worktree.resolve(self.project, "T01")["status"], "DIRTY")
        self.assertFalse((self.project.parent / ".devcrew-task-worktrees").exists())

    def test_feature_registration_preserves_ignored_files_conflicting_with_primary(self):
        self.git("switch", "-c", "feature")
        self.git("rm", "tracked.txt")
        (self.project / ".gitignore").write_text("tracked.txt\n")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore primary file")
        (self.project / "tracked.txt").write_text("private ignored content\n")
        self.assertEqual(self.git("status", "--porcelain"), "")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("branch", "--show-current"), "feature")
        self.assertEqual((self.project / "tracked.txt").read_text(), "private ignored content\n")
        self.assertEqual(self.git("branch", "--list", "codex/tasks/T01"), "")
        self.assertFalse((self.project.parent / ".devcrew-task-worktrees").exists())

    def test_worktree_command_requires_actual_explicit_request(self):
        for extra in ((), ("--worktree-request", ""), ("--worktree-request", " ")):
            with self.subTest(extra=extra):
                result = self.cli("task-worktree", "create", *extra)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("WORKTREE_REQUEST_REQUIRED", result.stderr)
        self.assertEqual(self.git("branch", "--show-current"), "master")
        self.assertFalse((self.project.parent / ".devcrew-task-worktrees").exists())

    def test_explicit_request_creates_worktree_and_retains_authorization(self):
        result = self.cli(
            "task-checkout",
            "create",
            "--worktree-request",
            "Давай в отдельной ветке пока сделаем задачу",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertNotEqual(receipt["task_worktree_root"], str(self.project.resolve()))
        self.assertEqual(self.git("branch", "--show-current"), "master")
        *_, state = devcrew_worktree.location(self.project, "T01")
        self.assertEqual(
            json.loads(state.read_text())["worktree_request"],
            "Давай в отдельной ветке пока сделаем задачу",
        )

    def test_legacy_missing_worktree_without_authorization_is_not_recreated(self):
        receipt = devcrew_worktree.create(self.project, "T01", worktree_request="Use a worktree")
        *_, state = devcrew_worktree.location(self.project, "T01")
        record = json.loads(state.read_text())
        del record["worktree_request"]
        state.write_text(json.dumps(record))
        self.git("worktree", "remove", receipt["task_worktree_root"])
        self.assertEqual(
            devcrew_worktree.resolve(self.project, "T01")["status"], "UNSAFE_MISSING_WORKTREE"
        )
        self.assertFalse(pathlib.Path(receipt["task_worktree_root"]).exists())

    def test_present_legacy_worktree_requires_explicit_authorization_before_use(self):
        devcrew_worktree.create(self.project, "T01", worktree_request="Use a worktree")
        *_, state = devcrew_worktree.location(self.project, "T01")
        record = json.loads(state.read_text())
        del record["worktree_request"]
        state.write_text(json.dumps(record))
        before = state.read_bytes()
        with self.assertRaisesRegex(ValueError, "WORKTREE_REQUEST_REQUIRED"):
            devcrew_worktree.resolve(self.project, "T01")
        self.assertEqual(state.read_bytes(), before)
        result = self.cli(
            "task-checkout", "resolve", "--worktree-request", "Continue T01 in its worktree"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(state.read_text())["worktree_request"], "Continue T01 in its worktree"
        )

    def test_authorized_worktree_also_starts_at_primary_head(self):
        self.git("switch", "-c", "feature")
        (self.project / "tracked.txt").write_text("feature\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "feature")
        feature_head = self.git("rev-parse", "HEAD")
        result = self.cli(
            "task-checkout",
            "create",
            "--worktree-request",
            "Use a worktree",
            "--base-sha",
            feature_head,
        )
        self.assertIn("PRIMARY_BASE_MISMATCH", result.stderr)
        self.assertFalse((self.project.parent / ".devcrew-task-worktrees").exists())
        result = self.cli("task-checkout", "create", "--worktree-request", "Use a worktree")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["expected_head"], self.base)
        self.assertEqual(self.git("rev-parse", "HEAD"), feature_head)

    def test_duplicate_create_preserves_dirty_current_task(self):
        self.assertEqual(self.cli().returncode, 0)
        (self.project / "tracked.txt").write_text("task work\n")
        result = self.cli()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)["status"], "TASK_CHECKOUT_ALREADY_EXISTS")
        self.assertEqual((self.project / "tracked.txt").read_text(), "task work\n")
        self.assertEqual(devcrew_worktree.resolve(self.project, "T01")["status"], "DIRTY")

    def test_wrong_base_is_rejected_and_branch_collision_uses_unused_suffix(self):
        result = self.cli("task-checkout", "create", "--base-sha", "0" * 40)
        self.assertIn("CHECKOUT_BASE_MISMATCH", result.stderr)
        self.git("branch", "codex/tasks/T01")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("branch", "--show-current"), "codex/tasks/T01-2")
        self.assertEqual(
            devcrew_worktree.resolve(self.project, "T01")["branch"], "codex/tasks/T01-2"
        )

    def test_both_primary_names_branch_from_current_head(self):
        self.git("branch", "main")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("branch", "--show-current"), "codex/tasks/T01")

    def test_only_local_trunk_is_primary_and_can_resume(self):
        self.git("branch", "-m", "master", "trunk")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["created_from_branch"], "trunk")
        self.assertEqual(devcrew_worktree.resolve(self.project, "T01")["state"], "RESOLVED")

    def test_remote_default_beats_conventional_name_and_linked_worktree_uses_local_head(self):
        self.git("switch", "-c", "release/stable")
        self.git("remote", "add", "origin", "https://example.invalid/repo")
        self.git("update-ref", "refs/remotes/origin/release/stable", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/release/stable")
        result = self.cli(
            "task-checkout", "create", "--worktree-request", "use a separate worktree"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["created_from_branch"], "release/stable")

    def test_feature_only_clone_preserves_checkout_when_remote_primary_is_not_local(self):
        self.git("branch", "-m", "master", "feature")
        self.git("remote", "add", "upstream", "https://example.invalid/repo")
        self.git("update-ref", "refs/remotes/upstream/trunk", self.base)
        self.git("symbolic-ref", "refs/remotes/upstream/HEAD", "refs/remotes/upstream/trunk")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["branch"], "feature")
        with self.assertRaisesRegex(ValueError, "PRIMARY_BRANCH_REQUIRED"):
            devcrew_worktree.primary_head(self.project)

    def test_configured_primary_keeps_feature_and_missing_configured_branch_blocks(self):
        self.git("branch", "integration")
        self.git("config", "devcrew.primaryBranch", "unknown")
        self.assertNotEqual(self.cli().returncode, 0)
        self.git("config", "devcrew.primaryBranch", "integration")
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.git("branch", "--show-current"), "master")

    def test_origin_default_does_not_replace_current_primary_head(self):
        self.git("branch", "main")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
        self.git("switch", "main")
        (self.project / "tracked.txt").write_text("main head\n")
        self.git("add", "tracked.txt")
        self.git("commit", "-qm", "main update")
        main_head = self.git("rev-parse", "HEAD")
        self.git("switch", "master")
        self.assertEqual(self.cli().returncode, 0)
        self.assertEqual(self.git("rev-parse", "HEAD"), self.base)
        self.assertNotEqual(self.base, main_head)

    def test_dirty_feature_is_registered_and_resumed_without_switching(self):
        self.git("switch", "-c", "test-fix")
        (self.project / "tracked.txt").write_text("staged\n")
        self.git("add", "tracked.txt")
        (self.project / "tracked.txt").write_text("unstaged\n")
        (self.project / "new.txt").write_text("untracked\n")
        before = self.git("status", "--porcelain", "--untracked-files=all")
        result = self.cli()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["branch"], "test-fix")
        self.assertEqual(self.git("branch", "--show-current"), "test-fix")
        self.assertEqual(self.git("status", "--porcelain", "--untracked-files=all"), before)
        self.assertEqual(self.git("show", ":tracked.txt"), "staged")
        self.assertEqual(devcrew_worktree.resolve(self.project, "T01")["branch"], "test-fix")

    def test_detached_head_is_rejected_without_losing_changes(self):
        self.git("switch", "--detach", self.base)
        (self.project / "tracked.txt").write_text("keep\n")
        result = self.cli()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TASK_CHECKOUT_BRANCH_REQUIRED", result.stderr)
        self.assertEqual((self.project / "tracked.txt").read_text(), "keep\n")

    def test_wrong_branch_and_corrupt_receipt_fail_closed(self):
        self.assertEqual(self.cli().returncode, 0)
        self.git("switch", "master")
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_PATH_COLLISION"):
            devcrew_worktree.resolve(self.project, "T01")
        *_, state = devcrew_worktree.location(self.project, "T01")
        state.write_text("corrupt")
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_STATE_INVALID"):
            devcrew_worktree.resolve(self.project, "T01")
        self.assertFalse((self.project.parent / ".devcrew-task-worktrees").exists())


class CurrentDirectoryPhaseLifecycle(unittest.TestCase):
    def fixture(self, fixture_type):
        fixture = fixture_type()
        fixture.worktree_request = None
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        self.assertEqual(fixture.task.resolve(), fixture.host.resolve())
        self.assertFalse((fixture.host.parent / ".devcrew-task-worktrees").exists())
        return fixture

    def test_executor_checks_and_candidate_commit_use_current_directory(self):
        fixture = self.fixture(candidate.CandidateCommitTests)
        fixture.test_candidate_commit_cli_records_and_commits_from_ordered_bindings()
        self.assertFalse((fixture.host.parent / ".devcrew-task-worktrees").exists())

    def test_existing_feature_runs_executor_and_candidate_lifecycle(self):
        create = devcrew_worktree.create

        def register_feature(project, *args, **kwargs):
            subprocess.run(
                ["git", "-C", str(project), "switch", "-c", "test-fix"],
                capture_output=True,
                text=True,
                check=True,
            )
            return create(project, *args, **kwargs)

        with mock.patch.object(devcrew_worktree, "create", side_effect=register_feature):
            fixture = self.fixture(candidate.CandidateCommitTests)
        self.assertEqual(fixture.receipt["branch"], "test-fix")
        fixture.test_candidate_commit_cli_records_and_commits_from_ordered_bindings()
        self.assertEqual(fixture.git(fixture.host, "branch", "--show-current"), "test-fix")
        self.assertEqual(fixture.git(fixture.host, "status", "--porcelain"), "")

    def test_review_acceptance_and_next_phase_use_current_directory(self):
        fixture = self.fixture(review.PhaseReviewLifecycleTests)
        committed = fixture.make_candidate()
        fixture.accept_candidate("P1", committed, ".codex/checks/review.json")
        self.assertEqual(fixture.manifest["phases"][0]["accepted_sha"], committed["sha"])
        fixture.manifest = devcrew_phase.advance_phase(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            "P2",
            fixture.host,
            fixture.task,
            fixture.verifier,
        )
        self.assertEqual(fixture.manifest["phases"][1]["base_sha"], committed["sha"])
        next_candidate = fixture.make_candidate("P2")
        self.assertEqual(next_candidate["base"], committed["sha"])
        self.assertEqual(fixture.git(fixture.host, "status", "--porcelain"), "")
        self.assertFalse((fixture.host.parent / ".devcrew-task-worktrees").exists())

    def test_local_fix_preserves_current_checkout_mode(self):
        fixture = self.fixture(review.PhaseReviewLifecycleTests)
        rejected = fixture.make_candidate()
        devcrew_phase.begin_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": fixture.base,
            "TARGET_SHA": rejected["sha"],
            "VERDICT": "FAIL",
            "FINDINGS": [
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair behavior",
                    "EVIDENCE": ["one.txt"],
                }
            ],
        }
        report.update(fixture.routed_reviewer_fields(report))
        reference = ".codex/checks/review-failed.json"
        report_path = fixture.host / reference
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            report,
            fixture.verifier,
            reference,
            report_path.read_bytes(),
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        devcrew_phase.start_local_fix(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        replacement = fixture.make_candidate(candidate_text="corrected candidate\n")
        self.assertNotEqual(replacement["sha"], rejected["sha"])
        fixture.accept_candidate("P1", replacement, ".codex/checks/review-passed.json")
        self.assertEqual(fixture.manifest["phases"][0]["accepted_sha"], replacement["sha"])
        self.assertEqual(fixture.git(fixture.host, "status", "--porcelain"), "")
        self.assertFalse((fixture.host.parent / ".devcrew-task-worktrees").exists())

    def test_interrupted_legacy_worktree_recovery_requires_user_request(self):
        for operation in ("local_fix", "systemic_replan"):
            with self.subTest(operation=operation):
                fixture = review.PhaseReviewLifecycleTests()
                self.addCleanup(fixture.doCleanups)
                fixture.setUp()
                if operation == "local_fix":
                    fixture.leave_local_fix_transition_pending("review-failed.json")
                    recover = devcrew_phase.recover_local_fix
                else:
                    _, _, _, replacement, planner_id = fixture._systemic_replan_fixture()
                    with mock.patch.object(
                        devcrew_worktree,
                        "advance_expected_head",
                        side_effect=ValueError("simulated interruption"),
                    ):
                        with self.assertRaises(devcrew_phase.PhaseError):
                            devcrew_phase.complete_systemic_replan(
                                fixture.path,
                                fixture.manifest,
                                fixture.receipt,
                                fixture.host,
                                fixture.task,
                                replacement,
                                planner_id,
                                fixture.verifier,
                            )
                    recover = devcrew_phase.recover_systemic_replan
                *_, state = devcrew_worktree.location(fixture.host, "T01")
                record = json.loads(state.read_text())
                del record["worktree_request"]
                state.write_text(json.dumps(record))
                before_head = fixture.git(fixture.task, "rev-parse", "HEAD")
                before_manifest = fixture.path.read_bytes()
                before_state = state.read_bytes()
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    recover(fixture.host, "T01", verifier=fixture.verifier)
                self.assertEqual(caught.exception.code, "WORKTREE_REQUEST_REQUIRED")
                self.assertEqual(fixture.git(fixture.task, "rev-parse", "HEAD"), before_head)
                self.assertEqual(fixture.path.read_bytes(), before_manifest)
                self.assertEqual(state.read_bytes(), before_state)

    def test_capacity_report_accepts_registered_current_directory(self):
        checker = capacity.CapacityBackpressure()
        checker.fixture = self.fixture(review.PhaseReviewLifecycleTests)
        checker.project = checker.fixture.host.resolve()
        checker.task_id = "T01"
        checker._prepared_cache = {}
        result = capacity.capacity.report(str(checker.project), checker.report())
        self.assertEqual(result["STATUS"], "AGENT_CAPACITY_BLOCKED")
        self.assertEqual(result["TASK_WORKTREE_ROOT"], str(checker.project))


class CheckoutPolicyContract(unittest.TestCase):
    def test_implementation_workflows_apply_explicit_user_checkout_policy(self):
        for name in ("devcrew-task", "devcrew-autopilot", "devcrew-coordinate"):
            content = (ROOT / "skills" / name / "SKILL.md").read_text()
            with self.subTest(workflow=name):
                self.assertIn("checkout-policy.md", content)
                self.assertIn("current working directory is the default", content)
                self.assertIn("explicit user instruction", content)
                self.assertIn("Keep an existing working branch", content)
                self.assertIn("Do not ask the user to choose or confirm a branch", content)
                self.assertNotIn("Task branches start at the primary master/main head", content)
        preflight = (ROOT / "skills/devcrew-task/references/dispatch-preflight.md").read_text()
        self.assertIn("TASK_WORKTREE_ROOT == PROJECT_ROOT", preflight)
        self.assertIn("--worktree-request TEXT", preflight)
        self.assertNotIn("For AGENT EXECUTION, create the task's linked worktree", preflight)
        self.assertNotIn("BASE_SHA comes from the primary master/main head", preflight)
        self.assertIn("BASE_SHA is the current HEAD", preflight)


if __name__ == "__main__":
    unittest.main()
