"""Pre-candidate contract refresh and exact environment fingerprint regressions."""

import json
import hashlib
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import devcrew_capacity
import devcrew_dispatch
import devcrew_context
import devcrew_phase
import devcrew_worktree
import devcrew_cost
import test_phase_review_lifecycle
from hooks import user_prompt_submit


def planner_receipt(project, task_id, base, message):
    receipt = devcrew_worktree.resolve(project, task_id)
    selection = devcrew_phase._source_snapshot_path(
        pathlib.Path(receipt["task_worktree_root"]), task_id, "0" * 64
    ).with_name("planning-source.json")
    if not selection.exists():
        path = project / "planner-source.txt"
        path.write_text("Fixture task source")
        devcrew_phase.bind_intent_source(
            project,
            receipt,
            {
                "mode": "file",
                "source_type": "document",
                "path": str(path),
                "expected_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "origin": "fixture document",
            },
        )
        path.unlink()
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
        identity={"TASK_ID": task_id, "BASE_SHA": base},
        intent={
            "role": "planner",
            "task_name": "planner_" + task_id,
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
            "BASE_SHA": base,
            "AGENT_ID": "planner-agent",
            "SPAWN_MODEL": issued["PREPARED"]["route"]["RESOLVED_MODEL"],
            "SPAWN_REASONING_EFFORT": issued["PREPARED"]["route"]["REASONING_EFFORT"],
            "SPAWN_MODE": issued["PREPARED"]["dispatch"]["SPAWN_MODE"],
            "PREPARED": issued["PREPARED"],
            "SPAWN_PAYLOAD": issued["SPAWN_PAYLOAD"],
            "SPAWN_PAYLOAD_SHA256": issued["SPAWN_PAYLOAD_SHA256"],
        },
    )


class PreCandidateReapprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = pathlib.Path(self.temp.name) / "host"
        self.host.mkdir()
        self.git(self.host, "init", "-q")
        self.git(self.host, "config", "user.name", "Test")
        self.git(self.host, "config", "user.email", "test@example.com")
        (self.host / ".gitignore").write_text("/.codex/\n")
        (self.host / "source.py").write_text("base\n")
        self.git(self.host, "add", ".")
        self.git(self.host, "commit", "-qm", "base")
        self.base = self.git(self.host, "rev-parse", "HEAD")
        devcrew_worktree.create(
            self.host, "T01", self.base, worktree_request="Use a worktree for this task"
        )
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.task = pathlib.Path(self.receipt["task_worktree_root"])
        self.phase = {
            "phase_id": "P1",
            "goal": "change source",
            "depends_on": [],
            "owned_scope": ["source.py"],
            "acceptance": ["source changes"],
            "verification": ["verify source"],
            "verification_bindings": [
                {
                    "index": 0,
                    "verification": "verify source",
                    "argv": [sys.executable, "-c", "pass"],
                }
            ],
            "commit_boundary": "P1 capability",
            "commit_subject": "type(T01): change source",
            "status": "PLANNED",
        }
        self.plan = {
            "schema_version": 1,
            "task_id": "T01",
            "task_mode": "SINGLE_PHASE",
            "semantic_contract": {"capabilities": ["recovery"]},
            "worktree": {
                "repository_id": self.receipt["repository_id"],
                "task_worktree_root": str(self.task),
                "branch": self.receipt["branch"],
                "base_sha": self.base,
            },
            "current_phase_id": "P1",
            "phases": [self.phase],
        }
        planner = planner_receipt(self.host, "T01", self.base, "initial planner")
        self.plan["planner_dispatch"] = {"spawn_receipt_id": planner["SPAWN_RECEIPT_ID"]}
        self.path = devcrew_phase.manifest_path(self.host, "T01")
        devcrew_phase.initialize_manifest(
            self.path, self.plan, self.receipt, self.host, planner["SPAWN_RECEIPT_ID"]
        )

    @staticmethod
    def git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    def replacement(self, *, goal="refreshed goal"):
        value = json.loads(json.dumps(self.plan))
        value.pop("planner_dispatch", None)
        phase = value["phases"][0]
        phase["goal"] = goal
        phase["verification_bindings"] = [
            {
                "index": 0,
                "verification": "verify source",
                "argv": [sys.executable, "-c", "print('fresh')"],
            }
        ]
        return value

    def v2_plan(self):
        source = devcrew_phase.bind_intent_source(
            self.task,
            self.receipt,
            {
                "mode": "unavailable",
                "source_type": "user_prompt",
                "origin": "original request",
                "reason": "the transient capture was unavailable",
            },
        )
        value = json.loads(json.dumps(self.plan))
        value.pop("planner_dispatch", None)
        value["schema_version"] = 2
        verified = devcrew_phase.intent_source_preflight(self.host, self.receipt)
        value["original_intent"] = {
            "sources": [source, verified],
            "requirements": [],
            "decisions": [],
            "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
            "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
        }
        return value

    def v2_assessment(self, manifest, *, previous=None):
        source = manifest["original_intent"]["sources"][0]
        return {
            "task_id": "T01",
            "plan_sha256": devcrew_phase.intent_plan_digest(manifest),
            "previous_plan_sha256": previous,
            "mode": "DIAGNOSIS",
            "result": "COMPLETE",
            "source_reads": [
                {
                    "source_id": row["source_id"],
                    "sha256": row["sha256"],
                    "findings": "Read the verified task document for diagnosis.",
                }
                for row in manifest["original_intent"]["sources"]
                if row["status"] == "VERIFIED"
            ],
            "unavailable_sources": [
                {
                    "source_id": source["source_id"],
                    "limitation": "The transient prompt capture was unavailable for this acceptance.",
                }
            ],
            "source_outcomes": [],
            "obligation_checks": [],
            "technical_change_rationale": "The phase investigates the request without inventing product requirements.",
        }

    def environment_reapproval_fixture(self, *, schema_v2=False):
        fixture = test_phase_review_lifecycle.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if schema_v2:
            plan = json.loads(json.dumps(fixture.manifest))
            source = devcrew_phase.bind_intent_source(
                fixture.task,
                fixture.receipt,
                {
                    "mode": "unavailable",
                    "source_type": "user_prompt",
                    "origin": "original request",
                    "reason": "the transient capture was unavailable",
                },
            )
            plan["schema_version"] = 2
            verified = devcrew_phase.intent_source_preflight(fixture.host, fixture.receipt)
            plan["original_intent"] = {
                "sources": [source, verified],
                "requirements": [],
                "decisions": [],
                "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
            }
            for phase in plan["phases"]:
                phase["verification_bindings"] = devcrew_phase.verification_bindings(
                    phase["verification"], phase["verification_bindings"]
                )
            planner = test_phase_review_lifecycle.issue_planner_receipt(
                fixture.host,
                "T01",
                fixture.base,
                "schema v2 environment reapproval initial planner",
                "v2-initial-planner",
            )
            fixture.path.unlink()
            assessment = {
                "task_id": "T01",
                "plan_sha256": devcrew_phase.intent_plan_digest(plan),
                "previous_plan_sha256": None,
                "mode": "DIAGNOSIS",
                "result": "COMPLETE",
                "source_reads": [
                    {
                        "source_id": verified["source_id"],
                        "sha256": verified["sha256"],
                        "findings": "Read the separate task document.",
                    }
                ],
                "unavailable_sources": [
                    {
                        "source_id": source["source_id"],
                        "limitation": "The transient original request capture was unavailable.",
                    }
                ],
                "source_outcomes": [],
                "obligation_checks": [],
                "technical_change_rationale": "The phase investigates the request without inventing product requirements.",
            }
            fixture.manifest = devcrew_phase.initialize_manifest(
                fixture.path,
                plan,
                fixture.receipt,
                fixture.host,
                planner["SPAWN_RECEIPT_ID"],
                intent_assessment={"assessment": assessment, "decisions": []},
            )
        fixture.enter_local_fix_with_history("environment-reapproval")
        verifying = devcrew_phase.set_phase_status(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            "P1",
            "VERIFYING",
            project=fixture.host,
            verifier=fixture.verifier,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        evidence = fixture.run_full_suite_record("environment-before-reapproval")
        fixture.manifest = devcrew_phase.set_phase_status(
            fixture.path,
            verifying,
            fixture.receipt,
            "P1",
            "READY_FOR_CLOSE",
            [evidence],
            fixture.host,
            fixture.verifier,
            fixture.routed_executor_fields("P1")["SPAWN_RECEIPT_ID"],
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        replacement = {
            key: json.loads(json.dumps(fixture.manifest[key]))
            for key in (
                "schema_version",
                "task_id",
                "task_mode",
                "semantic_contract",
                "worktree",
                "current_phase_id",
                "phases",
            )
        }
        if fixture.manifest["schema_version"] == 2:
            replacement["original_intent"] = json.loads(
                json.dumps(fixture.manifest["original_intent"])
            )
        current = fixture.manifest["phases"][0]
        replacement["phases"][0] = {
            key: json.loads(json.dumps(current[key]))
            for key in (devcrew_phase._REAPPROVAL_FIELDS | {"phase_id", "depends_on"})
            if key in current
        }
        replacement["phases"][0]["status"] = "PLANNED"
        planner = planner_receipt(
            fixture.host, "T01", fixture.base, "explicit environment reapproval"
        )
        return fixture, replacement, planner

    def test_environment_only_reapproval_preserves_local_fix_history_and_invalidates_old_writer(
        self,
    ):
        fixture, replacement, planner = self.environment_reapproval_fixture()
        previous = json.loads(json.dumps(fixture.manifest))
        previous_phase = previous["phases"][0]
        index_before = fixture.git(fixture.task, "rev-parse", "HEAD^{tree}")
        *_, state_path = devcrew_worktree.location(fixture.host, "T01")
        receipt_before = state_path.read_bytes()
        approved_environment = devcrew_phase.environment_digest()

        planner_path = pathlib.Path(self.temp.name) / "environment-only-plan.json"
        planner_path.write_text(json.dumps(replacement))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(fixture.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
                "--environment-only",
                "--approve-environment-sha256",
                approved_environment,
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["status"], "PRE_CANDIDATE_ENVIRONMENT_REAPPROVED"
        )
        updated = json.loads(fixture.path.read_text())

        phase = updated["phases"][0]
        archive = updated["pre_candidate_reapprovals"][-1]
        self.assertEqual(
            updated.get("contract_generation", 0), previous.get("contract_generation", 0) + 1
        )
        self.assertEqual(phase["status"], "EXECUTING")
        self.assertEqual(phase["review_cycles"], previous_phase["review_cycles"])
        self.assertEqual(phase["active_local_fix"], previous_phase["active_local_fix"])
        self.assertEqual(archive["previous_contract"], previous_phase)
        self.assertEqual(archive["reapproval_mode"], "ENVIRONMENT_ONLY")
        self.assertNotIn("evidence", phase)
        self.assertNotIn("executor_dispatch", phase)
        self.assertEqual(phase["verification_environment_sha256"], approved_environment)
        self.assertEqual(phase["verification_bindings"], previous_phase["verification_bindings"])
        self.assertEqual(phase["verification"], previous_phase["verification"])
        self.assertEqual(fixture.git(fixture.task, "write-tree"), index_before)
        self.assertEqual(fixture.git(fixture.task, "rev-parse", "HEAD"), fixture.base)
        self.assertEqual(state_path.read_bytes(), receipt_before)
        new_executor = fixture.routed_executor_fields("P1")
        devcrew_dispatch.verify_spawn(
            fixture.host,
            new_executor["SPAWN_RECEIPT_ID"],
            task_id="T01",
            role="executor",
            phase_id="P1",
            base_sha=fixture.base,
            writer_cycle=phase["active_local_fix"]["cycle_id"],
            rejected_candidate_sha=phase["active_local_fix"]["rejected_candidate_sha"],
        )
        with self.assertRaises(devcrew_dispatch.DispatchError):
            devcrew_dispatch.verify_spawn(
                fixture.host,
                previous_phase["executor_dispatch"]["spawn_receipt_id"],
                task_id="T01",
                role="executor",
                phase_id="P1",
                base_sha=fixture.base,
                writer_cycle=previous_phase["executor_dispatch"]["writer_cycle"],
                rejected_candidate_sha=previous_phase["executor_dispatch"][
                    "rejected_candidate_sha"
                ],
            )

    def test_environment_reapproval_rejects_tampered_archived_executor_issuance(self):
        fixture, replacement, planner = self.environment_reapproval_fixture()
        fixture.manifest = devcrew_phase.reapprove_pre_candidate(
            fixture.path,
            replacement,
            fixture.receipt,
            fixture.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            environment_only=True,
            approved_environment_sha256=devcrew_phase.environment_digest(),
            verifier=fixture.verifier,
        )
        archived = fixture.manifest["phases"][0]["review_cycles"][0]["executor_dispatch"]
        store_path = devcrew_capacity.dispatch_receipt_path(fixture.host, "T01")
        store = devcrew_dispatch._read(store_path)
        issued = next(
            row for row in store["issued_routes"] if row.get("ROUTE_ID") == archived["route_id"]
        )
        issued["WORKTREE_BINDING"]["expected_head"] = "f" * 40
        devcrew_dispatch._write(store_path, store)

        with self.assertRaisesRegex(
            devcrew_phase.PhaseError,
            "INVALID_EXECUTOR_ROUTE.*historical executor receipt or issuance is inconsistent",
        ):
            devcrew_phase.executor_input(
                fixture.manifest, fixture.task, fixture.host, fixture.verifier
            )

    def test_environment_only_reapproval_rejects_wrong_approval_and_contract_change_without_writes(
        self,
    ):
        fixture, replacement, planner = self.environment_reapproval_fixture()
        before = fixture.path.read_bytes()
        *_, state_path = devcrew_worktree.location(fixture.host, "T01")
        receipt_before = state_path.read_bytes()
        index_before = fixture.git(fixture.task, "rev-parse", "HEAD^{tree}")
        correct_environment = devcrew_phase.environment_digest()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_ENVIRONMENT_APPROVAL"):
            devcrew_phase.reapprove_pre_candidate(
                fixture.path,
                replacement,
                fixture.receipt,
                fixture.host,
                "P1",
                planner["SPAWN_RECEIPT_ID"],
                environment_only=True,
                approved_environment_sha256="0" * 64,
                verifier=fixture.verifier,
            )
        replacement["phases"][0]["goal"] = "changed goal"
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH"
        ):
            devcrew_phase.reapprove_pre_candidate(
                fixture.path,
                replacement,
                fixture.receipt,
                fixture.host,
                "P1",
                planner["SPAWN_RECEIPT_ID"],
                environment_only=True,
                approved_environment_sha256=correct_environment,
                verifier=fixture.verifier,
            )
        replacement["phases"][0]["goal"] = fixture.manifest["phases"][0]["goal"]
        old_planner_id = fixture.manifest["planner_dispatch"]["spawn_receipt_id"]
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PLANNER_ROUTE"):
            devcrew_phase.reapprove_pre_candidate(
                fixture.path,
                replacement,
                fixture.receipt,
                fixture.host,
                "P1",
                old_planner_id,
                environment_only=True,
                approved_environment_sha256=correct_environment,
                verifier=fixture.verifier,
            )
        self.assertEqual(fixture.path.read_bytes(), before)
        self.assertEqual(state_path.read_bytes(), receipt_before)
        self.assertEqual(fixture.git(fixture.task, "rev-parse", "HEAD^{tree}"), index_before)

    def test_environment_only_reapproval_preserves_v2_sources_and_assessment_without_recertifying(
        self,
    ):
        fixture, replacement, planner = self.environment_reapproval_fixture(schema_v2=True)
        original_intent = json.loads(json.dumps(fixture.manifest["original_intent"]))
        original_digest = devcrew_phase.intent_plan_digest(fixture.manifest)
        replacement["original_intent"]["sources"][0]["provenance"]["reason"] = "changed source"
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_IMMUTABILITY_VIOLATION"):
            devcrew_phase.reapprove_pre_candidate(
                fixture.path,
                replacement,
                fixture.receipt,
                fixture.host,
                "P1",
                planner["SPAWN_RECEIPT_ID"],
                environment_only=True,
                approved_environment_sha256=devcrew_phase.environment_digest(),
                verifier=fixture.verifier,
            )
        replacement["original_intent"] = json.loads(json.dumps(original_intent))
        updated = devcrew_phase.reapprove_pre_candidate(
            fixture.path,
            replacement,
            fixture.receipt,
            fixture.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            environment_only=True,
            approved_environment_sha256=devcrew_phase.environment_digest(),
            verifier=fixture.verifier,
        )
        self.assertEqual(updated["original_intent"], original_intent)
        self.assertEqual(devcrew_phase.intent_plan_digest(updated), original_digest)
        self.assertEqual(
            updated["original_intent"]["planning_assessment"]["assessment"],
            original_intent["planning_assessment"]["assessment"],
        )

    def test_environment_reapproval_history_survives_candidate_resume_and_formal_acceptance(self):
        fixture, replacement, planner = self.environment_reapproval_fixture()
        fixture.manifest = devcrew_phase.reapprove_pre_candidate(
            fixture.path,
            replacement,
            fixture.receipt,
            fixture.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            environment_only=True,
            approved_environment_sha256=devcrew_phase.environment_digest(),
            verifier=fixture.verifier,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        executor = fixture.routed_executor_fields("P1")
        rejected = fixture.make_candidate(
            candidate_text="candidate after environment-only reapproval\n",
            executor_receipt_override=executor,
        )
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
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        target = devcrew_phase.review_target(fixture.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "LOCAL_FIX",
                    "SUMMARY": "repair candidate after environment-only reapproval",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(fixture.routed_reviewer_fields(report))
        report_path = fixture.host / "environment-reapproval-local-fix.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            report,
            fixture.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        continuation = fixture.progress_assessment()
        devcrew_phase.start_local_fix(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
            continuation=continuation,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        self.assertEqual(
            fixture.manifest["phases"][0]["review_cycles"][-1]["candidate_commit"]["sha"],
            rejected["sha"],
        )

        candidate = fixture.make_candidate(
            candidate_text="replacement after environment-only reapproval\n"
        )
        fixture.accept_candidate("P1", candidate, "environment-reapproval-pass.json")
        self.assertEqual(fixture.manifest["phases"][0]["status"], "ACCEPTED")

        tampered_current = json.loads(json.dumps(fixture.manifest))
        tampered_current["phases"][0]["goal"] = "unauthorized technical mutation"
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PRE_CANDIDATE_HISTORY"):
            devcrew_phase._verify_pre_candidate_history(fixture.host, tampered_current)
        tampered_archive = json.loads(json.dumps(fixture.manifest))
        archive = tampered_archive["pre_candidate_reapprovals"][0]
        archive["previous_contract"]["active_local_fix"]["findings"][0]["SUMMARY"] = (
            "tampered history"
        )
        archive_body = {key: value for key, value in archive.items() if key != "archive_sha256"}
        archive["archive_sha256"] = hashlib.sha256(
            json.dumps(
                archive_body, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ).encode()
        ).hexdigest()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PRE_CANDIDATE_HISTORY"):
            devcrew_phase._verify_pre_candidate_history(fixture.host, tampered_archive)

    def test_environment_reapproval_history_accepts_authenticated_systemic_contract_change(self):
        fixture, replacement, planner = self.environment_reapproval_fixture()
        fixture.manifest = devcrew_phase.reapprove_pre_candidate(
            fixture.path,
            replacement,
            fixture.receipt,
            fixture.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            environment_only=True,
            approved_environment_sha256=devcrew_phase.environment_digest(),
            verifier=fixture.verifier,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        executor = fixture.routed_executor_fields("P1")
        rejected = fixture.make_candidate(
            candidate_text="candidate requiring authorized contract change\n",
            executor_receipt_override=executor,
        )
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
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        target = devcrew_phase.review_target(fixture.manifest, "P1")
        report = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        report.update(
            VERDICT="FAIL",
            FINDINGS=[
                {
                    "CLASSIFICATION": "SYSTEMIC_REPLAN",
                    "SUMMARY": "phase contract needs an authorized technical correction",
                    "EVIDENCE": ["tests/test_widget.py:42"],
                }
            ],
        )
        report.update(fixture.routed_reviewer_fields(report))
        report_path = fixture.host / "environment-reapproval-systemic-fail.json"
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            report,
            fixture.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        proposed = json.loads(json.dumps(fixture.manifest))
        proposed.pop("planner_dispatch", None)
        changed_phase = proposed["phases"][0]
        for field in (
            "candidate_commit",
            "review_report",
            "review_start",
            "evidence",
            "review_cycles",
        ):
            changed_phase.pop(field, None)
        changed_phase["status"] = "PLANNED"
        changed_phase["goal"] = "authorized replacement contract after environment reapproval"
        replan = planner_receipt(
            fixture.host,
            "T01",
            rejected["sha"],
            "authorize replacement contract after environment reapproval",
        )
        continuation = fixture.progress_assessment()
        result = devcrew_phase.complete_systemic_replan(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            proposed,
            replan["SPAWN_RECEIPT_ID"],
            fixture.verifier,
            continuation=continuation,
        )
        self.assertEqual(result["status"], "PHASE_REPLAN_COMPLETED")
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        self.assertEqual(
            fixture.manifest["phases"][0]["goal"],
            "authorized replacement contract after environment reapproval",
        )

    def captured_source(self, turn_id, prompt):
        plugin_data = pathlib.Path(self.temp.name) / "plugin-data"
        plugin_data.mkdir(exist_ok=True)
        contract = {
            "task_id": "T01",
            "goal": prompt,
            "requirements": [prompt],
            "acceptance": ["Observable recommendation"],
            "non_goals": [],
        }
        path = pathlib.Path(self.temp.name) / (turn_id + ".json")
        path.write_text(json.dumps(contract))
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        event = {
            "session_id": "session-1",
            "turn_id": turn_id,
            "cwd": str(self.task),
            "prompt": user_prompt_submit.confirmation_text("T01", digest),
        }
        identity = user_prompt_submit.capture_prompt(plugin_data, event, self.task)
        record = devcrew_phase.bind_intent_source(
            self.task,
            self.receipt,
            {
                "mode": "contract",
                "plugin_data": str(plugin_data),
                "path": str(path),
                "confirmation_source_id": identity["source_id"],
                "expected_sha256": digest,
            },
        )
        data = (self.task / record["snapshot"]).read_bytes()
        fragment = {
            "source_id": record["source_id"],
            "start_byte": 0,
            "end_byte": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        return record, fragment

    def test_v2_last_obligation_removal_accepts_empty_implementation_outcomes(self):
        original, original_fragment = self.captured_source(
            "request", "Recommend a card when SBP is blocked."
        )
        answer, answer_fragment = self.captured_source(
            "answer", "I no longer need that card recommendation."
        )
        plan = self.v2_plan()
        phase = plan["phases"][0]
        phase["acceptance"] = ["Investigate the request and report findings"]
        requirement = {
            "requirement_id": "R1",
            "kind": "explicit_user_obligation",
            "statement": "Recommend a card when SBP is blocked.",
            "source_fragments": [original_fragment],
            "acceptance_refs": [
                {
                    "phase_id": "P1",
                    "acceptance_index": 0,
                    "sha256": hashlib.sha256(phase["acceptance"][0].encode()).hexdigest(),
                }
            ],
            "evidence_records": [],
        }
        before = {key: requirement[key] for key in ("kind", "statement", "source_fragments")}
        plan["original_intent"]["sources"] = [original, answer]
        plan["original_intent"]["requirements"] = [requirement]
        decision = {
            "decision_id": "D1",
            "source_fragments": [answer_fragment],
            "changes": [{"requirement_id": "R1", "before": before, "after": None}],
            "human_confirmation": {
                "actor": "user",
                "channel": "codex_user_message",
                "interpretation": "The user explicitly removed the card recommendation.",
                "authority_rationale": "The answer was captured from the user.",
            },
        }
        devcrew_phase.pin_verification(phase)
        assessed_plan = json.loads(json.dumps(plan))
        assessed_plan["original_intent"]["decisions"] = [decision]
        assessment = {
            "task_id": "T01",
            "plan_sha256": devcrew_phase.intent_plan_digest(assessed_plan),
            "previous_plan_sha256": None,
            "mode": "IMPLEMENTATION",
            "result": "COMPLETE",
            "source_reads": [
                {
                    "source_id": row["source_id"],
                    "sha256": row["sha256"],
                    "findings": "Read the captured original request or the user's removal.",
                }
                for row in (original, answer)
            ],
            "unavailable_sources": [],
            "source_outcomes": [],
            "obligation_checks": [
                {
                    "requirement_id": "R1",
                    "change": "USER_REMOVED",
                    "preserved_outcome": "The user withdrew the recommendation requirement.",
                    "safety_rationale": "No recommendation remains required.",
                    "acceptance_rationale": "The plan reports investigation findings.",
                    "decision_id": "D1",
                }
            ],
            "technical_change_rationale": "The captured answer removes the sole explicit obligation.",
        }
        surviving = json.loads(json.dumps(plan))
        surviving_assessment = json.loads(json.dumps(assessment))
        surviving_assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(surviving)
        surviving_assessment["obligation_checks"][0].update(
            {"change": "PRESERVED", "decision_id": None}
        )
        surviving_planner = planner_receipt(
            self.host, "T01", self.base, "empty implementation with active obligation"
        )
        surviving_path = self.host / "surviving-obligation.json"
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "empty source outcomes require"):
            devcrew_phase.initialize_manifest(
                surviving_path,
                surviving,
                self.receipt,
                self.host,
                surviving_planner["SPAWN_RECEIPT_ID"],
                intent_assessment={"assessment": surviving_assessment, "decisions": []},
            )
        self.assertFalse(surviving_path.exists())
        planner = planner_receipt(self.host, "T01", self.base, "last-obligation removal plan")
        path = self.host / "removed-obligation.json"
        result = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": [decision]},
        )
        self.assertIsNone(devcrew_phase.effective_intent_requirements(result)["R1"])
        self.assertEqual(
            result["original_intent"]["planning_assessment"]["assessment"]["mode"], "IMPLEMENTATION"
        )

        wrong_removal = json.loads(json.dumps(result))
        first = wrong_removal["original_intent"]["decisions"][0]
        amended = {
            "kind": "explicit_user_obligation",
            "statement": "Recommend a card only after checking the account.",
            "source_fragments": [answer_fragment],
        }
        first["changes"][0]["after"] = amended
        second = json.loads(json.dumps(first))
        second["decision_id"] = "D2"
        second["changes"][0] = {"requirement_id": "R1", "before": amended, "after": None}
        wrong_removal["original_intent"]["decisions"].append(second)
        wrong_assessment = wrong_removal["original_intent"]["planning_assessment"]["assessment"]
        wrong_assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(wrong_removal)
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "removal check must cite the decision that removes"
        ):
            devcrew_phase.validate_manifest(wrong_removal, self.receipt, self.task)

        replacement = json.loads(json.dumps(plan))
        replacement["phases"][0]["goal"] = "report findings after the user's change"
        replacement["original_intent"] = json.loads(json.dumps(result["original_intent"]))
        devcrew_phase.pin_verification(replacement["phases"][0])
        for key in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement["phases"][0].pop(key, None)
        replacement_assessment = {
            "task_id": "T01",
            "plan_sha256": devcrew_phase.intent_plan_digest(replacement),
            "previous_plan_sha256": devcrew_phase.intent_plan_digest(result),
            "mode": "IMPLEMENTATION",
            "result": "COMPLETE",
            "source_reads": [
                {
                    "source_id": row["source_id"],
                    "sha256": row["sha256"],
                    "findings": "The original request and addressed removal remain in the immutable source history.",
                }
                for row in (original, answer)
            ],
            "unavailable_sources": [],
            "source_outcomes": [],
            "obligation_checks": [],
            "technical_change_rationale": "The technical phase wording changed; the addressed removal remains effective.",
        }
        next_planner = planner_receipt(
            self.host, "T01", self.base, "technical reapproval after addressed removal"
        )
        updated = devcrew_phase.reapprove_pre_candidate(
            path,
            replacement,
            self.receipt,
            self.host,
            "P1",
            next_planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": replacement_assessment, "decisions": []},
        )
        self.assertEqual(updated["original_intent"]["planning_assessment"]["history"], [assessment])
        self.assertEqual(
            updated["pre_candidate_reapprovals"][0]["original_intent"], result["original_intent"]
        )
        resumed = devcrew_phase.load_for_resume(path, self.receipt, self.host)
        self.assertIsNone(devcrew_phase.effective_intent_requirements(resumed)["R1"])

    def test_digest_ignores_only_tool_call_id(self):
        base = {"A": "1", "CODEX_TOOL_CALL_ID": "first"}
        self.assertEqual(
            devcrew_phase.environment_digest(base),
            devcrew_phase.environment_digest({**base, "CODEX_TOOL_CALL_ID": "second"}),
        )
        self.assertNotEqual(
            devcrew_phase.environment_digest(base), devcrew_phase.environment_digest({"A": "2"})
        )
        self.assertNotEqual(
            devcrew_phase.environment_digest(base),
            devcrew_phase.environment_digest({"A": "1", "B": "2"}),
        )
        self.assertNotEqual(
            devcrew_phase.environment_digest({"A": "1", "B": "2"}),
            devcrew_phase.environment_digest({"A": "1"}),
        )

    def test_v1_initialization_rejects_intent_assessment_input(self):
        planner = planner_receipt(self.host, "T01", self.base, "v1 cannot accept intent assessment")
        fresh = self.host / "fresh-v1.json"
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_INTENT_ASSESSMENT"):
            devcrew_phase.initialize_manifest(
                fresh,
                self.plan,
                self.receipt,
                self.host,
                planner["SPAWN_RECEIPT_ID"],
                intent_assessment={"assessment": {}, "decisions": []},
            )
        self.assertFalse(fresh.exists())

    def test_cli_init_accepts_separate_host_intent_assessment(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        self.path.unlink()
        planner = planner_receipt(self.host, "T01", self.base, "v2 CLI initialization")
        planner_path = pathlib.Path(self.temp.name) / "cli-v2-plan.json"
        assessment_path = pathlib.Path(self.temp.name) / "cli-v2-assessment.json"
        assessment = self.v2_assessment(plan)
        planner_path.write_text(json.dumps(plan))
        assessment_path.write_text(json.dumps({"assessment": assessment, "decisions": []}))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "init",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
                "--intent-assessment",
                str(assessment_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(result.stdout)["manifest"]
        self.assertEqual(saved["original_intent"]["planning_assessment"]["assessment"], assessment)

    def test_cli_intent_assessment_rejects_bad_input_before_worktree_recovery(self):
        missing = pathlib.Path(self.temp.name) / "missing-assessment.json"
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "resume",
                "--project",
                str(self.host),
                "--task-id",
                "NO_SUCH_TASK",
                "--intent-assessment",
                str(missing),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "INVALID_INTENT_ASSESSMENT")

    def test_cli_intent_assessment_rejects_oversize_and_non_object(self):
        oversized = pathlib.Path(self.temp.name) / "oversized-assessment.json"
        oversized.write_bytes(b" " * (1024 * 1024 + 1))
        non_object = pathlib.Path(self.temp.name) / "array-assessment.json"
        non_object.write_text("[]", encoding="utf-8")
        for source in (oversized, non_object):
            with self.subTest(source=source.name):
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/devcrew.py"),
                        "phase",
                        "resume",
                        "--project",
                        str(self.host),
                        "--task-id",
                        "NO_SUCH_TASK",
                        "--intent-assessment",
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 2)
                self.assertEqual(json.loads(result.stderr)["error"], "INVALID_INTENT_ASSESSMENT")

    def test_cli_intent_assessment_rejects_unsupported_operation_before_recovery(self):
        assessment = pathlib.Path(self.temp.name) / "unsupported-assessment.json"
        assessment.write_text("{}", encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "resume",
                "--project",
                str(self.host),
                "--task-id",
                "NO_SUCH_TASK",
                "--intent-assessment",
                str(assessment),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["error"], "INVALID_INTENT_ASSESSMENT")
        self.assertIn("supported only", json.loads(result.stderr)["message"])

    def test_cli_v2_pre_candidate_reapproval_accepts_separate_assessment(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        first = planner_receipt(self.host, "T01", self.base, "initialize v2 before CLI reapproval")
        initial_assessment = self.v2_assessment(plan)
        path = self.path
        path.unlink()
        initial = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            first["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": initial_assessment, "decisions": []},
        )
        replacement = json.loads(json.dumps(plan))
        replacement["phases"][0]["goal"] = "diagnose the captured request"
        replacement["phases"][0]["acceptance"] = ["record observed facts and diagnostic result"]
        devcrew_phase.pin_verification(replacement["phases"][0])
        for field in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement["phases"][0].pop(field, None)
        replacement["original_intent"] = json.loads(json.dumps(initial["original_intent"]))
        previous = devcrew_phase.intent_plan_digest(initial)
        assessment = self.v2_assessment(replacement, previous=previous)
        replacement_path = pathlib.Path(self.temp.name) / "cli-v2-replacement.json"
        assessment_path = pathlib.Path(self.temp.name) / "cli-v2-reapproval-assessment.json"
        replacement_path.write_text(json.dumps(replacement))
        assessment_path.write_text(json.dumps({"assessment": assessment, "decisions": []}))
        planner = planner_receipt(self.host, "T01", self.base, "CLI v2 reapproval")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(replacement_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
                "--intent-assessment",
                str(assessment_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(path.read_text())
        self.assertEqual(saved["original_intent"]["planning_assessment"]["assessment"], assessment)
        self.assertEqual(
            saved["pre_candidate_reapprovals"][0]["original_intent"], initial["original_intent"]
        )

    def test_cli_v2_replan_complete_accepts_separate_assessment(self):
        fixture = test_phase_review_lifecycle.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        _, _, _, replacement, planner_id = fixture._systemic_replan_fixture()
        authorization = fixture._make_v2_systemic_state(replacement, explicit_obligation=False)
        replacement_path = pathlib.Path(fixture.temp.name) / "cli-v2-replan.json"
        assessment_path = pathlib.Path(fixture.temp.name) / "cli-v2-replan-assessment.json"
        replacement_path.write_text(json.dumps(replacement))
        assessment_path.write_text(json.dumps(authorization))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "replan-complete",
                "--project",
                str(fixture.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(replacement_path),
                "--spawn-receipt-id",
                planner_id,
                "--intent-assessment",
                str(assessment_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        saved = devcrew_phase.load_for_resume(fixture.path, fixture.receipt, fixture.host)
        self.assertEqual(
            saved["original_intent"]["planning_assessment"]["assessment"],
            authorization["assessment"],
        )

    def test_v2_initialization_requires_and_installs_separate_host_assessment(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        planner = planner_receipt(self.host, "T01", self.base, "v2 initial planner")
        path = self.host / "v2-manifest.json"
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "INTENT_PLANNING_ASSESSMENT_REQUIRED"
        ):
            devcrew_phase.initialize_manifest(
                path, plan, self.receipt, self.host, planner["SPAWN_RECEIPT_ID"]
            )
        self.assertFalse(path.exists())
        assessment = self.v2_assessment(plan)
        result = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )
        self.assertEqual(result["original_intent"]["planning_assessment"]["assessment"], assessment)
        self.assertEqual(
            result["original_intent"]["task_acceptance"], {"status": "NOT_ASSESSED", "review": None}
        )

    def test_v2_initial_empty_implementation_outcomes_are_rejected(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        assessment = self.v2_assessment(plan)
        assessment["mode"] = "IMPLEMENTATION"
        assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(plan)
        planner = planner_receipt(
            self.host, "T01", self.base, "empty implementation without obligations"
        )
        path = self.host / "empty-implementation.json"
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "empty source outcomes require"):
            devcrew_phase.initialize_manifest(
                path,
                plan,
                self.receipt,
                self.host,
                planner["SPAWN_RECEIPT_ID"],
                intent_assessment={"assessment": assessment, "decisions": []},
            )
        self.assertFalse(path.exists())

    def test_v2_pre_candidate_reapproval_archives_intent_and_assessment_history(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        initial_planner = planner_receipt(
            self.host, "T01", self.base, "v2 initial planner for reapproval"
        )
        path = self.host / "v2-reapproval.json"
        initial_assessment = self.v2_assessment(plan)
        initial = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            initial_planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": initial_assessment, "decisions": []},
        )
        replacement = json.loads(json.dumps(plan))
        replacement["phases"][0]["goal"] = "diagnose the captured request"
        replacement["phases"][0]["acceptance"] = ["record observed facts and diagnostic result"]
        replacement["phases"][0]["verification_bindings"] = [
            {
                "index": 0,
                "verification": "verify source",
                "argv": [sys.executable, "-c", "print('fresh')"],
            }
        ]
        devcrew_phase.pin_verification(replacement["phases"][0])
        for key in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement["phases"][0].pop(key, None)
        replacement["original_intent"] = json.loads(json.dumps(initial["original_intent"]))
        previous = devcrew_phase.intent_plan_digest(initial)
        assessment = self.v2_assessment(replacement, previous=previous)
        planner = planner_receipt(self.host, "T01", self.base, "v2 reapproval planner")
        updated = devcrew_phase.reapprove_pre_candidate(
            path,
            replacement,
            self.receipt,
            self.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )
        self.assertEqual(
            updated["pre_candidate_reapprovals"][0]["original_intent"], initial["original_intent"]
        )
        self.assertEqual(
            updated["original_intent"]["planning_assessment"]["history"], [initial_assessment]
        )
        resumed = devcrew_phase.load_for_resume(path, self.receipt, self.host)
        self.assertEqual(
            resumed["original_intent"]["planning_assessment"]["assessment"], assessment
        )

    def test_v2_reapproval_allows_previously_absent_optional_phase_field(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        initial_planner = planner_receipt(
            self.host, "T01", self.base, "initial optional-field plan"
        )
        path = self.host / "v2-optional-field-reapproval.json"
        initial_assessment = self.v2_assessment(plan)
        initial = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            initial_planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": initial_assessment, "decisions": []},
        )
        replacement = json.loads(json.dumps(plan))
        replacement["phases"][0]["main_scenario"] = (
            "Investigate the captured request and report findings."
        )
        replacement["phases"][0]["goal"] = "diagnose the captured request"
        replacement["phases"][0]["verification_bindings"] = [
            {
                "index": 0,
                "verification": "verify source",
                "argv": [sys.executable, "-c", "print('fresh')"],
            }
        ]
        devcrew_phase.pin_verification(replacement["phases"][0])
        for key in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement["phases"][0].pop(key, None)
        replacement["original_intent"] = json.loads(json.dumps(initial["original_intent"]))
        assessment = self.v2_assessment(
            replacement, previous=devcrew_phase.intent_plan_digest(initial)
        )
        planner = planner_receipt(
            self.host, "T01", self.base, "reapprove with explicit main scenario"
        )
        devcrew_phase.reapprove_pre_candidate(
            path,
            replacement,
            self.receipt,
            self.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )
        resumed = devcrew_phase.load_for_resume(path, self.receipt, self.host)
        entry = resumed["pre_candidate_reapprovals"][0]
        self.assertNotIn("main_scenario", entry["technical_projection"]["phases"][0])
        tampered = json.loads(json.dumps(resumed))
        archived = tampered["pre_candidate_reapprovals"][0]
        archived["technical_projection"]["phases"][0]["goal"] = "tampered archived goal"
        body = {key: value for key, value in archived.items() if key != "archive_sha256"}
        archived["archive_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "P1 technical projection differs"):
            devcrew_phase._verify_pre_candidate_history(self.host, tampered)

    def test_v2_reapproval_history_survives_later_phase_systemic_replan(self):
        plan = self.v2_plan()
        plan["task_mode"] = "MULTI_PHASE"
        plan["semantic_contract"] = {"capabilities": ["recovery", "verification"]}
        later = json.loads(json.dumps(plan["phases"][0]))
        later.update(
            {
                "phase_id": "P2",
                "goal": "verify the reported findings",
                "depends_on": ["P1"],
                "acceptance": ["The findings are verified"],
                "commit_boundary": "P2 verification",
                "commit_subject": "type(T01): verify findings",
                "status": "PLANNED",
            }
        )
        plan["phases"].append(later)
        devcrew_phase.pin_verification(plan["phases"][0])
        devcrew_phase.pin_verification(plan["phases"][1])
        first = planner_receipt(self.host, "T01", self.base, "initial multiphase intent")
        path = self.host / "v2-later-phase-replan.json"
        initial_assessment = self.v2_assessment(plan)
        initial = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            first["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": initial_assessment, "decisions": []},
        )
        replacement = json.loads(json.dumps(plan))
        replacement["phases"][0]["main_scenario"] = "Investigate first, then verify findings."
        replacement["phases"][0]["goal"] = "diagnose before verification"
        replacement["phases"][0]["verification_bindings"] = [
            {
                "index": 0,
                "verification": "verify source",
                "argv": [sys.executable, "-c", "print('fresh')"],
            }
        ]
        replacement["phases"][1] = json.loads(json.dumps(initial["phases"][1]))
        devcrew_phase.pin_verification(replacement["phases"][0])
        for key in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement["phases"][0].pop(key, None)
        replacement["original_intent"] = json.loads(json.dumps(initial["original_intent"]))
        prior = devcrew_phase.intent_plan_digest(initial)
        assessment = self.v2_assessment(replacement, previous=prior)
        planner = planner_receipt(
            self.host, "T01", self.base, "reapprove before later phase replan"
        )
        updated = devcrew_phase.reapprove_pre_candidate(
            path,
            replacement,
            self.receipt,
            self.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )

        later_current = updated["original_intent"]["planning_assessment"]["assessment"]
        before_replan = devcrew_phase.intent_plan_digest(updated)
        updated["phases"][1]["goal"] = "verify all reported findings independently"
        updated["phases"][1]["acceptance"] = ["Each reported finding has verified evidence"]
        history = updated["original_intent"]["planning_assessment"]["history"]
        history.append(later_current)
        current_assessment = json.loads(json.dumps(later_current))
        current_assessment["previous_plan_sha256"] = before_replan
        current_assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(updated)
        current_assessment["technical_change_rationale"] = "P2 received a later technical replan."
        updated["original_intent"]["planning_assessment"]["assessment"] = current_assessment
        devcrew_phase._verify_pre_candidate_history(self.host, updated)
        legacy = json.loads(json.dumps(updated))
        archived = legacy["pre_candidate_reapprovals"][0]
        archived.pop("technical_projection")
        body = {key: value for key, value in archived.items() if key != "archive_sha256"}
        archived["archive_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        with self.assertRaisesRegex(
            devcrew_phase.PhaseError, "archived intent contract is invalid"
        ):
            devcrew_phase._verify_pre_candidate_history(self.host, legacy)

    def test_v2_legacy_reapproval_archive_reconstructs_exact_unchanged_contract(self):
        plan = self.v2_plan()
        devcrew_phase.pin_verification(plan["phases"][0])
        first = planner_receipt(self.host, "T01", self.base, "legacy reconstruction initial plan")
        path = self.host / "v2-legacy-reapproval.json"
        initial_assessment = self.v2_assessment(plan)
        initial = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            first["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": initial_assessment, "decisions": []},
        )
        replacement = json.loads(json.dumps(plan))
        replacement["phases"][0]["main_scenario"] = "Investigate and report findings."
        replacement["phases"][0]["goal"] = "clarify diagnostic goal"
        replacement["phases"][0]["verification_bindings"] = [
            {
                "index": 0,
                "verification": "verify source",
                "argv": [sys.executable, "-c", "print('fresh')"],
            }
        ]
        devcrew_phase.pin_verification(replacement["phases"][0])
        for key in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement["phases"][0].pop(key, None)
        replacement["original_intent"] = json.loads(json.dumps(initial["original_intent"]))
        assessment = self.v2_assessment(
            replacement, previous=devcrew_phase.intent_plan_digest(initial)
        )
        planner = planner_receipt(self.host, "T01", self.base, "legacy exact history reapproval")
        updated = devcrew_phase.reapprove_pre_candidate(
            path,
            replacement,
            self.receipt,
            self.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )
        archived = updated["pre_candidate_reapprovals"][0]
        archived.pop("technical_projection")
        body = {key: value for key, value in archived.items() if key != "archive_sha256"}
        archived["archive_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        devcrew_phase._verify_pre_candidate_history(self.host, updated)

    def test_v2_reapproval_allows_valid_obligation_remapping_and_append_only_evidence(self):
        source, fragment = self.captured_source(
            "reapproval", "Recommend a card when SBP is blocked."
        )
        plan = self.v2_plan()
        phase = plan["phases"][0]
        phase["acceptance"] = ["Recommend a card when SBP is blocked."]
        plan["original_intent"]["sources"] = [source]
        plan["original_intent"]["requirements"] = [
            {
                "requirement_id": "R1",
                "kind": "explicit_user_obligation",
                "statement": "Recommend a card when SBP is blocked.",
                "source_fragments": [fragment],
                "acceptance_refs": [
                    {
                        "phase_id": "P1",
                        "acceptance_index": 0,
                        "sha256": hashlib.sha256(phase["acceptance"][0].encode()).hexdigest(),
                    }
                ],
                "evidence_records": ["checks/original-evidence.json"],
            }
        ]
        for ref in plan["original_intent"]["requirements"][0]["evidence_records"]:
            target = self.host / ref
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original evidence\n")
        devcrew_phase.pin_verification(phase)
        planner = planner_receipt(self.host, "T01", self.base, "initial obligation planner")
        initial_assessment = {
            "task_id": "T01",
            "plan_sha256": devcrew_phase.intent_plan_digest(plan),
            "previous_plan_sha256": None,
            "mode": "IMPLEMENTATION",
            "result": "COMPLETE",
            "source_reads": [
                {
                    "source_id": source["source_id"],
                    "sha256": source["sha256"],
                    "findings": "The captured prompt requires a card recommendation when SBP is blocked.",
                }
            ],
            "unavailable_sources": [],
            "source_outcomes": [
                {
                    "source_fragments": [fragment],
                    "expected_outcome": "Recommend a card when SBP is blocked.",
                    "requirement_ids": ["R1"],
                    "disposition": "COVERED",
                    "rationale": "The mapped acceptance preserves the request.",
                }
            ],
            "obligation_checks": [
                {
                    "requirement_id": "R1",
                    "change": "PRESERVED",
                    "preserved_outcome": "A card is recommended when SBP is blocked.",
                    "safety_rationale": "The requested recommendation remains advisory.",
                    "acceptance_rationale": "The acceptance explicitly retains the recommendation.",
                    "decision_id": None,
                }
            ],
            "technical_change_rationale": "The phase acceptance is clarified while preserving the requested outcome.",
        }
        path = self.host / "v2-obligation-reapproval.json"
        initial = devcrew_phase.initialize_manifest(
            path,
            plan,
            self.receipt,
            self.host,
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": initial_assessment, "decisions": []},
        )

        replacement = json.loads(json.dumps(plan))
        replacement_phase = replacement["phases"][0]
        replacement_phase["goal"] = "retain the requested recommendation in clarified acceptance"
        replacement_phase["acceptance"] = [
            "Preserve diagnostic findings",
            "Recommend a card when SBP is blocked.",
        ]
        replacement_phase["verification_bindings"] = [
            {
                "index": 0,
                "verification": "verify source",
                "argv": [sys.executable, "-c", "print('fresh')"],
            }
        ]
        devcrew_phase.pin_verification(replacement_phase)
        for key in (
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
        ):
            replacement_phase.pop(key, None)
        replacement_intent = replacement["original_intent"]
        replacement_intent["requirements"][0]["acceptance_refs"] = [
            {
                "phase_id": "P1",
                "acceptance_index": 1,
                "sha256": hashlib.sha256(replacement_phase["acceptance"][1].encode()).hexdigest(),
            }
        ]
        replacement_intent["requirements"][0]["evidence_records"].append(
            "checks/reapproval-evidence.json"
        )
        (self.host / "checks/reapproval-evidence.json").write_text("reapproval evidence\n")
        assessment = json.loads(json.dumps(initial_assessment))
        assessment["previous_plan_sha256"] = devcrew_phase.intent_plan_digest(initial)
        assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(replacement)
        planner = planner_receipt(self.host, "T01", self.base, "reapprove obligation mapping")
        updated = devcrew_phase.reapprove_pre_candidate(
            path,
            replacement,
            self.receipt,
            self.host,
            "P1",
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )

        resumed = devcrew_phase.load_for_resume(path, self.receipt, self.host)
        self.assertEqual(
            resumed["original_intent"]["requirements"][0]["acceptance_refs"],
            replacement_intent["requirements"][0]["acceptance_refs"],
        )
        self.assertEqual(
            resumed["original_intent"]["requirements"][0]["evidence_records"],
            ["checks/original-evidence.json", "checks/reapproval-evidence.json"],
        )
        for mutation, expected in (
            ("definition", lambda row: row.update(statement="changed obligation")),
            (
                "evidence",
                lambda row: row.update(evidence_records=["checks/reapproval-evidence.json"]),
            ),
        ):
            tampered = json.loads(json.dumps(resumed))
            expected(tampered["original_intent"]["requirements"][0])
            with (
                self.subTest(mutation=mutation),
                self.assertRaisesRegex(devcrew_phase.PhaseError, "archived intent identity chain"),
            ):
                devcrew_phase._verify_pre_candidate_history(self.host, tampered)

    def test_wrong_phase_fails_without_changing_manifest_or_git(self):
        planner = planner_receipt(self.host, "T01", self.base, "wrong phase planner")
        planner_path = pathlib.Path(self.temp.name) / "wrong-phase.json"
        planner_path.write_text(json.dumps(self.replacement()))
        manifest_before = self.path.read_bytes()
        head_before = self.git(self.task, "rev-parse", "HEAD")
        index_before = self.git(self.task, "write-tree")
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P2",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), manifest_before)
        self.assertEqual(self.path.read_bytes(), manifest_before)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), head_before)
        self.assertEqual(self.git(self.task, "write-tree"), index_before)

    def test_generation_one_candidate_review_accept_and_later_phase_resume(self):
        fixture = test_phase_review_lifecycle.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        replacement = json.loads(json.dumps(fixture.manifest))
        replacement.pop("planner_dispatch", None)
        replacement.pop("manifest_revision", None)
        current = replacement["phases"][0]
        for key in (
            "base_sha",
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
            "evidence",
            "executor_dispatch",
        ):
            current.pop(key, None)
        planner = test_phase_review_lifecycle.issue_planner_receipt(
            fixture.host,
            "T01",
            fixture.base,
            "fresh planner for pre-candidate reapproval",
            "new-planner",
        )
        planner_path = pathlib.Path(fixture.temp.name) / "fresh-planner.json"
        planner_path.write_text(json.dumps(replacement))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(fixture.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        self.assertEqual(fixture.manifest["contract_generation"], 1)
        candidate = fixture.make_candidate("P1", candidate_text="generation one candidate\n")
        fixture.accept_candidate("P1", candidate, "generation-one-review.json")
        self.assertEqual(fixture.manifest["phases"][0]["status"], "ACCEPTED")
        advanced = devcrew_phase.advance_phase(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            "P2",
            fixture.host,
            fixture.task,
            fixture.verifier,
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        resumed = devcrew_phase.load_for_resume(fixture.path, fixture.receipt, fixture.host)
        self.assertEqual(resumed["contract_generation"], 1)
        self.assertEqual(advanced["current_phase_id"], "P2")
        devcrew_phase.executor_input(resumed, fixture.task, fixture.host, fixture.verifier)

    def test_initialized_multiphase_reapproval_preserves_later_pinned_contract(self):
        fixture = test_phase_review_lifecycle.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        planner = fixture.manifest["planner_dispatch"]["spawn_receipt_id"]
        fixture.path.unlink()
        devcrew_phase.initialize_manifest(
            fixture.path, fixture.manifest, fixture.receipt, fixture.host, planner
        )
        before = json.loads(fixture.path.read_text())
        replacement = json.loads(json.dumps(fixture.manifest))
        replacement.pop("planner_dispatch", None)
        replacement.pop("manifest_revision", None)
        current = replacement["phases"][0]
        for key in (
            "base_sha",
            "verification_sha256",
            "verification_environment_sha256",
            "verification_approved_at",
            "evidence",
            "executor_dispatch",
        ):
            current.pop(key, None)
        # Later phases must remain byte-for-byte equivalent to their initialized contracts.
        replacement["phases"][1:] = json.loads(json.dumps(before["phases"][1:]))
        new_planner = planner_receipt(
            fixture.host, "T01", fixture.base, "initialized multiphase replacement planner"
        )
        replacement_path = pathlib.Path(fixture.temp.name) / "initialized-multiphase.json"
        replacement_path.write_text(json.dumps(replacement))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(fixture.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(replacement_path),
                "--spawn-receipt-id",
                new_planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        after = json.loads(fixture.path.read_text())
        self.assertEqual(after["phases"][1:], before["phases"][1:])

    def test_corrupt_persisted_planner_authority_denies_new_planner(self):
        saved = json.loads(self.path.read_text())
        saved["planner_dispatch"]["spawn_receipt_id"] = "missing-prior-planner-receipt"
        self.path.write_text(json.dumps(saved))
        manifest_before = self.path.read_bytes()
        new_planner = planner_receipt(
            self.host, "T01", self.base, "replacement planner despite corrupt history"
        )
        replacement_path = pathlib.Path(self.temp.name) / "corrupt-prior-planner.json"
        replacement_path.write_text(json.dumps(self.replacement()))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(replacement_path),
                "--spawn-receipt-id",
                new_planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.path.read_bytes(), manifest_before)

    def test_corrupt_selected_planner_issuance_fails_before_manifest_write(self):
        planner = planner_receipt(
            self.host, "T01", self.base, "selected planner with corrupt issuance"
        )
        replacement = self.replacement()
        store_path = devcrew_capacity.dispatch_receipt_path(self.host, "T01")
        store = devcrew_dispatch._read(store_path)
        issued = next(
            item for item in store["issued_routes"] if item.get("ROUTE_ID") == planner["ROUTE_ID"]
        )
        issued["consumed"] = False
        devcrew_dispatch._write(store_path, store)
        planner_path = pathlib.Path(self.temp.name) / "corrupt-selected-planner.json"
        planner_path.write_text(json.dumps(replacement))

        (self.task / "source.py").write_text("staged implementation\n")
        self.git(self.task, "add", "source.py")
        (self.task / "source.py").write_text("staged and unstaged implementation\n")
        (self.task / "untracked.py").write_text("untracked implementation\n")
        self.receipt = devcrew_worktree.resolve(self.host, "T01", read_only=True)
        manifest_before = self.path.read_bytes()
        index_before = self.git(self.task, "write-tree")
        tracked_before = (self.task / "source.py").read_bytes()
        untracked_before = (self.task / "untracked.py").read_bytes()

        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PRE_CANDIDATE_HISTORY"):
            devcrew_phase.reapprove_pre_candidate(
                self.path, replacement, self.receipt, self.host, "P1", planner["SPAWN_RECEIPT_ID"]
            )
        self.assertEqual(self.path.read_bytes(), manifest_before)

        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertEqual(self.path.read_bytes(), manifest_before)
        self.assertEqual(self.git(self.task, "write-tree"), index_before)
        self.assertEqual((self.task / "source.py").read_bytes(), tracked_before)
        self.assertEqual((self.task / "untracked.py").read_bytes(), untracked_before)

    def test_missing_archived_planner_receipt_denies_resume(self):
        original = json.loads(self.path.read_text())
        original_planner_id = original["planner_dispatch"]["spawn_receipt_id"]
        authorizer = planner_receipt(self.host, "T01", self.base, "historical authority planner")
        updated = devcrew_phase.reapprove_pre_candidate(
            self.path,
            self.replacement(),
            self.receipt,
            self.host,
            "P1",
            authorizer["SPAWN_RECEIPT_ID"],
        )
        store_path = devcrew_capacity.dispatch_receipt_path(self.host, "T01")
        store = devcrew_dispatch._read(store_path)
        store["receipts"] = [
            item
            for item in store["receipts"]
            if item.get("SPAWN_RECEIPT_ID") != original_planner_id
        ]
        devcrew_dispatch._write(store_path, store)
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.load_for_resume(self.path, self.receipt, self.host)

    def test_recomputed_archive_digest_does_not_authenticate_wrong_task(self):
        authorizer = planner_receipt(self.host, "T01", self.base, "archive task identity planner")
        devcrew_phase.reapprove_pre_candidate(
            self.path,
            self.replacement(),
            self.receipt,
            self.host,
            "P1",
            authorizer["SPAWN_RECEIPT_ID"],
        )
        saved = json.loads(self.path.read_text())
        entry = saved["pre_candidate_reapprovals"][0]
        entry["task_id"] = "T02"
        body = {key: value for key, value in entry.items() if key != "archive_sha256"}
        entry["archive_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "initial task contract"):
            devcrew_phase.load_for_resume(self.path, self.receipt, self.host)

    def test_recomputed_archive_digest_does_not_authenticate_changed_verification(self):
        authorizer = planner_receipt(self.host, "T01", self.base, "archive verification planner")
        devcrew_phase.reapprove_pre_candidate(
            self.path,
            self.replacement(),
            self.receipt,
            self.host,
            "P1",
            authorizer["SPAWN_RECEIPT_ID"],
        )
        saved = json.loads(self.path.read_text())
        entry = saved["pre_candidate_reapprovals"][0]
        entry["previous_contract"]["verification"][0] = "different check"
        body = {key: value for key, value in entry.items() if key != "archive_sha256"}
        entry["archive_sha256"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        ).hexdigest()
        self.path.write_text(json.dumps(saved))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "archived verification contract"):
            devcrew_phase.load_for_resume(self.path, self.receipt, self.host)

    @staticmethod
    def raw_index_bytes(task):
        index_path = pathlib.Path(
            PreCandidateReapprovalTests.git(task, "rev-parse", "--git-path", "index")
        )
        if not index_path.is_absolute():
            index_path = task / index_path
        return index_path.read_bytes()

    def test_read_only_resolution_does_not_refresh_uncached_raw_index(self):
        dirty = self.task / "untracked-implementation.py"
        dirty.write_text("preserve me\n")
        tracked = self.task / "source.py"
        stat = tracked.stat()
        os.utime(tracked, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
        before = self.raw_index_bytes(self.task)
        resolved = devcrew_worktree.resolve(self.host, "T01", read_only=True)
        self.assertEqual(resolved["state"], "RESOLVED")
        self.assertEqual(self.raw_index_bytes(self.task), before)
        self.assertEqual(tracked.read_text(), "base\n")
        self.assertEqual(dirty.read_text(), "preserve me\n")

    def test_api_reapproval_preserves_uncached_raw_index_bytes(self):
        (self.task / "source.py").write_text("staged bytes\n")
        self.git(self.task, "add", "source.py")
        (self.task / "source.py").write_text("unstaged bytes\n")
        (self.task / "untracked-binary.bin").write_bytes(b"\x00untracked\xff")
        self.receipt = devcrew_worktree.resolve(self.host, "T01", read_only=True)
        index_before = self.raw_index_bytes(self.task)
        planner = planner_receipt(self.host, "T01", self.base, "raw index preservation planner")
        manifest = json.loads(self.path.read_text())
        replacement = self.replacement()
        updated = devcrew_phase.reapprove_pre_candidate(
            self.path, replacement, self.receipt, self.host, "P1", planner["SPAWN_RECEIPT_ID"]
        )
        self.assertEqual(updated["phases"][0]["status"], "PLANNED")
        self.assertEqual(self.raw_index_bytes(self.task), index_before)
        self.assertEqual((self.task / "source.py").read_text(), "unstaged bytes\n")
        self.assertEqual((self.task / "untracked-binary.bin").read_bytes(), b"\x00untracked\xff")

    def test_cli_success_after_resolution_preserves_uncached_raw_index_bytes(self):
        (self.task / "source.py").write_text("staged bytes\n")
        self.git(self.task, "add", "source.py")
        (self.task / "source.py").write_text("unstaged bytes\n")
        (self.task / "untracked-binary.bin").write_bytes(b"\x00cli\xff")
        index_before = self.raw_index_bytes(self.task)
        planner = planner_receipt(self.host, "T01", self.base, "CLI raw index preservation planner")
        replacement_path = pathlib.Path(self.temp.name) / "cli-raw-index.json"
        replacement_path.write_text(json.dumps(self.replacement()))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(replacement_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.raw_index_bytes(self.task), index_before)
        self.assertEqual((self.task / "source.py").read_text(), "unstaged bytes\n")
        self.assertEqual((self.task / "untracked-binary.bin").read_bytes(), b"\x00cli\xff")

    def test_cli_denial_after_read_only_resolution_preserves_uncached_raw_index(self):
        (self.task / "source.py").write_text("staged bytes\n")
        self.git(self.task, "add", "source.py")
        (self.task / "source.py").write_text("unstaged bytes\n")
        index_before = self.raw_index_bytes(self.task)
        planner = planner_receipt(self.host, "T01", self.base, "denied raw index planner")
        replacement_path = pathlib.Path(self.temp.name) / "denied-raw-index.json"
        replacement_path.write_text(json.dumps(self.replacement()))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
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
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.raw_index_bytes(self.task), index_before)

    def test_cli_pre_candidate_reapproval_archives_contract_and_preserves_dirty_state(self):
        (self.task / "source.py").write_text("unstaged\n")
        self.git(self.task, "add", "source.py")
        (self.task / "source.py").write_text("unstaged after staged\n")
        (self.task / "untracked.txt").write_text("untracked\n")
        index_before = self.git(self.task, "write-tree")
        status_before = self.git(self.task, "status", "--porcelain=v1")
        planner = planner_receipt(self.host, "T01", self.base, "replacement planner")
        replacement = self.replacement()
        planner_path = pathlib.Path(self.temp.name) / "replacement.json"
        planner_path.write_text(json.dumps(replacement))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.path.read_text())
        phase = saved["phases"][0]
        self.assertEqual(phase["goal"], "refreshed goal")
        self.assertEqual(phase["status"], "PLANNED")
        self.assertEqual(len(saved["pre_candidate_reapprovals"]), 1)
        self.assertEqual(saved["contract_generation"], 1)
        self.assertEqual(self.git(self.task, "write-tree"), index_before)
        self.assertEqual(self.git(self.task, "status", "--porcelain=v1"), status_before)

    def test_executor_receipt_from_previous_generation_is_rejected(self):
        (self.task / "source.py").write_text("staged implementation\n")
        self.git(self.task, "add", "source.py")
        (self.task / "source.py").write_text("staged plus unstaged implementation\n")
        (self.task / "untracked-implementation.py").write_text("untracked implementation\n")
        index_before = self.git(self.task, "write-tree")
        status_before = self.git(self.task, "status", "--porcelain=v1")
        first_planner = planner_receipt(self.host, "T01", self.base, "replacement planner one")
        planner_path = pathlib.Path(self.temp.name) / "replacement-one.json"
        planner_path.write_text(json.dumps(self.replacement()))
        cli = [
            sys.executable,
            str(ROOT / "scripts/devcrew.py"),
            "phase",
            "pre-candidate-reapprove",
            "--project",
            str(self.host),
            "--task-id",
            "T01",
            "--phase-id",
            "P1",
            "--planner-output",
            str(planner_path),
            "--spawn-receipt-id",
            first_planner["SPAWN_RECEIPT_ID"],
        ]
        result = subprocess.run(cli, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.task, "write-tree"), index_before)
        self.assertEqual(self.git(self.task, "status", "--porcelain=v1"), status_before)
        self.assertEqual(
            (self.task / "source.py").read_text(), "staged plus unstaged implementation\n"
        )
        self.assertEqual(
            (self.task / "untracked-implementation.py").read_text(), "untracked implementation\n"
        )

        runtime = {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]}
            ],
            "roles": {"executor-agent": {"available": True, "supports_overrides": True}},
            "generic_available": True,
            "explicit_model_effort": True,
        }
        phase = json.loads(self.path.read_text())["phases"][0]
        selection = devcrew_cost.route("executor", runtime=runtime)
        prepared = devcrew_context.routed_task_spawn(
            "executor_T01",
            "Implement the active phase",
            selection,
            runtime,
            task_id="T01",
            project_root=self.host,
            task_worktree_root=self.task,
            base_sha=self.base,
            owned_scope=phase["owned_scope"],
            acceptance=phase["acceptance"],
            verification=phase["verification"],
            receipt=self.receipt,
            role_contract="Execute only the current phase contract.",
        )
        pending_prepared = devcrew_context.routed_task_spawn(
            "pending_executor_T01",
            "Pending executor route",
            selection,
            runtime,
            task_id="T01",
            project_root=self.host,
            task_worktree_root=self.task,
            base_sha=self.base,
            owned_scope=phase["owned_scope"],
            acceptance=phase["acceptance"],
            verification=phase["verification"],
            receipt=self.receipt,
            role_contract="Execute only the current phase contract.",
        )
        pending = {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "executor",
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": self.base,
            "WRITER_CYCLE": 0,
            "REJECTED_CANDIDATE_SHA": None,
            "AGENT_ID": "pending-executor-agent",
            "SPAWN_MODEL": pending_prepared["spawn"]["model"],
            "SPAWN_REASONING_EFFORT": pending_prepared["spawn"]["reasoning_effort"],
            "SPAWN_MODE": pending_prepared["dispatch"]["SPAWN_MODE"],
            "FORK_TURNS": "none",
            "PREPARED": {
                "route": pending_prepared["route"],
                "dispatch": pending_prepared["dispatch"],
            },
            "SPAWN_PAYLOAD": pending_prepared["spawn"],
            "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(pending_prepared["spawn"]),
        }
        pending_issued = devcrew_dispatch.issued_for_report(self.host, pending)
        self.assertEqual(
            pending_issued["IDENTITY"], {key: pending[key] for key in pending_issued["IDENTITY"]}
        )
        devcrew_dispatch.validate_issued(self.host, pending)
        self.assertEqual(self.git(self.task, "write-tree"), index_before)
        self.assertEqual(self.git(self.task, "status", "--porcelain=v1"), status_before)
        executor = devcrew_dispatch.record_spawn(
            self.host,
            {
                "STATUS": "SPAWN_SUCCESS",
                "REQUESTED_ROLE": "executor",
                "TASK_ID": "T01",
                "PHASE_ID": "P1",
                "BASE_SHA": self.base,
                "WRITER_CYCLE": 0,
                "REJECTED_CANDIDATE_SHA": None,
                "AGENT_ID": "executor-agent",
                "SPAWN_MODEL": prepared["spawn"]["model"],
                "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
                "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
                "FORK_TURNS": "none",
                "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                "SPAWN_PAYLOAD": prepared["spawn"],
                "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(prepared["spawn"]),
            },
        )
        second_planner = planner_receipt(self.host, "T01", self.base, "replacement planner two")
        second_path = pathlib.Path(self.temp.name) / "replacement-two.json"
        second_path.write_text(json.dumps(self.replacement(goal="second refreshed goal")))
        result = subprocess.run(
            [
                *cli[:-2],
                "--planner-output",
                str(second_path),
                "--spawn-receipt-id",
                second_planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaisesRegex(devcrew_dispatch.DispatchError, "contract generation"):
            devcrew_dispatch.verify_spawn(
                self.host,
                executor["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="executor",
                phase_id="P1",
                base_sha=self.base,
                writer_cycle=0,
            )
        with self.assertRaisesRegex(devcrew_dispatch.DispatchError, "TASK_WORKTREE_BINDING_STALE"):
            devcrew_dispatch.validate_issued(self.host, pending)

    def test_tampered_reapproval_archive_fails_resume_without_mutation(self):
        planner = planner_receipt(self.host, "T01", self.base, "archive integrity planner")
        planner_path = pathlib.Path(self.temp.name) / "archive-integrity.json"
        planner_path.write_text(json.dumps(self.replacement()))
        result = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "pre-candidate-reapprove",
                "--project",
                str(self.host),
                "--task-id",
                "T01",
                "--phase-id",
                "P1",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["SPAWN_RECEIPT_ID"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        saved = json.loads(self.path.read_text())
        saved["pre_candidate_reapprovals"][0]["previous_contract"]["goal"] = "tampered"
        self.path.write_text(json.dumps(saved))
        before = self.path.read_bytes()
        head = self.git(self.task, "rev-parse", "HEAD")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PRE_CANDIDATE_HISTORY"):
            devcrew_phase.load_for_resume(self.path, self.receipt, self.host)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), head)


if __name__ == "__main__":
    unittest.main()
