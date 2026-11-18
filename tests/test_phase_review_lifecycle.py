import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_phase
import devcrew_capacity
from devcrew_test_helpers import run_check as run_check_api
import devcrew_worktree
import devcrew_dispatch
import devcrew_context
import devcrew_cost
import devcrew_capacity


def issue_planner_receipt(project, task_id, base_sha, message, agent_id):
    receipt = devcrew_worktree.resolve(project, task_id)
    selection = devcrew_phase._source_snapshot_path(
        pathlib.Path(receipt["task_worktree_root"]), task_id, "0" * 64
    ).with_name("planning-source.json")
    if not selection.exists():
        source_path = project.parent / "intent-source.txt"
        source_path.write_text("The checkout option must remain available.")
        devcrew_phase.bind_intent_source(
            project,
            devcrew_worktree.resolve(project, task_id),
            {
                "mode": "file",
                "source_type": "document",
                "path": str(source_path),
                "expected_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "origin": "test source",
            },
        )
        source_path.unlink()
    runtime = {
        "models": [
            {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
            {"id": "strong-test", "capabilities": ["strong"], "efforts": ["low", "medium", "high"]},
        ],
        "roles": {},
        "generic_available": True,
        "explicit_model_effort": True,
    }
    issued = devcrew_dispatch.issue_route(
        project,
        task_id=task_id,
        role="planner",
        identity={"TASK_ID": task_id, "BASE_SHA": base_sha},
        intent={
            "role": "planner",
            "task_name": f"planner_{task_id}",
            "message": message,
            "runtime": runtime,
            "route_options": {
                "task_class": "IMPLEMENTATION",
                "decision_complexity": "NORMAL",
                "risk": "low",
            },
            "role_contract": "Plan the current task.",
        },
    )
    return devcrew_dispatch.record_spawn(
        project,
        {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "planner",
            "TASK_ID": task_id,
            "BASE_SHA": base_sha,
            "AGENT_ID": agent_id,
            "SPAWN_MODEL": issued["PREPARED"]["route"]["RESOLVED_MODEL"],
            "SPAWN_REASONING_EFFORT": issued["PREPARED"]["route"]["REASONING_EFFORT"],
            "SPAWN_MODE": issued["PREPARED"]["dispatch"]["SPAWN_MODE"],
            "PREPARED": issued["PREPARED"],
            "SPAWN_PAYLOAD": issued["SPAWN_PAYLOAD"],
            "SPAWN_PAYLOAD_SHA256": issued["SPAWN_PAYLOAD_SHA256"],
        },
    )


class PhaseReviewLifecycleTests(unittest.TestCase):
    """Exercise the immutable candidate review lifecycle against real Git commits."""

    worktree_request = "Use a worktree for this task"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = pathlib.Path(self.temp.name) / "host"
        self.host.mkdir()
        self.git(self.host, "init", "-q")
        # Snapshot restores require Git to finish writing before each command returns.
        self.git(self.host, "config", "maintenance.auto", "false")
        self.git(self.host, "config", "user.name", "Test")
        self.git(self.host, "config", "user.email", "test@example.com")
        (self.host / ".gitignore").write_text("/.codex/\n")
        for path in ("one.txt", "two.txt"):
            (self.host / path).write_text("base\n")
        fixture_tests = self.host / "tests"
        fixture_tests.mkdir()
        (fixture_tests / "test_fixture_gate.py").write_text(
            "import unittest\n\nclass FixtureGateTest(unittest.TestCase):\n"
            "    def test_fixture(self):\n        self.assertTrue(True)\n"
        )
        self.git(self.host, "add", ".")
        self.git(self.host, "commit", "-qm", "base")
        self.base = self.git(self.host, "rev-parse", "HEAD")
        devcrew_worktree.create(self.host, "T01", self.base, worktree_request=self.worktree_request)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.task = pathlib.Path(self.receipt["task_worktree_root"])
        self.manifest = {
            "schema_version": 1,
            "task_id": "T01",
            "task_mode": "MULTI_PHASE",
            "semantic_contract": {"capabilities": ["first", "second", "third"]},
            "worktree": {
                "repository_id": self.receipt["repository_id"],
                "task_worktree_root": str(self.task),
                "branch": self.receipt["branch"],
                "base_sha": self.base,
            },
            "current_phase_id": "P1",
            "phases": [
                self.phase("P1", "one.txt", []),
                self.phase("P2", "two.txt", ["P1"]),
                self.phase("P3", "three.txt", ["P1"]),
            ],
        }
        planner = issue_planner_receipt(
            self.host, "T01", self.base, "planner payload", "planner-agent"
        )
        self.manifest["planner_dispatch"] = {"spawn_receipt_id": planner["SPAWN_RECEIPT_ID"]}
        self.path = devcrew_phase.manifest_path(self.host, "T01")
        devcrew_phase.save_manifest(self.path, self.manifest, self.receipt)
        self.verifier = __import__("devcrew")._verified_record

    def test_new_phase_denies_previous_executor_identity(self):
        self.test_pass_review_accepts_same_sha_and_only_then_unlocks_dependency()

    def test_new_phase_uses_fresh_receipt_and_context(self):
        self.test_pass_review_accepts_same_sha_and_only_then_unlocks_dependency()

    def test_same_phase_followup_allows_valid_local_fix(self):
        self.test_executor_continuation_receipt_binds_live_local_fix_cycle()

    def test_phase_lifecycle_requires_fresh_context_without_telemetry(self):
        self.test_pass_review_accepts_same_sha_and_only_then_unlocks_dependency()
        self.assertEqual(
            devcrew_context.evaluate({}),
            {
                "signal": "CONTINUE",
                "current_context_tokens": None,
                "context_limit": None,
                "context_usage_percent": None,
                "threshold_source": None,
            },
        )
        self.assertEqual(devcrew_context.normalize_usage(None)["total_recorded_tokens"], None)

    def test_new_context_unavailable_blocks_phase_execution(self):
        self.test_pass_review_accepts_same_sha_and_only_then_unlocks_dependency()

    def test_reviewer_identity_remains_independent(self):
        store = {"receipts": [{"TASK_ID": "T01", "ROLE": "executor", "AGENT_ID": "writer"}]}
        with self.assertRaisesRegex(
            devcrew_dispatch.DispatchError, "independent from the executor"
        ):
            devcrew_dispatch._require_independent_reviewer(store, task_id="T01", agent_id="writer")
        devcrew_dispatch._require_independent_reviewer(store, task_id="T01", agent_id="reviewer")

    def test_executor_replacement_preserves_loop_history(self):
        self.test_repeated_local_fix_uses_verified_progress_without_owner_each_cycle()

    def test_lifecycle_guard_preserves_sha_acceptance_and_ownership(self):
        self.test_pass_review_accepts_same_sha_and_only_then_unlocks_dependency()

    @staticmethod
    def phase(phase_id, owned_file, dependencies):
        return {
            "phase_id": phase_id,
            "goal": f"change {owned_file}",
            "depends_on": dependencies,
            "owned_scope": [owned_file],
            "acceptance": [f"{owned_file} changed"],
            "verification": [f"verify {owned_file}"],
            "verification_bindings": [
                {
                    "index": 0,
                    "verification": f"verify {owned_file}",
                    "argv": ["python3", "-B", "-m", "unittest", "discover", "-s", "tests"],
                }
            ],
            "commit_boundary": f"{phase_id} capability",
            "commit_subject": f"type(T01): change {owned_file}",
            "status": "PLANNED",
        }

    @staticmethod
    def git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def make_candidate(
        self,
        phase_id="P1",
        candidate_text=None,
        before_commit=None,
        run_check_override=None,
        binding_overrides=None,
        executor_receipt_override=None,
    ):
        phase = next(p for p in self.manifest["phases"] if p["phase_id"] == phase_id)
        if phase["status"] == "PLANNED":
            self.manifest = devcrew_phase.set_phase_status(
                self.path,
                self.manifest,
                self.receipt,
                phase_id,
                "EXECUTING",
                project=self.host,
                verifier=self.verifier,
            )
        executor = (
            executor_receipt_override
            if executor_receipt_override is not None
            else self.routed_executor_fields(phase_id)
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        owned_file = phase["owned_scope"][0]
        (self.task / owned_file).write_text(candidate_text or f"{phase_id} candidate\n")
        records = []
        for binding in phase["verification_bindings"]:
            check_path, check_payload = run_check_api(
                self.host,
                execution_root=self.task,
                task_id="T01",
                name="preflight",
                command=binding["argv"],
            )
            if check_payload["status"] != "passed":
                raise AssertionError(check_payload)
            records.append(str(check_path))
        evidence = devcrew_phase.verify_evidence_records(
            records, self.host, self.task, "T01", self.verifier
        )
        self.manifest = devcrew_phase.set_phase_status(
            self.path,
            self.manifest,
            self.receipt,
            phase_id,
            "VERIFYING",
            project=self.host,
            verifier=self.verifier,
        )
        self.manifest = devcrew_phase.set_phase_status(
            self.path,
            self.manifest,
            self.receipt,
            phase_id,
            "READY_FOR_CLOSE",
            evidence,
            self.host,
            self.verifier,
            executor["SPAWN_RECEIPT_ID"],
        )
        if before_commit:
            before_commit()
        bindings = pathlib.Path(self.temp.name) / "bindings.json"
        binding_items = []
        for binding in phase["verification_bindings"]:
            index, verification, argv = binding["index"], binding["verification"], binding["argv"]
            if binding_overrides and verification in binding_overrides:
                argv = binding_overrides[verification]
            binding_items.append({"index": index, "verification": verification, "argv": argv})
        bindings.write_text(json.dumps(binding_items))

        self.last_candidate_commands = []

        def run_check(argv, _verification):
            self.last_candidate_commands.append(argv)
            if run_check_override is not None:
                return run_check_override(argv, _verification)
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/devcrew.py"),
                    "run-check",
                    "--project",
                    str(self.host),
                    "--execution-root",
                    str(self.task),
                    "--task-id",
                    "T01",
                    "--name",
                    "candidate",
                    "--",
                    *argv,
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            return result.stdout.splitlines()[0]

        committed = devcrew_phase.candidate_commit(
            self.host, self.task, self.manifest, self.receipt, bindings, self.verifier, run_check
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        return committed["commit"]

    def require_full_suite_and_targeted_checks(self, extra=()):
        phase = next(
            item
            for item in self.manifest["phases"]
            if item["phase_id"] == self.manifest["current_phase_id"]
        )
        phase["verification"] = ["full suite", *extra, "verify changed capability"]
        phase["verification_bindings"] = [
            {
                "index": index,
                "verification": verification,
                "argv": (
                    ["python3", "-B", "-m", "unittest", "discover", "-s", "tests"]
                    if index == 0
                    else [
                        "env",
                        "PYTHONDONTWRITEBYTECODE=1",
                        "python3",
                        "-B",
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-p",
                        "test_fixture_gate.py",
                    ]
                ),
            }
            for index, verification in enumerate(phase["verification"])
        ]
        self.manifest = devcrew_phase.save_manifest(self.path, self.manifest, self.receipt)

    def test_local_fix_candidate_runs_full_suite_and_all_bindings(self):
        self.require_full_suite_and_targeted_checks()
        self.enter_local_fix_with_history("full-suite-required")
        candidate = self.make_candidate(candidate_text="replacement after local fix\n")
        self.assertEqual(
            self.last_candidate_commands,
            [
                ["python3", "-B", "-m", "unittest", "discover", "-s", "tests"],
                [
                    "env",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "python3",
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_fixture_gate.py",
                ],
            ],
        )
        self.assertEqual(len(candidate["evidence"]), 2)

    def test_local_fix_candidate_cannot_replace_full_suite_with_targeted_tests(self):
        self.require_full_suite_and_targeted_checks()
        self.enter_local_fix_with_history("missing-full-suite")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "VERIFICATION_PLAN_CHANGED"):
            self.make_candidate(
                candidate_text="replacement without full suite\n",
                binding_overrides={
                    "full suite": [
                        "python3",
                        "-B",
                        "-m",
                        "unittest",
                        "discover",
                        "-s",
                        "tests",
                        "-p",
                        "test_fixture_gate.py",
                    ]
                },
                run_check_override=lambda *_: self.fail("checks must not run"),
            )
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_stale_full_suite_record_cannot_accept_replacement_candidate(self):
        self.enter_local_fix_with_history("stale-full-accept")
        stale_suite = self.run_full_suite_record("suite-before-candidate")
        candidate = self.make_candidate(candidate_text="replacement for stale full suite\n")
        self.submit_pass_review("P1", candidate, "stale-full-pass.json")
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.accept_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                "P1",
                self.verifier,
                [stale_suite],
            )
        self.assertEqual(caught.exception.code, "STALE_ACCEPTANCE_EVIDENCE")

    def test_initial_candidate_runs_full_ordered_verification_contract(self):
        self.require_full_suite_and_targeted_checks()
        self.make_candidate(candidate_text="initial candidate with full verification\n")
        self.assertEqual(
            self.last_candidate_commands,
            [
                ["python3", "-B", "-m", "unittest", "discover", "-s", "tests"],
                [
                    "env",
                    "PYTHONDONTWRITEBYTECODE=1",
                    "python3",
                    "-B",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_fixture_gate.py",
                ],
            ],
        )

    def test_acceptance_rejects_missing_full_suite_record(self):
        candidate = self.make_candidate(candidate_text="candidate missing acceptance suite\n")
        self.submit_pass_review("P1", candidate, "missing-full-pass.json")
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.accept_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )
        self.assertEqual(caught.exception.code, "PHASE_ACCEPTANCE_EVIDENCE_REQUIRED")

    def test_fresh_full_suite_record_for_exact_candidate_allows_acceptance(self):
        candidate = self.make_candidate(candidate_text="candidate with final suite\n")
        self.submit_pass_review("P1", candidate, "fresh-full-pass.json")
        suite = self.run_full_suite_record("suite-for-candidate")
        accepted = devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [suite],
        )
        self.assertEqual(accepted["accepted_sha"], candidate["sha"])
        saved = json.loads(self.path.read_text())["phases"][0]
        self.assertEqual(saved["status"], "ACCEPTED")
        self.assertEqual(
            saved["acceptance_evidence"],
            [pathlib.Path(suite).resolve().relative_to(self.host.resolve()).as_posix()],
        )

    def routed_executor_fields(self, phase_id):
        phase = next(p for p in self.manifest["phases"] if p["phase_id"] == phase_id)
        runtime = {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]}
            ],
            "roles": {"executor-agent": {"available": True, "supports_overrides": True}},
            "generic_available": True,
            "explicit_model_effort": True,
        }
        selection = devcrew_cost.route("executor", runtime=runtime)
        prepared = devcrew_context.routed_task_spawn(
            "executor_T01",
            "Implement the active phase",
            selection,
            runtime,
            task_id="T01",
            project_root=self.host,
            task_worktree_root=self.task,
            base_sha=phase.get("base_sha", self.base),
            owned_scope=phase["owned_scope"],
            acceptance=phase["acceptance"],
            verification=phase["verification"],
            receipt=self.receipt,
            role_contract="Execute only the current phase contract.",
        )
        return devcrew_dispatch.record_spawn(
            self.host,
            {
                "STATUS": "SPAWN_SUCCESS",
                "REQUESTED_ROLE": "executor",
                "TASK_ID": "T01",
                "PHASE_ID": phase_id,
                "BASE_SHA": phase.get("base_sha", self.base),
                "WRITER_CYCLE": (phase.get("active_local_fix") or {}).get("cycle_id", 0),
                "REJECTED_CANDIDATE_SHA": (phase.get("active_local_fix") or {}).get(
                    "rejected_candidate_sha"
                ),
                "AGENT_ID": "executor-" + uuid.uuid4().hex,
                "SPAWN_MODEL": prepared["spawn"]["model"],
                "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
                "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
                "FORK_TURNS": "none",
                "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                "SPAWN_PAYLOAD": prepared["spawn"],
                "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
            },
        )

    def issued_local_fix_route(self):
        phase = next(
            p for p in self.manifest["phases"] if p["phase_id"] == self.manifest["current_phase_id"]
        )
        runtime = {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]}
            ],
            "roles": {"executor-agent": {"available": True, "supports_overrides": True}},
            "generic_available": True,
            "explicit_model_effort": True,
        }
        prepared = devcrew_context.routed_task_spawn(
            "executor_T01_local_fix",
            "Continue the exact active phase repair",
            devcrew_cost.route("executor", runtime=runtime),
            runtime,
            task_id="T01",
            project_root=self.host,
            task_worktree_root=self.task,
            base_sha=phase.get("base_sha", self.base),
            owned_scope=phase["owned_scope"],
            acceptance=phase["acceptance"],
            verification=phase["verification"],
            receipt=self.receipt,
            role_contract="Continue only the current phase repair.",
        )
        return prepared

    def routed_reviewer_fields(self, target):
        agent_id = "review-agent-" + uuid.uuid4().hex
        selection = devcrew_cost.route(
            "reviewer",
            runtime={
                "models": [
                    {
                        "id": "strong-test",
                        "capabilities": ["strong"],
                        "efforts": ["low", "medium", "high"],
                    }
                ],
                "roles": {"reviewer-agent": {"available": True, "supports_overrides": True}},
                "generic_available": True,
                "explicit_model_effort": True,
            },
        )
        manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        review_contract = devcrew_phase.review_target(manifest, target["PHASE_ID"])
        phase = next(item for item in manifest["phases"] if item["phase_id"] == target["PHASE_ID"])
        evidence = list(phase["candidate_commit"]["evidence"])
        prepared = devcrew_context.reviewer_spawn(
            "T01",
            runtime={
                "models": [
                    {
                        "id": "strong-test",
                        "capabilities": ["strong"],
                        "efforts": ["low", "medium", "high"],
                    }
                ],
                "roles": {"reviewer-agent": {"available": True, "supports_overrides": True}},
                "generic_available": True,
                "explicit_model_effort": True,
            },
            selection=selection,
            project_root=self.host,
            phase_id=target["PHASE_ID"],
            base_sha=target["BASE_SHA"],
            target_sha=target["TARGET_SHA"],
            review_contract=review_contract,
            verification_evidence=evidence,
        )
        identity = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        receipt = devcrew_dispatch.record_spawn(
            self.host,
            {
                "STATUS": "SPAWN_SUCCESS",
                "REQUESTED_ROLE": "reviewer",
                **identity,
                "AGENT_ID": agent_id,
                "FORK_TURNS": prepared["spawn"]["fork_turns"],
                "SPAWN_MODEL": prepared["spawn"]["model"],
                "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
                "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
                "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                "SPAWN_PAYLOAD": prepared["spawn"],
                "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
            },
        )
        return {"SPAWN_RECEIPT_ID": receipt["SPAWN_RECEIPT_ID"], "AGENT_ID": agent_id}

    def accept_candidate(self, phase_id, candidate, report_name):
        self.submit_pass_review(phase_id, candidate, report_name)
        suite_record = self.run_full_suite_record("accept")
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            phase_id,
            self.verifier,
            [suite_record],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)

    def submit_pass_review(self, phase_id, candidate, report_name):
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, phase_id, self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": phase_id,
            "BASE_SHA": candidate["base"],
            "TARGET_SHA": candidate["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / report_name
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)

    def run_full_suite_record(self, name):
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "run-check",
                "--project",
                str(self.host),
                "--execution-root",
                str(self.task),
                "--task-id",
                "T01",
                "--name",
                name,
                "--",
                "python3",
                "-B",
                "-m",
                "unittest",
                "discover",
                "-s",
                "tests",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()[0]

    def leave_local_fix_transition_pending(self, report_name, *, after_reset=False):
        candidate = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / report_name
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        if after_reset:
            with mock.patch.object(
                devcrew_worktree,
                "advance_expected_head",
                side_effect=ValueError("simulated interruption"),
            ):
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.start_local_fix(
                        self.path,
                        self.manifest,
                        self.receipt,
                        self.host,
                        self.task,
                        "P1",
                        self.verifier,
                    )
        else:
            with mock.patch.object(
                devcrew_phase,
                "recover_local_fix",
                side_effect=devcrew_phase.PhaseError("INTERRUPTED", "simulated interruption"),
            ):
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.start_local_fix(
                        self.path,
                        self.manifest,
                        self.receipt,
                        self.host,
                        self.task,
                        "P1",
                        self.verifier,
                    )
        return candidate

    def enter_local_fix_with_history(self, label):
        rejected = self.make_candidate(candidate_text=f"rejected {label}\n")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        findings = [
            {
                "CLASSIFICATION": "LOCAL_FIX",
                "SUMMARY": f"repair {label}",
                "EVIDENCE": ["tests/test_widget.py:42"],
            }
        ]
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(VERDICT="FAIL", FINDINGS=findings)
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / f"review-failed-{label}.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        return rejected

    def enter_p2_local_fix_after_p1_repair(self, label):
        self.enter_local_fix_with_history(f"{label}-p1")
        p1_replacement = self.make_candidate("P1", candidate_text=f"accepted P1 {label}\n")
        self.accept_candidate("P1", p1_replacement, f"{label}-p1-pass.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        rejected = self.make_candidate("P2", candidate_text=f"rejected P2 {label}\n")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P2", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P2")
        findings = [
            {
                "CLASSIFICATION": "LOCAL_FIX",
                "SUMMARY": f"repair P2 {label}",
                "EVIDENCE": ["tests/test_widget.py:42"],
            }
        ]
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(VERDICT="FAIL", FINDINGS=findings)
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / f"{label}-p2-failed.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        return rejected

    def interrupt_p2_local_fix_after_reset(self, label):
        rejected = self.enter_p2_local_fix_after_p1_repair(label)
        with mock.patch.object(
            devcrew_worktree,
            "advance_expected_head",
            side_effect=ValueError("simulated interruption"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.start_local_fix(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P2",
                    self.verifier,
                )
        return rejected

    def leave_p2_local_fix_transition_before_reset(self, label):
        rejected = self.enter_p2_local_fix_after_p1_repair(label)
        with mock.patch.object(
            devcrew_phase,
            "recover_local_fix",
            side_effect=devcrew_phase.PhaseError("INTERRUPTED", "before reset"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.start_local_fix(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P2",
                    self.verifier,
                )
        return rejected

    def local_fix_state_snapshot(self):
        *_, state_path = devcrew_worktree.location(self.host, "T01")
        return {
            "head": self.git(self.task, "rev-parse", "HEAD"),
            "branch": self.git(self.task, "symbolic-ref", "--short", "HEAD"),
            "receipt": state_path.read_bytes(),
            "manifest": self.path.read_bytes(),
        }

    def rewrite_current_failed_candidate_base(self, wrong_base):
        manifest = json.loads(self.path.read_text())
        phase = next(
            item for item in manifest["phases"] if item["phase_id"] == manifest["current_phase_id"]
        )
        phase["base_sha"] = wrong_base
        phase["candidate_commit"]["base"] = wrong_base
        phase["review_report"]["base_sha"] = wrong_base
        report_path = self.host / phase["review_report"]["report_ref"]
        report = json.loads(report_path.read_text())
        report["BASE_SHA"] = wrong_base
        report_bytes = json.dumps(report).encode()
        report_path.write_bytes(report_bytes)
        phase["review_report"]["sha256"] = hashlib.sha256(report_bytes).hexdigest()
        transition = phase.get("local_fix_transition")
        if transition:
            transition["base_sha"] = wrong_base
            transition["cycle"]["candidate_commit"] = json.loads(
                json.dumps(phase["candidate_commit"])
            )
            transition["cycle"]["review_report"] = json.loads(json.dumps(phase["review_report"]))
        self.path.write_text(json.dumps(manifest))
        return manifest

    def test_review_target_is_fixed_to_candidate_commit_and_snapshot(self):
        candidate = self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        target = json.loads(started.stdout)["review_target"]
        self.assertEqual(target["TARGET_SHA"], candidate["sha"])
        self.assertEqual(target["BASE_SHA"], self.base)
        self.assertEqual(target["ACCEPTANCE"], ["one.txt changed"])
        self.assertEqual(target["OWNED_SCOPE"], ["one.txt"])
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), candidate["sha"])

    def test_pass_report_then_acceptance_unlocks_dependent_phase(self):
        candidate = self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        target = json.loads(started.stdout)["review_target"]
        report = self.host / "review.json"
        payload = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "TARGET_SHA": candidate["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        payload.update(self.routed_reviewer_fields(payload))
        report.write_text(json.dumps(payload))
        recorded = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-report",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        suite_record = self.run_full_suite_record("cli-accept")
        accepted = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "accept",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--evidence-record",
                suite_record,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        advanced = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "advance",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P2",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        manifest = json.loads(self.path.read_text())
        self.assertEqual(manifest["phases"][0]["status"], "ACCEPTED")
        self.assertEqual(manifest["phases"][0]["accepted_sha"], candidate["sha"])
        self.assertEqual(manifest["phases"][1]["base_sha"], candidate["sha"])
        second = self.make_candidate("P2")
        self.assertEqual(second["base"], candidate["sha"])
        self.assertEqual(
            self.git(self.task, "show", "-s", "--format=%P", second["sha"]), candidate["sha"]
        )

    def test_stale_report_target_is_rejected(self):
        candidate = self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        report = self.host / "review.json"
        report.write_text(
            json.dumps(
                {
                    "TASK_ID": "T01",
                    "PHASE_ID": "P1",
                    "BASE_SHA": self.base,
                    "TARGET_SHA": "0" * len(candidate["sha"]),
                    "VERDICT": "PASS",
                    "FINDINGS": [],
                }
            )
        )
        recorded = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-report",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(recorded.returncode, 0)
        self.assertIn("STALE_PHASE_REVIEW", recorded.stderr)

    def test_reviewing_candidate_rejects_post_candidate_worktree_diff(self):
        candidate = self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.assertEqual(
            json.loads(started.stdout)["review_target"]["TARGET_SHA"], candidate["sha"]
        )
        (self.task / "one.txt").write_text("post-candidate change\n")
        resumed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "resume",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(resumed.returncode, 0)
        self.assertIn("PHASE_COMMIT_INCONSISTENT", resumed.stderr)

    def test_review_resume_preserves_target_and_never_requests_executor(self):
        candidate = self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        target = json.loads(started.stdout)["review_target"]
        resumed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "resume",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        payload = json.loads(resumed.stdout)
        self.assertEqual(payload["status"], "PHASE_REVIEW_REQUIRED")
        self.assertTrue(payload["reviewer_required"])
        self.assertFalse(payload["executor_required"])
        self.assertEqual(target["TARGET_SHA"], candidate["sha"])
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), candidate["sha"])

    def test_fail_classifications_halt_and_preserve_immutable_candidate(self):
        for classification, expected_status in (
            ("LOCAL_FIX", "LOCAL_FIX_REQUIRED"),
            ("SYSTEMIC_REPLAN", "REPLAN_REQUIRED"),
            ("OWNER_DECISION_REQUIRED", "BLOCKED"),
        ):
            with self.subTest(classification=classification):
                self.setUp()
                candidate = self.make_candidate()
                devcrew_phase.begin_review(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P1",
                    self.verifier,
                )
                self.receipt = devcrew_worktree.resolve(self.host, "T01")
                self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
                target = devcrew_phase.review_target(self.manifest, "P1")
                report = {
                    key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
                }
                report.update(
                    VERDICT="FAIL",
                    FINDINGS=[
                        {
                            "CLASSIFICATION": classification,
                            "SUMMARY": "review finding",
                            "EVIDENCE": ["tests/test_widget.py:42"],
                        }
                    ],
                )
                report.update(self.routed_reviewer_fields(report))
                report_path = self.host / "review.json"
                report_path.write_text(json.dumps(report))
                devcrew_phase.submit_review(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    report,
                    self.verifier,
                    "review.json",
                    report_path.read_bytes(),
                )
                saved = json.loads(self.path.read_text())
                phase = saved["phases"][0]
                self.assertEqual(phase["status"], expected_status)
                self.assertEqual(phase["candidate_commit"]["sha"], candidate["sha"])
                self.assertEqual(
                    phase["review_report"]["findings"][0]["CLASSIFICATION"], classification
                )
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.executor_input(saved, self.task)
                fresh = devcrew_worktree.resolve(self.host, "T01")
                resumed = devcrew_phase.resume(
                    devcrew_phase.load_for_resume(self.path, fresh),
                    self.host,
                    self.task,
                    self.verifier,
                )
                self.assertEqual(
                    resumed["status"],
                    {
                        "LOCAL_FIX": "PHASE_LOCAL_FIX_REQUIRED",
                        "SYSTEMIC_REPLAN": "PHASE_REPLAN_REQUIRED",
                        "OWNER_DECISION_REQUIRED": "PHASE_BLOCKED",
                    }[classification],
                )
                self.assertFalse(resumed["planner_required"])
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.advance_phase(
                        self.path, saved, fresh, "P2", self.host, self.task, self.verifier
                    )
                self.manifest = saved

    def test_pass_review_accepts_same_sha_and_only_then_unlocks_dependency(self):
        candidate = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "TARGET_SHA": candidate["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "review.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            "review.json",
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [self.run_full_suite_record("accept-pass")],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        self.assertEqual(self.manifest["phases"][0]["status"], "ACCEPTED")
        self.assertEqual(self.manifest["phases"][0]["accepted_sha"], candidate["sha"])
        corrupted = json.loads(json.dumps(self.manifest))
        corrupted["phases"][0]["accepted_sha"] = "0" * len(candidate["sha"])
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(corrupted, self.receipt, self.task)
        self.assertEqual(caught.exception.code, "PHASE_ACCEPTANCE_INCONSISTENT")
        advanced = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        self.assertEqual(advanced["current_phase_id"], "P2")
        self.assertEqual(advanced["phases"][1]["base_sha"], candidate["sha"])
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        resumed = devcrew_phase.resume(
            devcrew_phase.load_for_resume(self.path, self.receipt),
            self.host,
            self.task,
            self.verifier,
        )
        self.assertEqual(resumed["executor_lifecycle"], "NEW_CONTEXT_REQUIRED")
        self.assertEqual(devcrew_context.evaluate({})["signal"], "CONTINUE")
        self.assertIsNone(devcrew_context.normalize_usage(None)["total_recorded_tokens"])
        self.manifest = devcrew_phase.set_phase_status(
            self.path,
            advanced,
            self.receipt,
            "P2",
            "EXECUTING",
            project=self.host,
            verifier=self.verifier,
        )
        self.manifest = devcrew_phase.set_phase_status(
            self.path,
            self.manifest,
            self.receipt,
            "P2",
            "VERIFYING",
            project=self.host,
            verifier=self.verifier,
        )
        record_path, record = run_check_api(
            self.host,
            execution_root=self.task,
            task_id="T01",
            name="unavailable-context-preflight",
            command=[sys.executable, "-B", "-c", "pass"],
        )
        blocked = devcrew_capacity.report(
            str(self.host), self._capacity_report("executor", "PHASE_EXECUTOR")
        )
        self.assertEqual(blocked["STATUS"], "AGENT_CAPACITY_BLOCKED")
        stored = devcrew_dispatch._read(devcrew_capacity.dispatch_receipt_path(self.host, "T01"))
        self.assertFalse(
            any(
                row.get("ROLE") == "executor" and row.get("PHASE_ID") == "P2"
                for row in stored["receipts"]
            )
        )
        before = self.path.read_bytes()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_DISPATCH_ROUTE"):
            devcrew_phase.set_phase_status(
                self.path,
                self.manifest,
                self.receipt,
                "P2",
                "READY_FOR_CLOSE",
                [str(record_path)],
                self.host,
                self.verifier,
                "missing-fresh-context-receipt",
            )
        self.assertEqual(self.path.read_bytes(), before)
        store_path = devcrew_capacity.dispatch_receipt_path(self.host, "T01")
        store = devcrew_dispatch._read(store_path)
        old_executor = next(
            row
            for row in store["receipts"]
            if row.get("ROLE") == "executor" and row.get("PHASE_ID") == "P1"
        )
        p2 = advanced["phases"][1]
        selection = devcrew_cost.route(
            "executor",
            runtime={
                "models": [
                    {
                        "id": "bounded-test",
                        "capabilities": ["bounded"],
                        "efforts": ["low", "medium"],
                    }
                ],
                "roles": {"executor-agent": {"available": True, "supports_overrides": True}},
                "generic_available": True,
                "explicit_model_effort": True,
            },
        )
        prepared = devcrew_context.routed_task_spawn(
            "executor_T01_P2",
            "Implement P2",
            selection,
            {
                "models": [
                    {
                        "id": "bounded-test",
                        "capabilities": ["bounded"],
                        "efforts": ["low", "medium"],
                    }
                ],
                "roles": {"executor-agent": {"available": True, "supports_overrides": True}},
                "generic_available": True,
                "explicit_model_effort": True,
            },
            task_id="T01",
            project_root=self.host,
            task_worktree_root=self.task,
            base_sha=p2["base_sha"],
            owned_scope=p2["owned_scope"],
            acceptance=p2["acceptance"],
            verification=p2["verification"],
            receipt=self.receipt,
            role_contract="Implement only P2.",
        )
        attempted = {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "executor",
            "TASK_ID": "T01",
            "PHASE_ID": "P2",
            "BASE_SHA": p2["base_sha"],
            "WRITER_CYCLE": 0,
            "REJECTED_CANDIDATE_SHA": None,
            "AGENT_ID": old_executor["AGENT_ID"],
            "FORK_TURNS": "none",
            "SPAWN_MODEL": prepared["spawn"]["model"],
            "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
            "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
            "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
            "SPAWN_PAYLOAD": prepared["spawn"],
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        }
        with self.assertRaisesRegex(devcrew_dispatch.DispatchError, "different phase"):
            devcrew_dispatch.record_spawn(self.host, attempted)
        # Simulate a tampered persisted receipt to exercise the read-time guard too.
        store = devcrew_dispatch._read(store_path)
        fake_receipt = {
            "SPAWN_RECEIPT_ID": "tampered-cross-phase",
            "ROUTE_ID": prepared["route"]["ROUTE_ID"],
            "ROLE": "executor",
            "RESOLVED_MODEL": attempted["SPAWN_MODEL"],
            "REASONING_EFFORT": attempted["SPAWN_REASONING_EFFORT"],
            "SPAWN_MODE": attempted["SPAWN_MODE"],
            "AGENT_ID": old_executor["AGENT_ID"],
            "TASK_ID": "T01",
            "PHASE_ID": "P2",
            "BASE_SHA": p2["base_sha"],
            "WRITER_CYCLE": 0,
            "REJECTED_CANDIDATE_SHA": None,
            "CONTRACT_GENERATION": 0,
            "FORK_TURNS": "none",
            "route": prepared["route"],
            "dispatch": prepared["dispatch"],
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        }
        store["receipts"].append(fake_receipt)
        next(
            row
            for row in store["issued_routes"]
            if row.get("ROUTE_ID") == prepared["route"]["ROUTE_ID"]
        )["consumed"] = True
        devcrew_dispatch._write(store_path, store)
        with self.assertRaisesRegex(devcrew_dispatch.DispatchError, "different phase"):
            devcrew_dispatch.verify_spawn(
                self.host,
                "tampered-cross-phase",
                task_id="T01",
                role="executor",
                phase_id="P2",
                base_sha=p2["base_sha"],
                agent_id=old_executor["AGENT_ID"],
            )
        fresh_p2 = self.routed_executor_fields("P2")
        self.assertNotEqual(fresh_p2["AGENT_ID"], old_executor["AGENT_ID"])
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                self.host,
                fresh_p2["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="executor",
                phase_id="P2",
                base_sha=p2["base_sha"],
                agent_id=fresh_p2["AGENT_ID"],
                writer_cycle=0,
                rejected_candidate_sha=None,
            )
        )

    def test_unrouted_pass_report_is_advisory_and_cannot_accept_phase(self):
        candidate = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "TARGET_SHA": candidate["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        path = self.host / "advisory-review.json"
        path.write_text(json.dumps(report))
        result = devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            path.name,
            path.read_bytes(),
        )
        self.assertEqual(result["status"], "PHASE_REVIEW_ADVISORY")
        self.assertEqual(result["authority"], "ADVISORY")
        before = self.path.read_bytes()
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REQUIRED"):
            devcrew_phase.accept_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )
        resumed = devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)
        self.assertEqual(resumed["status"], "PHASE_REVIEW_REQUIRED")
        self.assertEqual(self.path.read_bytes(), before)

    def test_advisory_fail_classifications_preserve_reviewing_manifest(self):
        for classification in ("LOCAL_FIX", "SYSTEMIC_REPLAN", "OWNER_DECISION_REQUIRED"):
            with self.subTest(classification=classification):
                self.setUp()
                candidate = self.make_candidate()
                devcrew_phase.begin_review(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P1",
                    self.verifier,
                )
                self.receipt = devcrew_worktree.resolve(self.host, "T01")
                self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
                target = devcrew_phase.review_target(self.manifest, "P1")
                report = {
                    key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
                }
                report.update(
                    VERDICT="FAIL",
                    FINDINGS=[
                        {
                            "CLASSIFICATION": classification,
                            "SUMMARY": "advisory only",
                            "EVIDENCE": ["tests/test_widget.py:42"],
                        }
                    ],
                )
                report_path = self.host / "advisory-fail.json"
                report_path.write_text(json.dumps(report))
                before = self.path.read_bytes()
                result = devcrew_phase.submit_review(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    report,
                    self.verifier,
                    report_path.name,
                    report_path.read_bytes(),
                )
                self.assertEqual(result["authority"], "ADVISORY")
                self.assertEqual(self.path.read_bytes(), before)
                saved = json.loads(before)
                phase = saved["phases"][0]
                self.assertEqual(phase["status"], "REVIEWING")
                self.assertIsNone(phase.get("review_report"))
                self.assertEqual(phase["candidate_commit"]["sha"], candidate["sha"])
                self.assertIsNotNone(phase["review_start"])

    def test_wrong_target_report_is_rejected_without_advancing_state(self):
        candidate = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "TARGET_SHA": "0" * len(candidate["sha"]),
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        report_path = self.host / "review.json"
        report_path.write_text(json.dumps(report))
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                "review.json",
                report_path.read_bytes(),
            )
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["phases"][0]["status"], "REVIEWING")
        self.assertEqual(saved["phases"][0]["candidate_commit"]["sha"], candidate["sha"])

    def test_candidate_is_frozen_while_reviewing(self):
        self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        (self.task / "one.txt").write_text("post-candidate change\n")
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.resume(
                devcrew_phase.load_for_resume(
                    self.path, devcrew_worktree.resolve(self.host, "T01")
                ),
                self.host,
                self.task,
                self.verifier,
            )
        self.assertIn(
            caught.exception.code, {"PHASE_COMMIT_INCONSISTENT", "PHASE_REVIEW_INCONSISTENT"}
        )

    def test_review_report_uses_verdict_and_preserves_finding_schema(self):
        self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        target = json.loads(started.stdout)["review_target"]
        report = self.host / "review.json"
        payload = {
            "TASK_ID": target["TASK_ID"],
            "PHASE_ID": target["PHASE_ID"],
            "BASE_SHA": target["BASE_SHA"],
            "TARGET_SHA": target["TARGET_SHA"],
            "VERDICT": "PASS",
            "FINDINGS": [],
            "ADVISORIES": [
                {
                    "SUMMARY": "Consider extracting the duplicate fixture.",
                    "EVIDENCE": ["tests/example.py:12"],
                }
            ],
        }
        payload.update(self.routed_reviewer_fields(payload))
        report.write_text(json.dumps(payload))
        submitted = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-report",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        saved = json.loads(self.path.read_text())["phases"][0]["review_report"]
        self.assertEqual(saved["verdict"], "PASS")
        self.assertEqual(saved["findings"], [])
        self.assertEqual(json.loads(submitted.stdout)["advisories"], payload["ADVISORIES"])
        self.assertEqual(saved["advisories"], payload["ADVISORIES"])
        self.assertEqual(saved["report_ref"], "review.json")
        self.assertEqual(len(saved["sha256"]), 64)

    def test_failed_review_preserves_findings_and_maps_local_fix_state(self):
        self.make_candidate()
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        target = json.loads(started.stdout)["review_target"]
        report = self.host / "review.json"
        findings = [
            {
                "CLASSIFICATION": "LOCAL_FIX",
                "SUMMARY": "Broken behavior",
                "EVIDENCE": ["tests/test_widget.py:42"],
            }
        ]
        payload = {
            "TASK_ID": target["TASK_ID"],
            "PHASE_ID": target["PHASE_ID"],
            "BASE_SHA": target["BASE_SHA"],
            "TARGET_SHA": target["TARGET_SHA"],
            "VERDICT": "FAIL",
            "FINDINGS": findings,
            "ADVISORIES": [
                {
                    "SUMMARY": "Consider reusing the fixture.",
                    "EVIDENCE": ["tests/test_widget.py:12"],
                }
            ],
        }
        payload.update(self.routed_reviewer_fields(payload))
        report.write_text(json.dumps(payload))
        submitted = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-report",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--report",
                str(report),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        phase = json.loads(self.path.read_text())["phases"][0]
        self.assertEqual(phase["status"], "LOCAL_FIX_REQUIRED")
        self.assertEqual(phase["review_report"]["findings"], findings)
        self.assertEqual(phase["review_report"]["advisories"], payload["ADVISORIES"])
        self.assertEqual(phase["review_report"]["report_ref"], "review.json")

    def test_local_fix_resume_dispatches_only_bounded_same_phase_contract(self):
        candidate = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        findings = [
            {
                "CLASSIFICATION": "LOCAL_FIX",
                "SUMMARY": "Broken behavior",
                "EVIDENCE": ["tests/test_widget.py:42"],
            }
        ]
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(VERDICT="FAIL", FINDINGS=findings)
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "review.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            "review.json",
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        failed = devcrew_phase.load_for_resume(self.path, self.receipt)
        resumed = devcrew_phase.resume(failed, self.host, self.task, self.verifier)
        self.assertEqual(resumed["status"], "PHASE_LOCAL_FIX_REQUIRED")
        self.assertFalse(resumed["planner_required"])
        self.assertTrue(resumed["executor_required"])
        self.assertEqual(resumed["executor_role"], "executor")
        self.assertEqual(
            resumed["local_fix_input"],
            {
                "phase_id": "P1",
                "findings": findings,
                "base_sha": self.base,
                "rejected_candidate_sha": candidate["sha"],
                "owned_scope": ["one.txt"],
                "acceptance": ["one.txt changed"],
                "verification": ["verify one.txt"],
            },
        )
        self.assertEqual(
            set(resumed["local_fix_input"]),
            {
                "phase_id",
                "findings",
                "base_sha",
                "rejected_candidate_sha",
                "owned_scope",
                "acceptance",
                "verification",
            },
        )

    def test_local_fix_replacement_candidate_uses_same_base_and_fresh_review(self):
        rejected = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        findings = [
            {
                "CLASSIFICATION": "LOCAL_FIX",
                "SUMMARY": "Broken behavior",
                "EVIDENCE": ["tests/test_widget.py:42"],
            }
        ]
        failed = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        failed.update(VERDICT="FAIL", FINDINGS=findings)
        failed.update(self.routed_reviewer_fields(failed))
        report_path = self.host / "review-failed.json"
        report_path.write_text(json.dumps(failed))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            failed,
            self.verifier,
            "review-failed.json",
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        fix = devcrew_phase.start_local_fix(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.assertEqual(fix["executor_role"], "executor")
        self.assertEqual(fix["executor_input"]["base_sha"], self.base)
        self.assertEqual(
            set(fix["executor_input"]),
            {
                "task_id",
                "phase_id",
                "base_sha",
                "rejected_candidate_sha",
                "findings",
                "owned_scope",
                "acceptance",
                "verification",
                "instruction",
            },
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        replacement = self.make_candidate()
        self.assertNotEqual(replacement["sha"], rejected["sha"])
        self.assertEqual(replacement["base"], self.base)
        phase = self.manifest["phases"][0]
        self.assertEqual(phase["review_cycles"][0]["candidate_commit"]["sha"], rejected["sha"])
        self.assertEqual(phase["review_cycles"][0]["review_report"]["target_sha"], rejected["sha"])
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        next_target = devcrew_phase.review_target(self.manifest, "P1")
        self.assertEqual(next_target["BASE_SHA"], self.base)
        self.assertEqual(next_target["TARGET_SHA"], replacement["sha"])
        passed = {
            key: next_target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        passed.update(VERDICT="PASS", FINDINGS=[])
        passed.update(self.routed_reviewer_fields(passed))
        pass_path = self.host / "review-passed.json"
        pass_path.write_text(json.dumps(passed))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            passed,
            self.verifier,
            "review-passed.json",
            pass_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [self.run_full_suite_record("accept-retry")],
        )
        self.assertEqual(
            json.loads(self.path.read_text())["phases"][0]["accepted_sha"], replacement["sha"]
        )

    def _submit_local_fail(self, name, *, existing_candidate=False, classification="LOCAL_FIX"):
        candidate = (
            self.manifest["phases"][0]["candidate_commit"]
            if existing_candidate
            else self.make_candidate()
        )
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": candidate["base"],
            "TARGET_SHA": candidate["sha"],
            "VERDICT": "FAIL",
            "FINDINGS": [
                {
                    "CLASSIFICATION": classification,
                    "SUMMARY": "Disputed content assertion",
                    "EVIDENCE": ["one.txt:1"],
                }
            ],
        }
        report.update(self.routed_reviewer_fields(report))
        path = self.host / (name + ".json")
        path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            path.name,
            path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        return candidate, path, path.read_bytes()

    def progress_assessment(self):
        phase = self.manifest["phases"][0]
        text = (self.task / "one.txt").read_text()
        command = [
            sys.executable,
            "-B",
            "-c",
            "from pathlib import Path; assert Path('one.txt').read_text() == " + repr(text),
        ]
        check = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "run-check",
                "--project",
                str(self.host),
                "--execution-root",
                str(self.task),
                "--task-id",
                "T01",
                "--name",
                "progress-proof",
                "--",
                *command,
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return {
            "task_id": "T01",
            "phase_id": "P1",
            "target_sha": phase["candidate_commit"]["sha"],
            "report_sha256": phase["review_report"]["sha256"],
            "reason": "Recorded check establishes the exact candidate content",
            "next_action": "Correct only the evidenced finding or reconsider the mistaken assertion",
            "expected_result": "The bounded content assertion and phase acceptance agree",
            "cost_rationale": "Reuse the candidate and perform only the bounded check",
            "evidence_records": [check.stdout.splitlines()[0]],
        }

    def test_wrong_review_is_reconsidered_on_same_candidate_without_owner_or_writer(self):
        rejected, _, _ = self._submit_local_fail("mistaken-review")
        assessment = self.progress_assessment()
        assessment_path = self.host / "assessment.json"
        assessment_path.write_text(json.dumps(assessment))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-reconsider",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--continuation",
                str(assessment_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), rejected["sha"])
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        phase = self.manifest["phases"][0]
        self.assertEqual(phase["status"], "COMMITTED")
        self.assertEqual(phase["review_cycles"][0]["review_report"]["verdict"], "FAIL")
        self.assertNotIn("review_report", phase)
        self.assertNotIn("owner_decision", phase["review_cycles"][0])
        self.submit_pass_review("P1", rejected, "corrected-review.json")
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        accepted = devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [self.run_full_suite_record("accept-reconsidered")],
        )
        self.assertEqual(accepted["status"], "PHASE_ACCEPTED")

    def test_continuation_rejects_stale_or_empty_evidence_without_changing_state(self):
        self._submit_local_fail("invalid-progress")
        assessment = self.progress_assessment()
        for change in (
            {"target_sha": self.base},
            {"report_sha256": "a" * 64},
            {"evidence_records": []},
            {"cost_rationale": ""},
        ):
            with self.subTest(change=change):
                before = self.local_fix_state_snapshot()
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.reconsider_review(
                        self.path,
                        self.manifest,
                        self.receipt,
                        self.host,
                        self.task,
                        "P1",
                        self.verifier,
                        {**assessment, **change},
                    )
                self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_disproven_owner_finding_can_be_reconsidered_without_owner_answer(self):
        self._submit_local_fail("disputed-owner", classification="OWNER_DECISION_REQUIRED")
        assessment = self.progress_assessment()
        result = devcrew_phase.reconsider_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            assessment,
        )
        self.assertFalse(result["owner_decision_required"])
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), assessment["target_sha"])

    def test_changed_archived_progress_record_blocks_resume(self):
        self._submit_local_fail("archived-progress")
        assessment = self.progress_assessment()
        devcrew_phase.reconsider_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            assessment,
        )
        proof = pathlib.Path(assessment["evidence_records"][0])
        record = json.loads(proof.read_text())
        record["name"] = "changed after archive"
        proof.write_text(json.dumps(record))
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)

    def test_repeated_same_tree_check_and_reworded_reason_cannot_reopen_review(self):
        self._submit_local_fail("same-proof-first")
        assessment = self.progress_assessment()
        devcrew_phase.reconsider_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            assessment,
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        # A second FAIL on the same SHA is not resolved by rerunning the same assertion.
        self._submit_local_fail("same-proof-second", existing_candidate=True)
        repeated = self.progress_assessment()
        repeated["reason"] = "A renamed explanation does not supply new facts"
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "LOOP_DETECTED"):
            devcrew_phase.reconsider_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                "P1",
                self.verifier,
                repeated,
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_repeated_local_fix_uses_verified_progress_without_owner_each_cycle(self):
        rejected = []
        for cycle_id in (1, 2, 3):
            candidate = self.make_candidate(candidate_text=f"candidate cycle {cycle_id}\n")
            rejected.append(candidate)
            devcrew_phase.begin_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )
            self.receipt = devcrew_worktree.resolve(self.host, "T01")
            self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
            target = devcrew_phase.review_target(self.manifest, "P1")
            findings = [
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": f"finding {cycle_id}",
                    "EVIDENCE": [f"tests/test_widget.py:{40 + cycle_id}"],
                }
            ]
            report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
            report.update(VERDICT="FAIL", FINDINGS=findings)
            report.update(self.routed_reviewer_fields(report))
            report_path = self.host / f"review-fail-{cycle_id}.json"
            report_path.write_text(json.dumps(report))
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                report_path.name,
                report_path.read_bytes(),
            )
            self.receipt = devcrew_worktree.resolve(self.host, "T01")
            self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
            if cycle_id > 1:
                resumed = devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)
                self.assertEqual(resumed["status"], "PHASE_CONTINUATION_REQUIRED")
                self.assertFalse(resumed["executor_required"])
                self.assertFalse(resumed["planner_required"])
                self.assertFalse(resumed["owner_decision_required"])
                before = self.local_fix_state_snapshot()
                blocked = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/devcrew.py"),
                        "phase",
                        "local-fix-start",
                        "--project",
                        str(self.host),
                        "--task-id",
                        "T01",
                        "--phase-id",
                        "P1",
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(blocked.returncode, 0)
                self.assertIn("CONTINUATION_REQUIRED", blocked.stdout + blocked.stderr)
                self.assertEqual(self.local_fix_state_snapshot(), before)
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "LOOP_DETECTED"):
                    devcrew_phase.start_local_fix(
                        self.path,
                        self.manifest,
                        self.receipt,
                        self.host,
                        self.task,
                        "P1",
                        self.verifier,
                        owner_decision="  ",
                    )
            assessment = self.progress_assessment() if cycle_id > 1 else None
            if cycle_id == 2:
                with mock.patch.object(
                    devcrew_phase,
                    "recover_local_fix",
                    side_effect=devcrew_phase.PhaseError("INTERRUPTED", "test interruption"),
                ):
                    with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTERRUPTED"):
                        devcrew_phase.start_local_fix(
                            self.path,
                            self.manifest,
                            self.receipt,
                            self.host,
                            self.task,
                            "P1",
                            self.verifier,
                            continuation=assessment,
                        )
                pending_bytes = self.path.read_bytes()
                pending = json.loads(pending_bytes)
                pending["phases"][0]["local_fix_transition"]["cycle"].pop("continuation")
                self.path.write_text(json.dumps(pending))
                before = self.local_fix_state_snapshot()
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "CONTINUATION_REQUIRED"):
                    devcrew_phase.recover_local_fix(self.host, "T01", verifier=self.verifier)
                self.assertEqual(self.local_fix_state_snapshot(), before)
                self.path.write_bytes(pending_bytes)
                devcrew_phase.recover_local_fix(self.host, "T01", verifier=self.verifier)
            elif cycle_id == 3:
                assessment_path = self.host / "continuation.json"
                assessment_path.write_text(json.dumps(assessment))
                continued = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/devcrew.py"),
                        "phase",
                        "local-fix-start",
                        "--project",
                        str(self.host),
                        "--task-id",
                        "T01",
                        "--phase-id",
                        "P1",
                        "--continuation",
                        str(assessment_path),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(continued.returncode, 0, continued.stdout + continued.stderr)
            else:
                devcrew_phase.start_local_fix(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P1",
                    self.verifier,
                )
            self.receipt = devcrew_worktree.resolve(self.host, "T01")
            self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        current = devcrew_phase.load_for_resume(
            self.path, devcrew_worktree.resolve(self.host, "T01")
        )
        history = current["phases"][0]["review_cycles"]
        self.assertEqual([item["cycle_id"] for item in history], [1, 2, 3])
        self.assertEqual(
            [item["candidate_commit"]["sha"] for item in history],
            [item["sha"] for item in rejected],
        )
        self.assertEqual(
            [item["review_report"]["target_sha"] for item in history],
            [item["sha"] for item in rejected],
        )
        self.assertEqual(
            [item["review_report"]["findings"][0]["SUMMARY"] for item in history],
            ["finding 1", "finding 2", "finding 3"],
        )
        self.assertNotIn("owner_decision", history[0])
        for cycle in history[1:]:
            self.assertNotIn("owner_decision", cycle)
            self.assertEqual(cycle["candidate_commit"]["sha"], cycle["continuation"]["target_sha"])

    def _capacity_prepared(self, role):
        return {
            "route": {
                "ROUTE_ID": f"route-{role}-T01",
                "ROLE": role,
                "TASK_CLASS": "IMPLEMENTATION",
                "COORDINATION_COMPLEXITY": None,
                "DECISION_COMPLEXITY": None,
                "IMPLEMENTATION_COMPLEXITY": "NORMAL",
                "REVIEW_COMPLEXITY": None,
                "RISK": "low",
                "MODEL_CLASS": "bounded",
                "RESOLVED_MODEL": "bounded-test",
                "REASONING_EFFORT": "medium",
                "SELECTION_REASON": "normal_implementation",
            },
            "dispatch": {"SPAWN_MODE": "generic"},
        }

    def _capacity_report(self, role, stage):
        runtime = {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
                {
                    "id": "strong-test",
                    "capabilities": ["strong"],
                    "efforts": ["low", "medium", "high"],
                },
            ],
            "roles": {
                name: {"available": True, "supports_overrides": True}
                for name in ("executor-agent", "reviewer-agent")
            },
            "generic_available": True,
            "explicit_model_effort": True,
        }
        if role == "executor":
            phase_input = devcrew_phase.executor_input(
                self.manifest, self.task, self.host, self.verifier
            )
            active = next(item for item in self.manifest["phases"] if item["phase_id"] == "P1").get(
                "active_local_fix"
            )
            prepared = devcrew_context.routed_task_spawn(
                "executor_T01_capacity",
                "Resume current phase",
                devcrew_cost.route("executor", runtime=runtime),
                runtime,
                task_id="T01",
                project_root=self.host.resolve(),
                task_worktree_root=phase_input.get("task_worktree_root", str(self.task)),
                base_sha=phase_input["base_sha"],
                owned_scope=phase_input["owned_scope"],
                acceptance=phase_input["acceptance"],
                verification=phase_input["verification"],
                receipt=self.receipt,
                role_contract="Execute only the active phase contract.",
            )
            self.receipt = devcrew_worktree.resolve(self.host, "T01")
            identity = {
                "TASK_ID": "T01",
                "PHASE_ID": phase_input["phase_id"],
                "BASE_SHA": phase_input["base_sha"],
                "WRITER_CYCLE": active["cycle_id"] if active else 0,
                "REJECTED_CANDIDATE_SHA": phase_input.get("rejected_candidate_sha"),
            }
        else:
            target = devcrew_phase.review_target(self.manifest, "P1")
            phase = next(item for item in self.manifest["phases"] if item["phase_id"] == "P1")
            evidence = list(phase["candidate_commit"]["evidence"])
            prepared = devcrew_context.reviewer_spawn(
                "T01",
                runtime=runtime,
                selection=devcrew_cost.route("reviewer", runtime=runtime),
                project_root=self.host.resolve(),
                phase_id="P1",
                base_sha=target["BASE_SHA"],
                target_sha=target["TARGET_SHA"],
                review_contract=target,
                verification_evidence=evidence,
            )
            identity = {
                key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
            }
        payload = prepared["spawn"]
        return {
            "STATUS": "SPAWN_FAILED",
            "REQUESTED_ROLE": role,
            **identity,
            "RUNTIME_ERROR": devcrew_capacity.CAPACITY_ERROR,
            "TASK_ID": "T01",
            "HOST_PROJECT_ROOT": str(self.host.resolve()),
            "TASK_WORKTREE_ROOT": str(self.task.resolve()),
            "CURRENT_STAGE": stage,
            "ATTEMPT_NUMBER": 1,
            "BLOCKER_ID": None,
            "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
            "SPAWN_PAYLOAD": payload,
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
            "SPAWN_MODEL": payload["model"],
            "SPAWN_REASONING_EFFORT": payload["reasoning_effort"],
            "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
            "FORK_TURNS": payload["fork_turns"],
            "HOST_INITIATED": True,
        }

    def test_capacity_during_local_fix_resumes_executor_stage_only(self):
        rejected = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "review-local-fix.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        blocked = devcrew_capacity.report(
            str(self.host), self._capacity_report("executor", "LOCAL_FIX_EXECUTOR")
        )
        resumed = devcrew_capacity.resume(str(self.host), "T01", host_initiated=True)
        self.assertEqual(blocked["CURRENT_STAGE"], "LOCAL_FIX_EXECUTOR")
        self.assertEqual(
            (resumed["REQUESTED_ROLE"], resumed["CURRENT_STAGE"]),
            ("executor", "LOCAL_FIX_EXECUTOR"),
        )
        self.assertEqual(
            devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)["status"],
            "PHASE_LOCAL_FIX_EXECUTOR_REQUIRED",
        )
        dispatch = devcrew_phase.executor_input(self.manifest, self.task, self.host, self.verifier)
        self.assertEqual(dispatch["rejected_candidate_sha"], rejected["sha"])
        self.assertEqual(dispatch["base_sha"], self.base)
        self.assertEqual(resumed["REQUESTED_ROLE"], "executor")

    def test_capacity_during_replacement_review_resumes_exact_reviewer_target(self):
        rejected = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        old_target = devcrew_phase.review_target(self.manifest, "P1")
        fail_report = {
            key: old_target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        fail_report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        fail_report.update(self.routed_reviewer_fields(fail_report))
        failed_path = self.host / "review-fail-capacity.json"
        failed_path.write_text(json.dumps(fail_report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            fail_report,
            self.verifier,
            failed_path.name,
            failed_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        replacement = self.make_candidate(candidate_text="replacement under capacity test\n")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_capacity.report(
            str(self.host), self._capacity_report("reviewer", "REPLACEMENT_REVIEW")
        )
        resumed = devcrew_capacity.resume(str(self.host), "T01", host_initiated=True)
        resumed_phase = devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)
        self.assertEqual(
            (resumed["REQUESTED_ROLE"], resumed["CURRENT_STAGE"]),
            ("reviewer", "REPLACEMENT_REVIEW"),
        )
        self.assertEqual(resumed_phase["status"], "PHASE_REVIEW_REQUIRED")
        self.assertFalse(resumed_phase["executor_required"])
        self.assertEqual(
            devcrew_phase.review_target(self.manifest, "P1")["TARGET_SHA"], replacement["sha"]
        )
        self.assertNotEqual(replacement["sha"], rejected["sha"])

    def test_local_fix_active_contract_does_not_bypass_stop_or_close_status(self):
        self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "review-dispatch-stop.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        verifying = devcrew_phase.set_phase_status(
            self.path,
            json.loads(json.dumps(self.manifest)),
            self.receipt,
            "P1",
            "VERIFYING",
            project=self.host,
            verifier=self.verifier,
        )
        ready = devcrew_phase.set_phase_status(
            self.path,
            verifying,
            self.receipt,
            "P1",
            "READY_FOR_CLOSE",
            [self.run_full_suite_record("local-fix-close")],
            self.host,
            self.verifier,
            self.routed_executor_fields("P1")["SPAWN_RECEIPT_ID"],
        )
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_READY_FOR_CLOSE"):
            devcrew_phase.executor_input(ready, self.task, self.host, self.verifier)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_MANIFEST_CONFLICT"):
            devcrew_phase.set_phase_status(
                self.path,
                self.manifest,
                self.receipt,
                "P1",
                "BLOCKED",
                project=self.host,
                verifier=self.verifier,
            )
        # Exercise dispatch gates on valid alternative states without overwriting
        # the READY_FOR_CLOSE manifest with the old EXECUTING snapshot.
        blocked = json.loads(json.dumps(self.manifest))
        blocked["phases"][0]["status"] = "BLOCKED"
        devcrew_phase.validate_manifest(blocked, self.receipt, self.task)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_NOT_DISPATCHABLE"):
            devcrew_phase.executor_input(blocked, self.task, self.host, self.verifier)
        replan = json.loads(json.dumps(blocked))
        replan["phases"][0]["status"] = "REPLAN_REQUIRED"
        devcrew_phase.validate_manifest(replan, self.receipt, self.task)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_NOT_DISPATCHABLE"):
            devcrew_phase.executor_input(replan, self.task, self.host, self.verifier)

    def test_interrupted_local_fix_reset_recovers_lifecycle_receipt_and_manifest(self):
        rejected = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "review-interrupt.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        with mock.patch.object(
            devcrew_worktree,
            "advance_expected_head",
            side_effect=ValueError("simulated process interruption"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.start_local_fix(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P1",
                    self.verifier,
                )
        pending = json.loads(self.path.read_text())["phases"][0]["local_fix_transition"]
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        recovered = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "resume",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(
            json.loads(recovered.stdout)["status"], "PHASE_LOCAL_FIX_EXECUTOR_REQUIRED"
        )
        receipt = devcrew_worktree.resolve(self.host, "T01")
        manifest = devcrew_phase.load_for_resume(self.path, receipt)
        self.assertEqual(receipt["expected_head"], self.base)
        self.assertEqual(manifest["phases"][0]["status"], "EXECUTING")
        self.assertEqual(
            manifest["phases"][0]["active_local_fix"]["rejected_candidate_sha"], rejected["sha"]
        )
        self.assertEqual(
            manifest["phases"][0]["review_cycles"][0]["backup_ref"], pending["backup_ref"]
        )

    def _systemic_replan_fixture(self):
        rejected = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "SYSTEMIC_REPLAN",
                    "SUMMARY": "phase contract is incomplete",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(self.routed_reviewer_fields(report))
        report_path = (
            self.host
            / f"systemic-review-{len(self.manifest['phases'][0].get('review_cycles', [])) + 1}.json"
        )
        report_path.write_text(json.dumps(report))
        original_bytes = report_path.read_bytes()
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            original_bytes,
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        replacement = json.loads(json.dumps(self.manifest))
        replacement.pop("planner_dispatch", None)
        phase = replacement["phases"][0]
        for field in (
            "candidate_commit",
            "review_report",
            "review_start",
            "evidence",
            "review_cycles",
        ):
            phase.pop(field, None)
        phase["status"] = "PLANNED"
        phase["goal"] = "Implement the corrected health endpoint contract"
        phase["main_scenario"] = "GET /health returns the corrected health response."
        phase["acceptance"] = ["Correct health response is returned"]
        replacement["phases"][1:] = json.loads(json.dumps(self.manifest["phases"][1:]))
        planner = issue_planner_receipt(
            self.host,
            "T01",
            rejected["sha"],
            "Replace the current phase contract",
            "planner-replan",
        )
        return rejected, report_path, original_bytes, replacement, planner["SPAWN_RECEIPT_ID"]

    def _v2_intent(self, manifest, previous=None, *, explicit_obligation=False):
        source_file = pathlib.Path(self.temp.name) / "intent-source.txt"
        source_file.write_text("The checkout option must remain available.", encoding="utf-8")
        source = devcrew_phase.bind_intent_source(
            self.host,
            self.receipt,
            {
                "mode": "file",
                "source_type": "document",
                "path": str(source_file),
                "expected_sha256": hashlib.sha256(source_file.read_bytes()).hexdigest(),
                "origin": "test source",
            },
        )
        source_bytes = (self.task / source["snapshot"]).read_bytes()
        fragment = {
            "source_id": source["source_id"],
            "start_byte": 0,
            "end_byte": len(source_bytes),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        }
        acceptance = manifest["phases"][0]["acceptance"][0]
        requirement = {
            "requirement_id": "F1",
            "kind": "explicit_user_obligation" if explicit_obligation else "established_fact",
            "statement": "The checkout option remains available.",
            "source_fragments": [fragment],
            "acceptance_refs": (
                [
                    {
                        "phase_id": manifest["phases"][0]["phase_id"],
                        "acceptance_index": 0,
                        "sha256": hashlib.sha256(acceptance.encode()).hexdigest(),
                    }
                ]
                if explicit_obligation
                else []
            ),
            "evidence_records": [],
        }
        intent = {
            "sources": [source],
            "requirements": [requirement],
            "decisions": [],
            "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
            "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
        }
        manifest["schema_version"] = 2
        manifest["original_intent"] = intent
        current_digest = devcrew_phase.intent_plan_digest(manifest)
        assessment = {
            "task_id": manifest["task_id"],
            "plan_sha256": current_digest,
            "previous_plan_sha256": previous,
            "mode": "IMPLEMENTATION",
            "result": "COMPLETE",
            "source_reads": [
                {
                    "source_id": source["source_id"],
                    "sha256": source["sha256"],
                    "findings": "The source states the checkout requirement.",
                }
            ],
            "unavailable_sources": [],
            "source_outcomes": [
                {
                    "source_fragments": [fragment],
                    "expected_outcome": "The checkout option remains available.",
                    "requirement_ids": ["F1"],
                    "disposition": "COVERED",
                    "rationale": "The accepted phase covers this established fact.",
                }
            ],
            "obligation_checks": (
                [
                    {
                        "requirement_id": "F1",
                        "change": "PRESERVED",
                        "preserved_outcome": "The checkout option remains available.",
                        "safety_rationale": "The required choice stays available.",
                        "acceptance_rationale": "The phase acceptance maps the obligation.",
                        "decision_id": None,
                    }
                ]
                if explicit_obligation
                else []
            ),
            "technical_change_rationale": "The coordinator read the source and checked the phase contract.",
        }
        if previous is None:
            intent["planning_assessment"] = {
                "status": "ASSESSED",
                "assessment": assessment,
                "history": [],
            }
        return intent, assessment

    def _make_v2_systemic_state(self, replacement, *, explicit_obligation=False):
        # Promote only this isolated lifecycle fixture to v2; production task state
        # is never initialized or reapproved by this regression helper.
        # A normal schema-v2 initialization pins every phase. P1 already has its
        # candidate-bound pin; pin untouched future phases before deriving intent.
        for phase in self.manifest["phases"][1:]:
            if "verification_sha256" not in phase:
                devcrew_phase.pin_verification(phase)
        intent, old_assessment = self._v2_intent(
            self.manifest, explicit_obligation=explicit_obligation
        )
        self.manifest["original_intent"] = intent
        self.path.write_text(json.dumps(self.manifest))
        replacement["phases"][1:] = json.loads(json.dumps(self.manifest["phases"][1:]))
        devcrew_phase.pin_verification(replacement["phases"][0])
        replacement["schema_version"] = 2
        replacement["original_intent"] = json.loads(json.dumps(intent))
        previous_digest = devcrew_phase.intent_plan_digest(self.manifest)
        plan_digest = devcrew_phase.intent_plan_digest(replacement)
        authorization_assessment = json.loads(json.dumps(old_assessment))
        authorization_assessment["plan_sha256"] = plan_digest
        authorization_assessment["previous_plan_sha256"] = previous_digest
        authorization = {"assessment": authorization_assessment, "decisions": []}
        # _v2_intent leaves the replacement intent pending, which is the planner
        # form accepted by the host merge.
        replacement["original_intent"] = json.loads(json.dumps(intent))
        return authorization

    def _v2_final_review_fixture(self, *, remove_last_obligation=False):
        proposed = json.loads(json.dumps(self.manifest))
        proposed.pop("manifest_revision", None)
        proposed["task_mode"] = "SINGLE_PHASE"
        proposed["semantic_contract"] = {"capabilities": ["checkout"]}
        proposed["phases"] = [proposed["phases"][0]]
        proposed["schema_version"] = 2
        devcrew_phase.pin_verification(proposed["phases"][0])
        intent, assessment = self._v2_intent(proposed, explicit_obligation=True)
        intent["planning_assessment"] = {"status": "NOT_ASSESSED", "assessment": None}
        if remove_last_obligation:
            answer_file = pathlib.Path(self.temp.name) / "intent-removal-answer.txt"
            answer_file.write_text("I withdraw the checkout requirement.", encoding="utf-8")
            answer = devcrew_phase.bind_intent_source(
                self.host,
                self.receipt,
                {
                    "mode": "file",
                    "source_type": "owner_answer",
                    "path": str(answer_file),
                    "expected_sha256": hashlib.sha256(answer_file.read_bytes()).hexdigest(),
                    "origin": "explicit owner answer",
                },
            )
            answer_bytes = (self.task / answer["snapshot"]).read_bytes()
            answer_fragment = {
                "source_id": answer["source_id"],
                "start_byte": 0,
                "end_byte": len(answer_bytes),
                "sha256": hashlib.sha256(answer_bytes).hexdigest(),
            }
            requirement = intent["requirements"][0]
            decision = {
                "decision_id": "D_REMOVE_F1",
                "source_fragments": [answer_fragment],
                "changes": [
                    {
                        "requirement_id": "F1",
                        "before": {
                            key: requirement[key]
                            for key in ("kind", "statement", "source_fragments")
                        },
                        "after": None,
                    }
                ],
                "human_confirmation": {
                    "actor": "user",
                    "channel": "external_owner_message",
                    "interpretation": "The user withdrew the checkout requirement.",
                    "authority_rationale": "The answer source records the explicit owner decision.",
                },
            }
            intent["sources"].append(answer)
            intent["decisions"] = [decision]
            assessment["source_reads"].append(
                {
                    "source_id": answer["source_id"],
                    "sha256": answer["sha256"],
                    "findings": "The owner answer explicitly withdraws the sole requirement.",
                }
            )
            assessment["source_outcomes"] = []
            assessment["obligation_checks"] = [
                {
                    "requirement_id": "F1",
                    "change": "USER_REMOVED",
                    "preserved_outcome": "The owner withdrew the checkout requirement.",
                    "safety_rationale": "No replacement product behavior was invented.",
                    "acceptance_rationale": "No active obligation remains.",
                    "decision_id": "D_REMOVE_F1",
                }
            ]
            proposed["original_intent"] = json.loads(json.dumps(intent))
            assessed = json.loads(json.dumps(proposed))
            assessed["original_intent"]["decisions"] = [decision]
            assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(assessed)
            intent["planning_assessment"] = {"status": "NOT_ASSESSED", "assessment": None}
        proposed["original_intent"] = json.loads(json.dumps(intent))
        proposed["original_intent"]["decisions"] = []
        proposed["original_intent"]["planning_assessment"] = {
            "status": "NOT_ASSESSED",
            "assessment": None,
        }
        self.path.unlink()
        self.manifest = devcrew_phase.initialize_manifest(
            self.path,
            proposed,
            self.receipt,
            self.host,
            proposed["planner_dispatch"]["spawn_receipt_id"],
            intent_assessment={
                "assessment": assessment,
                "decisions": json.loads(json.dumps(intent["decisions"])),
            },
        )
        candidate = self.make_candidate(
            "P1", candidate_text="final implementation preserves checkout\n"
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        target = devcrew_phase.review_target(self.manifest, "P1")
        route = self.routed_reviewer_fields(target)
        evidence = list(self.manifest["phases"][0]["candidate_commit"]["evidence"])
        reads = assessment["source_reads"]
        outcomes = json.loads(json.dumps(assessment["source_outcomes"]))
        if remove_last_obligation:
            removal_fragments = []
            for source in self.manifest["original_intent"]["sources"]:
                source_path = self.task / source["snapshot"]
                source_bytes = source_path.read_bytes()
                removal_fragments.append(
                    {
                        "source_id": source["source_id"],
                        "start_byte": 0,
                        "end_byte": len(source_bytes),
                        "sha256": hashlib.sha256(source_bytes).hexdigest(),
                    }
                )
            outcomes = [
                {
                    "source_fragments": removal_fragments,
                    "expected_outcome": "The owner withdrew the sole checkout requirement.",
                    "requirement_ids": ["F1"],
                    "disposition": "COVERED",
                    "rationale": "The original requirement and its actual removal answer are both accounted for.",
                    "evidence_records": evidence,
                }
            ]
        for outcome in outcomes:
            outcome["evidence_records"] = evidence
        checks = [
            {
                "requirement_id": row["requirement_id"],
                "result": "VERIFIED",
                "rationale": row["preserved_outcome"] + " " + row["acceptance_rationale"],
                "evidence_records": evidence,
            }
            for row in assessment["obligation_checks"]
            if devcrew_phase.effective_intent_requirements(self.manifest)[row["requirement_id"]]
            is not None
        ]
        referenced = set(evidence)
        digests = {
            reference: hashlib.sha256((self.host / reference).read_bytes()).hexdigest()
            for reference in referenced
        }
        intent_review = {
            "plan_sha256": devcrew_phase.intent_plan_digest(self.manifest),
            "result": "COMPLETE",
            "source_reads": reads,
            "unavailable_sources": [],
            "decision_ids": [
                row["decision_id"] for row in self.manifest["original_intent"]["decisions"]
            ],
            "source_outcomes": outcomes,
            "obligation_checks": checks,
            "evidence_sha256": digests,
        }
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": candidate["base"],
            "TARGET_SHA": candidate["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
            "INTENT_REVIEW": intent_review,
            **route,
        }
        return candidate, report

    def test_final_v2_pass_requires_fresh_complete_intent_review_and_accepts_task(self):
        candidate, report = self._v2_final_review_fixture()
        report_path = self.host / "v2-final-intent-review.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        suite = self.run_full_suite_record("v2-final-acceptance")
        accepted = devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [suite],
        )
        self.assertEqual(accepted["status"], "PHASE_ACCEPTED")
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        saved = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        self.assertEqual(
            saved["original_intent"]["task_acceptance"],
            {
                "status": "ACCEPTED",
                "review": {
                    "phase_id": "P1",
                    "report_sha256": report_path.read_bytes()
                    and hashlib.sha256(report_path.read_bytes()).hexdigest(),
                },
            },
        )
        resumed = devcrew_phase.resume(saved, self.host, self.task, self.verifier)
        self.assertEqual(resumed["status"], "PHASE_ACCEPTED")
        self.assertEqual(resumed["task_status"], "TASK_DONE")
        report_path.write_text(report_path.read_text() + " ")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.resume(saved, self.host, self.task, self.verifier)

    def test_final_v2_acceptance_allows_evidence_backed_sole_requirement_removal(self):
        candidate, report = self._v2_final_review_fixture(remove_last_obligation=True)
        report_path = self.host / "v2-final-removed-obligation.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        suite = self.run_full_suite_record("v2-final-removed-obligation-acceptance")
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [suite],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        saved = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        self.assertIsNone(devcrew_phase.effective_intent_requirements(saved)["F1"])
        self.assertEqual(saved["original_intent"]["task_acceptance"]["status"], "ACCEPTED")
        resumed = devcrew_phase.resume(saved, self.host, self.task, self.verifier)
        self.assertEqual(resumed["task_status"], "TASK_DONE")

    def test_final_v2_removal_review_requires_both_original_and_answer_fragments(self):
        _candidate, report = self._v2_final_review_fixture(remove_last_obligation=True)
        report["INTENT_REVIEW"]["source_outcomes"][0]["source_fragments"].pop()
        report_path = self.host / "v2-final-removed-obligation-missing-answer.json"
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "removed obligation source fragment"):
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                report_path.name,
                report_path.read_bytes(),
            )

    def test_final_v2_removal_cannot_be_omitted_from_complete_review(self):
        _candidate, report = self._v2_final_review_fixture(remove_last_obligation=True)
        report["INTENT_REVIEW"]["source_outcomes"][0]["disposition"] = "OMITTED"
        report_path = self.host / "v2-final-removed-obligation-omitted.json"
        report_path.write_text(json.dumps(report))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "COMPLETE requires all sources"):
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                report_path.name,
                report_path.read_bytes(),
            )

    def test_final_v2_resume_revalidates_reviewer_receipt_and_issuance(self):
        candidate, report = self._v2_final_review_fixture()
        report_path = self.host / "v2-final-intent-review-receipt.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        suite = self.run_full_suite_record("v2-final-receipt-acceptance")
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [suite],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        saved = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        store_path = devcrew_capacity.dispatch_receipt_path(self.host, "T01")
        original_store = store_path.read_bytes()
        report_id = saved["phases"][0]["review_report"]["spawn_receipt_id"]
        store = json.loads(original_store)
        store["receipts"] = [
            row for row in store["receipts"] if row.get("SPAWN_RECEIPT_ID") != report_id
        ]
        store_path.write_text(json.dumps(store))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_DISPATCH_ROUTE"):
            devcrew_phase.resume(saved, self.host, self.task, self.verifier)

        store = json.loads(original_store)
        route_id = next(
            row["ROUTE_ID"] for row in store["receipts"] if row.get("SPAWN_RECEIPT_ID") == report_id
        )
        store["issued_routes"] = [
            row for row in store["issued_routes"] if row.get("ROUTE_ID") != route_id
        ]
        store_path.write_text(json.dumps(store))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_DISPATCH_ROUTE"):
            devcrew_phase.resume(saved, self.host, self.task, self.verifier)
        store_path.write_bytes(original_store)

    def test_generic_manifest_write_cannot_manufacture_task_acceptance(self):
        _candidate, _report = self._v2_final_review_fixture()
        forged = json.loads(json.dumps(self.manifest))
        forged["original_intent"]["task_acceptance"] = {
            "status": "ACCEPTED",
            "review": {"phase_id": "P1", "report_sha256": "0" * 64},
        }
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_CERTIFICATION_UNAVAILABLE"):
            devcrew_phase.save_manifest(self.path, forged, self.receipt)

    def _assert_final_v2_review_rejected(self, mutation, expected):
        _candidate, report = self._v2_final_review_fixture()
        if mutation == "missing":
            report.pop("INTENT_REVIEW")
        else:
            report["INTENT_REVIEW"]["plan_sha256"] = "0" * 64
        report_path = self.host / f"v2-final-{mutation}.json"
        report_path.write_text(json.dumps(report))
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                report_path.name,
                report_path.read_bytes(),
            )
        self.assertEqual(caught.exception.code, expected)

    def test_final_v2_pass_rejects_missing_intent_review(self):
        self._assert_final_v2_review_rejected("missing", "INVALID_INTENT_REVIEW")

    def test_final_v2_pass_rejects_stale_intent_review_binding(self):
        self._assert_final_v2_review_rejected("stale", "STALE_INTENT_REVIEW")

    def test_final_v2_pass_rejects_unmapped_source_omission_despite_complete_matrix(self):
        candidate, report = self._v2_final_review_fixture()
        outcome = report["INTENT_REVIEW"]["source_outcomes"][0]
        outcome["disposition"] = "OMITTED"
        outcome["requirement_ids"] = []
        report_path = self.host / "v2-final-omitted-source.json"
        report_path.write_text(json.dumps(report))
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                report_path.name,
                report_path.read_bytes(),
            )
        self.assertEqual(caught.exception.code, "INVALID_INTENT_REVIEW")

    def _remap_v2_acceptance(self, replacement, authorization, acceptance):
        phase = replacement["phases"][0]
        phase["acceptance"] = [acceptance]
        requirement = replacement["original_intent"]["requirements"][0]
        requirement["acceptance_refs"] = [
            {
                "phase_id": phase["phase_id"],
                "acceptance_index": 0,
                "sha256": hashlib.sha256(acceptance.encode()).hexdigest(),
            }
        ]
        if isinstance(authorization.get("assessment"), dict):
            authorization["assessment"]["plan_sha256"] = devcrew_phase.intent_plan_digest(
                replacement
            )

    def _v2_assessment(self, plan, previous_digest, *, source_outcomes, obligation_checks):
        intent = plan["original_intent"]
        return {
            "task_id": plan["task_id"],
            "plan_sha256": devcrew_phase.intent_plan_digest(plan),
            "previous_plan_sha256": previous_digest,
            "mode": "IMPLEMENTATION",
            "result": "COMPLETE",
            "source_reads": [
                {
                    "source_id": source["source_id"],
                    "sha256": source["sha256"],
                    "findings": "Read the immutable source bytes for this assessment.",
                }
                for source in intent["sources"]
                if source["status"] == "VERIFIED"
            ],
            "unavailable_sources": [],
            "source_outcomes": source_outcomes,
            "obligation_checks": obligation_checks,
            "technical_change_rationale": "The coordinator read the sources and assessed the replacement plan.",
        }

    def test_v2_systemic_replan_rejects_incomplete_explicit_obligation_assessment(self):
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement, explicit_obligation=True)
        self._remap_v2_acceptance(replacement, authorization, "The endpoint returns any response.")
        authorization["assessment"]["result"] = "INCOMPLETE"
        authorization["assessment"]["source_outcomes"][0].update(
            disposition="OMITTED",
            rationale="The rewritten acceptance does not require the original outcome.",
        )
        before_manifest = self.path.read_bytes()
        before_head = self.git(self.task, "rev-parse", "HEAD")
        before_refs = self.git(
            self.task, "for-each-ref", "--format=%(refname)", "refs/devcrew/rejected"
        )

        with self.assertRaisesRegex(devcrew_phase.PhaseError, "complete planning assessment"):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                replacement,
                planner_id,
                self.verifier,
                intent_assessment=authorization,
            )

        self.assertEqual(self.path.read_bytes(), before_manifest)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), before_head)
        self.assertEqual(
            self.git(self.task, "for-each-ref", "--format=%(refname)", "refs/devcrew/rejected"),
            before_refs,
        )

    def test_v2_systemic_replan_accepts_complete_technical_acceptance_remapping(self):
        rejected, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement, explicit_obligation=True)
        self._remap_v2_acceptance(
            replacement,
            authorization,
            "The checkout choice remains available after the technical change.",
        )

        result = devcrew_phase.complete_systemic_replan(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            replacement,
            planner_id,
            self.verifier,
            intent_assessment=authorization,
        )

        self.assertEqual(result["status"], "PHASE_REPLAN_COMPLETED")
        final = json.loads(self.path.read_text())
        self.assertEqual(
            final["original_intent"]["requirements"][0]["acceptance_refs"],
            replacement["original_intent"]["requirements"][0]["acceptance_refs"],
        )
        self.assertEqual(
            final["original_intent"]["planning_assessment"]["assessment"],
            authorization["assessment"],
        )
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        self.assertEqual(
            final["phases"][0]["review_cycles"][-1]["candidate_commit"]["sha"], rejected["sha"]
        )

    def test_v2_systemic_replan_stale_cas_precedes_backup_and_reset(self):
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement, explicit_obligation=True)
        self._remap_v2_acceptance(
            replacement, authorization, "The replacement preserves the checkout choice."
        )
        state_path = devcrew_worktree.location(self.host, "T01")[-1]
        state_before = state_path.read_bytes()
        head_before = self.git(self.task, "rev-parse", "HEAD")
        refs_before = self.git(
            self.task, "for-each-ref", "--format=%(refname)", "refs/devcrew/rejected"
        )
        manifest_before = json.loads(self.path.read_text())
        actual_atomic = devcrew_phase._atomic_json

        def race_before_cas(path, value, **kwargs):
            current = json.loads(path.read_text())
            current["manifest_revision"] += 1
            path.write_text(json.dumps(current), encoding="utf-8")
            return actual_atomic(path, value, **kwargs)

        with mock.patch.object(devcrew_phase, "_atomic_json", side_effect=race_before_cas):
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_MANIFEST_CONFLICT"):
                devcrew_phase.complete_systemic_replan(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    replacement,
                    planner_id,
                    self.verifier,
                    intent_assessment=authorization,
                )

        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), head_before)
        self.assertEqual(
            self.git(self.task, "for-each-ref", "--format=%(refname)", "refs/devcrew/rejected"),
            refs_before,
        )
        self.assertEqual(state_path.read_bytes(), state_before)
        current = json.loads(self.path.read_text())
        manifest_before["manifest_revision"] += (
            1  # the simulated concurrent writer owns this revision
        )
        self.assertEqual(current, manifest_before)

    def test_v2_systemic_recovery_rechecks_sources_inside_locked_callback(self):
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement)
        with mock.patch.object(
            devcrew_worktree,
            "advance_expected_head",
            side_effect=ValueError("simulated interrupted recovery"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.complete_systemic_replan(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    replacement,
                    planner_id,
                    self.verifier,
                    intent_assessment=authorization,
                )
        pending = json.loads(self.path.read_text())
        source_path = self.task / pending["original_intent"]["sources"][0]["snapshot"]
        source_bytes = source_path.read_bytes()
        state_path = devcrew_worktree.location(self.host, "T01")[-1]
        pending_bytes, receipt_bytes = self.path.read_bytes(), state_path.read_bytes()
        head_before = self.git(self.task, "rev-parse", "HEAD")
        actual_transition_guard = devcrew_phase._validate_intent_transition
        tampered = []

        def tamper_at_recovery_cas(old, proposed, **kwargs):
            if (
                not tampered
                and any(phase.get("systemic_replan_transition") for phase in old.get("phases", []))
                and not any(
                    phase.get("systemic_replan_transition") for phase in proposed.get("phases", [])
                )
            ):
                source_path.write_bytes(source_bytes + b"race")
                tampered.append(True)
            return actual_transition_guard(old, proposed, **kwargs)

        with mock.patch.object(
            devcrew_phase, "_validate_intent_transition", side_effect=tamper_at_recovery_cas
        ):
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_SNAPSHOT_CHANGED"):
                devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)

        self.assertEqual(tampered, [True])
        self.assertEqual(self.path.read_bytes(), pending_bytes)
        self.assertEqual(state_path.read_bytes(), receipt_bytes)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), head_before)
        source_path.write_bytes(source_bytes)

    def test_v2_last_obligation_removal_survives_systemic_recovery_and_later_technical_replan(self):
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement, explicit_obligation=True)
        self._remap_v2_acceptance(
            replacement, authorization, "The updated technical phase preserves the checkout option."
        )
        old_intent = json.loads(json.dumps(self.manifest["original_intent"]))
        old_digest = devcrew_phase.intent_plan_digest(self.manifest)
        old_requirement = old_intent["requirements"][0]

        answer_file = pathlib.Path(self.temp.name) / "owner-answer.txt"
        answer_file.write_text("I no longer need this checkout recommendation.", encoding="utf-8")
        answer = devcrew_phase.bind_intent_source(
            self.host,
            self.receipt,
            {
                "mode": "file",
                "source_type": "owner_answer",
                "path": str(answer_file),
                "expected_sha256": hashlib.sha256(answer_file.read_bytes()).hexdigest(),
                "origin": "user answer",
            },
        )
        answer_bytes = (self.task / answer["snapshot"]).read_bytes()
        answer_fragment = {
            "source_id": answer["source_id"],
            "start_byte": 0,
            "end_byte": len(answer_bytes),
            "sha256": hashlib.sha256(answer_bytes).hexdigest(),
        }
        decision = {
            "decision_id": "D_REMOVE_F1",
            "source_fragments": [answer_fragment],
            "changes": [
                {
                    "requirement_id": "F1",
                    "before": {
                        key: old_requirement[key]
                        for key in ("kind", "statement", "source_fragments")
                    },
                    "after": None,
                }
            ],
            "human_confirmation": {
                "actor": "user",
                "channel": "external_owner_message",
                "interpretation": "The user explicitly removed the checkout recommendation.",
                "authority_rationale": "The answer source records the direct user decision.",
            },
        }
        replacement["original_intent"]["sources"].append(answer)
        replacement["original_intent"]["decisions"] = []
        authorization["decisions"] = [decision]
        removal_check = {
            "requirement_id": "F1",
            "change": "USER_REMOVED",
            "preserved_outcome": "The recommendation is no longer required.",
            "safety_rationale": "No replacement choice was invented.",
            "acceptance_rationale": "The removed obligation has no active acceptance mapping.",
            "decision_id": decision["decision_id"],
        }
        assessed_replacement = json.loads(json.dumps(replacement))
        assessed_replacement["original_intent"]["decisions"] = [decision]
        authorization["assessment"] = self._v2_assessment(
            assessed_replacement, old_digest, source_outcomes=[], obligation_checks=[removal_check]
        )

        with mock.patch.object(
            devcrew_worktree,
            "advance_expected_head",
            side_effect=ValueError("simulated interruption before receipt advance"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.complete_systemic_replan(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    replacement,
                    planner_id,
                    self.verifier,
                    intent_assessment=authorization,
                )
        pending = json.loads(self.path.read_text())
        self.assertEqual(pending["original_intent"], old_intent)
        recovered = devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)
        self.assertEqual(recovered["status"], "PHASE_REPLAN_COMPLETED")
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        installed = self.manifest["original_intent"]
        self.assertEqual(devcrew_phase.effective_intent_requirements(self.manifest)["F1"], None)
        self.assertEqual(installed["decisions"], [decision])
        self.assertEqual(
            [row["source_id"] for row in installed["sources"]],
            [row["source_id"] for row in old_intent["sources"]] + [answer["source_id"]],
        )
        self.assertEqual(
            installed["planning_assessment"]["assessment"]["obligation_checks"], [removal_check]
        )

        _, _, _, technical_replacement, technical_planner_id = self._systemic_replan_fixture()
        technical_authorization = {"decisions": []}
        self._remap_v2_acceptance(
            technical_replacement,
            technical_authorization,
            "The remaining technical behavior is correct.",
        )
        technical_authorization["assessment"] = self._v2_assessment(
            technical_replacement,
            devcrew_phase.intent_plan_digest(self.manifest),
            source_outcomes=[],
            obligation_checks=[],
        )
        progress_check = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "run-check",
                "--project",
                str(self.host),
                "--execution-root",
                str(self.task),
                "--task-id",
                "T01",
                "--name",
                "last-removal-progress",
                "--",
                sys.executable,
                "-B",
                "-c",
                "import subprocess; subprocess.run(['git', 'rev-parse', 'HEAD'], check=True)",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        technical_continuation = {
            "task_id": "T01",
            "phase_id": "P1",
            "target_sha": self.manifest["phases"][0]["candidate_commit"]["sha"],
            "report_sha256": self.manifest["phases"][0]["review_report"]["sha256"],
            "reason": "The persisted technical check confirms the current candidate and bounded replan state.",
            "next_action": "Apply only the approved technical acceptance rewording and revalidate intent history.",
            "expected_result": "The replacement recovers with the removed obligation preserved in history.",
            "cost_rationale": "Reuse the failed candidate and run one bounded identity check.",
            "evidence_records": [progress_check.stdout.splitlines()[0]],
        }
        result = devcrew_phase.complete_systemic_replan(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            technical_replacement,
            technical_planner_id,
            self.verifier,
            intent_assessment=technical_authorization,
            continuation=technical_continuation,
        )
        self.assertEqual(result["status"], "PHASE_REPLAN_COMPLETED")
        final = json.loads(self.path.read_text())
        assessment = final["original_intent"]["planning_assessment"]["assessment"]
        self.assertEqual(assessment["source_outcomes"], [])
        self.assertEqual(assessment["obligation_checks"], [])
        self.assertEqual(
            {row["source_id"] for row in assessment["source_reads"]},
            {row["source_id"] for row in final["original_intent"]["sources"]},
        )
        self.assertEqual(final["original_intent"]["decisions"], [decision])
        history = final["original_intent"]["planning_assessment"]["history"]
        self.assertTrue(any(row["obligation_checks"] == [removal_check] for row in history))
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        resumed = devcrew_phase.load_for_resume(self.path, self.receipt)
        self.assertEqual(resumed["original_intent"]["decisions"], [decision])
        self.assertEqual(resumed["original_intent"]["planning_assessment"]["history"], history)

        self.manifest = resumed
        candidate = self.make_candidate(
            candidate_text="candidate after removal and technical replan\n"
        )
        self.accept_candidate("P1", candidate, "last-removal-after-replan-p1-review.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        self.assertEqual(
            self.manifest["original_intent"]["planning_assessment"]["assessment"]["plan_sha256"],
            devcrew_phase.intent_plan_digest(self.manifest),
        )
        candidate = self.make_candidate("P2", candidate_text="candidate for dependent phase two\n")
        self.accept_candidate("P2", candidate, "last-removal-after-replan-p2-review.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P3", self.host, self.task, self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        candidate = self.make_candidate(
            "P3", candidate_text="final candidate after removal and replan\n"
        )
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P3", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        phase = self.manifest["phases"][2]
        route = self.routed_reviewer_fields(
            {
                "TASK_ID": "T01",
                "PHASE_ID": "P3",
                "BASE_SHA": candidate["base"],
                "TARGET_SHA": candidate["sha"],
            }
        )
        evidence = list(phase["candidate_commit"]["evidence"])
        fragments = []
        for source in self.manifest["original_intent"]["sources"]:
            source_data = (self.task / source["snapshot"]).read_bytes()
            fragments.append(
                {
                    "source_id": source["source_id"],
                    "start_byte": 0,
                    "end_byte": len(source_data),
                    "sha256": hashlib.sha256(source_data).hexdigest(),
                }
            )
        outcomes = [
            {
                "source_fragments": fragments,
                "expected_outcome": "The owner removed the sole checkout requirement; no active obligation remains.",
                "requirement_ids": ["F1"],
                "disposition": "COVERED",
                "rationale": "The original obligation and actual removal answer are both independently accounted for.",
                "evidence_records": evidence,
            }
        ]
        reads = [
            {
                "source_id": source["source_id"],
                "sha256": source["sha256"],
                "findings": "The immutable source was independently read.",
            }
            for source in self.manifest["original_intent"]["sources"]
        ]
        review = {
            "plan_sha256": devcrew_phase.intent_plan_digest(self.manifest),
            "result": "COMPLETE",
            "source_reads": reads,
            "unavailable_sources": [],
            "decision_ids": [
                row["decision_id"] for row in self.manifest["original_intent"]["decisions"]
            ],
            "source_outcomes": outcomes,
            "obligation_checks": [],
            "evidence_sha256": {
                reference: hashlib.sha256((self.host / reference).read_bytes()).hexdigest()
                for reference in evidence
            },
        }
        report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P3",
            "BASE_SHA": phase["candidate_commit"]["base"],
            "TARGET_SHA": phase["candidate_commit"]["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
            "INTENT_REVIEW": review,
            **route,
        }
        report_path = self.host / "last-removal-after-technical-replan-review.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        suite = self.run_full_suite_record("last-removal-after-technical-replan-acceptance")
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P3",
            self.verifier,
            [suite],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        accepted = devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        self.assertEqual(
            devcrew_phase.resume(accepted, self.host, self.task, self.verifier)["task_status"],
            "TASK_DONE",
        )

    def test_v2_systemic_replan_separate_assessment_recovers_authorized_replacement(self):
        rejected, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement)
        with mock.patch.object(
            devcrew_worktree,
            "advance_expected_head",
            side_effect=ValueError("simulated process interruption"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.complete_systemic_replan(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    replacement,
                    planner_id,
                    self.verifier,
                    intent_assessment=authorization,
                )
        pending = json.loads(self.path.read_text())
        self.assertEqual(pending["original_intent"], self.manifest["original_intent"])
        self.assertEqual(
            pending["phases"][0]["systemic_replan_transition"]["replacement_contract"][
                "original_intent"
            ]["planning_assessment"]["assessment"],
            authorization["assessment"],
        )
        source_path = self.task / pending["original_intent"]["sources"][0]["snapshot"]
        source_bytes = source_path.read_bytes()
        state_path = devcrew_worktree.location(self.host, "T01")[-1]
        receipt_state_before = state_path.read_bytes()
        source_path.write_bytes(source_bytes + b"tamper")
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        self.assertEqual(state_path.read_bytes(), receipt_state_before)
        source_path.write_bytes(source_bytes)
        pending_bytes = self.path.read_bytes()
        tampered_history = json.loads(pending_bytes)
        completion = tampered_history["phases"][0]["systemic_replan_transition"]["cycle"][
            "systemic_replan_completion"
        ]
        completion["original_intent"]["planning_assessment"]["assessment"][
            "technical_change_rationale"
        ] = "Tampered archived assessment history."
        self.path.write_text(json.dumps(tampered_history), encoding="utf-8")
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        self.assertEqual(state_path.read_bytes(), receipt_state_before)
        self.path.write_bytes(pending_bytes)
        invalid_pair = json.loads(receipt_state_before)
        invalid_pair["expected_head"] = "f" * 40
        state_path.write_text(json.dumps(invalid_pair), encoding="utf-8")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "recovery checkout identity"):
            devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)
        self.assertEqual(self.path.read_bytes(), pending_bytes)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        state_path.write_bytes(receipt_state_before)
        with mock.patch.object(
            devcrew_dispatch,
            "verify_historical_planner",
            side_effect=devcrew_dispatch.DispatchError("simulated stale planner authority"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)
        self.assertEqual(self.path.read_bytes(), pending_bytes)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        self.assertEqual(state_path.read_bytes(), receipt_state_before)
        with mock.patch.object(
            devcrew_phase,
            "_verify_dispatch",
            side_effect=AssertionError("recovery must use persisted planner authority"),
        ):
            recovered = devcrew_phase.recover_systemic_replan(
                self.host, "T01", verifier=self.verifier
            )
        self.assertEqual(recovered["status"], "PHASE_REPLAN_COMPLETED")
        final = json.loads(self.path.read_text())
        self.assertEqual(
            final["original_intent"]["planning_assessment"]["assessment"],
            authorization["assessment"],
        )
        self.assertEqual(
            final["phases"][0]["review_cycles"][0]["candidate_commit"]["sha"], rejected["sha"]
        )
        resumed_receipt = devcrew_worktree.resolve(self.host, "T01")
        resumed = devcrew_phase.load_for_resume(self.path, resumed_receipt)
        self.assertEqual(resumed["original_intent"], final["original_intent"])
        self.assertEqual(resumed["phases"][0]["review_cycles"], final["phases"][0]["review_cycles"])

    def test_v2_systemic_replan_requires_separate_host_assessment_and_protects_nested_transition(
        self,
    ):
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        self._make_v2_systemic_state(replacement)
        before = json.loads(self.path.read_text())
        forged = json.loads(json.dumps(before))
        forged["phases"][0]["systemic_replan_transition"] = {
            "cycle": {"systemic_replan_completion": {}},
            "replacement_contract": replacement,
        }
        callback = []
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase._atomic_json(self.path, forged, before_save=lambda: callback.append(True))
        self.assertEqual(callback, [])
        self.assertEqual(json.loads(self.path.read_text()), before)

    def test_v2_systemic_replan_rejects_planner_supplied_assessment_and_definition_mutation(self):
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        authorization = self._make_v2_systemic_state(replacement)
        before = json.loads(self.path.read_text())
        forged = json.loads(json.dumps(replacement))
        forged["original_intent"]["planning_assessment"]["assessment"][
            "technical_change_rationale"
        ] = "Planner supplied an untrusted assessment."
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                forged,
                planner_id,
                self.verifier,
                intent_assessment=authorization,
            )
        changed = json.loads(json.dumps(replacement))
        changed["original_intent"]["requirements"][0]["statement"] = "Weakened fact."
        authorization["assessment"]["plan_sha256"] = devcrew_phase.intent_plan_digest(changed)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                changed,
                planner_id,
                self.verifier,
                intent_assessment=authorization,
            )
        self.assertEqual(json.loads(self.path.read_text()), before)
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "INTENT_PLANNING_ASSESSMENT_REQUIRED"
        ):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                replacement,
                planner_id,
                self.verifier,
            )
        self.assertEqual(json.loads(self.path.read_text()), before)

    def test_systemic_replan_after_local_fix_requires_progress_not_owner(self):
        self.enter_local_fix_with_history("loop-before-replan")
        _, _, _, replacement, planner_id = self._systemic_replan_fixture()
        resumed = devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)
        self.assertEqual(resumed["status"], "PHASE_CONTINUATION_REQUIRED")
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "CONTINUATION_REQUIRED"):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                replacement,
                planner_id,
                self.verifier,
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)
        replacement["phases"][0].pop("active_local_fix", None)
        assessment = self.progress_assessment()
        result = devcrew_phase.complete_systemic_replan(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            replacement,
            planner_id,
            self.verifier,
            continuation=assessment,
        )
        self.assertEqual(result["status"], "PHASE_REPLAN_COMPLETED")
        current = json.loads(self.path.read_text())
        self.assertEqual(
            current["phases"][0]["review_cycles"][-1]["continuation"]["reason"],
            assessment["reason"],
        )

    def test_systemic_replan_archives_failure_resets_and_dispatches_replacement(self):
        rejected, report_path, report_bytes, replacement, planner_id = (
            self._systemic_replan_fixture()
        )
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.executor_input(self.manifest, self.task, self.host, self.verifier)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.set_phase_status(
                self.path,
                self.manifest,
                self.receipt,
                "P1",
                "EXECUTING",
                project=self.host,
                verifier=self.verifier,
            )
        planner_output = pathlib.Path(self.temp.name) / "replacement-plan.json"
        planner_output.write_text(json.dumps(replacement))
        cli = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "replan-complete",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_output),
                "--spawn-receipt-id",
                planner_id,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(cli.returncode, 0, cli.stderr)
        result = json.loads(cli.stdout)
        self.assertEqual(result["status"], "PHASE_REPLAN_COMPLETED")
        self.assertEqual(
            result["executor_input"]["goal"], "Implement the corrected health endpoint contract"
        )
        self.assertEqual(
            result["executor_input"]["main_scenario"],
            "GET /health returns the corrected health response.",
        )
        saved = json.loads(self.path.read_text())
        phase = saved["phases"][0]
        self.assertEqual(phase["status"], "PLANNED")
        self.assertIsNone(phase["candidate_commit"])
        self.assertEqual(phase["evidence"], [])
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        self.assertEqual(devcrew_worktree.resolve(self.host, "T01")["expected_head"], self.base)
        cycle = phase["review_cycles"][0]
        self.assertEqual(cycle["candidate_commit"]["sha"], rejected["sha"])
        self.assertIn("goal", cycle["systemic_replan_completion"]["failed_contract"])
        self.assertEqual(
            cycle["systemic_replan_completion"]["replacement_contract"]["phases"][0]["goal"],
            "Implement the corrected health endpoint contract",
        )
        self.assertEqual(self.git(self.task, "rev-parse", cycle["backup_ref"]), rejected["sha"])
        self.assertNotIn("executor_dispatch", phase)
        self.assertEqual(report_path.read_bytes(), report_bytes)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.accept_review(
                self.path,
                saved,
                devcrew_worktree.resolve(self.host, "T01"),
                self.host,
                self.task,
                "P1",
                self.verifier,
            )

    def test_systemic_replan_then_cycle_zero_local_fix_preserves_dispatch_history(self):
        rejected, report_path, report_bytes, replacement, planner_id = (
            self._systemic_replan_fixture()
        )
        planner_output = pathlib.Path(self.temp.name) / "replacement-plan-local-fix.json"
        planner_output.write_text(json.dumps(replacement))
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "replan-complete",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_output),
                "--spawn-receipt-id",
                planner_id,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)

        first_replacement = self.make_candidate(candidate_text="replacement phase candidate\n")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        failure = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        failure.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair replacement after systemic replan",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        failure.update(self.routed_reviewer_fields(failure))
        failure_path = self.host / "systemic-then-local-fix-fail.json"
        failure_path.write_text(json.dumps(failure))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            failure,
            self.verifier,
            failure_path.name,
            failure_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            continuation=self.progress_assessment(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        archived = self.manifest["phases"][0]["review_cycles"]
        self.assertEqual(len(archived), 2)
        self.assertEqual(archived[0]["candidate_commit"]["sha"], rejected["sha"])
        self.assertEqual(archived[1]["candidate_commit"]["sha"], first_replacement["sha"])
        self.assertEqual(archived[1]["executor_dispatch"]["writer_cycle"], 0)
        repair = self.make_candidate(
            candidate_text="cycle two repair candidate after systemic reset\n"
        )
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        second_failure = {
            key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        second_failure.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair after systemic reset",
                    "EVIDENCE": ["tests/test_widget.py:43"],
                }
            ],
        )
        second_failure.update(self.routed_reviewer_fields(second_failure))
        second_path = self.host / "systemic-then-cycle-two-fail.json"
        second_path.write_text(json.dumps(second_failure))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            second_failure,
            self.verifier,
            second_path.name,
            second_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            continuation=self.progress_assessment(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        archived = self.manifest["phases"][0]["review_cycles"]
        self.assertEqual(archived[2]["executor_dispatch"]["writer_cycle"], 2)
        devcrew_phase._verify_all_review_cycles(self.host, self.task, self.manifest, self.verifier)

    def test_legacy_local_fix_cycle_without_dispatch_advances_writer_lineage(self):
        self.enter_local_fix_with_history("legacy-writer-lineage")
        phase = self.manifest["phases"][0]
        legacy_rejected_sha = phase["review_cycles"][0]["candidate_commit"]["sha"]
        phase["review_cycles"][0].pop("executor_dispatch")
        self.path.write_text(json.dumps(self.manifest))
        self.manifest = json.loads(self.path.read_text())
        replacement = self.make_candidate(candidate_text="candidate after legacy history\n")
        self._submit_local_fail("legacy-history-next-fail", existing_candidate=True)
        devcrew_phase.start_local_fix(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            continuation=self.progress_assessment(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        archived = self.manifest["phases"][0]["review_cycles"]
        self.assertEqual(archived[0]["candidate_commit"]["sha"], legacy_rejected_sha)
        self.assertNotEqual(replacement["sha"], legacy_rejected_sha)
        self.assertNotIn("executor_dispatch", archived[0])
        self.assertEqual(archived[1]["executor_dispatch"]["writer_cycle"], 1)
        devcrew_phase._verify_all_review_cycles(self.host, self.task, self.manifest, self.verifier)

    def test_review_correction_does_not_advance_writer_lineage(self):
        rejected, _, _ = self._submit_local_fail("lineage-review-correction")
        devcrew_phase.reconsider_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            self.progress_assessment(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        self._submit_local_fail("lineage-review-correction-again", existing_candidate=True)
        distinct_check = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "run-check",
                "--project",
                str(self.host),
                "--execution-root",
                str(self.task),
                "--task-id",
                "T01",
                "--name",
                "lineage-distinct-proof",
                "--",
                sys.executable,
                "-B",
                "-c",
                "from pathlib import Path; assert Path('one.txt').is_file()",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        current = self.manifest["phases"][0]
        continuation = {
            "task_id": "T01",
            "phase_id": "P1",
            "target_sha": current["candidate_commit"]["sha"],
            "report_sha256": current["review_report"]["sha256"],
            "reason": "An independent bounded check confirms the disputed file is present",
            "next_action": "Apply only the active local repair after checking the reviewed artifact",
            "expected_result": "The reported file presence and phase contract are assessed together",
            "cost_rationale": "Reuse the existing candidate and one bounded file check",
            "evidence_records": [distinct_check.stdout.splitlines()[0]],
        }
        devcrew_phase.start_local_fix(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            continuation=continuation,
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        cycles = self.manifest["phases"][0]["review_cycles"]
        self.assertEqual(cycles[0]["candidate_commit"]["sha"], rejected["sha"])
        self.assertTrue(cycles[0]["review_correction"])
        self.assertEqual(cycles[1]["executor_dispatch"]["writer_cycle"], 0)
        self.assertEqual(len(cycles), 2)
        devcrew_phase._verify_all_review_cycles(self.host, self.task, self.manifest, self.verifier)
        self.make_candidate(candidate_text="candidate after correction cycle\n")
        self._submit_local_fail("lineage-review-correction-repair-fail", existing_candidate=True)
        devcrew_phase.start_local_fix(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            continuation=self.progress_assessment(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        cycles = self.manifest["phases"][0]["review_cycles"]
        self.assertEqual(cycles[2]["executor_dispatch"]["writer_cycle"], 2)
        devcrew_phase._verify_all_review_cycles(self.host, self.task, self.manifest, self.verifier)

    def test_systemic_replan_of_p2_preserves_accepted_p1_history(self):
        p1_candidate = self.make_candidate("P1", candidate_text="accepted P1 before P2 replan\n")
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        p1_report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "TARGET_SHA": p1_candidate["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        p1_report.update(self.routed_reviewer_fields(p1_report))
        p1_report_path = self.host / "p1-pass-before-p2-replan.json"
        p1_report_path.write_text(json.dumps(p1_report))
        reviewed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-report",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--report",
                str(p1_report_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        suite_record = self.run_full_suite_record("p1-before-p2-replan")
        accepted = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "accept",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--evidence-record",
                suite_record,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        advanced = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "advance",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P2",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(advanced.returncode, 0, advanced.stderr)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        accepted_p1 = json.loads(json.dumps(self.manifest["phases"][0]))

        p2_candidate = self.make_candidate("P2", candidate_text="failed P2 requiring replan\n")
        started = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-start",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P2",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        p2_target = json.loads(started.stdout)["review_target"]
        p2_report = {
            key: p2_target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        p2_report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "SYSTEMIC_REPLAN",
                    "SUMMARY": "P2 contract requires a systemic replacement",
                    "EVIDENCE": ["tests/test_phase_review_lifecycle.py:0"],
                }
            ],
        )
        p2_report.update(self.routed_reviewer_fields(p2_report))
        p2_report_path = self.host / "p2-systemic-replan.json"
        p2_report_path.write_text(json.dumps(p2_report))
        reviewed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "review-report",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--report",
                str(p2_report_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        before = json.loads(json.dumps(self.manifest))
        replacement = json.loads(json.dumps(self.manifest))
        replacement.pop("planner_dispatch", None)
        p2 = replacement["phases"][1]
        for field in (
            "candidate_commit",
            "review_report",
            "review_start",
            "evidence",
            "review_cycles",
        ):
            p2.pop(field, None)
        p2["status"] = "PLANNED"
        p2["goal"] = "Implement the replacement P2 contract"
        p2["acceptance"] = ["Replacement P2 behavior is correct"]
        replacement["phases"][0] = json.loads(json.dumps(accepted_p1))

        tampered = json.loads(json.dumps(replacement))
        tampered["phases"][0]["accepted_sha"] = self.base
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                tampered,
                "not-used-before-preservation-check",
                self.verifier,
            )
        self.assertEqual(json.loads(self.path.read_text()), before)

        planner = issue_planner_receipt(
            self.host,
            "T01",
            p2_candidate["sha"],
            "Replace the P2 systemic contract",
            "planner-p2-replan",
        )
        replacement_path = pathlib.Path(self.temp.name) / "p2-replacement.json"
        replacement_path.write_text(json.dumps(replacement))
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "replan-complete",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P2",
                "--planner-output",
                str(replacement_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        after = json.loads(self.path.read_text())
        self.assertEqual(after["phases"][0], accepted_p1)
        self.assertEqual(after["phases"][1]["status"], "PLANNED")
        self.assertEqual(after["phases"][1]["goal"], "Implement the replacement P2 contract")
        self.assertEqual(after["phases"][1]["candidate_commit"], None)
        self.assertEqual(after["phases"][1]["review_report"], None)
        self.assertEqual(after["phases"][1]["evidence"], [])
        cycle = after["phases"][1]["review_cycles"][-1]
        self.assertEqual(cycle["candidate_commit"]["sha"], p2_candidate["sha"])
        self.assertEqual(
            cycle["review_report"]["sha256"], before["phases"][1]["review_report"]["sha256"]
        )
        self.assertIn(
            p2_candidate["sha"], devcrew_phase._rejected_candidate_shas(after["phases"][1])
        )
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), p1_candidate["sha"])

    def test_interrupted_systemic_replan_recovers_to_planned(self):
        rejected, _, _, replacement, planner_id = self._systemic_replan_fixture()
        with mock.patch.object(
            devcrew_worktree,
            "advance_expected_head",
            side_effect=ValueError("simulated interruption"),
        ):
            with self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.complete_systemic_replan(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    replacement,
                    planner_id,
                    self.verifier,
                )
        pending = json.loads(self.path.read_text())["phases"][0]
        self.assertIn("systemic_replan_transition", pending)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        recovered = devcrew_phase.recover_systemic_replan(self.host, "T01", verifier=self.verifier)
        self.assertEqual(recovered["status"], "PHASE_REPLAN_COMPLETED")
        self.assertEqual(recovered["executor_input"]["base_sha"], self.base)
        self.assertEqual(
            self.git(
                self.task,
                "rev-parse",
                "refs/devcrew/rejected/T01/" + hashlib.sha256(b"P1").hexdigest()[:16] + "/cycle-1",
            ),
            rejected["sha"],
        )

    def test_systemic_replan_rejects_stale_or_forged_inputs(self):
        _, report_path, report_bytes, replacement, planner_id = self._systemic_replan_fixture()
        invalid_contracts = []
        wrong_task = json.loads(json.dumps(replacement))
        wrong_task["task_id"] = "T02"
        invalid_contracts.append(("task", wrong_task, planner_id))
        wrong_phase = json.loads(json.dumps(replacement))
        wrong_phase["current_phase_id"] = "P2"
        invalid_contracts.append(("phase", wrong_phase, planner_id))
        executing = json.loads(json.dumps(replacement))
        executing["phases"][0]["status"] = "EXECUTING"
        invalid_contracts.append(("plan", executing, planner_id))
        invalid_contracts.append(("planner receipt", replacement, "missing-planner-receipt"))
        for name, contract, receipt_id in invalid_contracts:
            with self.subTest(name=name), self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.complete_systemic_replan(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    contract,
                    receipt_id,
                    self.verifier,
                )
        report_path.write_bytes(report_bytes + b" ")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                replacement,
                planner_id,
                self.verifier,
            )
        report_path.write_bytes(report_bytes)
        original_candidate = self.manifest["phases"][0]["candidate_commit"]["sha"]
        self.manifest["phases"][0]["candidate_commit"]["sha"] = "0" * 40
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.complete_systemic_replan(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                replacement,
                planner_id,
                self.verifier,
            )
        self.manifest["phases"][0]["candidate_commit"]["sha"] = original_candidate

    def test_rejected_candidate_ref_survives_reset_and_git_prune(self):
        rejected = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "review-pin.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        manifest = json.loads(self.path.read_text())
        ref = manifest["phases"][0]["review_cycles"][0]["backup_ref"]
        self.git(self.task, "gc", "--prune=now")
        self.assertEqual(self.git(self.task, "rev-parse", ref), rejected["sha"])
        self.assertEqual(self.git(self.task, "cat-file", "-t", rejected["sha"]), "commit")

    def test_local_fix_recovery_refuses_wrong_branch_at_rejected_sha(self):
        candidate = self.leave_local_fix_transition_pending("review-wrong-branch.json")
        self.git(self.task, "checkout", "-qb", "unrelated", candidate["sha"])
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "different branch"):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), candidate["sha"])
        self.assertEqual(self.git(self.task, "symbolic-ref", "--short", "HEAD"), "unrelated")

    def test_local_fix_recovery_refuses_dirty_checkout_without_discarding_edits(self):
        self.leave_local_fix_transition_pending("review-dirty.json", after_reset=True)
        edited = self.task / "one.txt"
        edited.write_text("unrelated dirty edit\n")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "uncommitted changes"):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(edited.read_text(), "unrelated dirty edit\n")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_local_fix_missing_or_changed_backup_ref_never_moves_rejected_head(self):
        for mutation in ("delete", "change"):
            with self.subTest(mutation=mutation):
                self.setUp()
                candidate = self.leave_local_fix_transition_pending(f"review-pin-{mutation}.json")
                manifest = json.loads(self.path.read_text())
                reference = manifest["phases"][0]["local_fix_transition"]["backup_ref"]
                if mutation == "delete":
                    self.git(self.task, "update-ref", "-d", reference, candidate["sha"])
                else:
                    self.git(self.task, "update-ref", reference, self.base, candidate["sha"])
                with self.assertRaisesRegex(
                    devcrew_phase.PhaseError, "recovery ref is missing or changed"
                ):
                    devcrew_phase.recover_local_fix(
                        self.host,
                        "T01",
                        path=self.path,
                        task_root=self.task,
                        verifier=self.verifier,
                    )
                self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), candidate["sha"])
                self.assertEqual(
                    self.git(self.task, "symbolic-ref", "--short", "HEAD"), "codex/tasks/T01"
                )

    def test_candidate_commit_rejects_changed_archived_review_report(self):
        self.enter_local_fix_with_history("candidate-check")
        saved = json.loads(self.path.read_text())["phases"][0]["review_cycles"][0]["review_report"]
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            self.make_candidate(
                candidate_text="replacement candidate\n",
                before_commit=lambda: (self.host / saved["report_ref"]).write_text(
                    "changed historical report\n"
                ),
            )
        manifest = json.loads(self.path.read_text())
        self.assertEqual(manifest["phases"][0]["status"], "READY_FOR_CLOSE")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_review_start_rejects_deleted_archived_candidate_pin(self):
        rejected = self.enter_local_fix_with_history("review-start-check")
        replacement = self.make_candidate(candidate_text="replacement for review-start\n")
        saved = json.loads(self.path.read_text())["phases"][0]["review_cycles"][0]
        self.git(self.task, "update-ref", "-d", saved["backup_ref"], rejected["sha"])
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "rejected candidate ref is missing or changed"
        ):
            devcrew_phase.begin_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )
        self.assertEqual(json.loads(self.path.read_text())["phases"][0]["status"], "COMMITTED")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), replacement["sha"])

    def test_review_report_rejects_changed_archived_review_before_recording(self):
        self.enter_local_fix_with_history("review-report-check")
        candidate = self.make_candidate(candidate_text="replacement for review-report\n")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(VERDICT="PASS", FINDINGS=[])
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "replacement-pass.json"
        report_path.write_text(json.dumps(report))
        old_ref = json.loads(self.path.read_text())["phases"][0]["review_cycles"][0][
            "review_report"
        ]["report_ref"]
        (self.host / old_ref).write_text("tampered before replacement report\n")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.submit_review(
                self.path,
                self.manifest,
                self.receipt,
                self.host,
                self.task,
                report,
                self.verifier,
                report_path.name,
                report_path.read_bytes(),
            )
        phase = json.loads(self.path.read_text())["phases"][0]
        self.assertEqual(phase["status"], "REVIEWING")
        self.assertIsNone(phase["review_report"])
        self.assertEqual(phase["candidate_commit"]["sha"], candidate["sha"])

    def test_accept_rejects_changed_archived_report_without_accepting_replacement(self):
        self.enter_local_fix_with_history("accept-check")
        candidate = self.make_candidate(candidate_text="replacement for accept\n")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(VERDICT="PASS", FINDINGS=[])
        report_path = self.host / "replacement-accept-pass.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        old_ref = json.loads(self.path.read_text())["phases"][0]["review_cycles"][0][
            "review_report"
        ]["report_ref"]
        (self.host / old_ref).write_text("tampered before acceptance\n")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.accept_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )
        self.assertEqual(json.loads(self.path.read_text())["phases"][0]["status"], "REVIEWING")
        self.assertNotEqual(
            json.loads(self.path.read_text())["phases"][0].get("accepted_sha"), candidate["sha"]
        )

    def test_advance_rejects_deleted_archived_review_report(self):
        self.enter_local_fix_with_history("advance-check")
        candidate = self.make_candidate(candidate_text="replacement for advance\n")
        self.accept_candidate("P1", candidate, "replacement-advance-pass.json")
        phase = json.loads(self.path.read_text())["phases"][0]
        old_ref = phase["review_cycles"][0]["review_report"]["report_ref"]
        (self.host / old_ref).unlink()
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "historical failed review report is unavailable"
        ):
            devcrew_phase.advance_phase(
                self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
            )
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved["current_phase_id"], "P1")
        self.assertNotIn("base_sha", saved["phases"][1])

    def test_archived_review_symlink_is_rejected_at_review_start(self):
        self.enter_local_fix_with_history("symlink-check")
        self.make_candidate(candidate_text="replacement for symlink check\n")
        saved = json.loads(self.path.read_text())["phases"][0]["review_cycles"][0]["review_report"]
        report = self.host / saved["report_ref"]
        original = report.read_bytes()
        report.unlink()
        target = self.host / "historical-review-copy.json"
        target.write_bytes(original)
        report.symlink_to(target)
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "historical failed review report is unavailable"
        ):
            devcrew_phase.begin_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )

    def test_resume_later_phase_validates_archived_cycles_of_accepted_predecessors(self):
        self.enter_local_fix_with_history("resume-later")
        replacement = self.make_candidate(candidate_text="replacement before advancing\n")
        self.accept_candidate("P1", replacement, "replacement-resume-pass.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        saved = self.manifest["phases"][0]["review_cycles"][0]["review_report"]
        (self.host / saved["report_ref"]).write_text("tampered accepted predecessor history\n")
        loaded = devcrew_phase.load_for_resume(
            self.path, devcrew_worktree.resolve(self.host, "T01")
        )
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.resume(loaded, self.host, self.task, self.verifier)

    def test_candidate_commit_rejects_reused_rejected_sha_before_publish(self):
        rejected = self.enter_local_fix_with_history("sha-collision")
        real_run = subprocess.run

        def force_rejected_commit(*args, **kwargs):
            command = args[0]
            if isinstance(command, (list, tuple)) and "commit-tree" in command:
                return subprocess.CompletedProcess(
                    command, 0, stdout=rejected["sha"] + "\n", stderr=""
                )
            return real_run(*args, **kwargs)

        with mock.patch.object(subprocess, "run", side_effect=force_rejected_commit):
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "REJECTED_CANDIDATE_REUSE"):
                self.make_candidate(candidate_text="replacement with forced old SHA\n")
        phase = json.loads(self.path.read_text())["phases"][0]
        self.assertEqual(phase["status"], "READY_FOR_CLOSE")
        self.assertIsNone(phase.get("candidate_commit"))
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_status_transition_rejects_corrupt_archived_cycle_without_advancing(self):
        self.enter_local_fix_with_history("transition-check")
        saved = self.manifest["phases"][0]["review_cycles"][0]["review_report"]
        (self.host / saved["report_ref"]).write_text("tampered before transition\n")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.set_phase_status(
                self.path,
                self.manifest,
                self.receipt,
                "P1",
                "VERIFYING",
                project=self.host,
                verifier=self.verifier,
            )
        phase = json.loads(self.path.read_text())["phases"][0]
        self.assertEqual(phase["status"], "EXECUTING")

    def test_p2_local_fix_start_rejects_corrupt_p1_cycle_before_any_mutation(self):
        self.enter_p2_local_fix_after_p1_repair("start-corrupt-p1")
        saved = self.manifest["phases"][0]["review_cycles"][0]["review_report"]
        (self.host / saved["report_ref"]).write_text("tampered P1 rejected report\n")
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.start_local_fix(
                self.path, self.manifest, self.receipt, self.host, self.task, "P2", self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_p2_local_fix_recovery_rejects_corrupt_p1_cycle_before_any_mutation(self):
        self.leave_p2_local_fix_transition_before_reset("recover-corrupt-p1")
        saved = self.manifest["phases"][0]["review_cycles"][0]["review_report"]
        (self.host / saved["report_ref"]).write_text("tampered P1 rejected report\n")
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_p2_local_fix_start_rejects_candidate_base_diverging_from_accepted_predecessor(self):
        self.enter_p2_local_fix_after_p1_repair("start-wrong-current-base")
        self.rewrite_current_failed_candidate_base(self.base)
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "current phase base and candidate"):
            devcrew_phase.start_local_fix(
                self.path,
                json.loads(self.path.read_text()),
                self.receipt,
                self.host,
                self.task,
                "P2",
                self.verifier,
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_p2_local_fix_recovery_rejects_candidate_base_diverging_from_accepted_predecessor(self):
        self.leave_p2_local_fix_transition_before_reset("recover-wrong-current-base")
        self.rewrite_current_failed_candidate_base(self.base)
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "current phase base and candidate"):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_p2_local_fix_start_rejects_corrupt_accepted_predecessor_before_mutation(self):
        for corrupt in ("report", "evidence"):
            with self.subTest(corrupt=corrupt):
                self.setUp()
                self.enter_p2_local_fix_after_p1_repair(f"start-corrupt-accepted-{corrupt}")
                p1 = self.manifest["phases"][0]
                ref = (
                    p1["review_report"]["report_ref"]
                    if corrupt == "report"
                    else p1["candidate_commit"]["evidence"][0]
                )
                (self.host / ref).write_text("corrupted accepted predecessor\n")
                before = self.local_fix_state_snapshot()
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.start_local_fix(
                        self.path,
                        self.manifest,
                        self.receipt,
                        self.host,
                        self.task,
                        "P2",
                        self.verifier,
                    )
                self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_p2_local_fix_recovery_rejects_corrupt_accepted_predecessor_before_mutation(self):
        for corrupt in ("report", "evidence"):
            with self.subTest(corrupt=corrupt):
                self.setUp()
                self.leave_p2_local_fix_transition_before_reset(
                    f"recover-corrupt-accepted-{corrupt}"
                )
                p1 = self.manifest["phases"][0]
                ref = (
                    p1["review_report"]["report_ref"]
                    if corrupt == "report"
                    else p1["candidate_commit"]["evidence"][0]
                )
                (self.host / ref).write_text("corrupted accepted predecessor\n")
                before = self.local_fix_state_snapshot()
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.recover_local_fix(
                        self.host,
                        "T01",
                        path=self.path,
                        task_root=self.task,
                        verifier=self.verifier,
                    )
                self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_recovery_rejects_corrupt_rejected_candidate_evidence_before_reset(self):
        self.leave_local_fix_transition_pending("recover-corrupt-current-evidence.json")
        phase = json.loads(self.path.read_text())["phases"][0]
        ref = phase["candidate_commit"]["evidence"][0]
        (self.host / ref).write_text("corrupted rejected verification receipt\n")
        before = self.local_fix_state_snapshot()
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_archived_rejected_candidate_evidence_is_rechecked_on_resume_and_accept(self):
        self.enter_local_fix_with_history("archived-evidence-resume")
        cycle = self.manifest["phases"][0]["review_cycles"][0]
        evidence_ref = cycle["candidate_commit"]["evidence"][0]
        (self.host / evidence_ref).write_text("corrupted archived run-check receipt\n")
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)

        self.setUp()
        rejected = self.enter_local_fix_with_history("archived-evidence-accept")
        replacement = self.make_candidate(
            candidate_text="replacement with archived evidence check\n"
        )
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(VERDICT="PASS", FINDINGS=[])
        report.update(self.routed_reviewer_fields(report))
        report_path = self.host / "archived-evidence-accept-pass.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            report,
            self.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        cycle = self.manifest["phases"][0]["review_cycles"][0]
        (self.host / cycle["candidate_commit"]["evidence"][0]).write_text(
            "corrupted archived run-check receipt\n"
        )
        before = self.local_fix_state_snapshot()
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.accept_review(
                self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)
        self.assertNotEqual(rejected["sha"], replacement["sha"])

    def test_resume_returns_executor_repair_stage_after_local_fix_start(self):
        self.enter_local_fix_with_history("resume-active-fix")
        resumed = devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)
        self.assertEqual(resumed["status"], "PHASE_LOCAL_FIX_EXECUTOR_REQUIRED")
        self.assertEqual(resumed["executor_lifecycle"], "LOCAL_FIX_CONTINUATION_ALLOWED")
        self.assertEqual(resumed["executor_role"], "executor")
        self.assertEqual(
            resumed["local_fix_input"]["rejected_candidate_sha"],
            self.manifest["phases"][0]["active_local_fix"]["rejected_candidate_sha"],
        )
        self.assertEqual(
            set(resumed["local_fix_input"]),
            {
                "task_id",
                "phase_id",
                "findings",
                "base_sha",
                "rejected_candidate_sha",
                "owned_scope",
                "acceptance",
                "verification",
            },
        )

    def test_executor_continuation_receipt_binds_live_local_fix_cycle(self):
        rejected = self.enter_local_fix_with_history("continuation-receipt")
        store_path = devcrew_capacity.dispatch_receipt_path(self.host, "T01")
        store = devcrew_dispatch._read(store_path)
        origin_receipt = next(
            row
            for row in store["receipts"]
            if row.get("ROLE") == "executor"
            and row.get("PHASE_ID") == "P1"
            and row.get("WRITER_CYCLE") == 0
        )
        prepared = self.issued_local_fix_route()
        fix = self.manifest["phases"][0]["active_local_fix"]
        report = {
            "STATUS": "FOLLOWUP_SUCCESS",
            "REQUESTED_ROLE": "executor",
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "WRITER_CYCLE": fix["cycle_id"],
            "REJECTED_CANDIDATE_SHA": fix["rejected_candidate_sha"],
            "AGENT_ID": origin_receipt["AGENT_ID"],
            "ORIGIN_SPAWN_RECEIPT_ID": origin_receipt["SPAWN_RECEIPT_ID"],
            "SPAWN_MODEL": origin_receipt["RESOLVED_MODEL"],
            "SPAWN_REASONING_EFFORT": origin_receipt["REASONING_EFFORT"],
            "SPAWN_MODE": origin_receipt["SPAWN_MODE"],
            "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
            "SPAWN_PAYLOAD": prepared["spawn"],
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        }
        continuation = devcrew_dispatch.record_executor_continuation(self.host, report)
        self.assertTrue(continuation["CONTINUATION"])
        self.assertEqual(
            continuation["ORIGIN_SPAWN_RECEIPT_ID"], origin_receipt["SPAWN_RECEIPT_ID"]
        )
        self.assertNotEqual(continuation["ROUTE_ID"], origin_receipt["ROUTE_ID"])
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                self.host,
                continuation["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="executor",
                phase_id="P1",
                base_sha=self.base,
                agent_id=origin_receipt["AGENT_ID"],
                writer_cycle=fix["cycle_id"],
                rejected_candidate_sha=fix["rejected_candidate_sha"],
            )
        )
        candidate = self.make_candidate(
            candidate_text="continued repair candidate\n", executor_receipt_override=continuation
        )
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        target = devcrew_phase.review_target(self.manifest, "P1")
        failure = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        failure.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "continue one more bounded repair",
                    "EVIDENCE": ["one.txt:1"],
                }
            ],
        )
        failure.update(self.routed_reviewer_fields(failure))
        review_path = self.host / "continued-repair-fail.json"
        review_path.write_text(json.dumps(failure))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            failure,
            self.verifier,
            review_path.name,
            review_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.start_local_fix(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            continuation=self.progress_assessment(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        self.assertEqual(
            self.manifest["phases"][0]["review_cycles"][1]["executor_dispatch"]["spawn_receipt_id"],
            continuation["SPAWN_RECEIPT_ID"],
        )
        devcrew_phase._verify_all_review_cycles(self.host, self.task, self.manifest, self.verifier)
        candidate = self.make_candidate(candidate_text="final cycle-two candidate\n")
        self.submit_pass_review("P1", candidate, "continued-repair-pass.json")
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [self.run_full_suite_record("continued-repair-accept")],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                self.host,
                continuation["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="executor",
                phase_id="P1",
                base_sha=self.base,
                agent_id=origin_receipt["AGENT_ID"],
                writer_cycle=fix["cycle_id"],
                rejected_candidate_sha=fix["rejected_candidate_sha"],
            )
        )
        self.assertEqual(len(self.manifest["phases"][0]["review_cycles"]), 2)
        self.assertEqual(rejected["sha"], fix["rejected_candidate_sha"])
        for changed in (
            dict(report, WRITER_CYCLE=fix["cycle_id"] + 1),
            dict(report, REJECTED_CANDIDATE_SHA="0" * 40),
            dict(report, PHASE_ID="P2"),
            dict(report, AGENT_ID="different-agent"),
        ):
            with self.subTest(
                changed={
                    key: changed.get(key)
                    for key in ("WRITER_CYCLE", "REJECTED_CANDIDATE_SHA", "PHASE_ID", "AGENT_ID")
                }
            ):
                with self.assertRaises(devcrew_dispatch.DispatchError):
                    devcrew_dispatch.record_executor_continuation(self.host, changed)

    def test_p2_recovery_rejects_forged_pending_cycle_report_before_reset(self):
        self.leave_p2_local_fix_transition_before_reset("recover-forged-transition")
        manifest = json.loads(self.path.read_text())
        phase = manifest["phases"][1]
        phase["local_fix_transition"]["cycle"]["review_report"]["findings"][0]["SUMMARY"] = (
            "forged finding"
        )
        self.path.write_text(json.dumps(manifest))
        before = self.local_fix_state_snapshot()
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "interrupted local-fix transition does not match"
        ):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_recovery_stop_statuses_do_not_reset_rejected_candidate(self):
        for status in ("BLOCKED", "REPLAN_REQUIRED"):
            with self.subTest(status=status):
                self.setUp()
                self.leave_local_fix_transition_pending(f"recover-stop-{status.lower()}")
                manifest = json.loads(self.path.read_text())
                manifest["phases"][0]["status"] = status
                self.path.write_text(json.dumps(manifest))
                before = self.local_fix_state_snapshot()
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.recover_local_fix(
                        self.host,
                        "T01",
                        path=self.path,
                        task_root=self.task,
                        verifier=self.verifier,
                    )
                self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_initial_executor_receipt_cannot_close_repair_cycle(self):
        stale_executor = self.routed_executor_fields("P1")
        rejected = self.enter_local_fix_with_history("stale-executor-receipt")
        self.assertEqual(
            self.manifest["phases"][0]["active_local_fix"]["rejected_candidate_sha"],
            rejected["sha"],
        )
        verifying = devcrew_phase.set_phase_status(
            self.path,
            self.manifest,
            self.receipt,
            "P1",
            "VERIFYING",
            project=self.host,
            verifier=self.verifier,
        )
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.set_phase_status(
                self.path,
                verifying,
                self.receipt,
                "P1",
                "READY_FOR_CLOSE",
                [".codex/checks/fake.json"],
                self.host,
                self.verifier,
                stale_executor["SPAWN_RECEIPT_ID"],
            )
        self.assertEqual(caught.exception.code, "INVALID_DISPATCH_ROUTE")

    def test_recovery_rejects_tampered_pending_cycle_classification_before_reset(self):
        self.leave_local_fix_transition_pending("recover-tampered-classification")
        manifest = json.loads(self.path.read_text())
        phase = manifest["phases"][0]
        phase["local_fix_transition"]["cycle"]["review_report"]["findings"][0]["CLASSIFICATION"] = (
            "SYSTEMIC_REPLAN"
        )
        self.path.write_text(json.dumps(manifest))
        before = self.local_fix_state_snapshot()
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.recover_local_fix(
                self.host, "T01", path=self.path, task_root=self.task, verifier=self.verifier
            )
        self.assertEqual(self.local_fix_state_snapshot(), before)

    def test_acceptance_rejects_missing_or_edited_host_report(self):
        for tamper in ("delete", "edit"):
            with self.subTest(tamper=tamper):
                self.setUp()
                candidate = self.make_candidate()
                devcrew_phase.begin_review(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    "P1",
                    self.verifier,
                )
                self.receipt = devcrew_worktree.resolve(self.host, "T01")
                self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
                report_path = self.host / "review.json"
                report = {
                    "TASK_ID": "T01",
                    "PHASE_ID": "P1",
                    "BASE_SHA": self.base,
                    "TARGET_SHA": candidate["sha"],
                    "VERDICT": "PASS",
                    "FINDINGS": [],
                }
                report.update(self.routed_reviewer_fields(report))
                report_path.write_text(json.dumps(report))
                devcrew_phase.submit_review(
                    self.path,
                    self.manifest,
                    self.receipt,
                    self.host,
                    self.task,
                    report,
                    self.verifier,
                    "review.json",
                    report_path.read_bytes(),
                )
                if tamper == "delete":
                    report_path.unlink()
                else:
                    report_path.write_text(json.dumps(report | {"TARGET_SHA": self.base}))
                self.receipt = devcrew_worktree.resolve(self.host, "T01")
                self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.resume(self.manifest, self.host, self.task, self.verifier)
                with self.assertRaises(devcrew_phase.PhaseError):
                    devcrew_phase.accept_review(
                        self.path,
                        self.manifest,
                        self.receipt,
                        self.host,
                        self.task,
                        "P1",
                        self.verifier,
                    )
                self.assertEqual(
                    json.loads(self.path.read_text())["phases"][0]["status"], "REVIEWING"
                )

    def test_p3_base_is_immediately_preceding_accepted_phase_sha(self):
        first = self.make_candidate()
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P1", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        pass_report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "TARGET_SHA": first["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        pass_report.update(self.routed_reviewer_fields(pass_report))
        report_path = self.host / "review-p1.json"
        report_path.write_text(json.dumps(pass_report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            pass_report,
            self.verifier,
            "review-p1.json",
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P1",
            self.verifier,
            [self.run_full_suite_record("accept-p1")],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        second = self.make_candidate("P2")
        devcrew_phase.begin_review(
            self.path, self.manifest, self.receipt, self.host, self.task, "P2", self.verifier
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        second_report = {
            "TASK_ID": "T01",
            "PHASE_ID": "P2",
            "BASE_SHA": first["sha"],
            "TARGET_SHA": second["sha"],
            "VERDICT": "PASS",
            "FINDINGS": [],
        }
        second_report.update(self.routed_reviewer_fields(second_report))
        report_path = self.host / "review-p2.json"
        report_path.write_text(json.dumps(second_report))
        devcrew_phase.submit_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            second_report,
            self.verifier,
            "review-p2.json",
            report_path.read_bytes(),
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        devcrew_phase.accept_review(
            self.path,
            self.manifest,
            self.receipt,
            self.host,
            self.task,
            "P2",
            self.verifier,
            [self.run_full_suite_record("accept-p2")],
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.load_for_resume(self.path, self.receipt)
        advanced = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P3", self.host, self.task, self.verifier
        )
        self.assertEqual(advanced["phases"][2]["base_sha"], second["sha"])
        self.assertNotEqual(advanced["phases"][2]["base_sha"], first["sha"])
        self.assertEqual(
            devcrew_phase.executor_input(advanced, self.task, self.host, self.verifier)["base_sha"],
            second["sha"],
        )
        self.assertEqual(advanced["phases"][1]["accepted_sha"], second["sha"])

    def test_advance_rejects_deleted_accepted_predecessor_report(self):
        first = self.make_candidate()
        self.accept_candidate("P1", first, "review-p1.json")
        (self.host / "review-p1.json").unlink()
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.advance_phase(
                self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
            )

    def test_later_phase_resume_and_dispatch_reject_edited_predecessor_report(self):
        first = self.make_candidate()
        self.accept_candidate("P1", first, "review-p1.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        (self.host / "review-p1.json").write_text("changed\n")
        loaded = devcrew_phase.load_for_resume(
            self.path, devcrew_worktree.resolve(self.host, "T01")
        )
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.resume(loaded, self.host, self.task, self.verifier)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.executor_input(loaded, self.task, self.host, self.verifier)

    def test_later_phase_resume_and_dispatch_reject_altered_accepted_tree(self):
        first = self.make_candidate()
        self.accept_candidate("P1", first, "review-p1.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        self.manifest["phases"][0]["candidate_commit"]["tree"] = "0" * len(first["tree"])
        self.path.write_text(json.dumps(self.manifest))
        loaded = devcrew_phase.load_for_resume(
            self.path, devcrew_worktree.resolve(self.host, "T01")
        )
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.resume(loaded, self.host, self.task, self.verifier)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.executor_input(loaded, self.task, self.host, self.verifier)

    def test_later_phase_resume_and_dispatch_reject_dirty_accepted_boundary(self):
        first = self.make_candidate()
        self.accept_candidate("P1", first, "review-p1.json")
        self.manifest = devcrew_phase.advance_phase(
            self.path, self.manifest, self.receipt, "P2", self.host, self.task, self.verifier
        )
        (self.task / "one.txt").write_text("dirty after accepted boundary\n")
        loaded = devcrew_phase.load_for_resume(
            self.path, devcrew_worktree.resolve(self.host, "T01")
        )
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.resume(loaded, self.host, self.task, self.verifier)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.executor_input(loaded, self.task, self.host, self.verifier)


if __name__ == "__main__":
    unittest.main()
