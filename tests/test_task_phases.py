import copy
import hashlib
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_phase
import devcrew_dispatch
import devcrew_context
import devcrew_cost
import devcrew_worktree


def plan(mode="SINGLE_PHASE"):
    phases = [
        {
            "phase_id": "P1",
            "goal": "Add health endpoint",
            "depends_on": [],
            "owned_scope": ["src/health.py"],
            "acceptance": ["GET /health returns status"],
            "verification": ["health endpoint test passes"],
            "commit_boundary": "Health endpoint capability",
            "commit_subject": "type(T01): add health endpoint",
            "status": "PLANNED",
        }
    ]
    if mode == "MULTI_PHASE":
        phases.append(
            {
                "phase_id": "P2",
                "goal": "Add readiness endpoint",
                "depends_on": ["P1"],
                "owned_scope": ["src/ready.py"],
                "acceptance": ["Readiness reports dependencies"],
                "verification": ["readiness test passes"],
                "commit_boundary": "Readiness capability",
                "commit_subject": "type(T01): add readiness endpoint",
                "status": "PLANNED",
            }
        )
    for phase in phases:
        phase["verification_bindings"] = [
            {
                "index": 0,
                "verification": phase["verification"][0],
                "argv": ["project-test", phase["phase_id"]],
            }
        ]
    return {
        "schema_version": 1,
        "task_id": "T01",
        "task_mode": mode,
        "semantic_contract": {
            "capabilities": ["health endpoint"]
            if mode == "SINGLE_PHASE"
            else ["health endpoint", "readiness endpoint"]
        },
        "worktree": {
            "repository_id": "repo",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "base_sha": "a" * 40,
        },
        "current_phase_id": "P1",
        "phases": phases,
    }


