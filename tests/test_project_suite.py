"""Stack-independent gate tests with real Git and run-check receipts.

Go/PHPUnit/Jest executables are deterministic protocol fixtures; installing these
frameworks is not required to test DevCrew's argv/evidence contract. Python uses
the real unittest runner.
"""

import json
import pathlib
import subprocess
import sys
import unittest
import tempfile

import test_phase_review_lifecycle as lifecycle
import devcrew_phase
from devcrew_test_helpers import run_check


class ProjectSuiteTests(unittest.TestCase):
    stacks = ("go", "phpunit", "jest", "unittest")

    def prepare(self, stack, multiple=False):
        f = lifecycle.PhaseReviewLifecycleTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        if stack == "unittest":
            argv = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"]
        else:
            args = {
                "go": ["test", "./..."],
                "phpunit": ["--configuration", "phpunit.xml"],
                "jest": ["--runInBand", "--ci"],
            }[stack]
            runner = pathlib.Path(f.temp.name) / stack
            runner.write_text(
                f"#!{sys.executable}\nimport sys\nfrom pathlib import Path\n"
                f"assert sys.argv[1:] == {args!r}\n"
                "assert Path('one.txt').read_text() != 'fail\\n'\n"
                f"print({stack!r} + ' project suite passed')\n"
            )
            runner.chmod(0o755)
            argv = [str(runner), *args]
        phase = f.manifest["phases"][0]
        phase["verification"] = [f"required {stack} project suite"]
        phase["verification_bindings"] = [
            {"index": 0, "verification": phase["verification"][0], "argv": argv}
        ]
        if multiple:
            phase["verification"].append("required additional project check")
            phase["verification_bindings"].append(
                {
                    "index": 1,
                    "verification": phase["verification"][1],
                    "argv": [
                        sys.executable,
                        "-B",
                        "-c",
                        "from pathlib import Path; assert Path('one.txt').is_file()",
                    ],
                }
            )
        f.manifest = devcrew_phase.save_manifest(f.path, f.manifest, f.receipt)
        return f, argv

    def record(self, f, argv):
        record_path, payload = run_check(
            f.host, execution_root=f.task, task_id="T01", name="project-suite", command=argv
        )
        return payload, str(record_path)

    def test_run_check_helper_matches_cli_for_success_and_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = pathlib.Path(temporary).resolve()
            for command, expected_status, expected_exit in (
                ([sys.executable, "-c", "print('ok')"], "passed", 0),
                ([sys.executable, "-c", "raise SystemExit(7)"], "failed", 7),
            ):
                with self.subTest(expected_status=expected_status):
                    api_record, api = run_check(project, name="parity", command=command)
                    cli = subprocess.run(
                        [
                            sys.executable,
                            str(lifecycle.ROOT / "scripts/devcrew.py"),
                            "run-check",
                            "--project",
                            str(project),
                            "--name",
                            "parity",
                            "--",
                            *command,
                        ],
                        capture_output=True,
                        text=True,
                    )
                    cli_record = pathlib.Path(cli.stdout.splitlines()[0])
                    cli_payload = json.loads(cli.stdout.splitlines()[1])
                    self.assertEqual(api["status"], expected_status)
                    self.assertEqual(api["exit_code"], expected_exit)
                    self.assertEqual(cli.returncode, 0 if expected_status == "passed" else 1)
                    self.assertEqual(cli_payload["status"], api["status"])
                    self.assertEqual(cli_payload["exit_code"], api["exit_code"])
                    self.assertEqual(api["command"], json.loads(cli_record.read_text())["command"])
                    self.assertEqual(
                        (api_record.parent / api["log"]).read_bytes(),
                        (cli_record.parent / cli_payload["log"]).read_bytes(),
                    )

    def accept(self, f, records):
        return devcrew_phase.accept_review(
            f.path, f.manifest, f.receipt, f.host, f.task, "P1", f.verifier, records
        )

    def test_each_stack_can_commit_accept_and_resume_with_all_approved_checks(self):
        for stack in self.stacks:
            with self.subTest(stack=stack):
                f, _ = self.prepare(stack, multiple=True)
                candidate = f.make_candidate()
                f.submit_pass_review("P1", candidate, "pass.json")
                records = []
                for binding in f.manifest["phases"][0]["verification_bindings"]:
                    result, record = self.record(f, binding["argv"])
                    self.assertEqual(result["status"], "passed", result)
                    records.append(record)
                self.assertEqual(self.accept(f, records)["status"], "PHASE_ACCEPTED")
                saved = json.loads(f.path.read_text())
                self.assertEqual(len(saved["phases"][0]["acceptance_evidence"]), 2)
                self.assertEqual(
                    devcrew_phase.resume(saved, f.host, f.task, f.verifier)["status"],
                    "PHASE_ACCEPTED",
                )

    def test_each_stack_rejects_successful_substitution_and_missing_check_at_acceptance(self):
        for stack in self.stacks:
            with self.subTest(stack=stack):
                f, argv = self.prepare(stack, multiple=True)
                candidate = f.make_candidate()
                f.submit_pass_review("P1", candidate, "pass.json")
                result, unrelated = self.record(f, [sys.executable, "-c", "print('success')"])
                self.assertEqual(result["status"], "passed")
                result, suite = self.record(f, argv)
                self.assertEqual(result["status"], "passed")
                for records in ([unrelated], [suite], [suite, unrelated]):
                    with self.assertRaisesRegex(devcrew_phase.PhaseError, "REQUIRED_CHECK_MISSING"):
                        self.accept(f, records)
                self.assertEqual(json.loads(f.path.read_text())["phases"][0]["status"], "REVIEWING")

    def test_each_stack_rejects_candidate_argv_substitution(self):
        for stack in self.stacks:
            with self.subTest(stack=stack):
                f, _ = self.prepare(stack)
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "VERIFICATION_PLAN_CHANGED"):
                    f.make_candidate(
                        binding_overrides={f.manifest["phases"][0]["verification"][0]: ["true"]},
                        run_check_override=lambda *_: self.fail(
                            "must not execute substituted argv"
                        ),
                    )
                self.assertEqual(f.git(f.task, "rev-parse", "HEAD"), f.base)

    def test_each_stack_rejects_failed_required_evidence(self):
        for stack in self.stacks:
            with self.subTest(stack=stack):
                f, argv = self.prepare(stack)
                candidate = f.make_candidate()
                if stack == "unittest":
                    (f.task / "tests/test_fixture_gate.py").write_text(
                        "import unittest\nclass FailedTest(unittest.TestCase):\n"
                        "    def test_failure(self): self.fail('required test failed')\n"
                    )
                else:
                    (f.task / "one.txt").write_text("fail\n")
                result, failed = self.record(f, argv)
                self.assertEqual(result["status"], "failed")
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PHASE_EVIDENCE"):
                    devcrew_phase._verify_acceptance_evidence(
                        [failed], f.host, f.task, "T01", candidate, f.verifier
                    )

    def test_each_stack_rejects_stale_candidate_evidence_at_acceptance(self):
        for stack in self.stacks:
            with self.subTest(stack=stack):
                f, _ = self.prepare(stack)
                candidate = f.make_candidate()
                f.submit_pass_review("P1", candidate, "pass.json")
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "STALE_ACCEPTANCE_EVIDENCE"):
                    self.accept(f, candidate["evidence"])

    def test_each_stack_preserves_execution_identity_freshness_and_plan_guards(self):
        for stack in self.stacks:
            with self.subTest(stack=stack):
                f, argv = self.prepare(stack)
                candidate = f.make_candidate()
                f.submit_pass_review("P1", candidate, "pass.json")
                result, reference = self.record(f, argv)
                self.assertEqual(result["status"], "passed", result)
                record = pathlib.Path(reference)
                original = record.read_bytes()
                data = json.loads(original)
                for key, value, error in (
                    ("command", argv + ["--skip-tests"], "REQUIRED_CHECK_MISSING"),
                    ("timeout_seconds", 42, "REQUIRED_CHECK_MISSING"),
                    ("idle_timeout_seconds", 42, "REQUIRED_CHECK_MISSING"),
                    ("environment_sha256", "0" * 64, "REQUIRED_CHECK_MISSING"),
                    ("started_at", "2000-01-01T00:00:00+00:00", "REQUIRED_CHECK_MISSING"),
                    ("git_head_before", "0" * len(candidate["sha"]), "STALE_PHASE_EVIDENCE"),
                    (
                        "candidate_tree_before",
                        "0" * len(candidate["tree"]),
                        "STALE_ACCEPTANCE_EVIDENCE",
                    ),
                ):
                    with self.subTest(field=key):
                        record.write_text(json.dumps({**data, key: value}))
                        with self.assertRaisesRegex(devcrew_phase.PhaseError, error):
                            self.accept(f, [reference])
                        record.write_bytes(original)
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "DUPLICATE_PHASE_EVIDENCE"):
                    self.accept(f, [reference, reference])
                log = record.parent / data["log"]
                original_log = log.read_bytes()
                log.write_text("successful output substituted\n")
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PHASE_EVIDENCE"):
                    self.accept(f, [reference])
                log.write_bytes(original_log)
                f.manifest["phases"][0]["verification_bindings"][0]["argv"] = ["true"]
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "VERIFICATION_PLAN_CHANGED"):
                    self.accept(f, [reference])
                self.assertEqual(json.loads(f.path.read_text())["phases"][0]["status"], "REVIEWING")


if __name__ == "__main__":
    unittest.main()
