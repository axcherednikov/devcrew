import hashlib
import copy
import pathlib
import sys
import json
import subprocess
import tempfile
import tomllib
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import devcrew_context as context
import devcrew_capacity
import devcrew_cost as cost
import devcrew_dispatch
import devcrew_requirements
import devcrew_phase
import devcrew_worktree
import test_phase_review_lifecycle as phase_review
from devcrew_test_helpers import (
    ExactPathSeed,
    copied_attributes,
    create_task_checkout,
    restore_attributes,
)


def _requirements_envelope(message):
    marker = next(
        line.partition(": ")[2]
        for line in message.splitlines()
        if line.startswith("ENGINEERING_REQUIREMENTS: ")
    )
    return json.loads(marker)


class AgentContext(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        try:
            if hasattr(cls, "_reviewer_seed"):
                cls._reviewer_seed.close()
            if hasattr(cls, "_reviewer_seed_builder"):
                cls._reviewer_seed_builder.doCleanups()
        finally:
            for name in (
                "_reviewer_seed",
                "_reviewer_seed_builder",
                "_reviewer_seed_attributes",
                "_reviewer_seed_target",
                "_reviewer_seed_evidence",
                "_reviewer_seed_active",
            ):
                if name in cls.__dict__:
                    delattr(cls, name)
            super().tearDownClass()

    @classmethod
    def _ensure_reviewer_seed(cls):
        if hasattr(cls, "_reviewer_seed"):
            return
        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.setUp()
        fixture.manifest["phases"][0]["main_scenario"] = (
            "A request changes one.txt and verification observes the result."
        )
        devcrew_phase.save_manifest(fixture.path, fixture.manifest, fixture.receipt)
        fixture.make_candidate()
        ready = devcrew_phase.begin_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
        )
        fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        cls._reviewer_seed_builder = fixture
        cls._reviewer_seed_attributes = copied_attributes(
            fixture, ("host", "base", "receipt", "task", "manifest", "path", "verifier")
        )
        cls._reviewer_seed_target = copy.deepcopy(ready["review_target"])
        cls._reviewer_seed_evidence = list(
            fixture.manifest["phases"][0]["candidate_commit"]["evidence"]
        )
        cls._reviewer_seed = ExactPathSeed(fixture.temp.name)
        cls._reviewer_seed_active = False

    @classmethod
    def _release_reviewer_seed(cls):
        try:
            cls._reviewer_seed.restore()
        finally:
            cls._reviewer_seed_active = False

    def _fresh_reviewer_fixture(self):
        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.manifest["phases"][0]["main_scenario"] = (
            "A request changes one.txt and verification observes the result."
        )
        devcrew_phase.save_manifest(fixture.path, fixture.manifest, fixture.receipt)
        fixture.make_candidate()
        ready = devcrew_phase.begin_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
        )
        fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        return (
            fixture,
            ready["review_target"],
            list(fixture.manifest["phases"][0]["candidate_commit"]["evidence"]),
        )

    def reviewer_fixture(self):
        if getattr(self, "_reviewer_fixture_used", False):
            return self._fresh_reviewer_fixture()
        self._reviewer_fixture_used = True
        self._ensure_reviewer_seed()
        seed = self._reviewer_seed
        if not seed.matches_environment() or self.__class__._reviewer_seed_active:
            return self._fresh_reviewer_fixture()
        seed.restore()
        self.__class__._reviewer_seed_active = True
        self.addCleanup(self.__class__._release_reviewer_seed)
        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.temp = self._reviewer_seed_builder.temp
        restore_attributes(fixture, self._reviewer_seed_attributes)
        return (
            fixture,
            copy.deepcopy(self._reviewer_seed_target),
            list(self._reviewer_seed_evidence),
        )

    def runtime(self, **changes):
        return {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
                {
                    "id": "strong-test",
                    "capabilities": ["strong"],
                    "efforts": ["low", "medium", "high"],
                },
            ],
            "roles": {
                role: {"available": True, "supports_overrides": True}
                for role in ("executor-agent", "reviewer-agent", "decision-agent")
            },
            "generic_available": True,
            "explicit_model_effort": True,
        } | changes

    def build_routed(self, task_name, message, selected, runtime, *, role_contract=None, **extra):
        role = selected["ROLE"]
        if role == "reviewer":
            fixture, review_contract, evidence = self.reviewer_fixture()
            return context.reviewer_spawn(
                "T01",
                runtime=runtime,
                selection=selected,
                role_contract=role_contract,
                project_root=fixture.host,
                phase_id=review_contract["PHASE_ID"],
                base_sha=review_contract["BASE_SHA"],
                target_sha=review_contract["TARGET_SHA"],
                review_contract=review_contract,
                verification_evidence=evidence,
            )
        project, worktree, base, contract = self.task_checkout("T04")
        project = project.resolve()
        if role == "executor":
            return context.routed_task_spawn(
                task_name,
                message,
                selected,
                runtime,
                task_id="T04",
                project_root=project,
                task_worktree_root=worktree,
                base_sha=base,
                owned_scope=contract["OWNED_SCOPE"],
                acceptance=contract["ACCEPTANCE"],
                verification=contract["VERIFICATION"],
                receipt=contract["TASK_WORKTREE_RECEIPT"],
                role_contract=role_contract,
            )
        identity = {"TASK_ID": "T04", "BASE_SHA": base}
        return context.routed_spawn(
            task_name,
            message,
            selected,
            runtime,
            role_contract=role_contract,
            project_root=project,
            task_id="T04",
            identity=identity,
            **extra,
        )

    def build_reviewer(self, task_id="T04", *, prepared_fixture=None, **kwargs):
        fixture, review_contract, evidence = prepared_fixture or self.reviewer_fixture()
        return context.reviewer_spawn(
            "T01",
            project_root=fixture.host,
            phase_id="P1",
            base_sha=review_contract["BASE_SHA"],
            target_sha=review_contract["TARGET_SHA"],
            review_contract=review_contract,
            verification_evidence=evidence,
            **kwargs,
        )

    def agent(self, **changes):
        return {
            "agent_id": "executor-1",
            "role": "executor",
            "task_id": "T04",
            "current_context_tokens": 500,
            "context_limit": 1000,
            "runtime": self.runtime(),
        } | changes

    def task_checkout(self, task_id="T04", branch=None):
        root = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(root, ignore_errors=True))
        project = root / "repo"
        project.mkdir()

        def git(*args, cwd=project):
            return subprocess.run(
                ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
            )

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        (project / "tracked.txt").write_text("base\n")
        (project / ".gitignore").write_text("/.codex/\n")
        git("add", "tracked.txt", ".gitignore")
        git("commit", "-qm", "base")
        base = git("rev-parse", "HEAD").stdout.strip()
        if branch:
            key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]
            worktree = (
                project.parent / ".devcrew-task-worktrees" / f"{project.name}-{key}" / task_id
            )
            worktree.parent.mkdir(parents=True)
            git("worktree", "add", "-b", branch, str(worktree), base)
            receipt = None
        else:
            receipt = create_task_checkout(
                project, task_id, base, worktree_request="Use a worktree for this task"
            )
            worktree = pathlib.Path(receipt["task_worktree_root"])
        contract = {
            "PROJECT_ROOT": str(project),
            "TASK_WORKTREE_ROOT": str(worktree),
            "BASE_SHA": base,
            "OWNED_SCOPE": ["tracked.txt"],
            "ACCEPTANCE": ["behavior works"],
            "VERIFICATION": ["unit tests pass"],
            "TASK_WORKTREE_RECEIPT": receipt,
        }
        if receipt:
            resolved = __import__("devcrew_worktree").resolve(project, task_id)
            contract["TASK_WORKTREE_RECEIPT"] = resolved
            phase_manifest = {
                "schema_version": 1,
                "task_id": task_id,
                "task_mode": "SINGLE_PHASE",
                "semantic_contract": {"capabilities": ["bounded task capability"]},
                "worktree": {
                    "repository_id": resolved["repository_id"],
                    "task_worktree_root": resolved["task_worktree_root"],
                    "branch": resolved["branch"],
                    "base_sha": resolved["expected_head"],
                },
                "current_phase_id": "P1",
                "phases": [
                    {
                        "phase_id": "P1",
                        "goal": "bounded phase goal",
                        "main_scenario": "A request changes tracked.txt and returns the result.",
                        "depends_on": [],
                        "owned_scope": ["tracked.txt"],
                        "acceptance": ["phase behavior works"],
                        "verification": ["phase tests pass"],
                        "verification_bindings": [
                            {
                                "index": 0,
                                "verification": "phase tests pass",
                                "argv": [sys.executable, "-c", "print('pass')"],
                            }
                        ],
                        "commit_boundary": "One capability",
                        "commit_subject": f"type({task_id}): implement bounded phase",
                        "status": "PLANNED",
                    }
                ],
            }
            planner = phase_review.issue_planner_receipt(
                project, task_id, resolved["expected_head"], "planner payload", f"planner-{task_id}"
            )
            phase_manifest["planner_dispatch"] = {
                "spawn_receipt_id": planner["SPAWN_RECEIPT_ID"],
                "route_id": planner["ROUTE_ID"],
                "agent_id": planner["AGENT_ID"],
                "model": planner["RESOLVED_MODEL"],
                "effort": planner["REASONING_EFFORT"],
                "spawn_mode": planner["SPAWN_MODE"],
            }
            devcrew_phase.save_manifest(
                devcrew_phase.manifest_path(project, task_id), phase_manifest, resolved
            )
        return project, worktree, base, contract

    def replacement_contract(self, goal="continue", task_id="T04"):
        _, _, _, fields = self.task_checkout(task_id)
        return {"GOAL": goal, **fields}

    def executor_route(self, task_id):
        project, worktree, base, contract = self.task_checkout(task_id)
        project = project.resolve()
        runtime = self.runtime()
        selected = cost.route("executor", runtime=runtime)
        prepared = context.routed_task_spawn(
            "executor",
            "Implement",
            selected,
            runtime,
            task_id=task_id,
            project_root=str(project),
            task_worktree_root=str(worktree),
            base_sha=base,
            owned_scope=contract["OWNED_SCOPE"],
            acceptance=contract["ACCEPTANCE"],
            verification=contract["VERIFICATION"],
            receipt=contract["TASK_WORKTREE_RECEIPT"],
            role_contract="Exact executor contract",
        )
        payload = prepared["spawn"]
        report = {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "executor",
            "TASK_ID": task_id,
            "PHASE_ID": "P1",
            "BASE_SHA": base,
            "WRITER_CYCLE": 0,
            "REJECTED_CANDIDATE_SHA": None,
            "AGENT_ID": "executor-" + task_id,
            "SPAWN_MODEL": payload["model"],
            "SPAWN_REASONING_EFFORT": payload["reasoning_effort"],
            "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
            "FORK_TURNS": "none",
            "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
            "SPAWN_PAYLOAD": payload,
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        }
        return project, worktree, base, contract, prepared, report

    def test_thresholds_and_unknown_limit(self):
        self.assertEqual(
            context.evaluate(self.agent(current_context_tokens=440))["signal"], "CONTINUE"
        )
        self.assertEqual(
            context.evaluate(self.agent(current_context_tokens=450))["signal"],
            "HANDOFF_RECOMMENDED",
        )
        self.assertEqual(
            context.evaluate(self.agent(current_context_tokens=600))["signal"], "HANDOFF_REQUIRED"
        )
        self.assertEqual(
            context.evaluate(
                self.agent(current_context_tokens=500),
                {"soft_context_percent": 45, "hard_context_percent": None},
            )["signal"],
            "HANDOFF_RECOMMENDED",
        )
        unknown = context.evaluate(self.agent(context_limit=None))
        self.assertEqual(unknown["signal"], "CONTINUE")
        self.assertIsNone(unknown["context_usage_percent"])
        self.assertEqual(
            context.evaluate(
                self.agent(context_limit=None),
                {"soft_context_tokens": 400, "hard_context_tokens": 600},
            )["signal"],
            "HANDOFF_RECOMMENDED",
        )

    def test_usage_preserves_unknown_and_does_not_double_count_subcategories(self):
        usage = context.normalize_usage(None)
        self.assertIsNone(usage["total_recorded_tokens"])
        usage = context.normalize_usage(
            {
                "input_tokens": 100,
                "cached_input_tokens": 20,
                "output_tokens": 30,
                "reasoning_tokens": 5,
            }
        )
        self.assertEqual(usage["total_recorded_tokens"], 130)
        self.assertIsNone(context.normalize_usage({"input_tokens": 100})["total_recorded_tokens"])

    def test_handoff_is_fresh_same_task_without_commit(self):
        contract = self.replacement_contract("finish T04")
        contract["ACCEPTANCE"] = ["tests pass"]
        handoff = context.prepare_handoff(
            self.agent(),
            contract,
            {"DONE": ["first step"], "NEXT_ACTION": "second step"},
            "executor-2",
        )
        self.assertEqual(handoff["spawn"]["fork_turns"], "none")
        self.assertEqual(handoff["spawn"]["task_name"], "executor_2")
        self.assertEqual(handoff["spawn"]["agent_type"], "executor-agent")
        self.assertEqual(
            set(handoff["spawn"]),
            {"task_name", "agent_type", "fork_turns", "message", "model", "reasoning_effort"},
        )
        self.assertIn('"TASK_ID": "T04"', handoff["spawn"]["message"])
        self.assertEqual(handoff["handoff"]["TASK_ID"], "T04")
        self.assertEqual(handoff["handoff"]["DONE"], ["first step"])
        # Loop-attempt history is required; unbounded reasoning-history payloads remain forbidden.
        self.assertNotIn('"history":', json.dumps(handoff).lower())
        self.assertIn("Do not commit", handoff["spawn"]["message"])
        self.assertNotIn("COMMIT", handoff["handoff"])
        spaced_contract = self.replacement_contract("finish T04")
        spaced_contract.update({"TASK ID": "T04", "IN SCOPE": "code"})
        spaced = context.prepare_handoff(self.agent(), spaced_contract, {}, "executor-2")
        phase_line = next(
            line.removeprefix("PHASE_EXECUTION_CONTRACT: ")
            for line in spaced["spawn"]["message"].splitlines()
            if line.startswith("PHASE_EXECUTION_CONTRACT: ")
        )
        self.assertEqual(json.loads(phase_line)["PHASE_ID"], "P1")
        self.assertNotIn("IN_SCOPE", phase_line)
        with self.assertRaises(ValueError):
            context.prepare_handoff(
                self.agent(), {"GOAL": "finish", "history": "old transcript"}, {}, "executor-2"
            )

    def test_multiple_handoffs_reviewer_and_failed_spawn(self):
        contract = self.replacement_contract("finish T04")
        first = context.prepare_handoff(self.agent(), contract, {}, "executor-2")
        refreshed = __import__("devcrew_worktree").resolve(contract["PROJECT_ROOT"], "T04")
        second_contract = contract | {"TASK_WORKTREE_RECEIPT": refreshed}
        second = context.prepare_handoff(
            self.agent(agent_id="executor-2", handoff_count=1), second_contract, {}, "executor-3"
        )
        self.assertEqual(first["handoff_count"], 1)
        self.assertEqual(second["handoff_count"], 2)
        prepared_reviewer = self.reviewer_fixture()
        reviewer = self.build_reviewer(runtime=self.runtime(), prepared_fixture=prepared_reviewer)
        self.assertEqual(reviewer["spawn"]["fork_turns"], "none")
        self.assertEqual(reviewer["spawn"]["task_name"], "reviewer_t01_1")
        self.assertEqual(reviewer["spawn"]["agent_type"], "reviewer-agent")
        self.assertEqual(
            self.build_reviewer(
                review_number=2, runtime=self.runtime(), prepared_fixture=prepared_reviewer
            )["spawn"]["task_name"],
            "reviewer_t01_2",
        )
        self.assertEqual(
            context.spawn_failure("HANDOFF_REQUIRED"), "AGENT_HANDOFF_REQUIRED_BUT_UNAVAILABLE"
        )
        self.assertEqual(
            context.spawn_failure("NEW_CONTEXT_REQUIRED"), "AGENT_HANDOFF_REQUIRED_BUT_UNAVAILABLE"
        )

    def test_decision_role_resolves_to_its_exact_runtime_type(self):
        _, _, base, contract = self.task_checkout()
        handoff = context.prepare_handoff(
            self.agent(role="decision-agent"),
            {
                "GOAL": "decide technical replan",
                "PROJECT_ROOT": contract["PROJECT_ROOT"],
                "BASE_SHA": base,
            },
            {},
            "decision-2",
        )
        self.assertEqual(handoff["spawn"]["agent_type"], "decision-agent")
        self.assertNotEqual(handoff["spawn"]["agent_type"], "default")
        self.assertEqual(handoff["route"]["ROLE"], "planner")
        self.assertIn("issued_route", handoff)

    def test_decision_replan_handoff_requires_and_issues_planner_identity(self):
        runtime = self.runtime()
        selected = cost.route("planner", decision_complexity="HARD", runtime=runtime)
        agent = self.agent(role="decision-agent", route=selected, runtime=runtime)
        with self.assertRaisesRegex(ValueError, "DISPATCH_ROUTE_REQUIRED"):
            context.prepare_handoff(agent, {"GOAL": "decide technical replan"}, {}, "decision-2")
        _, _, base, task_contract = self.task_checkout()
        handoff = context.prepare_handoff(
            agent,
            {
                "GOAL": "decide technical replan",
                "PROJECT_ROOT": task_contract["PROJECT_ROOT"],
                "BASE_SHA": base,
            },
            {},
            "decision-2",
        )
        self.assertIn("issued_route", handoff)
        self.assertEqual(handoff["route"]["ROLE"], "planner")

    def test_reviewer_replacement_requires_exact_target_and_returns_receiptable_route(self):
        runtime = self.runtime()
        selected = cost.route("reviewer", runtime=runtime)
        agent = self.agent(role="reviewer", task_id="T01", route=selected, runtime=runtime)
        with self.assertRaisesRegex(ValueError, "DISPATCH_ROUTE_REQUIRED"):
            context.prepare_handoff(agent, {"GOAL": "review candidate"}, {}, "reviewer-2")
        fixture, review_target, evidence = self.reviewer_fixture()
        base = review_target["BASE_SHA"]
        contract = {
            "GOAL": "review candidate",
            "PROJECT_ROOT": str(fixture.host),
            "PHASE_ID": "P1",
            "BASE_SHA": base,
            "TARGET_SHA": review_target["TARGET_SHA"],
        }
        contract.update({"REVIEW_CONTRACT": review_target, "VERIFICATION_EVIDENCE": evidence})
        prepared = context.prepare_handoff(agent, contract, {}, "reviewer-2")
        self.assertEqual(prepared["route"]["ROLE"], "reviewer")
        self.assertEqual(prepared["spawn"]["model"], "strong-test")
        self.assertEqual(prepared["dispatch"]["SPAWN_MODE"], "typed")
        self.assertIn("issued_route", prepared)
        self.assertEqual(prepared["spawn"]["fork_turns"], "none")
        self.assertIn('"PHASE_ID": "P1"', prepared["spawn"]["message"])
        self.assertIn('"BASE_SHA": "' + base + '"', prepared["spawn"]["message"])
        self.assertIn(
            '"TARGET_SHA": "' + review_target["TARGET_SHA"] + '"', prepared["spawn"]["message"]
        )
        self.assertIn("PHASE_REVIEW_CONTRACT:", prepared["spawn"]["message"])
        self.assertIn("VERIFICATION_EVIDENCE:", prepared["spawn"]["message"])
        receipt = devcrew_dispatch.record_spawn(
            fixture.host,
            {
                "STATUS": "SPAWN_SUCCESS",
                "REQUESTED_ROLE": "reviewer",
                "TASK_ID": "T01",
                "PHASE_ID": "P1",
                "BASE_SHA": base,
                "TARGET_SHA": review_target["TARGET_SHA"],
                "AGENT_ID": "review-agent",
                "FORK_TURNS": "none",
                "SPAWN_MODEL": prepared["spawn"]["model"],
                "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
                "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
                "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                "SPAWN_PAYLOAD": prepared["spawn"],
                "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
            },
        )
        self.assertTrue(
            devcrew_dispatch.verify_spawn(
                fixture.host,
                receipt["SPAWN_RECEIPT_ID"],
                task_id="T01",
                role="reviewer",
                phase_id="P1",
                base_sha=base,
                target_sha=review_target["TARGET_SHA"],
                agent_id="review-agent",
            )
        )

    def test_explicit_route_reaches_reviewer_and_replacement_spawn(self):
        reviewer = self.build_reviewer(risk="high", runtime=self.runtime())
        self.assertEqual(
            (reviewer["spawn"]["model"], reviewer["spawn"]["reasoning_effort"]),
            ("strong-test", "high"),
        )
        executor_route = cost.route(
            "executor", risk="high", implementation_complexity="COMPLEX", runtime=self.runtime()
        )
        handoff = context.prepare_handoff(
            self.agent(),
            self.replacement_contract("finish"),
            {},
            "executor-2",
            selection=executor_route,
        )
        self.assertEqual(
            (handoff["spawn"]["model"], handoff["spawn"]["reasoning_effort"]),
            ("bounded-test", "medium"),
        )
        self.assertFalse(handoff["route"]["ROUTE_APPLIED"])
        self.assertEqual(handoff["route"]["ROLE"], "executor")
        route = cost.route("reviewer", runtime=self.runtime())
        self.assertFalse(cost.confirm_route(route)["ROUTE_APPLIED"])
        self.assertEqual(
            cost.confirm_route(route, {"model": "bounded-test", "reasoning_effort": "low"})[
                "CONFIRMATION_STATUS"
            ],
            "MISMATCH",
        )
        self.assertEqual(
            cost.confirm_route(route, {"model": "older-strong", "reasoning_effort": "medium"})[
                "CONFIRMATION_STATUS"
            ],
            "MISMATCH",
        )
        self.assertTrue(
            cost.confirm_route(route, {"model": "strong-test", "reasoning_effort": "medium"})[
                "ROUTE_APPLIED"
            ]
        )

    def test_initial_executor_dispatch_is_bounded_medium_and_stale_strong_route_is_rejected(self):
        runtime = self.runtime()
        selected = cost.route(
            "executor", risk="high", implementation_complexity="COMPLEX", runtime=runtime
        )
        exact = (
            "Own only app.py under the accepted plan. Return REPLAN_REQUEST on changed assumptions."
        )
        prepared = self.build_routed(
            "executor_task", "Implement the approved slice", selected, runtime, role_contract=exact
        )
        self.assertEqual(
            (prepared["spawn"]["model"], prepared["spawn"]["reasoning_effort"]),
            ("bounded-test", "medium"),
        )
        self.assertEqual(prepared["route"]["SELECTION_REASON"], "normal_implementation")
        for rule in (
            "LOOP_GUARD:",
            "STATUS: LOOP_DETECTED",
            "coordinator, which resolves",
            "ask the user only",
            "Preserve history across handoffs",
            "bounded next action",
        ):
            self.assertIn(rule, prepared["spawn"]["message"])

        old_strong_route = selected | {
            "MODEL_CLASS": "strong",
            "RESOLVED_MODEL": "strong-test",
            "REASONING_EFFORT": "high",
            "SELECTION_REASON": "concurrency_implementation",
            "model": "strong-test",
            "effort": "high",
            "reason": "concurrency_implementation",
        }
        with self.assertRaisesRegex(ValueError, "stale or conflicts"):
            self.build_routed(
                "executor_task",
                "Implement the approved slice",
                old_strong_route,
                runtime,
                role_contract=exact,
            )

    def test_requirement_envelope_is_issued_and_revalidated_against_current_registry(self):
        project, worktree, base, contract = self.task_checkout("T_REQUIREMENTS")
        paths = {
            "context": "docs/context.md",
            "tasks": ".codex/tasks",
            "sessions": ".codex/sessions",
            "memory": "memory",
            "checks": ".codex/checks",
        }
        state = project / ".codex" / "devcrew.json"
        state.parent.mkdir(exist_ok=True)
        state.write_text(json.dumps({"schema_version": 1, "paths": paths}), encoding="utf-8")
        requirement = {
            "id": "req-before-edit",
            "revision": 1,
            "selectors": {
                "paths": ["tracked.txt"],
                "roles": ["executor", "reviewer"],
                "task_classes": ["IMPLEMENTATION"],
            },
            "trigger": "The task changes tracked state.",
            "action": "Preserve the prior state until replacement succeeds.",
            "check": "Confirm the failure path preserves prior contents.",
            "origin": {"kind": "user_requirement", "reference": "test"},
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "No existing rule covers it.",
                "evidence": [{"kind": "user_requirement", "reference": "test"}],
            },
        }
        devcrew_requirements.record(
            project, paths["memory"], requirement, expected_registry_revision=0
        )
        canonical_line = "base"
        canonical_raw = (worktree / "tracked.txt").read_bytes()
        canonical = {
            "id": "req-existing-source",
            "revision": 1,
            "selectors": {
                "paths": ["tracked.txt"],
                "roles": ["executor", "reviewer"],
                "task_classes": ["IMPLEMENTATION"],
            },
            "source": {
                "path": "tracked.txt",
                "fragment": canonical_line,
                "fragment_sha256": hashlib.sha256(canonical_line.encode()).hexdigest(),
                "sha256": hashlib.sha256(canonical_raw).hexdigest(),
            },
            "origin": {"kind": "project_rule", "reference": "tracked.txt"},
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "The existing task source owns this rule.",
                "evidence": [{"kind": "canonical_source", "reference": "tracked.txt"}],
            },
        }
        repo_root = pathlib.Path(__file__).resolve().parents[1]
        recorded_source = subprocess.run(
            [
                sys.executable,
                "-B",
                str(repo_root / "scripts/devcrew.py"),
                "requirements-record",
                "--project",
                str(project),
                "--task-id",
                "T_REQUIREMENTS",
                "--expected-registry-revision",
                "1",
                "--record",
                json.dumps(canonical),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(recorded_source.returncode, 0, recorded_source.stderr)
        runtime = self.runtime()
        selected = cost.route("executor", task_class="IMPLEMENTATION", runtime=runtime)
        prepared = context.routed_task_spawn(
            "executor",
            "Implement",
            selected,
            runtime,
            task_id="T_REQUIREMENTS",
            project_root=str(project),
            task_worktree_root=str(worktree),
            base_sha=base,
            owned_scope=contract["OWNED_SCOPE"],
            acceptance=contract["ACCEPTANCE"],
            verification=contract["VERIFICATION"],
            receipt=contract["TASK_WORKTREE_RECEIPT"],
            role_contract="Exact executor contract",
        )
        message = prepared["spawn"]["message"]
        envelope = _requirements_envelope(message)
        self.assertIn("req-before-edit", {row["id"] for row in envelope["requirements"]})
        self.assertGreater(envelope["context_bytes"], 0)
        serialized_envelope = next(
            line.partition(": ")[2]
            for line in message.splitlines()
            if line.startswith("ENGINEERING_REQUIREMENTS: ")
        )
        self.assertEqual(envelope["context_bytes"], len(serialized_envelope.encode("utf-8")))
        source_entry = next(
            row for row in envelope["requirements"] if row["id"] == "req-existing-source"
        )
        self.assertIn("source", source_entry)
        self.assertNotIn("action", source_entry)
        issued = project / ".codex" / "sessions" / "agent-dispatch-T_REQUIREMENTS.json"
        self.assertTrue(issued.is_file())
        spawn_payload = prepared["spawn"]
        report = {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "executor",
            "TASK_ID": "T_REQUIREMENTS",
            "PHASE_ID": "P1",
            "BASE_SHA": base,
            "WRITER_CYCLE": 0,
            "REJECTED_CANDIDATE_SHA": None,
            "AGENT_ID": "executor-T_REQUIREMENTS",
            "SPAWN_MODEL": spawn_payload["model"],
            "SPAWN_REASONING_EFFORT": spawn_payload["reasoning_effort"],
            "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
            "FORK_TURNS": "none",
            "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
            "SPAWN_PAYLOAD": spawn_payload,
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        }
        with self.assertRaisesRegex(ValueError, "ENGINEERING_REQUIREMENTS_REQUIRED"):
            context._validate_dispatch_authority(
                project,
                "T_REQUIREMENTS",
                "executor",
                {
                    "TASK_ID": "T_REQUIREMENTS",
                    "PHASE_ID": "P1",
                    "BASE_SHA": base,
                    "WRITER_CYCLE": 0,
                    "REJECTED_CANDIDATE_SHA": None,
                },
                {
                    **spawn_payload,
                    "message": "\n".join(
                        line
                        for line in spawn_payload["message"].splitlines()
                        if not line.startswith("ENGINEERING_REQUIREMENTS: ")
                    ),
                },
            )
        receipt = devcrew_dispatch.record_spawn(project, report)
        observed = devcrew_dispatch.inspect_issued(
            project,
            "T_REQUIREMENTS",
            route_id=prepared["route"]["ROUTE_ID"],
            spawn_receipt_id=receipt["SPAWN_RECEIPT_ID"],
        )
        self.assertEqual(
            {row["id"] for row in observed["delivered_requirements"]},
            {"req-before-edit", "req-existing-source"},
        )
        self.assertEqual(len(observed["delivered_requirements"]), 2)
        self.assertEqual(observed["spawn_receipt"]["SPAWN_RECEIPT_ID"], receipt["SPAWN_RECEIPT_ID"])
        incident_path = project / "incident.json"
        incident_path.write_text(
            json.dumps(
                {
                    "task_id": "T_REQUIREMENTS",
                    "spawn_receipt_id": receipt["SPAWN_RECEIPT_ID"],
                    "role": "executor",
                    "task_class": "IMPLEMENTATION",
                    "paths": ["tracked.txt"],
                    "check_evidence": [],
                    "agent_evidence": [],
                }
            ),
            encoding="utf-8",
        )
        diagnosis = subprocess.run(
            [
                sys.executable,
                "-B",
                str(repo_root / "scripts/devcrew.py"),
                "requirements-diagnose",
                "--project",
                str(project),
                "--incident",
                str(incident_path),
            ],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(diagnosis.returncode, 0, diagnosis.stderr)
        diagnosed = json.loads(diagnosis.stdout)
        self.assertEqual(
            diagnosed["observed_delivery"]["spawn_receipt"]["SPAWN_RECEIPT_ID"],
            receipt["SPAWN_RECEIPT_ID"],
        )
        self.assertEqual(diagnosed["diagnosis"], "undetermined")
        requirement2 = dict(
            requirement,
            id="req-stale-after-issue",
            trigger="A later task changes a different state format.",
        )
        devcrew_requirements.record(
            project, paths["memory"], requirement2, expected_registry_revision=2
        )
        binding, _ = context._dispatch_worktree_binding(project, "T_REQUIREMENTS")
        with self.assertRaisesRegex(ValueError, "ENGINEERING_REQUIREMENTS_STALE"):
            context._validate_dispatch_authority(
                project,
                "T_REQUIREMENTS",
                "executor",
                {
                    "TASK_ID": "T_REQUIREMENTS",
                    "PHASE_ID": "P1",
                    "BASE_SHA": base,
                    "WRITER_CYCLE": 0,
                    "REJECTED_CANDIDATE_SHA": None,
                },
                spawn_payload,
                binding,
                already_issued=True,
            )

    def test_routed_spawn_refuses_runnable_payload_without_durable_identity(self):
        runtime = self.runtime()
        selected = cost.route("executor", runtime=runtime)
        with self.assertRaisesRegex(ValueError, "DISPATCH_ROUTE_REQUIRED"):
            context.routed_spawn(
                "executor_task",
                "Implement",
                selected,
                runtime,
                role_contract="Exact executor contract",
            )

    def test_caller_cannot_supply_reserved_requirements_marker_with_empty_registry(self):
        project, _worktree, base, _contract = self.task_checkout("T_EMPTY_REQ_MARKER")
        runtime = self.runtime()
        selection = cost.route("planner", task_class="ANALYSIS", runtime=runtime)
        with self.assertRaisesRegex(ValueError, "DISPATCH_SERVICE_MARKER_DUPLICATE"):
            context.routed_spawn(
                "planner_T_EMPTY_REQ_MARKER",
                "ENGINEERING_REQUIREMENTS: {}",
                selection,
                runtime,
                project_root=project,
                task_id="T_EMPTY_REQ_MARKER",
                identity={"TASK_ID": "T_EMPTY_REQ_MARKER", "BASE_SHA": base},
                requirements_scope={"paths": ["tracked.txt"], "task_class": "ANALYSIS"},
            )

    def test_nonempty_requirements_reach_reviewer_and_executor_replacement(self):
        fixture, review_contract, evidence = self.reviewer_fixture()
        requirement = {
            "id": "req-reviewer",
            "revision": 1,
            "selectors": {
                "paths": ["one.txt"],
                "roles": ["reviewer"],
                "task_classes": ["IMPLEMENTATION"],
            },
            "trigger": "A reviewer inspects the current phase.",
            "action": "Check the declared phase requirement before reporting.",
            "check": "Match the review against the persisted phase scope.",
            "origin": {"kind": "user_requirement", "reference": "test"},
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "No existing reviewer instruction covers this check.",
                "evidence": [{"kind": "user_requirement", "reference": "test"}],
            },
        }
        devcrew_requirements.record(
            fixture.host, "memory", requirement, expected_registry_revision=0
        )
        runtime = self.runtime()
        selection = cost.route("reviewer", task_class="IMPLEMENTATION", runtime=runtime)
        identity = {
            key: review_contract[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        prepared = context.routed_spawn(
            "reviewer_T01_1",
            "Review the candidate",
            selection,
            runtime,
            project_root=fixture.host,
            task_id="T01",
            identity=identity,
            review_contract=review_contract,
            verification_evidence=evidence,
        )
        message = prepared["spawn"]["message"]
        reviewer_envelope = _requirements_envelope(message)
        self.assertEqual({row["id"] for row in reviewer_envelope["requirements"]}, {"req-reviewer"})
        caller_envelope = devcrew_requirements.context(
            fixture.host, "memory", review_contract["OWNED_SCOPE"], "reviewer", "ANALYSIS"
        )
        caller_envelope.update(
            role="reviewer", task_class="ANALYSIS", scope_paths=review_contract["OWNED_SCOPE"]
        )
        injected_message = (
            "ENGINEERING_REQUIREMENTS: "
            + devcrew_requirements.serialized_context(caller_envelope)[0]
        )
        with self.assertRaisesRegex(ValueError, "DISPATCH_SERVICE_MARKER_DUPLICATE"):
            context.routed_spawn(
                "reviewer_T01_1",
                injected_message,
                selection,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity=identity,
                review_contract=review_contract,
                verification_evidence=evidence,
            )
        binding, _receipt = context._dispatch_worktree_binding(fixture.host, "T01")
        forged = devcrew_requirements.context(
            fixture.host, "memory", review_contract["OWNED_SCOPE"], "reviewer", "ANALYSIS"
        )
        forged.update(
            role="reviewer", task_class="ANALYSIS", scope_paths=review_contract["OWNED_SCOPE"]
        )
        forged_message = "\n".join(
            line
            for line in message.splitlines()
            if not line.startswith("ENGINEERING_REQUIREMENTS: ")
        )
        forged_message += (
            "\nENGINEERING_REQUIREMENTS: " + devcrew_requirements.serialized_context(forged)[0]
        )
        with self.assertRaisesRegex(ValueError, "ENGINEERING_REQUIREMENTS_MISMATCH"):
            context._validate_dispatch_authority(
                fixture.host,
                "T01",
                "reviewer",
                identity,
                {**prepared["spawn"], "message": forged_message},
                binding,
                already_issued=True,
            )
        with self.assertRaisesRegex(ValueError, "ENGINEERING_REQUIREMENTS_REQUIRED"):
            context._validate_dispatch_authority(
                fixture.host,
                "T01",
                "reviewer",
                identity,
                {
                    **prepared["spawn"],
                    "message": "\n".join(
                        line
                        for line in message.splitlines()
                        if not line.startswith("ENGINEERING_REQUIREMENTS: ")
                    ),
                },
                binding,
                already_issued=True,
            )
        report = {
            "STATUS": "SPAWN_SUCCESS",
            "REQUESTED_ROLE": "reviewer",
            "TASK_ID": "T01",
            "PHASE_ID": "P1",
            "BASE_SHA": review_contract["BASE_SHA"],
            "TARGET_SHA": review_contract["TARGET_SHA"],
            "AGENT_ID": "reviewer-T01",
            "SPAWN_MODEL": prepared["spawn"]["model"],
            "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
            "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
            "FORK_TURNS": "none",
            "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
            "SPAWN_PAYLOAD": prepared["spawn"],
            "SPAWN_PAYLOAD_SHA256": prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        }
        spawn_receipt = devcrew_dispatch.record_spawn(fixture.host, report)
        delivered = devcrew_dispatch.inspect_issued(
            fixture.host,
            "T01",
            route_id=prepared["route"]["ROUTE_ID"],
            spawn_receipt_id=spawn_receipt["SPAWN_RECEIPT_ID"],
        )
        self.assertEqual(
            delivered["delivered_requirements"], [{"id": "req-reviewer", "revision": 1}]
        )

        project, worktree, base, task_contract = self.task_checkout("T_REQ_REPLACEMENT")
        replacement_requirement = dict(
            requirement,
            id="req-replacement",
            selectors={
                "paths": ["tracked.txt"],
                "roles": ["executor"],
                "task_classes": ["IMPLEMENTATION"],
            },
        )
        devcrew_requirements.record(
            project, "memory", replacement_requirement, expected_registry_revision=0
        )
        runtime = self.runtime()
        route = cost.route("executor", task_class="IMPLEMENTATION", runtime=runtime)
        agent = self.agent(task_id="T_REQ_REPLACEMENT", runtime=runtime, route=route)
        handoff = context.prepare_handoff(
            agent,
            {"GOAL": "Continue the bounded task", **task_contract},
            {},
            "executor-requirements-replacement",
            selection=route,
        )
        replacement_envelope = _requirements_envelope(handoff["spawn"]["message"])
        self.assertEqual(
            {row["id"] for row in replacement_envelope["requirements"]}, {"req-replacement"}
        )

    def test_unissued_builder_mode_is_not_exposed_on_routed_spawn(self):
        runtime = self.runtime()
        selected = cost.route("executor", runtime=runtime)
        with self.assertRaisesRegex(TypeError, "_defer_issue"):
            context.routed_spawn(
                "executor_task",
                "Implement",
                selected,
                runtime,
                project_root="/host",
                task_id="T04",
                identity={
                    "TASK_ID": "T04",
                    "PHASE_ID": "P1",
                    "BASE_SHA": "a" * 40,
                    "WRITER_CYCLE": 0,
                    "REJECTED_CANDIDATE_SHA": None,
                },
                _defer_issue=True,
            )

    def test_reviewer_builder_requires_formal_review_contract_and_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError, "REVIEW_CONTRACT_REQUIRED"):
                context.reviewer_spawn(
                    "T04",
                    runtime=self.runtime(),
                    project_root=root,
                    phase_id="P1",
                    base_sha="a" * 40,
                    target_sha="b" * 40,
                )

    def test_public_reviewer_route_cannot_issue_without_review_contract(self):
        runtime = self.runtime()
        selected = cost.route("reviewer", runtime=runtime)
        fixture, review_contract, evidence = self.reviewer_fixture()
        identity = {
            key: review_contract[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        with self.assertRaisesRegex(ValueError, "REVIEW_CONTRACT_REQUIRED"):
            context.routed_spawn(
                "reviewer_T01_1",
                "arbitrary reviewer text",
                selected,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity=identity,
            )
        prepared = context.routed_spawn(
            "reviewer_T01_1",
            "Review this candidate",
            selected,
            runtime,
            project_root=fixture.host,
            task_id="T01",
            identity=identity,
            review_contract=review_contract,
            verification_evidence=evidence,
        )
        message = prepared["spawn"]["message"]
        self.assertEqual(prepared["spawn"]["fork_turns"], "none")
        self.assertIn("PHASE_REVIEW_CONTRACT:", message)
        self.assertIn('"PHASE_ID": "P1"', message)
        self.assertIn('"BASE_SHA": "' + review_contract["BASE_SHA"] + '"', message)
        self.assertIn('"TARGET_SHA": "' + review_contract["TARGET_SHA"] + '"', message)
        self.assertIn("VERIFICATION_EVIDENCE:", message)
        self.assertIn(review_contract["GOAL"], message)
        self.assertIn(review_contract["MAIN_SCENARIO"], message)
        self.assertEqual(message.count("PHASE_REVIEW_CONTRACT:"), 1)

    def test_public_reviewer_route_derives_then_revalidates_under_lock(self):
        fixture, review_contract, evidence = self.reviewer_fixture()
        runtime = self.runtime()
        selection = cost.route("reviewer", task_class="IMPLEMENTATION", runtime=runtime)
        identity = {
            key: review_contract[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        validate = context._validate_reviewer_issuance
        receipt_path = devcrew_capacity.dispatch_receipt_path
        with (
            mock.patch.object(context, "_validate_reviewer_issuance", wraps=validate) as checks,
            mock.patch.object(
                devcrew_capacity, "dispatch_receipt_path", wraps=receipt_path
            ) as paths,
        ):
            prepared = context.routed_spawn(
                "reviewer_T01_1",
                "Review the candidate",
                selection,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity=identity,
                review_contract=review_contract,
                verification_evidence=evidence,
            )
        self.assertEqual(checks.call_count, 2)  # canonical derivation and fresh locked authority
        self.assertTrue(paths.call_args_list)
        self.assertTrue(
            all(call.kwargs.get("create", False) is False for call in paths.call_args_list)
        )
        message = prepared["spawn"]["message"]
        self.assertEqual(message.count("PHASE_REVIEW_CONTRACT:"), 1)
        self.assertEqual(message.count("VERIFICATION_EVIDENCE:"), 1)
        self.assertEqual(prepared["spawn"]["fork_turns"], "none")

    def test_public_reviewer_route_rejects_stale_caller_selection(self):
        fixture, review_contract, evidence = self.reviewer_fixture()
        runtime = self.runtime()
        selection = cost.route("reviewer", task_class="IMPLEMENTATION", runtime=runtime)
        stale_selection = {**selection, "RESOLVED_MODEL": "bounded-test"}
        identity = {
            key: review_contract[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        with self.assertRaisesRegex(ValueError, "route is stale or conflicts with policy"):
            context.routed_spawn(
                "reviewer_T01_1",
                "Review the candidate",
                stale_selection,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity=identity,
                review_contract=review_contract,
                verification_evidence=evidence,
            )

    def test_reviewer_seed_lease_isolates_overlapping_fixtures_and_contracts(self):
        first = AgentContext(methodName=self.id().split(".")[-1])
        second = AgentContext(methodName=self.id().split(".")[-1])
        third = AgentContext(methodName=self.id().split(".")[-1])
        first_cleaned = False
        try:
            fixture1, target1, _evidence1 = first.reviewer_fixture()
            original_head = fixture1.git(fixture1.task, "rev-parse", "HEAD")
            state_path = devcrew_worktree.location(fixture1.host, "T01")[-1]
            original_receipt = state_path.read_bytes()
            original_file = (fixture1.task / "one.txt").read_bytes()
            target1["ACCEPTANCE"].append("mutated caller copy")
            (fixture1.task / "one.txt").write_text("first fixture mutation\n", encoding="utf-8")

            fixture2, target2, _evidence2 = second.reviewer_fixture()
            self.assertNotEqual(fixture1.temp.name, fixture2.temp.name)
            self.assertNotIn("mutated caller copy", target2["ACCEPTANCE"])
            self.assertEqual(
                (fixture1.task / "one.txt").read_text(encoding="utf-8"), "first fixture mutation\n"
            )
            self.assertEqual(fixture1.git(fixture1.task, "rev-parse", "HEAD"), original_head)
            self.assertEqual(state_path.read_bytes(), original_receipt)

            first.doCleanups()
            first_cleaned = True
            self.assertEqual((fixture1.task / "one.txt").read_bytes(), original_file)
            self.assertEqual(fixture1.git(fixture1.task, "rev-parse", "HEAD"), original_head)
            self.assertEqual(state_path.read_bytes(), original_receipt)

            fixture3, target3, _evidence3 = third.reviewer_fixture()
            self.assertEqual(fixture3.temp.name, fixture1.temp.name)
            self.assertNotIn("mutated caller copy", target3["ACCEPTANCE"])
            self.assertEqual((fixture3.task / "one.txt").read_bytes(), original_file)
        finally:
            if not first_cleaned:
                first.doCleanups()
            second.doCleanups()
            third.doCleanups()

    def test_reviewer_phase_error_precedes_invalid_runtime_during_derivation(self):
        fixture, review_contract, evidence = self.reviewer_fixture()
        manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        manifest["phases"][0]["status"] = "VERIFYING"
        devcrew_phase.save_manifest(fixture.path, manifest, fixture.receipt)
        runtime = self.runtime(generic_available=False, explicit_model_effort=False)
        selection = cost.route("reviewer", task_class="IMPLEMENTATION", runtime=self.runtime())
        identity = {
            key: review_contract[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        with self.assertRaisesRegex(ValueError, "PHASE_REVIEW_START_REQUIRED"):
            context.routed_spawn(
                "reviewer_T01_1",
                "Review the candidate",
                selection,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity=identity,
                review_contract=review_contract,
                verification_evidence=evidence,
            )

    def test_reviewer_request_invalidated_after_preparation_creates_no_dispatch_state(self):
        fixture, review_contract, evidence = self.reviewer_fixture()
        runtime = self.runtime()
        selection = cost.route("reviewer", task_class="IMPLEMENTATION", runtime=runtime)
        identity = {
            key: review_contract[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        }
        route_options = context._route_options
        receipt_path = devcrew_capacity.dispatch_receipt_path

        def invalidate_after_preparation(route):
            # This runs after the former public preflight, exposing whether
            # store-path preparation creates state before canonical derivation.
            manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
            manifest["phases"][0]["status"] = "VERIFYING"
            devcrew_phase.save_manifest(fixture.path, manifest, fixture.receipt)
            return route_options(route)

        with (
            mock.patch.object(context, "_route_options", side_effect=invalidate_after_preparation),
            mock.patch.object(
                devcrew_capacity, "dispatch_receipt_path", wraps=receipt_path
            ) as paths,
        ):
            with self.assertRaisesRegex(ValueError, "PHASE_REVIEW_START_REQUIRED"):
                context.routed_spawn(
                    "reviewer_T01_1",
                    "Review the candidate",
                    selection,
                    runtime,
                    project_root=fixture.host,
                    task_id="T01",
                    identity=identity,
                    review_contract=review_contract,
                    verification_evidence=evidence,
                )
        self.assertTrue(paths.call_args_list)
        self.assertTrue(
            all(call.kwargs.get("create", False) is False for call in paths.call_args_list)
        )

    def test_reviewer_issuance_cross_checks_every_persisted_contract_field(self):
        fixture, target, evidence = self.reviewer_fixture()
        runtime = self.runtime()
        selection = cost.route("reviewer", runtime=runtime)
        identity = {key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        for field in sorted(context.REVIEW_CONTRACT_FIELDS):
            altered = dict(target)
            value = altered[field]
            altered[field] = value + " altered" if isinstance(value, str) else value + ["altered"]
            with self.subTest(field=field), self.assertRaises(ValueError):
                context.routed_spawn(
                    "reviewer_T01_1",
                    "Review this candidate",
                    selection,
                    runtime,
                    project_root=fixture.host,
                    task_id="T01",
                    identity=identity,
                    review_contract=altered,
                    verification_evidence=evidence,
                )
        with self.assertRaisesRegex(ValueError, "PHASE_REVIEW_EVIDENCE_MISMATCH"):
            context.routed_spawn(
                "reviewer_T01_1",
                "Review this candidate",
                selection,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity=identity,
                review_contract=target,
                verification_evidence=evidence + ["substituted"],
            )

    def test_reviewer_route_requires_persisted_review_start(self):
        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.manifest["phases"][0]["main_scenario"] = (
            "A request changes one.txt and verification observes the result."
        )
        devcrew_phase.save_manifest(fixture.path, fixture.manifest, fixture.receipt)
        fixture.make_candidate()
        fixture.receipt = __import__("devcrew_worktree").resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(fixture.path, fixture.receipt)
        target = devcrew_phase.review_target(fixture.manifest, "P1")
        evidence = list(fixture.manifest["phases"][0]["candidate_commit"]["evidence"])
        runtime = self.runtime()
        selection = cost.route("reviewer", runtime=runtime)
        with self.assertRaisesRegex(ValueError, "PHASE_REVIEW_START_REQUIRED"):
            context.routed_spawn(
                "reviewer_T01_1",
                "Review this candidate",
                selection,
                runtime,
                project_root=fixture.host,
                task_id="T01",
                identity={
                    key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
                },
                review_contract=target,
                verification_evidence=evidence,
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
        prepared = context.routed_spawn(
            "reviewer_T01_1",
            "Review this candidate",
            selection,
            runtime,
            project_root=fixture.host,
            task_id="T01",
            identity={
                key: target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
            },
            review_contract=target,
            verification_evidence=evidence,
        )
        self.assertIn("issued_route", prepared)
        self.assertEqual(prepared["spawn"]["fork_turns"], "none")

    def test_role_change_requires_fresh_matching_dispatch(self):
        runtime = self.runtime()
        planner_route = cost.route("planner", decision_complexity="HARD", runtime=runtime)
        with self.assertRaisesRegex(ValueError, "route role does not match"):
            context.prepare_handoff(
                self.agent(role="scout", route=planner_route),
                {"GOAL": "continue"},
                {},
                "executor-2",
            )

    def test_reviewer_cycle_uses_selected_strong_review_route(self):
        runtime = self.runtime()
        selected = cost.route("reviewer", risk="high", runtime=runtime)
        spawn = self.build_reviewer(runtime=runtime, selection=selected)
        self.assertEqual(
            (spawn["spawn"]["model"], spawn["spawn"]["reasoning_effort"]), ("strong-test", "high")
        )
        self.assertIn('"ROLE": "reviewer"', spawn["spawn"]["message"])

    def test_pinned_local_executor_uses_generic_with_exact_contract(self):
        runtime = self.runtime(
            roles={
                "executor-agent": {
                    "available": True,
                    "supports_overrides": False,
                    "model": "strong-test",
                    "reasoning_effort": "high",
                }
            }
        )
        exact = "Own only app.py; implement approved brief; return REPLAN_REQUEST on changed assumptions. Do not commit."
        handoff = context.prepare_handoff(
            self.agent(runtime=runtime, role_contract=exact),
            self.replacement_contract("finish"),
            {},
            "executor-2",
        )
        spawn = handoff["spawn"]
        self.assertEqual(spawn["agent_type"], "default")
        self.assertEqual((spawn["model"], spawn["reasoning_effort"]), ("bounded-test", "medium"))
        self.assertIn(exact, spawn["message"])
        self.assertEqual(handoff["dispatch"]["ROLE_FALLBACK"], "generic")
        with self.assertRaisesRegex(ValueError, "role contract"):
            context.prepare_handoff(
                self.agent(runtime=runtime), self.replacement_contract("finish"), {}, "executor-2"
            )

    def test_executor_replacement_requires_and_preserves_registered_worktree_contract(self):
        with self.assertRaisesRegex(ValueError, "worktree"):
            context.prepare_handoff(self.agent(), {"GOAL": "continue"}, {}, "executor-2")
        _, worktree, _, task_contract = self.task_checkout()
        complete = {
            "GOAL": "continue",
            "PROJECT_ROOT": task_contract["PROJECT_ROOT"],
            "TASK_WORKTREE_ROOT": task_contract["TASK_WORKTREE_ROOT"],
            "TASK_WORKTREE_RECEIPT": task_contract["TASK_WORKTREE_RECEIPT"],
            "BASE_SHA": task_contract["BASE_SHA"],
            "OWNED_SCOPE": task_contract["OWNED_SCOPE"],
            "ACCEPTANCE": task_contract["ACCEPTANCE"],
            "VERIFICATION": task_contract["VERIFICATION"],
        }
        prepared = context.prepare_handoff(
            self.agent(), complete, {"DONE": "step one"}, "executor-2"
        )
        self.assertIn(str(worktree), prepared["spawn"]["message"])
        self.assertIn('"TASK_WORKTREE_ROOT":', prepared["spawn"]["message"])
        self.assertEqual(prepared["spawn"]["model"], "bounded-test")
        self.assertEqual(prepared["spawn"]["reasoning_effort"], "medium")

    def test_task_executor_payload_rejects_unregistered_paths_and_wrong_repository(self):
        project, worktree, base, contract = self.task_checkout("T10")
        runtime = self.runtime()
        selection = cost.route("executor", runtime=runtime)
        arguments = {
            "task_id": "T10",
            "project_root": str(project),
            "task_worktree_root": str(worktree),
            "base_sha": base,
            "receipt": contract["TASK_WORKTREE_RECEIPT"],
            "owned_scope": contract["OWNED_SCOPE"],
            "acceptance": contract["ACCEPTANCE"],
            "verification": contract["VERIFICATION"],
            "role_contract": "Exact executor contract",
        }
        context.routed_task_spawn("executor", "Implement", selection, runtime, **arguments)
        bad_paths = [
            str(pathlib.Path(tempfile.gettempdir()) / "unregistered-task-root"),
            str(project),
        ]
        for bad_path in bad_paths:
            with (
                self.subTest(path=bad_path),
                self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"),
            ):
                context.routed_task_spawn(
                    "executor",
                    "Implement",
                    selection,
                    runtime,
                    task_id="T10",
                    project_root=str(project),
                    task_worktree_root=bad_path,
                    base_sha=base,
                    owned_scope=["tracked.txt"],
                    acceptance=["works"],
                    verification=["tests"],
                    role_contract="Exact executor contract",
                )

        _, other_worktree, _, _ = self.task_checkout("T11")
        with (
            self.subTest(path=str(other_worktree)),
            self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"),
        ):
            context.routed_task_spawn(
                "executor",
                "Implement",
                selection,
                runtime,
                task_id="T10",
                project_root=str(project),
                task_worktree_root=str(other_worktree),
                base_sha=base,
                owned_scope=["tracked.txt"],
                acceptance=["works"],
                verification=["tests"],
                role_contract="Exact executor contract",
            )

        _, wrong_branch_worktree, wrong_base, _ = self.task_checkout("T12", "codex/tasks/OTHER")
        with (
            self.subTest(path=str(wrong_branch_worktree)),
            self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"),
        ):
            context.routed_task_spawn(
                "executor",
                "Implement",
                selection,
                runtime,
                task_id="T12",
                project_root=str(project),
                task_worktree_root=str(wrong_branch_worktree),
                base_sha=wrong_base,
                owned_scope=["tracked.txt"],
                acceptance=["works"],
                verification=["tests"],
                role_contract="Exact executor contract",
            )

    def test_task_executor_payload_requires_fresh_lifecycle_receipt(self):
        project, worktree, base, contract = self.task_checkout("T16")
        runtime = self.runtime()
        selection = cost.route("executor", runtime=runtime)
        kwargs = dict(
            task_id="T16",
            project_root=str(project),
            task_worktree_root=str(worktree),
            base_sha=base,
            owned_scope=contract["OWNED_SCOPE"],
            acceptance=contract["ACCEPTANCE"],
            verification=contract["VERIFICATION"],
            role_contract="Exact executor contract",
        )
        receipt = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"),
                    "task-worktree",
                    "resolve",
                    "--project",
                    str(project),
                    "--task-id",
                    "T16",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertIn("repository_id", receipt)
        context.routed_task_spawn(
            "executor", "Implement", selection, runtime, receipt=receipt, **kwargs
        )
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"):
            context.routed_task_spawn("executor", "Implement", selection, runtime, **kwargs)
        subprocess.run(
            ["git", "-C", str(project), "worktree", "remove", str(worktree)],
            check=True,
            capture_output=True,
            text=True,
        )
        missing = subprocess.run(
            [
                sys.executable,
                str(pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"),
                "task-worktree",
                "resolve",
                "--project",
                str(project),
                "--task-id",
                "T16",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(json.loads(missing.stdout)["status"], "UNSAFE_MISSING_WORKTREE")

    def test_issued_executor_route_revalidates_without_changing_payload_then_consumes_once(self):
        project, worktree, _base, contract, prepared, report = self.executor_route(
            "T_ROUTE_READONLY"
        )
        issued_path = project / ".codex" / "sessions" / "agent-dispatch-T_ROUTE_READONLY.json"
        before = json.loads(issued_path.read_text())
        payload = prepared["spawn"]
        phase_contract = json.loads(
            payload["message"]
            .split("PHASE_EXECUTION_CONTRACT: ", 1)[1]
            .split("\nMODEL_ROUTE:", 1)[0]
        )
        self.assertEqual(phase_contract["GOAL"], "bounded phase goal")
        self.assertEqual(
            phase_contract["MAIN_SCENARIO"], "A request changes tracked.txt and returns the result."
        )
        self.assertNotEqual(
            phase_contract["TASK_WORKTREE_RECEIPT"]["receipt_nonce"],
            contract["TASK_WORKTREE_RECEIPT"]["receipt_nonce"],
        )
        (worktree / "after-issue.txt").write_text("dirty after issue\n")
        devcrew_dispatch.validate_issued(project, report)
        after_validation = json.loads(issued_path.read_text())
        self.assertEqual(after_validation, before)
        receipt = devcrew_dispatch.record_spawn(project, report)
        self.assertEqual(receipt["SPAWN_PAYLOAD_SHA256"], report["SPAWN_PAYLOAD_SHA256"])
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.validate_issued(project, report)
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.record_spawn(project, report)

    def test_issued_executor_route_fails_closed_when_registered_worktree_is_missing(self):
        project, worktree, _base, _contract, _prepared, report = self.executor_route(
            "T_ROUTE_MISSING"
        )
        __import__("shutil").rmtree(worktree)
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_UNSAFE_MISSING"):
            devcrew_dispatch.validate_issued(project, report)
        self.assertFalse(worktree.exists())

    def test_issued_executor_route_fails_closed_on_symlink_collision(self):
        project, worktree, _base, _contract, _prepared, report = self.executor_route(
            "T_ROUTE_SYMLINK"
        )
        import shutil

        shutil.rmtree(worktree)
        target = project.parent / "outside-worktree"
        target.mkdir()
        worktree.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_PATH_COLLISION"):
            devcrew_dispatch.validate_issued(project, report)

    def test_issued_executor_route_fails_closed_on_head_drift(self):
        project, worktree, _base, _contract, _prepared, report = self.executor_route(
            "T_ROUTE_BRANCH"
        )
        (worktree / "later.txt").write_text("move branch head\n")
        subprocess.run(
            ["git", "-C", str(worktree), "add", "later.txt"],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "advance task head",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_REPOSITORY_MISMATCH"):
            devcrew_dispatch.validate_issued(project, report)

    def test_issued_executor_route_rejects_identity_and_binding_tampering(self):
        project, _worktree, _base, _contract, _prepared, report = self.executor_route(
            "T_ROUTE_TAMPER"
        )
        altered = dict(report, PHASE_ID="P2")
        with self.assertRaisesRegex(ValueError, "INVALID_DISPATCH_ROUTE"):
            devcrew_dispatch.validate_issued(project, altered)
        issued_path = project / ".codex" / "sessions" / "agent-dispatch-T_ROUTE_TAMPER.json"
        store = json.loads(issued_path.read_text())
        store["issued_routes"][1]["WORKTREE_BINDING"]["branch"] = "codex/tasks/OTHER"
        issued_path.write_text(json.dumps(store))
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_BINDING_STALE"):
            devcrew_dispatch.validate_issued(project, report)

    def test_executor_capacity_retry_reuses_exact_issued_payload(self):
        project, worktree, _base, _contract, prepared, report = self.executor_route(
            "T_ROUTE_CAPACITY"
        )
        report.update(
            HOST_PROJECT_ROOT=str(project),
            TASK_WORKTREE_ROOT=str(worktree),
            CURRENT_STAGE="EXECUTOR_DISPATCH",
            ATTEMPT_NUMBER=1,
            RUNTIME_ERROR=devcrew_capacity.CAPACITY_ERROR,
            HOST_INITIATED=True,
            STATUS="SPAWN_FAILED",
        )
        with self.assertRaisesRegex(
            ValueError, "TASK_WORKTREE_ROOT differs from issued worktree binding"
        ):
            devcrew_dispatch.validate_issued(project, report | {"TASK_WORKTREE_ROOT": str(project)})
        before = json.loads(
            (project / ".codex" / "sessions" / "agent-dispatch-T_ROUTE_CAPACITY.json").read_text()
        )
        blocked = devcrew_capacity.report(str(project), report)
        resume = devcrew_capacity.resume(str(project), "T_ROUTE_CAPACITY", host_initiated=True)
        self.assertEqual(resume["SPAWN_PAYLOAD"], prepared["spawn"])
        self.assertEqual(
            resume["SPAWN_PAYLOAD_SHA256"], prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"]
        )
        successful = dict(
            report,
            STATUS="SPAWN_SUCCESS",
            RUNTIME_ERROR=None,
            ATTEMPT_NUMBER=2,
            BLOCKER_ID=blocked["BLOCKER_ID"],
        )
        result = devcrew_capacity.report(str(project), successful)
        self.assertEqual(
            result["SPAWN_RECEIPT"]["SPAWN_PAYLOAD_SHA256"],
            prepared["issued_route"]["SPAWN_PAYLOAD_SHA256"],
        )
        after = json.loads(
            (project / ".codex" / "sessions" / "agent-dispatch-T_ROUTE_CAPACITY.json").read_text()
        )
        self.assertEqual(
            after["issued_routes"][1]["SPAWN_PAYLOAD"], before["issued_routes"][1]["SPAWN_PAYLOAD"]
        )

    def test_executor_dispatch_is_bound_to_current_phase_and_handoff_requires_manifest(self):
        project, worktree, base, contract = self.task_checkout("T_PHASE")
        receipt = json.loads(
            subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"),
                    "task-worktree",
                    "resolve",
                    "--project",
                    str(project),
                    "--task-id",
                    "T_PHASE",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        multi = {
            "schema_version": 1,
            "task_id": "T_PHASE",
            "task_mode": "MULTI_PHASE",
            "semantic_contract": {"capabilities": ["charge", "refund"]},
            "worktree": {
                "repository_id": receipt["repository_id"],
                "task_worktree_root": receipt["task_worktree_root"],
                "branch": receipt["branch"],
                "base_sha": receipt["expected_head"],
            },
            "current_phase_id": "CHARGE",
            "phases": [
                {
                    "phase_id": "CHARGE",
                    "goal": "Implement charge path",
                    "depends_on": [],
                    "owned_scope": ["tracked.txt"],
                    "acceptance": ["charge succeeds"],
                    "verification": ["charge tests pass"],
                    "commit_boundary": "Charge capability",
                    "commit_subject": "type(T_PHASE): implement charge path",
                    "status": "PLANNED",
                },
                {
                    "phase_id": "REFUND",
                    "goal": "Implement refund path",
                    "depends_on": ["CHARGE"],
                    "owned_scope": ["refund.py"],
                    "acceptance": ["refund succeeds"],
                    "verification": ["refund tests pass"],
                    "commit_boundary": "Refund capability",
                    "commit_subject": "type(T_PHASE): implement refund path",
                    "status": "PLANNED",
                },
            ],
        }
        saved = devcrew_phase.load_for_resume(
            devcrew_phase.manifest_path(project, "T_PHASE"), receipt
        )
        multi["planner_dispatch"] = saved["planner_dispatch"]
        multi["manifest_revision"] = saved["manifest_revision"]
        devcrew_phase.save_manifest(devcrew_phase.manifest_path(project, "T_PHASE"), multi, receipt)
        runtime = self.runtime()
        route = cost.route("executor", runtime=runtime)
        payload = context.routed_task_spawn(
            "executor",
            "FULL TASK PLAN: implement charges and refunds",
            route,
            runtime,
            task_id="T_PHASE",
            project_root=str(project),
            task_worktree_root=str(worktree),
            base_sha=base,
            owned_scope=["everything"],
            acceptance=["all task capabilities"],
            verification=["all tests"],
            receipt=receipt,
            role_contract="Exact executor role contract",
        )
        message = payload["spawn"]["message"]
        self.assertIn('"PHASE_ID": "CHARGE"', message)
        self.assertIn("Implement charge path", message)
        self.assertIn("charge succeeds", message)
        self.assertNotIn("Implement refund path", message)
        self.assertNotIn("refund succeeds", message)
        self.assertNotIn("FULL TASK PLAN", message)
        self.assertNotIn("all task capabilities", message)

        executor = self.agent(task_id="T_PHASE", runtime=runtime)
        contract.update({"GOAL": "legacy narrative", "OWNED_SCOPE": ["tracked.txt"]})
        handoff = context.prepare_handoff(executor, contract, {}, "executor-next", selection=route)
        self.assertIn('"PHASE_ID": "CHARGE"', handoff["spawn"]["message"])
        self.assertNotIn("legacy narrative", handoff["spawn"]["message"])

        devcrew_phase.manifest_path(project, "T_PHASE").unlink()
        with self.assertRaisesRegex(ValueError, "PHASE_MANIFEST_REQUIRED"):
            context.routed_task_spawn(
                "executor",
                "Implement",
                route,
                runtime,
                task_id="T_PHASE",
                project_root=str(project),
                task_worktree_root=str(worktree),
                base_sha=base,
                owned_scope=["tracked.txt"],
                acceptance=["works"],
                verification=["tests"],
                receipt=receipt,
                role_contract="Exact executor role contract",
            )
        with self.assertRaisesRegex(ValueError, "PHASE_MANIFEST_REQUIRED"):
            context.prepare_handoff(executor, contract, {}, "executor-next", selection=route)

    def test_local_fix_executor_routing_preserves_only_bounded_repair_contract(self):
        project, worktree, base, contract = self.task_checkout("T_LOCAL_FIX")
        receipt = contract["TASK_WORKTREE_RECEIPT"]
        manifest_path = devcrew_phase.manifest_path(project, "T_LOCAL_FIX")
        manifest = json.loads(manifest_path.read_text())
        findings = [
            {
                "CLASSIFICATION": "LOCAL_FIX",
                "SUMMARY": "repair this defect",
                "EVIDENCE": ["tests/test_widget.py:12"],
            }
        ]
        phase = manifest["phases"][0]
        phase["status"] = "EXECUTING"
        tree = subprocess.run(
            ["git", "-C", str(worktree), "rev-parse", f"{base}^{{tree}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        devcrew_phase.pin_verification(phase)
        subject = phase["commit_subject"]
        message = (
            subject + f"\n\nDevCrew-Task: T_LOCAL_FIX\nDevCrew-Phase: P1"
            f"\nDevCrew-Base: {base}\nDevCrew-Tree: {tree}"
            f"\nDevCrew-Verification: {phase['verification_sha256']}"
        )
        rejected_sha = subprocess.run(
            ["git", "-C", str(worktree), "commit-tree", tree, "-p", base, "-m", message],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        check_cli = pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"
        check = subprocess.run(
            [
                sys.executable,
                str(check_cli),
                "run-check",
                "--project",
                str(project),
                "--execution-root",
                str(worktree),
                "--task-id",
                "T_LOCAL_FIX",
                "--name",
                "rejected-candidate",
                "--",
                sys.executable,
                "-c",
                "print('pass')",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        rejected_evidence = (
            pathlib.Path(check.stdout.splitlines()[0])
            .resolve()
            .relative_to(project.resolve())
            .as_posix()
        )
        backup_ref = devcrew_phase._rejected_candidate_ref("T_LOCAL_FIX", "P1", 1)
        subprocess.run(
            ["git", "-C", str(worktree), "update-ref", backup_ref, rejected_sha], check=True
        )
        report = {
            "TASK_ID": "T_LOCAL_FIX",
            "PHASE_ID": "P1",
            "BASE_SHA": base,
            "TARGET_SHA": rejected_sha,
            "VERDICT": "FAIL",
            "FINDINGS": findings,
        }
        report_path = project / "reviews" / "rejected.json"
        report_path.parent.mkdir()
        report_bytes = json.dumps(report).encode()
        report_path.write_bytes(report_bytes)
        phase["review_cycles"] = [
            {
                "cycle_id": 1,
                "backup_ref": backup_ref,
                "candidate_commit": {
                    "sha": rejected_sha,
                    "tree": tree,
                    "base": base,
                    "evidence": [rejected_evidence],
                    "verification_contract": devcrew_phase._verification_contract(phase),
                },
                "review_report": {
                    "task_id": "T_LOCAL_FIX",
                    "phase_id": "P1",
                    "base_sha": base,
                    "target_sha": rejected_sha,
                    "verdict": "FAIL",
                    "findings": findings,
                    "report_ref": "reviews/rejected.json",
                    "sha256": hashlib.sha256(report_bytes).hexdigest(),
                },
            }
        ]
        phase["active_local_fix"] = {
            "cycle_id": 1,
            "rejected_candidate_sha": rejected_sha,
            "findings": findings,
        }
        devcrew_phase.save_manifest(manifest_path, manifest, receipt)
        devcrew_requirements.record(
            project,
            "memory",
            {
                "id": "req-bounded-repair",
                "revision": 1,
                "selectors": {
                    "paths": ["tracked.txt"],
                    "roles": ["executor"],
                    "task_classes": ["IMPLEMENTATION"],
                },
                "trigger": "A rejected candidate enters bounded repair.",
                "action": "Carry the applicable canonical requirement into the repair dispatch.",
                "check": "Ensure the repair payload contains the selected requirement.",
                "origin": {"kind": "user_requirement", "reference": "test"},
                "assessment": {
                    "status": "active",
                    "durable": True,
                    "decision": "distinct",
                    "rationale": "This requirement applies to repair work.",
                    "evidence": [{"kind": "user_requirement", "reference": "test"}],
                },
            },
            expected_registry_revision=0,
        )
        runtime = self.runtime()
        selection = cost.route("executor", task_class="IMPLEMENTATION", runtime=runtime)
        spawned = context.routed_task_spawn(
            "local_fix_executor",
            "FULL TASK PLAN MUST NOT BE FORWARDED",
            selection,
            runtime,
            task_id="T_LOCAL_FIX",
            project_root=str(project),
            task_worktree_root=str(worktree),
            base_sha=base,
            owned_scope=["everything"],
            acceptance=["whole-task accepted"],
            verification=["all checks"],
            receipt=receipt,
            role_contract="Use the exact local-fix contract.",
        )
        message = spawned["spawn"]["message"]
        contract_line = next(
            line for line in message.splitlines() if line.startswith("PHASE_EXECUTION_CONTRACT: ")
        )
        contract_text = contract_line.partition(": ")[2]
        payload = json.loads(contract_text)
        repair_envelope = _requirements_envelope(message)
        self.assertEqual(
            {row["id"] for row in repair_envelope["requirements"]}, {"req-bounded-repair"}
        )
        self.assertEqual(
            set(payload),
            {
                "TASK_ID",
                "PHASE_ID",
                "BASE_SHA",
                "REJECTED_CANDIDATE_SHA",
                "FINDINGS",
                "OWNED_SCOPE",
                "ACCEPTANCE",
                "VERIFICATION",
                "INSTRUCTION",
            },
        )
        self.assertEqual(payload["BASE_SHA"], base)
        self.assertEqual(payload["REJECTED_CANDIDATE_SHA"], rejected_sha)
        self.assertEqual(payload["FINDINGS"], findings)
        self.assertEqual(payload["OWNED_SCOPE"], ["tracked.txt"])
        self.assertEqual(payload["ACCEPTANCE"], ["phase behavior works"])
        self.assertEqual(payload["VERIFICATION"], ["phase tests pass"])
        location = json.loads(message.split("EXECUTION_LOCATION: ", 1)[1].splitlines()[0])
        self.assertEqual(
            location, {"PROJECT_ROOT": str(project), "TASK_WORKTREE_ROOT": str(worktree)}
        )
        self.assertNotIn("FULL TASK PLAN", message)
        self.assertNotIn("whole-task accepted", message)
        self.assertNotIn("all checks", message)
        report_path.write_text("tampered archived rejection\n")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_REVIEW_REPORT_CHANGED"):
            context.routed_task_spawn(
                "local_fix_executor",
                "",
                selection,
                runtime,
                task_id="T_LOCAL_FIX",
                project_root=str(project),
                task_worktree_root=str(worktree),
                base_sha=base,
                owned_scope=["everything"],
                acceptance=["whole-task accepted"],
                verification=["all checks"],
                receipt=receipt,
                role_contract="Use the exact local-fix contract.",
            )

    def test_unarchived_active_local_fix_cannot_route_executor(self):
        project, worktree, base, contract = self.task_checkout("T_FAKE_FIX")
        receipt = contract["TASK_WORKTREE_RECEIPT"]
        manifest_path = devcrew_phase.manifest_path(project, "T_FAKE_FIX")
        manifest = json.loads(manifest_path.read_text())
        phase = manifest["phases"][0]
        phase["status"] = "EXECUTING"
        phase["active_local_fix"] = {
            "cycle_id": 1,
            "rejected_candidate_sha": "b" * 40,
            "findings": [
                {"CLASSIFICATION": "LOCAL_FIX", "SUMMARY": "forged repair", "EVIDENCE": ["fake"]}
            ],
        }
        manifest_path.write_text(json.dumps(manifest))
        runtime = self.runtime()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_LOCAL_FIX_INCONSISTENT"):
            context.routed_task_spawn(
                "forged_local_fix",
                "",
                cost.route("executor", runtime=runtime),
                runtime,
                task_id="T_FAKE_FIX",
                project_root=str(project),
                task_worktree_root=str(worktree),
                base_sha=base,
                owned_scope=["tracked.txt"],
                acceptance=["phase behavior works"],
                verification=["phase tests pass"],
                receipt=receipt,
                role_contract="Use the exact local-fix contract.",
            )

    def test_phase_init_succeeds_without_existing_phase_manifest(self):
        project, _worktree, _base, contract = self.task_checkout("T_INIT")
        path = devcrew_phase.manifest_path(project, "T_INIT")
        planner = json.loads(path.read_text())
        path.unlink()
        planner_path = project / "planner.json"
        forged = json.loads(json.dumps(planner))
        forged["phases"][0]["active_local_fix"] = {
            "cycle_id": 1,
            "rejected_candidate_sha": "b" * 40,
            "findings": [
                {"CLASSIFICATION": "LOCAL_FIX", "SUMMARY": "forged", "EVIDENCE": ["fake finding"]}
            ],
        }
        planner_path.write_text(json.dumps(forged))
        script = pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"
        rejected = subprocess.run(
            [
                sys.executable,
                str(script),
                "phase",
                "init",
                "--project",
                str(project),
                "--task-id",
                "T_INIT",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["planner_dispatch"]["spawn_receipt_id"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("INVALID_INITIAL_PHASE_STATE", rejected.stderr)
        self.assertFalse(path.exists())
        planner_path.write_text(json.dumps(planner))
        result = subprocess.run(
            [
                sys.executable,
                str(script),
                "phase",
                "init",
                "--project",
                str(project),
                "--task-id",
                "T_INIT",
                "--planner-output",
                str(planner_path),
                "--spawn-receipt-id",
                planner["planner_dispatch"]["spawn_receipt_id"],
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(path.is_file())

    def test_task_executor_payload_rejects_symlink_alias_to_registered_external_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            project = root / "repo"
            project.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
                )

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            (project / "tracked.txt").write_text("base\n")
            git("add", "tracked.txt")
            git("commit", "-qm", "base")
            base = git("rev-parse", "HEAD").stdout.strip()
            arbitrary = root / "arbitrary-linked-worktree"
            git("worktree", "add", "-b", "codex/tasks/T13", str(arbitrary), base)
            key = hashlib.sha256(str(project.resolve()).encode()).hexdigest()[:16]
            expected = project.parent / ".devcrew-task-worktrees" / f"{project.name}-{key}" / "T13"
            expected.parent.mkdir(parents=True)
            expected.symlink_to(arbitrary, target_is_directory=True)
            runtime = self.runtime()
            selection = cost.route("executor", runtime=runtime)
            for supplied_path in (expected, arbitrary):
                with (
                    self.subTest(path=str(supplied_path)),
                    self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"),
                ):
                    context.routed_task_spawn(
                        "executor",
                        "Implement",
                        selection,
                        runtime,
                        task_id="T13",
                        project_root=str(project),
                        task_worktree_root=str(supplied_path),
                        base_sha=base,
                        owned_scope=["tracked.txt"],
                        acceptance=["works"],
                        verification=["tests"],
                        role_contract="Exact executor contract",
                    )

    def test_task_executor_payload_rejects_existing_base_that_differs_from_worktree_head(self):
        project, worktree, base, _ = self.task_checkout("T14")
        (project / "later.txt").write_text("later commit\n")
        subprocess.run(["git", "-C", str(project), "add", "later.txt"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "-qm",
                "later",
            ],
            check=True,
        )
        different_base = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertNotEqual(base, different_base)
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            base,
        )
        runtime = self.runtime()
        with self.assertRaisesRegex(ValueError, "TASK_WORKTREE_INVALID"):
            context.routed_task_spawn(
                "executor",
                "Implement",
                cost.route("executor", runtime=runtime),
                runtime,
                task_id="T14",
                project_root=str(project),
                task_worktree_root=str(worktree),
                base_sha=different_base,
                owned_scope=["tracked.txt"],
                acceptance=["works"],
                verification=["tests"],
                role_contract="Exact executor contract",
            )

    def test_unavailable_role_generic_reviewer_and_no_silent_defaults(self):
        runtime = self.runtime(roles={})
        spawn = self.build_reviewer(
            runtime=runtime, role_contract="Exact read-only reviewer contract"
        )
        self.assertEqual(spawn["spawn"]["agent_type"], "default")
        self.assertEqual(
            (spawn["spawn"]["model"], spawn["spawn"]["reasoning_effort"]), ("strong-test", "medium")
        )
        self.assertIn("Exact read-only reviewer contract", spawn["spawn"]["message"])
        self.assertIn("route", spawn)
        self.assertIn("dispatch", spawn)
        self.assertIn("issued_route", spawn)
        with self.assertRaisesRegex(ValueError, "DISPATCH_ROUTE_REQUIRED"):
            context.reviewer_spawn("T04", runtime=runtime)
        for settings in ({"generic_available": False}, {"explicit_model_effort": False}):
            with self.assertRaisesRegex(ValueError, "AGENT_RUNTIME_REQUIRED"):
                self.build_reviewer(runtime=runtime | settings, role_contract="Review read-only")
        with self.assertRaisesRegex(ValueError, "route"):
            self.build_reviewer(
                runtime=self.runtime(), selection=cost.route("executor", runtime=self.runtime())
            )

    def test_pinned_bounded_reviewer_selects_generic_before_spawn(self):
        runtime = self.runtime(
            roles={
                "reviewer-agent": {
                    "available": True,
                    "supports_overrides": False,
                    "model": "bounded-test",
                    "reasoning_effort": "low",
                }
            }
        )
        exact = (
            "Review read-only against the accepted contract and evidence. Do not edit or commit."
        )
        prepared_fixture = self.reviewer_fixture()
        with (
            mock.patch("subprocess.Popen", wraps=subprocess.Popen) as process,
            mock.patch("socket.socket") as network,
        ):
            spawn = self.build_reviewer(
                runtime=runtime,
                review_complexity="NORMAL",
                role_contract=exact,
                prepared_fixture=prepared_fixture,
            )
        for call in process.call_args_list:
            argv = call.args[0]
            self.assertTrue(
                pathlib.Path(argv[0]).name == "git"
                or (
                    pathlib.Path(argv[0]) == pathlib.Path(sys.executable)
                    and pathlib.Path(argv[1]).name == "devcrew.py"
                )
            )
        network.assert_not_called()
        self.assertEqual(spawn["spawn"]["agent_type"], "default")
        self.assertEqual(
            (spawn["spawn"]["model"], spawn["spawn"]["reasoning_effort"]), ("strong-test", "medium")
        )
        self.assertTrue(spawn["spawn"]["message"].startswith(exact + "\n"))
        dispatch = json.loads(spawn["spawn"]["message"].split("\nDISPATCH_SELECTION: ", 1)[1])
        self.assertEqual(dispatch["SPAWN_MODE"], "generic")
        self.assertEqual(dispatch["SPAWN_REASON"], "pinned_model_mismatch")
        self.assertFalse(dispatch["ROUTE_APPLIED"])

    def test_stale_model_and_effort_are_rejected_before_payload(self):
        selected = cost.route("reviewer", runtime=self.runtime())
        for changes in (
            {"RESOLVED_MODEL": "bounded-test"},
            {"RESOLVED_MODEL": "older-strong"},
            {"RESOLVED_MODEL": "deep-test"},
            {"REASONING_EFFORT": "low"},
        ):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "route"):
                self.build_reviewer(runtime=self.runtime(), selection=selected | changes)
        handoff = context.prepare_handoff(
            self.agent(model="strong-test", reasoning_effort="high"),
            self.replacement_contract("finish"),
            {},
            "executor-2",
        )
        self.assertEqual(handoff["spawn"]["model"], "bounded-test")
        self.assertEqual(handoff["spawn"]["reasoning_effort"], "medium")

    def test_pinned_alternate_reviewer_cannot_downgrade_generic_dispatch(self):
        runtime = self.runtime(
            roles={
                "reviewer-agent": {
                    "available": True,
                    "supports_overrides": False,
                    "model": "other-strong",
                    "reasoning_effort": "medium",
                }
            }
        )
        prepared = self.build_reviewer(runtime=runtime, role_contract="Review read-only.")
        self.assertEqual(prepared["spawn"]["agent_type"], "default")
        self.assertEqual(prepared["spawn"]["model"], "strong-test")
        self.assertEqual(prepared["dispatch"]["SPAWN_REASON"], "pinned_model_mismatch")
        with self.assertRaisesRegex(ValueError, "AGENT_RUNTIME_REQUIRED"):
            self.build_reviewer(
                runtime=runtime | {"generic_available": False}, role_contract="Review read-only."
            )

    def test_matching_fixed_role_is_supported_without_generic(self):
        runtime = self.runtime(
            roles={
                "reviewer-agent": {
                    "available": True,
                    "supports_overrides": False,
                    "model": "strong-test",
                    "reasoning_effort": "medium",
                }
            },
            generic_available=False,
        )
        self.assertEqual(
            self.build_reviewer(runtime=runtime)["spawn"]["agent_type"], "reviewer-agent"
        )

    def test_pre_spawn_mode_is_selected_from_role_specific_evidence(self):
        runtime = self.runtime()
        runtime["models"].append(
            {"id": "deep-test", "capabilities": ["deep", "strong"], "efforts": ["high"]}
        )
        cases = [
            (
                "planner",
                {"decision_complexity": "ROUTINE"},
                "decision-agent",
                {"available": True, "model": "deep-test", "reasoning_effort": "high"},
                "generic",
                "pinned_model_mismatch",
                "strong-test",
                "low",
            ),
            (
                "planner",
                {"decision_complexity": "DEEP"},
                "decision-agent",
                {"available": True, "model": "deep-test", "reasoning_effort": "high"},
                "typed",
                "typed_model_effort_match",
                "deep-test",
                "high",
            ),
            (
                "executor",
                {},
                "executor-agent",
                {"available": True, "model": "strong-test", "reasoning_effort": "high"},
                "generic",
                "pinned_model_mismatch",
                "bounded-test",
                "medium",
            ),
            (
                "executor",
                {},
                "executor-agent",
                {"available": True, "model": "bounded-test", "reasoning_effort": "medium"},
                "typed",
                "typed_model_effort_match",
                "bounded-test",
                "medium",
            ),
            (
                "planner",
                {"decision_complexity": "ROUTINE"},
                "decision-agent",
                {
                    "available": True,
                    "supports_overrides": True,
                    "model": "deep-test",
                    "reasoning_effort": "high",
                },
                "typed",
                "typed_supported_override",
                "strong-test",
                "low",
            ),
            (
                "planner",
                {"decision_complexity": "ROUTINE"},
                "decision-agent",
                {"available": True, "model": "strong-test", "reasoning_effort": "high"},
                "generic",
                "pinned_effort_mismatch",
                "strong-test",
                "low",
            ),
            (
                "planner",
                {"decision_complexity": "ROUTINE"},
                "decision-agent",
                {},
                "generic",
                "unavailable_role",
                "strong-test",
                "low",
            ),
            (
                "planner",
                {"decision_complexity": "ROUTINE"},
                "decision-agent",
                {"available": True},
                "generic",
                "unconfirmed_role_override",
                "strong-test",
                "low",
            ),
        ]
        exact = "Exact role contract. Preserve ownership and acceptance. Do not commit."
        for role, options, typed, settings, mode, reason, model, effort in cases:
            with self.subTest(role=role, settings=settings):
                observed = runtime | {"roles": {typed: settings}}
                selected = cost.route(role, runtime=observed, **options)
                prepared = self.build_routed(
                    "selected_agent", "Bounded task", selected, observed, role_contract=exact
                )
                self.assertEqual(prepared["dispatch"]["SPAWN_MODE"], mode)
                self.assertEqual(prepared["dispatch"]["SPAWN_REASON"], reason)
                self.assertEqual(
                    prepared["spawn"]["agent_type"], typed if mode == "typed" else "default"
                )
                self.assertEqual(
                    (prepared["spawn"]["model"], prepared["spawn"]["reasoning_effort"]),
                    (model, effort),
                )
                self.assertTrue(prepared["spawn"]["message"].startswith(exact + "\n"))
                self.assertFalse(prepared["dispatch"]["ROUTE_APPLIED"])

    def test_post_sync_unloaded_roles_use_current_contract_and_explicit_routes(self):
        runtime = self.runtime(roles={})
        runtime["models"].append(
            {"id": "deep-test", "capabilities": ["deep", "strong"], "efforts": ["high"]}
        )
        roles = pathlib.Path(__file__).resolve().parents[1] / "skills/devcrew-setup/assets/agents"
        cases = (
            ("planner", {"decision_complexity": "ROUTINE"}, "decision-agent", "strong-test", "low"),
            ("executor", {}, "executor-agent", "bounded-test", "medium"),
            ("reviewer", {"risk": "low"}, "reviewer-agent", "strong-test", "medium"),
            ("reviewer", {"risk": "high"}, "reviewer-agent", "strong-test", "high"),
            ("planner", {"decision_complexity": "DEEP"}, "decision-agent", "deep-test", "high"),
        )
        original = (
            "PROJECT_ROOT: /workspace/DEMO-103\n"
            "USER_REQUEST: Получи DEMO-103 из Yandex Tracker и приступай к реализации.\n"
            "SCOPE: Owned files from accepted plan only.\n"
            "ACCEPTANCE: Implement DEMO-103 and run required checks."
        )
        for role, options, typed_role, expected_model, expected_effort in cases:
            with self.subTest(role=role, options=options):
                contract = tomllib.loads((roles / f"{typed_role}.toml").read_text())[
                    "developer_instructions"
                ]
                selected = cost.route(role, runtime=runtime, **options)
                prepared = self.build_routed(
                    "post_sync_role", original, selected, runtime, role_contract=contract
                )
                self.assertEqual(prepared["dispatch"]["SPAWN_MODE"], "generic")
                self.assertEqual(prepared["spawn"]["agent_type"], "default")
                self.assertEqual(
                    (prepared["spawn"]["model"], prepared["spawn"]["reasoning_effort"]),
                    (expected_model, expected_effort),
                )
                self.assertTrue(prepared["spawn"]["message"].startswith(contract + "\n"))
                if role == "planner":
                    for preserved in (
                        "PROJECT_ROOT: /workspace/DEMO-103",
                        "USER_REQUEST: Получи DEMO-103",
                        "SCOPE: Owned files",
                        "ACCEPTANCE: Implement DEMO-103",
                    ):
                        self.assertIn(preserved, prepared["spawn"]["message"])
                else:
                    self.assertNotIn("/workspace/DEMO-103", prepared["spawn"]["message"])
                    self.assertNotIn("USER_REQUEST: Получи DEMO-103", prepared["spawn"]["message"])
                    if role == "executor":
                        line = next(
                            line
                            for line in prepared["spawn"]["message"].splitlines()
                            if line.startswith("PHASE_EXECUTION_CONTRACT: ")
                        )
                        actual = json.loads(line.split(": ", 1)[1])
                        self.assertTrue(pathlib.Path(actual["PROJECT_ROOT"]).is_dir())
                        self.assertTrue(pathlib.Path(actual["TASK_WORKTREE_ROOT"]).is_dir())
                        self.assertEqual(actual["TASK_ID"], "T04")
                        self.assertEqual(actual["OWNED_SCOPE"], ["tracked.txt"])
                        self.assertEqual(actual["ACCEPTANCE"], ["phase behavior works"])
                        self.assertEqual(actual["VERIFICATION"], ["phase tests pass"])
                    else:
                        line = next(
                            line
                            for line in prepared["spawn"]["message"].splitlines()
                            if line.startswith("PHASE_REVIEW_CONTRACT: ")
                        )
                        actual = json.loads(line.split(": ", 1)[1])
                        self.assertEqual(actual["TASK_ID"], "T01")
                        self.assertTrue(actual["OWNED_SCOPE"])
                        self.assertTrue(actual["ACCEPTANCE"])
        selected = cost.route("executor", runtime=runtime)
        with self.assertRaisesRegex(ValueError, "AGENT_RUNTIME_REQUIRED"):
            self.build_routed(
                "post_sync_role",
                "Continue original user request",
                selected,
                runtime | {"generic_available": False},
                role_contract="current role contract",
            )

    def test_conflicting_role_is_never_probe_spawned(self):
        runtime = self.runtime(
            roles={
                "decision-agent": {
                    "available": True,
                    "model": "deep-test",
                    "reasoning_effort": "high",
                }
            }
        )
        # Generic model/effort controls are present, but prove no typed override.
        selected = cost.route("planner", decision_complexity="ROUTINE", runtime=runtime)
        before = json.dumps([runtime, selected], sort_keys=True)
        with (
            mock.patch("subprocess.Popen", wraps=subprocess.Popen) as process,
            mock.patch("socket.socket") as network,
        ):
            prepared = self.build_routed(
                "planner",
                "Plan bounded task",
                selected,
                runtime,
                role_contract="Exact planner contract",
            )
        for call in process.call_args_list:
            argv = call.args[0]
            self.assertTrue(
                pathlib.Path(argv[0]).name == "git"
                or (
                    pathlib.Path(argv[0]) == pathlib.Path(sys.executable)
                    and pathlib.Path(argv[1]).name == "devcrew.py"
                )
            )
        network.assert_not_called()
        self.assertEqual(json.dumps([runtime, selected], sort_keys=True), before)
        self.assertEqual(prepared["spawn"]["agent_type"], "default")
        self.assertEqual(prepared["dispatch"]["SPAWN_MODE"], "generic")
        self.assertNotIn("agent_id", prepared)
        with self.assertRaisesRegex(ValueError, "AGENT_RUNTIME_REQUIRED"):
            self.build_routed(
                "planner",
                "Plan bounded task",
                selected,
                runtime | {"generic_available": False},
                role_contract="Exact planner contract",
            )

    def test_cli_handoff_uses_supplied_snapshot_and_pinned_role_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            contract = self.replacement_contract("finish")
            inputs = {
                "agent": self.agent(
                    runtime=self.runtime(roles={}), role_contract="Exact executor contract"
                ),
                "contract": {"GOAL": "finish", "OWNED_PATHS": ["app.py"], **contract},
                "state": {"DONE": "one", "NEXT_ACTION": "two"},
            }
            for name, value in inputs.items():
                (root / (name + ".json")).write_text(json.dumps(value))
            command = [
                sys.executable,
                str(pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"),
                "context-handoff",
                "--agent",
                str(root / "agent.json"),
                "--contract",
                str(root / "contract.json"),
                "--state",
                str(root / "state.json"),
                "--replacement-id",
                "executor-2",
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            prepared = json.loads(result.stdout)
            self.assertEqual(prepared["spawn"]["agent_type"], "default")
            self.assertEqual(
                (prepared["spawn"]["model"], prepared["spawn"]["reasoning_effort"]),
                ("bounded-test", "medium"),
            )
            self.assertFalse(prepared["route"]["ROUTE_APPLIED"])

    def test_allowance_and_report(self):
        self.assertEqual(
            context.normalize_allowance(None)["status"], "CODEX_ALLOWANCE_TELEMETRY_NOT_AVAILABLE"
        )
        self.assertIsNone(context.normalize_allowance(None)["weekly_remaining_percent"])
        agents = [
            self.agent(role="orchestrator", total_recorded_tokens=None),
            self.agent(agent_id="executor-1", total_recorded_tokens=100, handoff_count=1),
            self.agent(agent_id="executor-2", total_recorded_tokens=200, handoff_count=0),
            self.agent(agent_id="reviewer-1", role="reviewer", total_recorded_tokens=50),
        ]
        report = context.task_report("T04", agents)
        self.assertEqual(report["executors"]["count"], 2)
        self.assertEqual(report["executors"]["handoffs"], 1)
        self.assertEqual(report["total_recorded_tokens"], 350)
        self.assertIn("orchestrator", report["unknown_usage"])
        self.assertIsNone(report["orchestrator"]["turns"])
        self.assertEqual(
            context.task_report("T04", [self.agent(usage={"total_recorded_tokens": 7})])[
                "total_recorded_tokens"
            ],
            7,
        )
        phase = context.phase_report(agents)
        self.assertEqual(phase["most_expensive_tasks"][0]["task_id"], "T04")
        self.assertEqual(phase["sessions_with_handoff"], ["executor-1"])
        multiple = context.task_report(
            "T04", [self.agent(handoff_count=1), self.agent(agent_id="executor-2", handoff_count=2)]
        )
        self.assertEqual(multiple["executors"]["handoffs"], 2)

    def test_agents_api_usage_and_allowance_guardrail(self):
        usage = context.normalize_usage(
            {
                "input_tokens": 100,
                "input_tokens_details": {"cached_tokens": 20},
                "output_tokens": 30,
                "output_tokens_details": {"reasoning_tokens": 5},
                "total_tokens": 130,
            }
        )
        self.assertEqual(usage["cached_input_tokens"], 20)
        self.assertEqual(usage["reasoning_tokens"], 5)
        self.assertEqual(usage["total_recorded_tokens"], 130)
        settings = {"weekly_soft_remaining_percent": 15, "weekly_hard_remaining_percent": 5}
        self.assertEqual(context.allowance_guardrail(None, settings), "UNAVAILABLE")
        self.assertEqual(
            context.allowance_guardrail({"weekly_remaining_percent": 10}, settings), "SOFT_LIMIT"
        )
        self.assertEqual(
            context.allowance_guardrail({"weekly_remaining_percent": 4}, settings), "HARD_LIMIT"
        )
        observed = context.codex_limits_allowance(
            {
                "rateLimitsByLimitId": {
                    "codex": {
                        "primary": {
                            "usedPercent": 96,
                            "windowDurationMins": 10080,
                            "resetsAt": 123,
                        },
                        "secondary": None,
                    }
                }
            }
        )
        self.assertEqual(observed["weekly_remaining_percent"], 4)
        self.assertIsNone(observed["session_remaining_percent"])
        self.assertEqual(observed["reset_at"], {"weekly": 123})

    def test_cli_uses_project_configuration_and_preserves_null(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            (project / ".codex").mkdir()
            (project / ".codex/devcrew.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "paths": {
                            "context": "docs/context.md",
                            "tasks": "docs/tasks",
                            "sessions": "docs/sessions",
                            "memory": "memory",
                            "checks": ".codex/checks",
                        },
                        "agent_context": {"soft_context_tokens": 400, "hard_context_tokens": 600},
                    }
                )
            )
            agent_file = project / "agent.json"
            agent_file.write_text(json.dumps(self.agent(context_limit=None)))
            result = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"),
                    "context-evaluate",
                    "--project",
                    str(project),
                    "--agent",
                    str(agent_file),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["signal"], "HANDOFF_RECOMMENDED")
            self.assertIsNone(payload["usage"]["total_recorded_tokens"])
            self.assertEqual(
                payload["allowance"]["status"], "CODEX_ALLOWANCE_TELEMETRY_NOT_AVAILABLE"
            )
            config_file = project / ".codex/devcrew.json"
            configuration = json.loads(config_file.read_text())
            configuration["agent_context"]["telemetry"] = False
            config_file.write_text(json.dumps(configuration))
            agents_file = project / "agents.json"
            agents_file.write_text(json.dumps([self.agent(total_recorded_tokens=123)]))
            report = subprocess.run(
                [
                    sys.executable,
                    str(pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"),
                    "context-report",
                    "--project",
                    str(project),
                    "--task-id",
                    "T04",
                    "--agents",
                    str(agents_file),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertEqual(json.loads(report.stdout), {"status": "TELEMETRY_DISABLED"})


if __name__ == "__main__":
    unittest.main()
