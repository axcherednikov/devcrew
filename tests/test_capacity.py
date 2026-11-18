import base64
import json
import copy
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import devcrew_capacity as capacity
import devcrew_dispatch
import devcrew_context
import devcrew_cost
import devcrew_phase
import devcrew_worktree
import test_phase_review_lifecycle as phase_review


def run_envelope_command(command, offset=None, *, check=False):
    if offset is not None:
        command += " " + str(offset)
    if os.name == "nt":
        script = (command + "; exit $LASTEXITCODE").encode("utf-16le")
        argv = ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
                base64.b64encode(script).decode("ascii")]
    else:
        argv = ["/bin/sh", "-c", command]
    return subprocess.run(argv, capture_output=True, encoding="utf-8", check=check)


class EnvelopeCommands(unittest.TestCase):
    def test_legacy_delivery_hashes_still_verify(self):
        path = pathlib.PurePosixPath("/tmp/store.json")
        payload = {"message": "Read me"}
        for role, digest in (
            ("executor", "125a432326d94e4ad0f52f3b890f35bb33fa61dcebcb31b2ce225448c8c465eb"),
            ("reviewer", "8ca0ccd5c99440c9a66d29e96ae161eb6cfea9d7d6a5fd98b0e3093ceccb9c29"),
        ):
            with self.subTest(role=role):
                row = {"ROLE": role, "ROUTE_ID": "r1", "IDENTITY": {}, "SPAWN_PAYLOAD": payload,
                       "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(payload),
                       "FILE_DELIVERY": {"path": str(path), "spawn_sha256": digest}}
                existing = devcrew_dispatch._issued_spawn_payload(row, path)
                self.assertEqual(devcrew_dispatch._canonical_digest(existing), digest)

    def test_native_shell_reads_quoted_path_and_reuses_issued_interpreter(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "O'Brien $crew & `x` ’ пакет.json"
            payload = {"message": "x" * 4000 + "Привет, 世界!"}
            for role in ("executor", "reviewer"):
                with self.subTest(role=role):
                    issued = {"ROLE": role, "ROUTE_ID": "r1", "IDENTITY": {}, "SPAWN_PAYLOAD": payload,
                              "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(payload)}
                    spawn, metadata = devcrew_dispatch._file_delivery(issued, path)
                    issued["FILE_DELIVERY"] = metadata
                    path.write_text(json.dumps({"issued_routes": [issued]}), encoding="utf-8")
                    self.assertEqual(metadata["reader"]["python"], sys.executable)
                    self.assertIn("PowerShell" if os.name == "nt" else "POSIX", spawn["message"])
                    command = spawn["message"].splitlines()[-1]
                    result = run_envelope_command(command, 4000, check=True)
                    self.assertEqual(result.stdout, "Characters 4000:4011 of 4011\nПривет, 世界!")
                    with mock.patch.object(sys, "executable", "different-python"):
                        self.assertEqual(devcrew_dispatch._issued_spawn_payload(issued, path), spawn)


class CapacityBackpressure(unittest.TestCase):
    def setUp(self):
        self.install_lifecycle_fixture()

    def install_lifecycle_fixture(self):
        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.project = fixture.host.resolve()
        self.task_id = "T01"
        self._prepared_cache = {}

    def prepared_route(self, role):
        key = (self.task_id, role)
        if key in self._prepared_cache:
            return self._prepared_cache[key]
        fixture = self.fixture
        runtime = {"models": [{"id": "bounded-test", "capabilities": ["bounded"],
                               "efforts": ["low", "medium"]},
                              {"id": "strong-test", "capabilities": ["strong"],
                               "efforts": ["low", "medium", "high"]}],
                   "roles": {name: {"available": True, "supports_overrides": True}
                             for name in ("executor-agent", "reviewer-agent", "decision-agent")},
                   "generic_available": True, "explicit_model_effort": True}
        if role == "planner":
            prepared = devcrew_context.routed_spawn(
                "planner_T01_capacity", "Plan", devcrew_cost.route("planner", runtime=runtime), runtime,
                project_root=self.project, task_id="T01",
                identity={"TASK_ID": "T01", "BASE_SHA": fixture.base},
                role_contract="Plan the registered task.")
        elif role == "executor":
            phase = next(item for item in fixture.manifest["phases"] if item["phase_id"] == "P1")
            if phase["status"] != "EXECUTING":
                fixture.manifest = devcrew_phase.set_phase_status(
                    fixture.path, fixture.manifest, fixture.receipt, "P1", "EXECUTING",
                    project=fixture.host, verifier=fixture.verifier)
            fields = fixture.manifest["phases"][0]
            prepared = devcrew_context.routed_task_spawn(
                "executor_T01_capacity", "Implement phase", devcrew_cost.route("executor", runtime=runtime),
                runtime, task_id="T01", project_root=self.project, task_worktree_root=fixture.task,
                base_sha=fixture.base, owned_scope=fields["owned_scope"], acceptance=fields["acceptance"],
                verification=fields["verification"], receipt=fixture.receipt,
                role_contract=getattr(self, "executor_role_contract", "Execute the registered phase."))
            fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        elif role == "reviewer":
            fixture.make_candidate()
            fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
            fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
            devcrew_phase.begin_review(fixture.path, fixture.manifest, fixture.receipt,
                                      fixture.host, fixture.task, "P1", fixture.verifier)
            fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
            fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
            target = devcrew_phase.review_target(fixture.manifest, "P1")
            evidence = list(fixture.manifest["phases"][0]["candidate_commit"]["evidence"])
            prepared = devcrew_context.reviewer_spawn(
                "T01", runtime=runtime,
                selection=devcrew_cost.route("reviewer", runtime=runtime),
                project_root=self.project, phase_id="P1", base_sha=target["BASE_SHA"],
                target_sha=target["TARGET_SHA"], review_contract=target,
                verification_evidence=evidence)
        else:
            raise AssertionError(role)
        self._prepared_cache[key] = prepared
        return prepared

    def prepared(self, role="executor"):
        return {
            "route": {"ROUTE_ID": "route-" + role + "-" + self.task_id, "ROLE": role, "TASK_CLASS": "IMPLEMENTATION",
                      "COORDINATION_COMPLEXITY": None, "DECISION_COMPLEXITY": None,
                      "IMPLEMENTATION_COMPLEXITY": "NORMAL", "REVIEW_COMPLEXITY": None,
                      "RISK": "low", "MODEL_CLASS": "bounded",
                      "RESOLVED_MODEL": "strong-test",
                      "REASONING_EFFORT": "medium", "SELECTION_REASON": "normal_implementation"},
            "dispatch": {"SPAWN_MODE": "generic", "SPAWN_REASON": "pinned_model_mismatch",
                         "ROLE_FALLBACK": "generic", "ROUTE_APPLIED": False},
        }

    def test_executor_file_delivery_reads_original_and_binds_observed_spawn(self):
        self.executor_role_contract = "Инструкция " * 6200
        inline_report = self.report(status="SPAWN_SUCCESS", runtime_error=None, AGENT_ID="writer")
        route_id = inline_report["PREPARED"]["route"]["ROUTE_ID"]
        delivery = devcrew_dispatch.prepare_executor_delivery(self.project, self.task_id, route_id)
        self.assertEqual(delivery, devcrew_dispatch.prepare_executor_delivery(
            self.project, self.task_id, route_id))
        spawn = delivery["spawn"]
        self.assertEqual({**spawn, "message": inline_report["SPAWN_PAYLOAD"]["message"]},
                         inline_report["SPAWN_PAYLOAD"])
        read = spawn["message"].splitlines()[-1]
        original = inline_report["SPAWN_PAYLOAD"]["message"]
        parts = []
        for start in range(0, len(original), 4000):
            completed = run_envelope_command(read, start, check=True)
            header, _, body = completed.stdout.partition("\n")
            end = min(start + 4000, len(original))
            self.assertEqual(header, f"Characters {start}:{end} of {len(original)}")
            parts.append(body)
        self.assertEqual("".join(parts), original)
        with self.assertRaisesRegex(ValueError, "postflight differs"):
            capacity.report(str(self.project), inline_report)

        delivered_report = dict(inline_report, SPAWN_PAYLOAD=spawn,
                                SPAWN_PAYLOAD_SHA256=delivery["spawn_payload_sha256"])
        receipt = capacity.report(str(self.project), delivered_report)["SPAWN_RECEIPT"]
        self.assertEqual(receipt["SPAWN_PAYLOAD_SHA256"], inline_report["SPAWN_PAYLOAD_SHA256"])
        self.assertEqual(receipt["DELIVERED_SPAWN_SHA256"], delivery["spawn_payload_sha256"])
        identity = dict(task_id=self.task_id, role="executor", phase_id="P1",
                        base_sha=self.fixture.base, writer_cycle=0)
        devcrew_dispatch.verify_spawn(self.project, receipt["SPAWN_RECEIPT_ID"], **identity)
        with self.assertRaisesRegex(ValueError, "unconsumed executor issuance"):
            devcrew_dispatch.prepare_executor_delivery(self.project, self.task_id, route_id)

        path = capacity.dispatch_receipt_path(self.project, self.task_id)
        store = devcrew_dispatch._read(path)
        issued = next(row for row in store["issued_routes"] if row["ROUTE_ID"] == route_id)
        issued["SPAWN_PAYLOAD"]["message"] += "\nchanged"
        devcrew_dispatch._write(path, store)
        rejected = run_envelope_command(read)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, "")
        self.assertIn("Dispatch package digest mismatch", rejected.stderr)
        with self.assertRaisesRegex(ValueError, "package digest"):
            devcrew_dispatch.verify_spawn(self.project, receipt["SPAWN_RECEIPT_ID"], **identity)

    def test_executor_file_delivery_rejects_other_roles(self):
        planner = self.prepared_route("planner")
        with self.assertRaisesRegex(ValueError, "unconsumed executor issuance"):
            devcrew_dispatch.prepare_executor_delivery(
                self.project, self.task_id, planner["route"]["ROUTE_ID"])

    def test_reviewer_file_delivery_reads_exact_large_candidate_package_and_binds_receipt(self):
        inline = self.report(role="reviewer", status="SPAWN_SUCCESS", runtime_error=None,
                             AGENT_ID="reviewer-independent")
        route_id = inline["PREPARED"]["route"]["ROUTE_ID"]
        path = capacity.dispatch_receipt_path(self.project, self.task_id)
        store = devcrew_dispatch._read(path)
        issued = next(row for row in store["issued_routes"] if row["ROUTE_ID"] == route_id)
        original_payload = copy.deepcopy(issued["SPAWN_PAYLOAD"])
        original_package = (
            original_payload["message"] + "\nORIGINAL_INTENT: " + "intent source " * 1500 +
            "\nPHASE_REVIEW_CONTRACT: " + "exact candidate acceptance " * 1500 +
            "\nVERIFICATION_EVIDENCE: candidate evidence refs"
        )
        self.assertGreater(len(original_package), 44773)
        issued["SPAWN_PAYLOAD"]["message"] = original_package
        issued["SPAWN_PAYLOAD_SHA256"] = devcrew_dispatch._canonical_digest(issued["SPAWN_PAYLOAD"])
        original_digest = issued["SPAWN_PAYLOAD_SHA256"]
        devcrew_dispatch._write(path, store)

        with mock.patch.object(devcrew_dispatch, "_validate_authority", return_value={}):
            delivery = devcrew_dispatch.prepare_reviewer_delivery(self.project, self.task_id, route_id)
            self.assertEqual(delivery, devcrew_dispatch.prepare_reviewer_delivery(
                self.project, self.task_id, route_id))
            spawn = delivery["spawn"]
            self.assertEqual({**spawn, "message": original_package},
                             {**original_payload, "message": original_package})
            self.assertIn("Exact candidate identity", spawn["message"])
            self.assertIn("review package", spawn["message"])
            read = spawn["message"].splitlines()[-1]
            parts = []
            for start in range(0, len(original_package), 4000):
                completed = run_envelope_command(read, start, check=True)
                header, _, body = completed.stdout.partition("\n")
                end = min(start + 4000, len(original_package))
                self.assertEqual(header, f"Characters {start}:{end} of {len(original_package)}")
                parts.append(body)
            self.assertEqual("".join(parts), original_package)

            report = dict(inline, SPAWN_PAYLOAD=spawn,
                          SPAWN_PAYLOAD_SHA256=delivery["spawn_payload_sha256"])
            with self.assertRaisesRegex(ValueError, "unconsumed executor issuance"):
                devcrew_dispatch.prepare_executor_delivery(self.project, self.task_id, route_id)
            altered_spawn = dict(spawn, message=spawn["message"] + " changed")
            with self.assertRaisesRegex(ValueError, "postflight differs"):
                capacity.report(str(self.project), dict(report, SPAWN_PAYLOAD=altered_spawn))
            with self.assertRaisesRegex(ValueError, "postflight differs"):
                capacity.report(str(self.project), dict(report, SPAWN_PAYLOAD_SHA256="0" * 64))
            receipt = capacity.report(str(self.project), report)["SPAWN_RECEIPT"]
            self.assertEqual(receipt["SPAWN_PAYLOAD_SHA256"], original_digest)
            self.assertEqual(receipt["DELIVERED_SPAWN_SHA256"], delivery["spawn_payload_sha256"])
            self.assertEqual(receipt["TARGET_SHA"], inline["TARGET_SHA"])
            store = devcrew_dispatch._read(path)
            with self.assertRaisesRegex(ValueError, "already been used"):
                devcrew_dispatch._require_independent_reviewer(
                    store, task_id=self.task_id, agent_id="reviewer-independent")
            stored_receipt = next(row for row in store["receipts"]
                                  if row["SPAWN_RECEIPT_ID"] == receipt["SPAWN_RECEIPT_ID"])
            stored_receipt["TARGET_SHA"] = "f" * 40
            devcrew_dispatch._write(path, store)
            with self.assertRaisesRegex(ValueError, "identity differs"):
                devcrew_dispatch.verify_spawn(
                    self.project, receipt["SPAWN_RECEIPT_ID"], task_id=self.task_id, role="reviewer",
                    phase_id="P1", base_sha=receipt["BASE_SHA"], target_sha="f" * 40,
                    rejected_candidate_sha=None)
            with self.assertRaisesRegex(ValueError, "unconsumed reviewer issuance"):
                devcrew_dispatch.prepare_reviewer_delivery(self.project, self.task_id, route_id)

        store = devcrew_dispatch._read(path)
        stored = next(row for row in store["issued_routes"] if row["ROUTE_ID"] == route_id)
        stored["SPAWN_PAYLOAD"]["message"] += "\ntampered"
        devcrew_dispatch._write(path, store)
        rejected = run_envelope_command(read)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(rejected.stdout, "")
        self.assertIn("Review package digest mismatch", rejected.stderr)

    def test_executor_file_delivery_keeps_capacity_retry_transport(self):
        inline = self.report()
        route_id = inline["PREPARED"]["route"]["ROUTE_ID"]
        blocked = capacity.report(str(self.project), inline)
        with self.assertRaisesRegex(ValueError, "active capacity retry transport"):
            devcrew_dispatch.prepare_executor_delivery(self.project, self.task_id, route_id)
        resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
        self.assertEqual(resumed["SPAWN_PAYLOAD"], inline["SPAWN_PAYLOAD"])
        capacity.report(str(self.project), dict(inline, ATTEMPT_NUMBER=2,
                        BLOCKER_ID=blocked["BLOCKER_ID"], RUNTIME_ERROR="different failure"))
        delivery = devcrew_dispatch.prepare_executor_delivery(self.project, self.task_id, route_id)
        file_report = dict(inline, SPAWN_PAYLOAD=delivery["spawn"],
                           SPAWN_PAYLOAD_SHA256=delivery["spawn_payload_sha256"])
        blocked = capacity.report(str(self.project), file_report)
        resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
        self.assertEqual(resumed["SPAWN_PAYLOAD"], delivery["spawn"])
        result = capacity.report(str(self.project), dict(file_report, STATUS="SPAWN_SUCCESS",
                                 AGENT_ID="writer", RUNTIME_ERROR=None, ATTEMPT_NUMBER=2,
                                 BLOCKER_ID=blocked["BLOCKER_ID"]))
        self.assertEqual(result["SPAWN_RECEIPT"]["DELIVERED_SPAWN_SHA256"],
                         delivery["spawn_payload_sha256"])

    def test_capacity_does_not_persist_retry_for_transport_changed_after_validation(self):
        inline = self.report()
        route_id = inline["PREPARED"]["route"]["ROUTE_ID"]
        validate = devcrew_dispatch.validate_issued

        def convert_after_validation(*args, **kwargs):
            issued = validate(*args, **kwargs)
            devcrew_dispatch.prepare_executor_delivery(self.project, self.task_id, route_id)
            return issued

        with mock.patch.object(devcrew_dispatch, "validate_issued", side_effect=convert_after_validation):
            with self.assertRaisesRegex(ValueError, "postflight differs"):
                capacity.report(str(self.project), inline)
        with self.assertRaisesRegex(ValueError, "no resumable capacity blocker"):
            capacity.resume(str(self.project), self.task_id, host_initiated=True)

    def report(self, *, role="executor", status="SPAWN_FAILED",
               runtime_error="agent thread limit reached", attempt=1,
               task_root="__default__", blocker_id=None, **changes):
        if task_root == "__default__":
            task_root = None if role == "planner" else str(self.fixture.task)
        identity = {"TASK_ID": self.task_id, "BASE_SHA": self.fixture.base}
        if role == "executor":
            identity.update(PHASE_ID="P1", WRITER_CYCLE=0, REJECTED_CANDIDATE_SHA=None)
        elif role == "reviewer":
            identity.update(PHASE_ID="P1", TARGET_SHA="b" * 40)
        prepared = self.prepared_route(role)
        if role == "executor":
            identity.update(PHASE_ID="P1", BASE_SHA=self.fixture.base,
                            WRITER_CYCLE=0, REJECTED_CANDIDATE_SHA=None)
        elif role == "reviewer":
            target = devcrew_phase.review_target(self.fixture.manifest, "P1")
            identity.update(PHASE_ID="P1", BASE_SHA=target["BASE_SHA"], TARGET_SHA=target["TARGET_SHA"])
        payload = prepared["spawn"]
        return {"STATUS": status, "REQUESTED_ROLE": role, "TASK_ID": self.task_id,
                "HOST_PROJECT_ROOT": str(self.project), "TASK_WORKTREE_ROOT": task_root,
                "CURRENT_STAGE": f"{role.upper()}_DISPATCH", "ATTEMPT_NUMBER": attempt,
                "RUNTIME_ERROR": runtime_error, "HOST_INITIATED": True,
                "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                "SPAWN_PAYLOAD": payload,
                "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
                "SPAWN_MODEL": payload["model"], "SPAWN_REASONING_EFFORT": payload["reasoning_effort"],
                "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"], "FORK_TURNS": payload["fork_turns"],
                **identity, "BLOCKER_ID": blocker_id} | changes

    def test_exact_runtime_failure_normalizes_but_ambiguous_outcomes_do_not(self):
        self.assertEqual(capacity.normalize_report(self.report())["STATUS"],
                         "AGENT_CAPACITY_BLOCKED")
        other_error = self.report(runtime_error="agent unavailable")
        self.assertEqual(capacity.normalize_report(other_error)["STATUS"], "SPAWN_FAILED")
        unknown = dict(self.report(), STATUS="SPAWN_UNKNOWN")
        with self.assertRaisesRegex(ValueError, "STATUS"):
            capacity.normalize_report(unknown)

    def test_blocker_receipt_has_required_fields_and_null_preplanner_worktree(self):
        data = self.report(role="planner", task_root=None)
        receipt = capacity.report(str(self.project), data)
        expected = {"STATUS": "AGENT_CAPACITY_BLOCKED", "REQUESTED_ROLE": "planner",
                    "TASK_ID": self.task_id, "HOST_PROJECT_ROOT": str(self.project.resolve()),
                    "TASK_WORKTREE_ROOT": None, "CURRENT_STAGE": "PLANNER_DISPATCH",
                    "RESOLVED_MODEL": "strong-test", "REASONING_EFFORT": "medium",
                    "ATTEMPT_NUMBER": 1, "RUNTIME_ERROR": capacity.CAPACITY_ERROR,
                    "RESUMABLE": True}
        for key, value in expected.items():
            self.assertEqual(receipt[key], value)
        stored = json.loads((self.project / ".codex/sessions" /
                             f"agent-capacity-{self.task_id}.json").read_text())
        self.assertEqual(stored, receipt)

    def test_resume_returns_only_blocked_role_and_reuses_prepared_selection(self):
        data = self.report(role="executor")
        capacity.report(str(self.project), data)
        resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
        self.assertEqual(resumed["STATUS"], "RESUME_READY")
        self.assertEqual(resumed["REQUESTED_ROLE"], "executor")
        self.assertEqual(resumed["CURRENT_STAGE"], "EXECUTOR_DISPATCH")
        self.assertEqual(resumed["ATTEMPT_NUMBER"], 2)
        self.assertEqual(resumed["PREPARED"], data["PREPARED"])
        self.assertEqual(resumed["BLOCKER_ID"], json.loads(
            (self.project / ".codex/sessions" / f"agent-capacity-{self.task_id}.json").read_text())["BLOCKER_ID"])
        with self.assertRaisesRegex(ValueError, "host initiation"):
            capacity.resume(str(self.project), self.task_id, host_initiated=False)

    def test_second_actual_capacity_rejection_requires_incremented_attempt(self):
        capacity.report(str(self.project), self.report())
        resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
        with self.assertRaisesRegex(ValueError, "increment once"):
            capacity.report(str(self.project), self.report(attempt=1, blocker_id=resumed["BLOCKER_ID"]))
        second = capacity.report(str(self.project), self.report(attempt=2, blocker_id=resumed["BLOCKER_ID"]))
        self.assertEqual(second["ATTEMPT_NUMBER"], 2)

    def test_successful_role_spawn_clears_active_blocker_and_keeps_success_contract(self):
        for role in ("planner", "executor", "reviewer"):
            with self.subTest(role=role):
                self.install_lifecycle_fixture()
                blocked = self.report(role=role)
                initial = capacity.report(str(self.project), blocked)
                resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
                successful = self.report(role=role, status="SPAWN_SUCCESS",
                                         runtime_error=None, attempt=2,
                                         blocker_id=resumed["BLOCKER_ID"], AGENT_ID="agent-" + role,
                                         FORK_TURNS="none")
                if role == "planner":
                    self.assertEqual(successful["BASE_SHA"], self.fixture.base)
                elif role == "executor":
                    self.assertEqual((successful["PHASE_ID"], successful["WRITER_CYCLE"],
                                      successful["REJECTED_CANDIDATE_SHA"]), ("P1", 0, None))
                else:
                    target = devcrew_phase.review_target(self.fixture.manifest, "P1")
                    self.assertEqual((successful["PHASE_ID"], successful["TARGET_SHA"]),
                                     ("P1", target["TARGET_SHA"]))
                result = capacity.report(str(self.project), successful)
                self.assertEqual(result["STATUS"], "SPAWN_SUCCESS")
                self.assertEqual(result["SPAWN_RECEIPT"]["AGENT_ID"], "agent-" + role)
                state = self.project / ".codex/sessions" / f"agent-capacity-{self.task_id}.json"
                self.assertFalse(state.exists())

    def test_other_failure_classification_is_preserved(self):
        failed = self.report(runtime_error="permission denied")
        result = capacity.report(str(self.project), failed)
        self.assertEqual(result["STATUS"], "SPAWN_FAILED")
        self.assertFalse((self.project / ".codex/sessions" /
                          f"agent-capacity-{self.task_id}.json").exists())

    def test_noncapacity_failure_after_resume_clears_blocker_and_is_terminal(self):
        capacity.report(str(self.project), self.report())
        resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
        failure = self.report(runtime_error="permission denied", attempt=2,
                              blocker_id=resumed["BLOCKER_ID"])
        result = capacity.report(str(self.project), failure)
        self.assertEqual(result["STATUS"], "SPAWN_FAILED")
        with self.assertRaisesRegex(ValueError, "no resumable capacity blocker"):
            capacity.resume(str(self.project), self.task_id, host_initiated=True)

    def test_followup_reports_must_match_saved_blocker_identity(self):
        initial = self.report()
        capacity.report(str(self.project), initial)
        resumed = capacity.resume(str(self.project), self.task_id, host_initiated=True)
        blocker_id = resumed["BLOCKER_ID"]
        def retry(**changes):
            result = copy.deepcopy(initial)
            result.update(ATTEMPT_NUMBER=2, BLOCKER_ID=blocker_id)
            result.update(changes)
            return result
        mismatches = [
            retry(TASK_ID="other-task"),
            retry(REQUESTED_ROLE="reviewer"),
            retry(CURRENT_STAGE="OTHER_STAGE"),
            retry(TASK_WORKTREE_ROOT=str(self.project.parent / "other-task")),
            retry(PREPARED={"route": initial["PREPARED"]["route"],
                            "dispatch": {"SPAWN_MODE": "typed"}}),
        ]
        for mismatch in mismatches:
            with self.subTest(mismatch=mismatch):
                with self.assertRaisesRegex(ValueError, "blocker|INVALID_DISPATCH_ROUTE|prepared route"):
                    capacity.report(str(self.project), mismatch)
        changed_host = retry()
        changed_host["HOST_PROJECT_ROOT"] = str(self.project.parent)
        with self.assertRaisesRegex(ValueError, "HOST_PROJECT_ROOT"):
            capacity.report(str(self.project), changed_host)
        successful = retry(STATUS="SPAWN_SUCCESS", RUNTIME_ERROR=None,
                           CURRENT_STAGE="CHANGED_STAGE")
        with self.assertRaisesRegex(ValueError, "blocker identity|INVALID_DISPATCH_ROUTE"):
            capacity.report(str(self.project), successful)
        self.assertEqual(capacity.resume(str(self.project), self.task_id,
                                         host_initiated=True)["BLOCKER_ID"], blocker_id)

    def test_task_checkout_root_is_required_and_matches_issued_binding(self):
        for role in ("executor", "reviewer"):
            with self.subTest(role=role):
                with self.assertRaisesRegex(ValueError, "require TASK_WORKTREE_ROOT"):
                    capacity.report(str(self.project), self.report(role=role, task_root=None))
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_ROOT differs from issued worktree binding"):
            capacity.report(str(self.project),
                            self.report(role="executor", task_root=str(self.project)))

    def test_capacity_reporting_never_mutates_or_creates_task_worktrees(self):
        self.prepared_route("executor")
        existing_root = self.fixture.task
        marker = existing_root / "dirty.txt"
        marker.write_text("preserve this uncommitted content\n")
        capacity.report(str(self.project),
                        self.report(task_root=str(existing_root)))
        self.assertTrue(existing_root.is_dir())
        self.assertEqual(marker.read_text(), "preserve this uncommitted content\n")

        absent_root = self.project.parent / "absent-task-worktree"
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_ROOT differs from issued worktree binding"):
            capacity.report(str(self.project), self.report(task_root=str(absent_root)))
        self.assertFalse(absent_root.exists())

    def test_configured_host_sessions_directory_is_used(self):
        config_dir = self.project / ".codex"
        config_dir.mkdir(exist_ok=True)
        paths = {"context": "docs/context.md", "tasks": "docs/tasks",
                 "sessions": "state/host-sessions", "memory": "memory",
                 "checks": ".codex/checks"}
        (config_dir / "devcrew.json").write_text(json.dumps({"schema_version": 1,
                                                              "paths": paths}))
        source = self.project / ".codex/sessions" / f"agent-dispatch-{self.task_id}.json"
        destination = self.project / paths["sessions"] / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        capacity.report(str(self.project), self.report())
        self.assertTrue((self.project / paths["sessions"] /
                         f"agent-capacity-{self.task_id}.json").is_file())

    def test_unsafe_task_id_and_non_host_report_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "safe task identifier"):
            capacity.report(str(self.project), dict(self.report(), TASK_ID="../escape"))
        report = dict(self.report(), HOST_INITIATED=False)
        with self.assertRaisesRegex(ValueError, "host-initiated"):
            capacity.report(str(self.project), report)

    def test_cli_report_and_manual_resume_emit_one_saved_role_selection(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        report_path = self.project / "host-report.json"
        report_path.write_text(json.dumps(self.report(role="reviewer")))
        reported = subprocess.run([sys.executable, str(root / "scripts/devcrew.py"),
                                   "capacity-report", "--project", str(self.project),
                                   "--report", str(report_path)], capture_output=True, text=True)
        self.assertEqual(reported.returncode, 0, reported.stderr)
        resumed = subprocess.run([sys.executable, str(root / "scripts/devcrew.py"),
                                  "capacity-resume", "--project", str(self.project),
                                  "--task-id", self.task_id, "--host-initiated"],
                                 capture_output=True, text=True)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        payload = json.loads(resumed.stdout)
        self.assertEqual(payload["REQUESTED_ROLE"], "reviewer")
        self.assertEqual(payload["ATTEMPT_NUMBER"], 2)
        self.assertEqual(payload["PREPARED"], self.report(role="reviewer")["PREPARED"])

    def test_manual_resume_rejects_model_substitution_without_writes(self):
        capacity.report(str(self.project), self.report(role="reviewer"))
        path = self.project / ".codex/sessions" / f"agent-capacity-{self.task_id}.json"
        original = path.read_text()
        for model in ("another-qualified-reviewer", "bounded-test"):
            with self.subTest(model=model):
                corrupted = json.loads(original)
                corrupted["PREPARED"]["route"]["RESOLVED_MODEL"] = model
                path.write_text(json.dumps(corrupted))
                before = path.read_bytes()
                with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
                    capacity.resume(str(self.project), self.task_id, host_initiated=True)
                self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
