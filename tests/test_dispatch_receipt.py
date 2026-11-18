import json
import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import devcrew_capacity
import devcrew_context
import devcrew_cost
import devcrew_dispatch
import test_phase_review_lifecycle as phase_review


class DispatchReceiptContract(unittest.TestCase):
    def test_executor_freshness_rejects_inherited_and_cross_phase_identity(self):
        store = {
            "receipts": [
                {
                    "ROLE": "executor",
                    "TASK_ID": "T01",
                    "PHASE_ID": "P1",
                    "AGENT_ID": "old-agent",
                    "FORK_TURNS": "none",
                }
            ]
        }
        with self.assertRaisesRegex(ValueError, "fresh executor context"):
            devcrew_dispatch._require_fresh_executor_context(
                store, task_id="T01", phase_id="P2", agent_id="new-agent", fork_turns="all"
            )
        with self.assertRaisesRegex(ValueError, "different phase"):
            devcrew_dispatch._require_fresh_executor_context(
                store, task_id="T01", phase_id="P2", agent_id="old-agent", fork_turns="none"
            )
        devcrew_dispatch._require_fresh_executor_context(
            store, task_id="T01", phase_id="P1", agent_id="old-agent", fork_turns="none"
        )
        with self.assertRaisesRegex(ValueError, "linked continuation receipt"):
            devcrew_dispatch._require_fresh_executor_context(
                store,
                task_id="T01",
                phase_id="P1",
                agent_id="old-agent",
                fork_turns="none",
                reject_same_phase_reuse=True,
            )

    def test_reviewer_identity_must_be_separate_from_executor(self):
        store = {
            "receipts": [
                {"ROLE": "executor", "TASK_ID": "T01", "PHASE_ID": "P1", "AGENT_ID": "writer"}
            ]
        }
        with self.assertRaisesRegex(ValueError, "independent from the executor"):
            devcrew_dispatch._require_independent_reviewer(store, task_id="T01", agent_id="writer")
        devcrew_dispatch._require_independent_reviewer(store, task_id="T01", agent_id="reviewer")

    def test_receipt_store_matches_session_location_with_and_without_config(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory).resolve()
            expected = project / ".codex/sessions/agent-dispatch-T01.json"
            self.assertEqual(devcrew_dispatch._store_path(project, "T01"), expected)
            self.assertEqual(devcrew_capacity.dispatch_receipt_path(project, "T01"), expected)
            config = project / ".codex/devcrew.json"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "paths": {
                            "context": "docs/context.md",
                            "tasks": ".codex/tasks",
                            "sessions": ".codex/custom-sessions",
                            "memory": "memory",
                            "checks": ".codex/checks",
                        },
                    }
                )
            )
            expected = project / ".codex/custom-sessions/agent-dispatch-T01.json"
            self.assertEqual(devcrew_dispatch._store_path(project, "T01"), expected)
            self.assertEqual(devcrew_capacity.dispatch_receipt_path(project, "T01"), expected)
            self.assertFalse(expected.parent.exists())

    def lifecycle_fixture(self):
        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.last_fixture = fixture
        return fixture

    def routed(self, root, role, **identity):
        fixture = self.lifecycle_fixture()
        if role == "planner":
            runtime = {
                "models": [
                    {
                        "id": "bounded-test",
                        "capabilities": ["bounded"],
                        "efforts": ["low", "medium"],
                    },
                    {
                        "id": "strong-test",
                        "capabilities": ["strong"],
                        "efforts": ["low", "medium", "high"],
                    },
                ],
                "roles": {"decision-agent": {"available": True, "supports_overrides": True}},
                "generic_available": True,
                "explicit_model_effort": True,
            }
            prepared = devcrew_context.routed_spawn(
                "planner_T01",
                "Plan current task",
                devcrew_cost.route("planner", runtime=runtime),
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity={"TASK_ID": "T01", "BASE_SHA": fixture.base},
                role_contract="Plan the bounded task.",
            )
            return devcrew_dispatch.record_spawn(
                fixture.host,
                {
                    "STATUS": "SPAWN_SUCCESS",
                    "REQUESTED_ROLE": "planner",
                    "TASK_ID": "T01",
                    "BASE_SHA": fixture.base,
                    "AGENT_ID": "agent-planner",
                    "SPAWN_MODEL": prepared["spawn"]["model"],
                    "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
                    "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
                    "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                    "SPAWN_PAYLOAD": prepared["spawn"],
                    "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
                },
            )
        if role == "executor":
            fixture.manifest = __import__("devcrew_phase").set_phase_status(
                fixture.path,
                fixture.manifest,
                fixture.receipt,
                "P1",
                "EXECUTING",
                project=fixture.host,
                verifier=fixture.verifier,
            )
            return fixture.routed_executor_fields("P1")
        if role == "reviewer":
            fixture.make_candidate()
            fixture.receipt = __import__("devcrew_worktree").resolve(fixture.host, "T01")
            fixture.manifest = __import__("devcrew_phase").load_for_resume(
                fixture.path, fixture.receipt
            )
            __import__("devcrew_phase").begin_review(
                fixture.path,
                fixture.manifest,
                fixture.receipt,
                fixture.host,
                fixture.task,
                "P1",
                fixture.verifier,
            )
            fixture.receipt = __import__("devcrew_worktree").resolve(fixture.host, "T01")
            fixture.manifest = __import__("devcrew_phase").load_for_resume(
                fixture.path, fixture.receipt
            )
            return fixture.routed_reviewer_fields(
                __import__("devcrew_phase").review_target(fixture.manifest, "P1")
            )
        raise AssertionError(role)

    def test_route_is_required_before_spawn_receipt_can_be_recorded(self):
        with self.assertRaisesRegex(ValueError, "DISPATCH_ROUTE_REQUIRED"):
            devcrew_dispatch.record_spawn(
                tempfile.gettempdir(),
                {
                    "STATUS": "SPAWN_SUCCESS",
                    "TASK_ID": "T1",
                    "AGENT_ID": "agent-1",
                    "PREPARED": {"route": {}, "dispatch": {"SPAWN_MODE": "generic"}},
                },
            )

    def test_public_issuance_rejects_caller_built_route_dispatch_and_payload(self):
        fixture = self.lifecycle_fixture()
        with self.assertRaisesRegex(TypeError, "unexpected keyword argument"):
            devcrew_dispatch.issue_route(
                fixture.host,
                task_id="T01",
                role="planner",
                identity={"TASK_ID": "T01", "BASE_SHA": fixture.base},
                route={},
                dispatch={"SPAWN_MODE": "generic"},
                spawn_payload={},
            )

    def test_fabricated_or_altered_postflight_cannot_consume_route(self):
        fixture = self.lifecycle_fixture()
        root = fixture.host
        intent = {
            "role": "planner",
            "task_name": "planner_T01",
            "message": "plan",
            "runtime": {
                "models": [
                    {
                        "id": "bounded-test",
                        "capabilities": ["bounded"],
                        "efforts": ["low", "medium"],
                    },
                    {
                        "id": "strong-test",
                        "capabilities": ["strong"],
                        "efforts": ["low", "medium", "high"],
                    },
                ],
                "roles": {},
                "generic_available": True,
                "explicit_model_effort": True,
            },
            "route_options": {"task_class": "PLAN", "decision_complexity": "NORMAL", "risk": "low"},
            "role_contract": "Plan the current task.",
        }
        identity = {"TASK_ID": "T01", "BASE_SHA": fixture.base}
        issued = devcrew_dispatch.issue_route(
            root, task_id="T01", role="planner", identity=identity, intent=intent
        )
        route, dispatch, payload = (
            issued["PREPARED"]["route"],
            issued["PREPARED"]["dispatch"],
            issued["SPAWN_PAYLOAD"],
        )
        report = {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "planner",
            "TASK_ID": "T01",
            "BASE_SHA": fixture.base,
            "AGENT_ID": "agent-1",
            "SPAWN_MODEL": route["RESOLVED_MODEL"],
            "SPAWN_REASONING_EFFORT": route["REASONING_EFFORT"],
            "SPAWN_MODE": dispatch["SPAWN_MODE"],
            "PREPARED": issued["PREPARED"],
            "SPAWN_PAYLOAD": payload,
            "SPAWN_PAYLOAD_SHA256": issued["SPAWN_PAYLOAD_SHA256"],
        }
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.record_spawn(
                root,
                {
                    **report,
                    "PREPARED": {
                        "route": {**route, "ROUTE_ID": "fabricated"},
                        "dispatch": dispatch,
                    },
                },
            )
        altered = dict(report)
        altered["SPAWN_PAYLOAD"] = {**payload, "message": "changed after issuance"}
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.record_spawn(root, altered)
        altered_route = dict(report)
        altered_route["PREPARED"] = {**report["PREPARED"], "route": {**route, "RISK": "high"}}
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.record_spawn(root, altered_route)
        for changed in (dict(report, SPAWN_MODEL="deep-test"), dict(report, SPAWN_MODE="typed")):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "does not match|INVALID_DISPATCH_ROUTE"),
            ):
                devcrew_dispatch.record_spawn(root, changed)
        self.assertEqual(devcrew_dispatch.record_spawn(root, report)["ROLE"], "planner")
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.record_spawn(root, report)

    def test_same_role_identity_allows_distinct_routes_but_duplicate_markers_fail(self):
        fixture = self.lifecycle_fixture()
        root = fixture.host
        intent = {
            "role": "planner",
            "task_name": "planner",
            "message": "plan",
            "runtime": {
                "models": [
                    {
                        "id": "bounded-test",
                        "capabilities": ["bounded"],
                        "efforts": ["low", "medium"],
                    },
                    {
                        "id": "strong-test",
                        "capabilities": ["strong"],
                        "efforts": ["low", "medium", "high"],
                    },
                ],
                "roles": {},
                "generic_available": True,
                "explicit_model_effort": True,
            },
            "route_options": {"task_class": "PLAN", "decision_complexity": "NORMAL", "risk": "low"},
            "role_contract": "Plan the current task.",
        }
        args = dict(
            task_id="T01",
            role="planner",
            identity={"TASK_ID": "T01", "BASE_SHA": fixture.base},
            intent=intent,
        )
        first = devcrew_dispatch.issue_route(root, **args)
        second = devcrew_dispatch.issue_route(
            root, **{**args, "intent": {**intent, "message": "different"}}
        )
        self.assertNotEqual(first["ROUTE_ID"], second["ROUTE_ID"])
        duplicate_marker = {**intent, "message": "plan\nMODEL_ROUTE: caller-substitution"}
        with self.assertRaisesRegex(ValueError, "DISPATCH_SERVICE_MARKER_DUPLICATE"):
            devcrew_dispatch.issue_route(root, **{**args, "intent": duplicate_marker})

    def test_spawn_receipt_binds_route_agent_and_phase_review_target(self):
        fixture = self.lifecycle_fixture()
        fixture.make_candidate()
        fixture.receipt = __import__("devcrew_worktree").resolve(fixture.host, "T01")
        fixture.manifest = __import__("devcrew_phase").load_for_resume(
            fixture.path, fixture.receipt
        )
        __import__("devcrew_phase").begin_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
        )
        fixture.receipt = __import__("devcrew_worktree").resolve(fixture.host, "T01")
        fixture.manifest = __import__("devcrew_phase").load_for_resume(
            fixture.path, fixture.receipt
        )
        target = __import__("devcrew_phase").review_target(fixture.manifest, "P1")
        result = fixture.routed_reviewer_fields(target)
        self.assertEqual(result["AGENT_ID"].startswith("review-agent-"), True)
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                fixture.host,
                result["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="reviewer",
                phase_id="P1",
                base_sha=target["BASE_SHA"],
                target_sha=target["TARGET_SHA"],
                agent_id=result["AGENT_ID"],
            )
        )

    def test_reviewer_inherited_context_is_not_authoritative(self):
        with tempfile.TemporaryDirectory() as root:
            route = {
                "ROUTE_ID": "r-2",
                "ROLE": "reviewer",
                "RESOLVED_MODEL": "strong-test",
                "REASONING_EFFORT": "high",
                "TASK_CLASS": "REVIEW",
                "COORDINATION_COMPLEXITY": None,
                "DECISION_COMPLEXITY": None,
                "IMPLEMENTATION_COMPLEXITY": None,
                "REVIEW_COMPLEXITY": "COMPLEX",
                "RISK": "high",
                "MODEL_CLASS": "strong",
                "SELECTION_REASON": "high_risk_review",
            }
            with self.assertRaisesRegex(ValueError, "fresh reviewer context"):
                devcrew_dispatch.record_spawn(
                    root,
                    {
                        "STATUS": "SPAWN_SUCCESS",
                        "REQUESTED_ROLE": "reviewer",
                        "TASK_ID": "T1",
                        "PHASE_ID": "P1",
                        "AGENT_ID": "agent-2",
                        "FORK_TURNS": "all",
                        "SPAWN_MODEL": "strong-test",
                        "SPAWN_REASONING_EFFORT": "high",
                        "SPAWN_MODE": "generic",
                        "PREPARED": {"route": route, "dispatch": {"SPAWN_MODE": "generic"}},
                    },
                )

    def test_planner_and_executor_receipts_bind_phase_and_writer_cycle(self):
        planner = self.routed(None, "planner")
        root = self.last_fixture.host
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                root,
                planner["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="planner",
                base_sha=self.last_fixture.base,
            )
        )
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.verify_spawn(
                root,
                planner["SPAWN_RECEIPT_ID"],
                task_id="T02",
                role="planner",
                base_sha=self.last_fixture.base,
            )
        executor = self.routed(None, "executor")
        root = self.last_fixture.host
        resumed = __import__("devcrew_phase").resume(
            self.last_fixture.manifest, root, self.last_fixture.task, self.last_fixture.verifier
        )
        self.assertEqual(resumed["executor_lifecycle"], "CURRENT_EXECUTOR_CONTEXT_AUTHORIZED")
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                root,
                executor["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="executor",
                phase_id="P1",
                base_sha=self.last_fixture.base,
                writer_cycle=0,
                rejected_candidate_sha=None,
            )
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            devcrew_dispatch.verify_spawn(
                root,
                executor["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="executor",
                phase_id="P1",
                base_sha=self.last_fixture.base,
                writer_cycle=1,
                rejected_candidate_sha=None,
            )


if __name__ == "__main__":
    unittest.main()