class PhaseManifestTests(unittest.TestCase):
    def planner_receipt(self, project):
        project.mkdir(parents=True, exist_ok=True)

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        (project / "README").write_text("registered task fixture\n")
        git("add", "README")
        git("commit", "-qm", "fixture")
        base = git("rev-parse", "HEAD")
        devcrew_worktree.create(
            project, "T01", base, worktree_request="Use a worktree for this task"
        )
        self.receipt = devcrew_worktree.resolve(project, "T01")
        source_path = project / "planner-source.txt"
        source_path.write_text("Fixture task source")
        devcrew_phase.bind_intent_source(
            project,
            self.receipt,
            {
                "mode": "file",
                "source_type": "document",
                "path": str(source_path),
                "expected_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
                "origin": "test fixture document",
            },
        )
        source_path.unlink()
        runtime = {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
                {
                    "id": "strong-test",
                    "capabilities": ["strong"],
                    "efforts": ["low", "medium", "high"],
                },
            ],
            "roles": {"planner-agent": {"available": True, "supports_overrides": True}},
            "generic_available": True,
            "explicit_model_effort": True,
        }
        prepared = devcrew_context.routed_spawn(
            "planner_T01",
            "Plan the registered task",
            devcrew_cost.route("planner", runtime=runtime),
            runtime,
            project_root=project.resolve(),
            task_id="T01",
            identity={"TASK_ID": "T01", "BASE_SHA": base},
            role_contract="Plan the task.",
        )
        route, dispatch, payload = prepared["route"], prepared["dispatch"], prepared["spawn"]
        return devcrew_dispatch.record_spawn(
            project,
            {
                "STATUS": "SPAWN_SUCCESS",
                "REQUESTED_ROLE": "planner",
                "TASK_ID": "T01",
                "AGENT_ID": "planner-1",
                "SPAWN_MODEL": payload["model"],
                "SPAWN_REASONING_EFFORT": payload["reasoning_effort"],
                "SPAWN_MODE": dispatch["SPAWN_MODE"],
                "BASE_SHA": base,
                "PREPARED": {"route": route, "dispatch": dispatch},
                "SPAWN_PAYLOAD": payload,
                "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(payload),
            },
        )

    def test_single_and_multi_phase_contracts_validate(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        self.assertEqual(
            devcrew_phase.validate_manifest(plan(), receipt)["task_mode"], "SINGLE_PHASE"
        )
        self.assertEqual(
            devcrew_phase.validate_manifest(plan("MULTI_PHASE"), receipt)["task_mode"],
            "MULTI_PHASE",
        )

    def test_validation_order_and_partial_scope_normalization(self):
        manifest = plan("MULTI_PHASE")
        first, second = manifest["phases"]
        receipt = dict(
            manifest["worktree"],
            owner="DEVCREW",
            state="RESOLVED",
            task_id=manifest["task_id"],
            expected_head=manifest["worktree"]["base_sha"],
        )
        first_scope, second_scope = first["owned_scope"], second["owned_scope"]
        manifest["current_phase_id"] = "unknown"
        first["depends_on"] = ["missing"]
        second["goal"] = ""
        second["depends_on"] = ["P2"]

        def expect_error(code, message):
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.validate_manifest(manifest, receipt, root)
            self.assertEqual((caught.exception.code, caught.exception.message), (code, message))

        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            expect_error("EMPTY_GOAL", "phase P2 goal must be non-empty")
            self.assertIsNot(first["owned_scope"], first_scope)
            self.assertIs(second["owned_scope"], second_scope)
            second["goal"] = "Add readiness endpoint"
            expect_error("SELF_DEPENDENCY", "phase P2 depends on itself")
            second["depends_on"] = ["P1"]
            expect_error("MISSING_DEPENDENCY", "phase P1 depends on missing phase missing")
            first["depends_on"] = []
            expect_error("INVALID_CURRENT_PHASE", "current_phase_id must identify a phase")
            manifest["current_phase_id"] = "P1"
            self.assertIs(devcrew_phase.validate_manifest(manifest, receipt, root), manifest)

    def test_v2_original_intent_allows_empty_diagnostic_requirements_only_when_pending(self):
        with tempfile.TemporaryDirectory() as root:
            task_root = pathlib.Path(root)
            receipt = {
                "owner": "DEVCREW",
                "state": "RESOLVED",
                "repository_id": "repo",
                "task_id": "T01",
                "task_worktree_root": str(task_root),
                "branch": "codex/tasks/T01",
                "expected_head": "a" * 40,
            }
            manifest = plan()
            manifest["schema_version"] = 2
            manifest["worktree"]["task_worktree_root"] = str(task_root)
            source = devcrew_phase.bind_intent_source(
                task_root,
                receipt,
                {
                    "mode": "unavailable",
                    "source_type": "user_prompt",
                    "origin": "original request",
                    "reason": "capture was unavailable",
                },
            )
            manifest["original_intent"] = {
                "sources": [source],
                "requirements": [],
                "decisions": [],
                "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
            }
            validated = devcrew_phase.validate_manifest(manifest, receipt)
            self.assertEqual(
                devcrew_phase.original_intent_status(validated),
                {"status": "NOT_ASSESSED", "unverified_source_ids": [source["source_id"]]},
            )
            forged = copy.deepcopy(manifest)
            forged["original_intent"]["planning_assessment"] = {"status": "PASS", "assessment": {}}
            with self.assertRaisesRegex(
                devcrew_phase.PhaseError, "INTENT_CERTIFICATION_UNAVAILABLE"
            ):
                devcrew_phase.validate_manifest(forged, receipt)
            legacy = copy.deepcopy(manifest)
            legacy["schema_version"] = 1
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_ORIGINAL_INTENT"):
                devcrew_phase.validate_manifest(legacy, receipt)

    def test_v2_requirement_binds_exact_source_bytes_and_acceptance_reference(self):
        with tempfile.TemporaryDirectory() as root:
            task_root = pathlib.Path(root)
            receipt = {
                "owner": "DEVCREW",
                "state": "RESOLVED",
                "repository_id": "repo",
                "task_id": "T01",
                "task_worktree_root": str(task_root),
                "branch": "codex/tasks/T01",
                "expected_head": "a" * 40,
            }
            manifest = plan()
            manifest["schema_version"] = 2
            manifest["worktree"]["task_worktree_root"] = str(task_root)
            source_path = task_root / "attachment.txt"
            content = "Need a card.\r\n".encode("utf-8")
            source_path.write_bytes(content)
            source = devcrew_phase.bind_intent_source(
                task_root,
                receipt,
                {
                    "mode": "file",
                    "source_type": "attachment",
                    "path": str(source_path),
                    "expected_sha256": hashlib.sha256(content).hexdigest(),
                    "origin": "supplied attachment",
                },
            )
            acceptance = manifest["phases"][0]["acceptance"][0]
            manifest["original_intent"] = {
                "sources": [source],
                "requirements": [
                    {
                        "requirement_id": "R1",
                        "kind": "explicit_user_obligation",
                        "statement": "Recommend a card",
                        "source_fragments": [
                            {
                                "source_id": source["source_id"],
                                "start_byte": 0,
                                "end_byte": len(content),
                                "sha256": hashlib.sha256(content).hexdigest(),
                            }
                        ],
                        "acceptance_refs": [
                            {
                                "phase_id": "P1",
                                "acceptance_index": 0,
                                "sha256": hashlib.sha256(acceptance.encode()).hexdigest(),
                            }
                        ],
                        "evidence_records": [],
                    }
                ],
                "decisions": [],
                "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
            }
            devcrew_phase.validate_manifest(manifest, receipt)
            (task_root / source["snapshot"]).write_bytes(b"tampered")
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_SNAPSHOT_CHANGED"):
                devcrew_phase.validate_manifest(manifest, receipt)

    def test_v2_save_refuses_rewriting_an_existing_requirement(self):
        with tempfile.TemporaryDirectory() as root:
            task_root = pathlib.Path(root)
            receipt = {
                "owner": "DEVCREW",
                "state": "RESOLVED",
                "repository_id": "repo",
                "task_id": "T01",
                "task_worktree_root": str(task_root),
                "branch": "codex/tasks/T01",
                "expected_head": "a" * 40,
            }
            manifest = plan()
            manifest["schema_version"] = 2
            manifest["worktree"]["task_worktree_root"] = str(task_root)
            source = devcrew_phase.bind_intent_source(
                task_root,
                receipt,
                {
                    "mode": "unavailable",
                    "source_type": "user_prompt",
                    "origin": "original request",
                    "reason": "capture unavailable",
                },
            )
            manifest["original_intent"] = {
                "sources": [source],
                "requirements": [
                    {
                        "requirement_id": "F1",
                        "kind": "established_fact",
                        "statement": "Known fact",
                        "source_fragments": [],
                        "acceptance_refs": [],
                        "evidence_records": [],
                    }
                ],
                "decisions": [],
                "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
            }
            manifest["manifest_revision"] = 1
            path = task_root / "T01.phases.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            changed = copy.deepcopy(manifest)
            changed["original_intent"]["requirements"][0]["statement"] = "Rewritten fact"
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_IMMUTABILITY_VIOLATION"):
                devcrew_phase.save_manifest(path, changed, receipt)

    def test_commit_subject_must_be_task_bound_conventional_one_line(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        for subject in (
            "feat: action",
            "type(T02): action",
            "type(T01): ",
            "type(T01): action\nextra",
            "type(T01): action\tbad",
        ):
            with self.subTest(subject=subject):
                value = plan()
                value["phases"][0]["commit_subject"] = subject
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase.validate_manifest(value, receipt)
                self.assertEqual(caught.exception.code, "INVALID_COMMIT_SUBJECT")

    def test_duplicate_phase_id_is_machine_readable_error(self):
        value = plan("MULTI_PHASE")
        value["phases"][1]["phase_id"] = "P1"
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(value, receipt)
        self.assertEqual(caught.exception.code, "DUPLICATE_PHASE_ID")

    def test_systemic_replan_completion_has_dedicated_operation(self):
        self.assertTrue(hasattr(devcrew_phase, "complete_systemic_replan"))

    def test_multi_phase_next_phase_waits_for_close_gate(self):
        manifest = plan("MULTI_PHASE")
        payload = devcrew_phase.executor_input(manifest, pathlib.Path("/tmp/T01"))
        self.assertEqual(payload["phase_id"], "P1")
        self.assertNotIn("P2", str(payload))
        manifest["phases"][0]["status"] = "READY_FOR_CLOSE"
        manifest["phases"][0]["evidence"] = [".codex/checks/check.json"]
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.resume(manifest)
        self.assertEqual(caught.exception.code, "PHASE_EVIDENCE_VALIDATION_REQUIRED")

    def test_status_transition_cannot_start_next_phase(self):
        manifest = plan("MULTI_PHASE")
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "phase.json"
            devcrew_phase.save_manifest(path, manifest, receipt)
            manifest = devcrew_phase.set_phase_status(path, manifest, receipt, "P1", "EXECUTING")
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.set_phase_status(path, manifest, receipt, "P2", "EXECUTING")
            self.assertEqual(caught.exception.code, "CURRENT_PHASE_ONLY")
            manifest = devcrew_phase.set_phase_status(path, manifest, receipt, "P1", "VERIFYING")
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.set_phase_status(path, manifest, receipt, "P1", "READY_FOR_CLOSE")
            self.assertEqual(caught.exception.code, "PHASE_EVIDENCE_REQUIRED")
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.set_phase_status(
                    path,
                    manifest,
                    receipt,
                    "P1",
                    "READY_FOR_CLOSE",
                    [".codex/checks/check.json"],
                    pathlib.Path(root),
                )
            self.assertEqual(caught.exception.code, "INVALID_DISPATCH_ROUTE")

    def test_manifest_resume_loads_without_planner(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as root:
            project = pathlib.Path(root)
            subprocess = __import__("subprocess")
            subprocess.run(["git", "init", "-q", str(project)], check=True)
            subprocess.run(["git", "-C", str(project), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(project), "config", "user.email", "test@example.com"], check=True
            )
            (project / "base").write_text("base\n")
            subprocess.run(["git", "-C", str(project), "add", "base"], check=True)
            subprocess.run(["git", "-C", str(project), "commit", "-qm", "base"], check=True)
            path = devcrew_phase.manifest_path(project, "T01")
            devcrew_phase.save_manifest(path, plan(), receipt)
            resumed = devcrew_phase.load_for_resume(path, receipt)
            self.assertEqual(resumed["current_phase_id"], "P1")
            self.assertFalse(devcrew_phase.resume(resumed)["planner_required"])

    def test_worktree_identity_mismatch_fails_closed(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "other",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(plan(), receipt)
        self.assertEqual(caught.exception.code, "WORKTREE_LINKAGE_MISMATCH")

    def test_dependency_and_scope_errors_are_rejected(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        cases = [
            (
                "missing dependency",
                lambda m: m["phases"][0].update(depends_on=["absent"]),
                "MISSING_DEPENDENCY",
            ),
            (
                "self dependency",
                lambda m: m["phases"][0].update(depends_on=["P1"]),
                "SELF_DEPENDENCY",
            ),
            (
                "duplicate dependency",
                lambda m: m["phases"][1].update(depends_on=["P1", "P1"]),
                "DUPLICATE_DEPENDENCY",
            ),
            (
                "dependency cycle",
                lambda m: (
                    m["phases"][0].update(depends_on=["P2"]),
                    m["phases"][1].update(depends_on=["P1"]),
                ),
                "DEPENDENCY_CYCLE",
            ),
            (
                "unsafe scope",
                lambda m: m["phases"][0].update(owned_scope=["../escape"]),
                "UNSAFE_OWNED_SCOPE",
            ),
            ("empty goal", lambda m: m["phases"][0].update(goal=" "), "EMPTY_GOAL"),
            (
                "empty acceptance",
                lambda m: m["phases"][0].update(acceptance=[]),
                "EMPTY_ACCEPTANCE",
            ),
            (
                "missing verification",
                lambda m: m["phases"][0].pop("verification"),
                "MISSING_VERIFICATION",
            ),
            (
                "empty verification",
                lambda m: m["phases"][0].update(verification=[""]),
                "MISSING_VERIFICATION",
            ),
            (
                "invalid verification item",
                lambda m: m["phases"][0].update(verification=[None]),
                "MISSING_VERIFICATION",
            ),
            (
                "missing boundary",
                lambda m: m["phases"][0].update(commit_boundary=""),
                "MISSING_COMMIT_BOUNDARY",
            ),
        ]
        for label, mutate, code in cases:
            with self.subTest(label=label):
                value = (
                    plan("MULTI_PHASE")
                    if label in ("duplicate dependency", "dependency cycle")
                    else plan()
                )
                mutate(value)
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase.validate_manifest(value, receipt)
                self.assertEqual(caught.exception.code, code)

    def test_invalid_current_phase_and_multiple_active_phases_rejected(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        invalid_current = plan("MULTI_PHASE")
        invalid_current["current_phase_id"] = "MISSING"
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(invalid_current, receipt)
        self.assertEqual(caught.exception.code, "INVALID_CURRENT_PHASE")
        multiple = plan("MULTI_PHASE")
        multiple["phases"][0]["status"] = "EXECUTING"
        multiple["phases"][1]["status"] = "VERIFYING"
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(multiple, receipt)
        self.assertEqual(caught.exception.code, "MULTIPLE_ACTIVE_PHASES")

    def test_invalid_task_worktree_linkage_rejected(self):
        manifest = plan()
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "wrong",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(manifest, receipt)
        self.assertEqual(caught.exception.code, "WORKTREE_LINKAGE_MISMATCH")

    def test_planner_semantic_contract_rejects_giant_single_phase_claim(self):
        value = plan()
        value["semantic_contract"]["capabilities"] = [
            "3DS",
            "saved token payments",
            "refunds",
            "webhook verification",
        ]
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.validate_manifest(value, receipt)
        self.assertEqual(caught.exception.code, "SEMANTIC_CONTRACT_MISMATCH")

    def test_owned_scope_rejects_non_normalized_aliases(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        for alias in (".", "foo//bar", "foo/./bar", "foo/", "foo\\bar"):
            with self.subTest(alias=alias):
                value = plan()
                value["phases"][0]["owned_scope"] = [alias]
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase.validate_manifest(value, receipt)
                self.assertEqual(caught.exception.code, "UNSAFE_OWNED_SCOPE")

    def test_init_refuses_to_replace_existing_manifest(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "manifest.json"
            project = pathlib.Path(root)
            spawn = self.planner_receipt(project)
            receipt = self.receipt
            value = plan()
            value["worktree"].update(
                repository_id=receipt["repository_id"],
                task_worktree_root=receipt["task_worktree_root"],
                branch=receipt["branch"],
                base_sha=receipt["expected_head"],
            )
            devcrew_phase.initialize_manifest(
                path, value, receipt, project, spawn["SPAWN_RECEIPT_ID"]
            )
            prior = path.read_bytes()
            self.assertEqual(
                json.loads(prior)["planner_dispatch"]["spawn_receipt_id"], spawn["SPAWN_RECEIPT_ID"]
            )
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.initialize_manifest(
                    path, value, receipt, project, spawn["SPAWN_RECEIPT_ID"]
                )
            self.assertEqual(caught.exception.code, "PHASE_MANIFEST_ALREADY_EXISTS")
            self.assertEqual(path.read_bytes(), prior)

    def test_init_rejects_unrouted_planner_result(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "manifest.json"
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.initialize_manifest(
                    path, plan(), receipt, pathlib.Path(root), "not-a-receipt"
                )
            self.assertEqual(caught.exception.code, "INVALID_DISPATCH_ROUTE")
            self.assertFalse(path.exists())

    def test_resume_revalidates_persisted_planner_receipt(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        with tempfile.TemporaryDirectory() as root:
            project = pathlib.Path(root)
            path = project / "manifest.json"
            spawn = self.planner_receipt(project)
            receipt = self.receipt
            value = plan()
            value["worktree"].update(
                repository_id=receipt["repository_id"],
                task_worktree_root=receipt["task_worktree_root"],
                branch=receipt["branch"],
                base_sha=receipt["expected_head"],
            )
            devcrew_phase.initialize_manifest(
                path, value, receipt, project, spawn["SPAWN_RECEIPT_ID"]
            )
            self.assertEqual(
                devcrew_phase.load_for_resume(path, receipt, project)["planner_dispatch"][
                    "agent_id"
                ],
                "planner-1",
            )
            dispatch_path = project / ".codex/sessions/agent-dispatch-T01.json"
            store = json.loads(dispatch_path.read_text())
            store["receipts"][0]["AGENT_ID"] = "replaced-agent"
            dispatch_path.write_text(json.dumps(store))
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.load_for_resume(path, receipt, project)
            self.assertEqual(caught.exception.code, "INVALID_DISPATCH_ROUTE")

    def test_ready_plan_registration_then_resume_needs_no_new_planner(self):
        with tempfile.TemporaryDirectory() as root:
            project = pathlib.Path(root)
            path = project / "manifest.json"
            spawn = self.planner_receipt(project)
            value = plan()
            value["worktree"].update(
                repository_id=self.receipt["repository_id"],
                task_worktree_root=self.receipt["task_worktree_root"],
                branch=self.receipt["branch"],
                base_sha=self.receipt["expected_head"],
            )
            original = copy.deepcopy(value)
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.initialize_manifest(
                    path, value, self.receipt, project, "missing-receipt"
                )
            self.assertEqual(caught.exception.code, "INVALID_DISPATCH_ROUTE")
            self.assertFalse(path.exists())

            devcrew_phase.initialize_manifest(
                path, value, self.receipt, project, spawn["SPAWN_RECEIPT_ID"]
            )
            self.assertEqual(value, original)
            manifest_bytes = path.read_bytes()
            dispatch_path = project / ".codex/sessions/agent-dispatch-T01.json"
            dispatch_bytes = dispatch_path.read_bytes()
            fresh_receipt = devcrew_worktree.resolve(project, "T01")
            resumed = devcrew_phase.load_for_resume(path, fresh_receipt, project)
            task_root = pathlib.Path(fresh_receipt["task_worktree_root"])
            next_step = devcrew_phase.resume(resumed, project, task_root)
            self.assertFalse(next_step["planner_required"])
            self.assertTrue(next_step["executor_required"])
            executor = devcrew_phase.executor_input(resumed, task_root, project)
            self.assertEqual(executor["phase_id"], "P1")
            self.assertEqual(executor["base_sha"], fresh_receipt["expected_head"])
            for field in ("goal", "owned_scope", "acceptance", "verification"):
                with self.subTest(field=field):
                    self.assertEqual(executor[field], original["phases"][0][field])
            self.assertEqual(path.read_bytes(), manifest_bytes)
            self.assertEqual(dispatch_path.read_bytes(), dispatch_bytes)

    def test_init_rejects_planner_supplied_runtime_status_or_evidence(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        for label, mutate in (
            (
                "runtime status",
                lambda value: value["phases"][0].update(
                    status="READY_FOR_CLOSE", evidence=[".codex/checks/fake.json"]
                ),
            ),
            (
                "supplied evidence",
                lambda value: value["phases"][0].update(evidence=[".codex/checks/fake.json"]),
            ),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                value = plan()
                mutate(value)
                path = pathlib.Path(root) / "manifest.json"
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase.initialize_manifest(
                        path, value, receipt, pathlib.Path(root), "unused"
                    )
                self.assertEqual(caught.exception.code, "INVALID_INITIAL_PHASE_STATE")
                self.assertFalse(path.exists())

    def test_unhashable_mode_status_and_current_phase_are_machine_errors(self):
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": "/tmp/T01",
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        for label, mutate, code in (
            ("mode", lambda value: value.update(task_mode=[]), "INVALID_TASK_MODE"),
            ("status", lambda value: value["phases"][0].update(status=[]), "INVALID_PHASE_STATUS"),
            ("current", lambda value: value.update(current_phase_id=[]), "INVALID_CURRENT_PHASE"),
        ):
            with self.subTest(label=label):
                value = plan()
                mutate(value)
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase.validate_manifest(value, receipt)
                self.assertEqual(caught.exception.code, code)

    def test_evidence_records_require_passed_host_local_check_and_resume_revalidates(self):
        with tempfile.TemporaryDirectory() as root:
            project = pathlib.Path(root) / "host"
            task_root = pathlib.Path(root) / "task"
            foreign = pathlib.Path(root) / "foreign"
            for directory in (project, task_root, foreign):
                directory.mkdir()
            check_path = project / ".codex/checks/check.json"
            check_path.parent.mkdir(parents=True)
            check_path.write_text("{}")
            manifest = plan()
            manifest["phases"][0]["status"] = "READY_FOR_CLOSE"
            manifest["phases"][0]["evidence"] = [".codex/checks/check.json"]
            devcrew_phase.pin_verification(manifest["phases"][0])
            contract = manifest["phases"][0]
            calls = []

            def passed_verifier(path, **kwargs):
                calls.append((path, kwargs))
                return (
                    pathlib.Path(path),
                    {
                        "status": "passed",
                        "log_sha256": "verified",
                        "command": contract["verification_bindings"][0]["argv"],
                        "timeout_seconds": 300,
                        "idle_timeout_seconds": None,
                        "environment_sha256": contract["verification_environment_sha256"],
                        "started_at": contract["verification_approved_at"],
                    },
                    project,
                )

            refs = devcrew_phase.verify_evidence_records(
                [str(check_path)], project, task_root, "T01", passed_verifier
            )
            self.assertEqual(refs, [".codex/checks/check.json"])
            result = devcrew_phase.resume(manifest, project, task_root, passed_verifier)
            self.assertEqual(result["status"], "PHASE_READY_FOR_CLOSE")
            self.assertEqual(len(calls), 2)
            self.assertFalse(result["executor_required"])
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.verify_evidence_records(
                    [str(check_path)],
                    project,
                    task_root,
                    "T01",
                    lambda *_args, **_kwargs: (_ for _ in ()).throw(
                        ValueError("record failed validation")
                    ),
                )
            self.assertEqual(caught.exception.code, "INVALID_PHASE_EVIDENCE")
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase.verify_evidence_records(
                    [str(check_path)],
                    project,
                    task_root,
                    "T01",
                    lambda *_args, **_kwargs: (check_path, {}, foreign),
                )
            self.assertEqual(caught.exception.code, "FOREIGN_PHASE_EVIDENCE")


if __name__ == "__main__":
    unittest.main()
