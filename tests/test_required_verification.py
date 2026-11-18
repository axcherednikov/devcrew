"""H4 regressions use the real CLI and evidence, without assuming a test framework."""

import json
import os
import pathlib
import subprocess
import sys
import unittest

import test_devcrew
import test_phase_candidate_commit
import devcrew_phase
import devcrew_worktree


class RequiredWaveChecks(unittest.TestCase):
    run_cli = test_devcrew.DevCrewCLITest.run_cli
    project = test_devcrew.DevCrewCLITest.project

    def setUp(self):
        self.prepare_plan()

    def prepare_plan(self, second=None):
        temp, self.root = self.project()
        self.addCleanup(temp.cleanup)
        checks = self.root / ".codex/checks"
        checks.mkdir(parents=True)
        self.plan = checks / "plan.json"
        self.results = checks / "results.json"
        (self.root / "required.py").write_text(
            "from pathlib import Path\nassert Path('tracked.txt').read_text() == 'initial\\n'\n"
        )
        self.argv = [sys.executable, "-B", "required.py"]
        self.contract = {
            "waves": [
                {
                    "id": "w",
                    "slices": [{"id": "s", "files": ["tracked.txt"]}],
                    "verification": ["required project test"],
                    "verification_bindings": [
                        {"index": 0, "verification": "required project test", "argv": self.argv}
                    ],
                }
            ]
        }
        if second is not None:
            wave = self.contract["waves"][0]
            wave["verification"].append("second project check")
            wave["verification_bindings"].append(
                {"index": 1, "verification": "second project check", "argv": second}
            )
        self.plan.write_text(json.dumps(self.contract))
        self.results.write_text(json.dumps({"waves": []}))
        self.assertEqual(self.gate(0).returncode, 0)

    def gate(self, wave=1, *extra):
        return self.run_cli(
            "wave-gate",
            "--project",
            self.root,
            "--plan",
            self.plan,
            "--results",
            self.results,
            "--wave",
            wave,
            *extra,
        )

    def record(self, argv, **options):
        args = ["run-check", "--project", self.root, "--name", "required-project-test"]
        for key, value in options.items():
            args += ["--" + key.replace("_", "-"), value]
        result = self.run_cli(*args, "--", *argv)
        return result, result.stdout.splitlines()[0]

    def completed(self, records):
        self.results.write_text(
            json.dumps(
                {
                    "waves": [
                        {
                            "id": "w",
                            "slices": [{"id": "s", "done": True, "problems": []}],
                            "checks": records,
                        }
                    ]
                }
            )
        )
        return self.gate()

    def test_required_failed_unrelated_passed_rejected(self):
        (self.root / "tracked.txt").write_text("broken\n")
        required, _ = self.record(self.argv)
        unrelated, record = self.record([sys.executable, "-c", "print('success')"])
        self.assertNotEqual(required.returncode, 0)
        self.assertEqual(unrelated.returncode, 0)
        rejected = self.completed([record])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("required commands", rejected.stderr)

    def test_missing_required_check_rejected(self):
        self.assertNotEqual(self.completed([]).returncode, 0)

    def test_all_required_checks_pass(self):
        _, record = self.record(self.argv)
        result = self.completed([record])
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_all_multiple_required_commands_must_be_present(self):
        second = [sys.executable, "-B", "-c", "print('second project check')"]
        self.prepare_plan(second=second)
        _, first_record = self.record(self.argv)
        self.assertNotEqual(self.completed([first_record]).returncode, 0)
        _, second_record = self.record(second)
        passed = self.completed([first_record, second_record])
        self.assertEqual(passed.returncode, 0, passed.stderr)

    def test_successful_old_code_evidence_rejected(self):
        _, record = self.record(self.argv)
        (self.root / "tracked.txt").write_text("new code\n")
        self.assertNotEqual(self.completed([record]).returncode, 0)

    def test_same_check_name_different_actual_argv_rejected(self):
        _, record = self.record([sys.executable, "-c", "print('required project test')"])
        self.assertNotEqual(self.completed([record]).returncode, 0)

    def test_matching_argv_different_timeout_rejected(self):
        _, record = self.record(self.argv, timeout="42")
        self.assertNotEqual(self.completed([record]).returncode, 0)

    def test_matching_argv_different_environment_rejected(self):
        result = subprocess.run(
            [
                sys.executable,
                str(test_devcrew.CLI),
                "run-check",
                "--project",
                str(self.root),
                "--name",
                "env",
                "--",
                *self.argv,
            ],
            env={**os.environ, "PROJECT_TEST_FILTER": "skip"},
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotEqual(self.completed([result.stdout.splitlines()[0]]).returncode, 0)

    def test_run_check_passes_tool_call_id_but_never_records_its_value(self):
        tool_call_id = "private-tool-call-value"
        environment = {
            **os.environ,
            "CODEX_TOOL_CALL_ID": tool_call_id,
            "CHECK_ENV_SENTINEL": "retained",
        }
        command = [
            sys.executable,
            "-B",
            "-c",
            "import os; assert os.environ.get('CODEX_TOOL_CALL_ID'); assert os.environ['CHECK_ENV_SENTINEL'] == 'retained'",
        ]
        result = subprocess.run(
            [
                sys.executable,
                str(test_devcrew.CLI),
                "run-check",
                "--project",
                str(self.root),
                "--name",
                "tool-call-environment",
                "--",
                *command,
            ],
            env=environment,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record_path = pathlib.Path(result.stdout.splitlines()[0])
        record = json.loads(record_path.read_text())
        self.assertEqual(record["status"], "passed")
        self.assertEqual(
            record["environment_sha256"], devcrew_phase.environment_digest(environment)
        )
        self.assertNotIn(tool_call_id, record_path.read_text())

    def approval_bytes(self):
        paths = list(self.plan.parent.glob("wave-plan-*.json"))
        self.assertEqual(len(paths), 1)
        return paths[0], paths[0].read_bytes()

    def replace_required_with_true(self):
        self.contract["waves"][0]["verification_bindings"][0]["argv"] = ["true"]
        self.plan.write_text(json.dumps(self.contract))

    def test_executor_replan_reason_cannot_replace_required_test_with_true(self):
        approval, original = self.approval_bytes()
        (self.root / "tracked.txt").write_text("broken\n")
        failed, _ = self.record(self.argv)
        self.assertNotEqual(failed.returncode, 0)
        self.replace_required_with_true()
        rejected = self.gate(0, "--replan-reason", "executor chooses faster checks")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(
            json.loads(rejected.stderr)["details"]["status"], "REPLAN_APPROVAL_REQUIRED"
        )
        self.assertEqual(approval.read_bytes(), original)
        unrelated, record = self.record(["true"])
        self.assertEqual(unrelated.returncode, 0)
        self.assertNotEqual(self.completed([record]).returncode, 0)
        self.assertEqual(approval.read_bytes(), original)

    def test_executor_cannot_reapprove_by_repeated_calls(self):
        approval, original = self.approval_bytes()
        self.replace_required_with_true()
        for options in (
            (),
            ("--replan-reason", "owner-approved"),
            ("--replan-reason", "planner"),
            ("--replan-reason", "executor request"),
        ):
            rejected = self.gate(0, *options)
            self.assertNotEqual(rejected.returncode, 0)
            self.assertEqual(approval.read_bytes(), original)

    def test_change_without_trusted_approval_rejected(self):
        approval, original = self.approval_bytes()
        self.replace_required_with_true()
        for wave in (0, 1):
            rejected = self.gate(wave)
            self.assertNotEqual(rejected.returncode, 0)
            details = json.loads(rejected.stderr)["details"]
            self.assertEqual(
                details, {"status": "REPLAN_APPROVAL_REQUIRED", "replan_requested": False}
            )
            self.assertEqual(approval.read_bytes(), original)

    def test_request_cannot_refresh_approval_even_for_unchanged_plan(self):
        approval, original = self.approval_bytes()
        rejected = self.gate(0, "--replan-reason", "owner-approved")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(approval.read_bytes(), original)
        _, record = self.record(self.argv)
        self.assertEqual(self.gate(0).returncode, 0)
        self.assertEqual(approval.read_bytes(), original)
        passed = self.completed([record])
        self.assertEqual(passed.returncode, 0, passed.stderr)

    def test_request_cannot_create_initial_approval(self):
        temp, root = self.project()
        self.addCleanup(temp.cleanup)
        self.root = root
        self.plan = root / "plan.json"
        self.results = root / "results.json"
        self.plan.write_text(json.dumps(self.contract))
        self.results.write_text(json.dumps({"waves": []}))
        rejected = self.gate(0, "--replan-reason", "owner-approved")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(list((root / ".codex/checks").glob("wave-plan-*.json")), [])
        self.assertEqual(self.gate(0).returncode, 0)

    def test_renaming_plan_cannot_reset_required_commands(self):
        self.contract["waves"][0]["verification_bindings"][0]["argv"] = ["true"]
        self.plan = self.plan.with_name("executor-replacement-plan.json")
        self.plan.write_text(json.dumps(self.contract))
        rejected = self.gate(0)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("separate trusted approval", rejected.stderr)

    def test_go_php_typescript_bindings_preserve_exact_argv(self):
        for argv in (
            ["go", "test", "./..."],
            ["php", "vendor/bin/phpunit"],
            ["npm", "test", "--", "--run"],
        ):
            with self.subTest(argv=argv):
                contract = {
                    "verification": ["project suite"],
                    "verification_bindings": [
                        {"index": 0, "verification": "project suite", "argv": argv}
                    ],
                }
                devcrew_phase.pin_verification(contract)
                binding = devcrew_phase.required_verification(contract)[0]
                data = {
                    "command": argv,
                    "timeout_seconds": 300,
                    "idle_timeout_seconds": None,
                    "environment_sha256": contract["verification_environment_sha256"],
                    "started_at": contract["verification_approved_at"],
                }
                self.assertTrue(devcrew_phase.match_verification(data, binding, contract))
                self.assertFalse(
                    devcrew_phase.match_verification(
                        {**data, "command": ["true"]}, binding, contract
                    )
                )


class RequiredPhaseChecks(unittest.TestCase):
    def setUp(self):
        self.fixture = test_phase_candidate_commit.CandidateCommitTests(
            "test_candidate_commit_binds_checks_and_stops_for_review"
        )
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_candidate_rejects_executor_binding_substitution(self):
        f = self.fixture
        bindings = json.loads(f.bindings.read_text())
        bindings[0]["argv"] = [sys.executable, "-c", "print('irrelevant success')"]
        f.bindings.write_text(json.dumps(bindings))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "VERIFICATION_PLAN_CHANGED"):
            devcrew_phase.candidate_commit(
                f.host,
                f.task,
                f.manifest,
                f.receipt,
                f.bindings,
                f.verifier,
                lambda *_: self.fail("must not execute"),
            )
        self.assertEqual(f.git(f.task, "rev-parse", "HEAD"), f.base)

    def test_candidate_resume_rejects_substituted_evidence(self):
        f = self.fixture
        f.test_candidate_commit_binds_checks_and_stops_for_review()
        phase = json.loads(f.manifest_path.read_text())["phases"][0]
        evidence = f.host / phase["candidate_commit"]["evidence"][0]
        data = json.loads(evidence.read_text())
        # Keep the successful status, log and Git identity: only argv differs.
        data["command"] = [sys.executable, "-c", "print('unrelated')"]
        evidence.write_text(json.dumps(data))
        manifest = devcrew_phase.load_for_resume(
            f.manifest_path, devcrew_worktree.resolve(f.host, "T01")
        )
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "REQUIRED_CHECK_MISSING"):
            devcrew_phase.resume(manifest, f.host, f.task, f.verifier)

    def test_saved_required_list_cannot_change_after_execution(self):
        f = self.fixture
        f.manifest["phases"][0]["verification_bindings"][0]["argv"] = ["true"]
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "VERIFICATION_PLAN_CHANGED"):
            devcrew_phase.save_manifest(f.manifest_path, f.manifest, f.receipt)

    def test_candidate_commit_seals_verification_metadata(self):
        f = self.fixture
        f.test_candidate_commit_binds_checks_and_stops_for_review()
        saved = json.loads(f.manifest_path.read_text())
        phase = saved["phases"][0]
        phase["verification_bindings"][0]["argv"] = ["true"]
        phase["verification_sha256"] = devcrew_phase._verification_digest(phase)
        phase["candidate_commit"]["verification_contract"] = devcrew_phase._verification_contract(
            phase
        )
        f.manifest_path.write_text(json.dumps(saved))
        manifest = devcrew_phase.load_for_resume(
            f.manifest_path, devcrew_worktree.resolve(f.host, "T01")
        )
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_COMMIT_INCONSISTENT"):
            devcrew_phase.resume(manifest, f.host, f.task, f.verifier)

    def test_acceptance_rejects_same_full_suite_shape_with_other_executable(self):
        f = self.fixture
        f.test_candidate_commit_binds_checks_and_stops_for_review()
        candidate = json.loads(f.manifest_path.read_text())["phases"][0]["candidate_commit"]
        argv = ["python3", "-B", "-m", "unittest", "discover", "-s", "tests"]
        result = subprocess.run(
            [
                sys.executable,
                str(test_devcrew.CLI),
                "run-check",
                "--project",
                str(f.host),
                "--execution-root",
                str(f.task),
                "--task-id",
                "T01",
                "--name",
                "different-python",
                "--",
                *argv,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "REQUIRED_CHECK_MISSING"):
            devcrew_phase._verify_acceptance_evidence(
                [result.stdout.splitlines()[0]], f.host, f.task, "T01", candidate, f.verifier
            )


class RequiredAcceptanceChecks(unittest.TestCase):
    def test_pass_review_cannot_accept_unrelated_candidate_evidence(self):
        import test_phase_review_lifecycle

        f = test_phase_review_lifecycle.PhaseReviewLifecycleTests(
            "test_fresh_full_suite_record_for_exact_candidate_allows_acceptance"
        )
        f.setUp()
        self.addCleanup(f.doCleanups)
        candidate = f.make_candidate()
        f.submit_pass_review("P1", candidate, "required-check-review.json")
        suite = f.run_full_suite_record("real-full-suite")
        record = f.host / candidate["evidence"][0]
        data = json.loads(record.read_text())
        data["command"] = ["true"]
        record.write_text(json.dumps(data))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "REQUIRED_CHECK_MISSING"):
            devcrew_phase.accept_review(
                f.path, f.manifest, f.receipt, f.host, f.task, "P1", f.verifier, [suite]
            )
        saved = json.loads(f.path.read_text())["phases"][0]
        self.assertEqual(saved["status"], "REVIEWING")
        self.assertNotIn("accepted_sha", saved)
