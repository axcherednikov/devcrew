import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import devcrew_context as context
import devcrew_cost as cost
import devcrew_phase
import test_phase_review_lifecycle as phase_review


class CostPolicy(unittest.TestCase):
    def runtime(self, deep=True):
        models = [
            {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
            {"id": "strong-test", "capabilities": ["strong"], "efforts": ["low", "medium", "high"]},
        ]
        if deep:
            models.append(
                {"id": "deep-test", "capabilities": ["strong", "deep"], "efforts": ["high"]}
            )
        return {"models": models}

    def selected(self, role, **kwargs):
        result = cost.route(role, runtime=self.runtime(), **kwargs)
        return result["RESOLVED_MODEL"], result["REASONING_EFFORT"]

    def test_any_qualified_model_can_be_selected_by_runtime_priority(self):
        for role, capability in (("executor", "bounded"), ("reviewer", "strong")):
            with self.subTest(role=role):
                runtime = {
                    "models": [
                        {
                            "id": "provider/model-a",
                            "capabilities": [capability],
                            "efforts": ["medium"],
                        },
                        {
                            "id": "local/model-b",
                            "capabilities": [capability],
                            "efforts": ["medium"],
                        },
                    ]
                }
                self.assertEqual(cost.route(role, runtime=runtime)["model"], "provider/model-a")
                runtime["models"][1]["priority"] = 1
                self.assertEqual(cost.route(role, runtime=runtime)["model"], "local/model-b")

    def test_four_independent_dimensions_and_risk(self):
        cases = [
            ("coordinator", {}, ("strong-test", "low")),
            ("coordinator", {"coordination_complexity": "COMPLEX"}, ("strong-test", "medium")),
            ("planner", {"decision_complexity": "ROUTINE"}, ("strong-test", "low")),
            ("planner", {"decision_complexity": "NORMAL"}, ("strong-test", "medium")),
            ("planner", {"decision_complexity": "HARD"}, ("strong-test", "high")),
            ("planner", {"decision_complexity": "DEEP"}, ("deep-test", "high")),
            ("executor", {}, ("bounded-test", "medium")),
            ("executor", {"risk": "high"}, ("bounded-test", "medium")),
            ("executor", {"implementation_complexity": "MECHANICAL"}, ("bounded-test", "low")),
            ("reviewer", {"review_complexity": "ROUTINE"}, ("strong-test", "low")),
            ("reviewer", {}, ("strong-test", "medium")),
            ("reviewer", {"risk": "high", "review_complexity": "ROUTINE"}, ("strong-test", "high")),
            ("research", {}, ("bounded-test", "low")),
        ]
        for role, options, expected in cases:
            with self.subTest(role=role, options=options):
                self.assertEqual(self.selected(role, **options), expected)
        for risk in cost.RISKS:
            self.assertEqual(
                self.selected("planner", risk=risk, decision_complexity="NORMAL"),
                ("strong-test", "medium"),
            )
            self.assertEqual(
                self.selected("executor", risk=risk, decision_complexity="DEEP"),
                ("bounded-test", "medium"),
            )
            self.assertEqual(
                self.selected("coordinator", risk=risk, decision_complexity="DEEP"),
                ("strong-test", "low"),
            )
        result = cost.route("executor", runtime=self.runtime(), decision_complexity="DEEP")
        self.assertIsNone(result["DECISION_COMPLEXITY"])
        self.assertIsNone(result["REVIEW_COMPLEXITY"])

    def test_deep_fallback_uses_only_confirmed_capabilities(self):
        result = cost.route("planner", decision_complexity="DEEP", runtime=self.runtime(deep=False))
        self.assertEqual(result["RESOLVED_MODEL"], "strong-test")
        self.assertEqual(result["RESOLUTION_STATUS"], "FALLBACK")
        other = {"models": [{"id": "other-deep", "capabilities": ["deep"], "efforts": ["high"]}]}
        self.assertEqual(
            cost.route("planner", decision_complexity="DEEP", runtime=other)["model"], "other-deep"
        )
        unavailable = cost.route("planner", decision_complexity="DEEP", runtime={"models": []})
        self.assertIsNone(unavailable["model"])
        self.assertEqual(unavailable["RESOLUTION_STATUS"], "BLOCKED")
        self.assertIsNone(cost.route("executor")["RESOLVED_MODEL"])

    def test_executor_complexity_risk_and_retries_never_escalate(self):
        for kwargs in (
            {
                "implementation_complexity": "COMPLEX",
                "escalation_reason": "concurrency_implementation",
            },
            {"risk": "high", "escalation_reason": "security_risk"},
            {"retry_stage": 3, "escalation_reason": "repeated_failure"},
            {"retry_stage": 2, "task_class": "LOCAL_FIX"},
        ):
            with self.subTest(kwargs=kwargs):
                result = cost.route("executor", runtime=self.runtime(), **kwargs)
                self.assertEqual(
                    (result["RESOLVED_MODEL"], result["REASONING_EFFORT"]),
                    ("bounded-test", "medium"),
                )
                self.assertEqual(result["MODEL_CLASS"], "bounded")
                self.assertEqual(result["SELECTION_REASON"], "normal_implementation")

    def test_executor_requires_bounded_capability_and_requested_effort(self):
        for runtime in (
            {"models": [{"id": "other", "capabilities": ["strong"], "efforts": ["medium"]}]},
            {"models": [{"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low"]}]},
        ):
            result = cost.route("executor", runtime=runtime)
            self.assertIsNone(result["RESOLVED_MODEL"])
            self.assertEqual(result["RESOLUTION_STATUS"], "BLOCKED")

    def test_systemic_replan_targets_decision_and_keeps_compact_reason(self):
        result = cost.route("executor", task_class="SYSTEMIC_REPLAN", runtime=self.runtime())
        self.assertEqual(result["ROLE"], "planner")
        self.assertEqual(result["DECISION_COMPLEXITY"], "HARD")
        self.assertIsNone(result["IMPLEMENTATION_COMPLEXITY"])
        self.assertEqual(result["SELECTION_REASON"], "systemic_replan")
        with self.assertRaises(ValueError):
            cost.route("executor", escalation_reason="Here is my long private reasoning")
        self.assertEqual(self.selected("executor"), ("bounded-test", "medium"))

    def test_replan_classes_preserve_planner_policy_and_executor_ceiling(self):
        for task_class in ("REPLAN_REQUEST", "SYSTEMIC_REPLAN"):
            result = cost.route("executor", task_class=task_class, runtime=self.runtime())
            self.assertEqual(result["ROLE"], "planner")
            self.assertEqual(result["MODEL_CLASS"], "strong")
        # A DEEP planner decision is routed to the deep model; the next
        # separately dispatched executor uses bounded capability at medium effort.
        planner = cost.route("planner", decision_complexity="DEEP", runtime=self.runtime())
        executor = cost.route("executor", decision_complexity="DEEP", runtime=self.runtime())
        self.assertEqual(planner["RESOLVED_MODEL"], "deep-test")
        self.assertEqual(
            (executor["RESOLVED_MODEL"], executor["REASONING_EFFORT"]), ("bounded-test", "medium")
        )

    def test_cost_modes_do_not_mix_risk_and_implementation(self):
        self.assertEqual(cost.policy(), {"cost_mode": "economy"})
        for mode in cost.MODES:
            self.assertEqual(
                self.selected("executor", cost_mode=mode, risk="high"), ("bounded-test", "medium")
            )

    def test_reviewer_requires_strong_capability_across_modes_risks_and_retries(self):
        runtime = self.runtime()
        runtime["models"][1]["id"] = "qualified-reviewer"
        for role in ("reviewer", "reviewer-agent"):
            for mode in cost.MODES:
                for risk in cost.RISKS:
                    for complexity, effort in (
                        ("ROUTINE", "low"),
                        ("NORMAL", "medium"),
                        ("COMPLEX", "high"),
                    ):
                        with self.subTest(role=role, mode=mode, risk=risk, complexity=complexity):
                            selected = cost.route(
                                role,
                                runtime=runtime,
                                cost_mode=mode,
                                risk=risk,
                                review_complexity=complexity,
                            )
                            self.assertEqual(selected["model"], "qualified-reviewer")
                            self.assertEqual(
                                selected["effort"], "high" if risk == "high" else effort
                            )
                            retried = cost.route(
                                role,
                                runtime=runtime,
                                cost_mode=mode,
                                risk=risk,
                                review_complexity=complexity,
                                retry_stage=3,
                                escalation_reason="repeated_failure",
                            )
                            self.assertEqual(
                                (retried["model"], retried["effort"]),
                                ("qualified-reviewer", "high"),
                            )

    def test_reviewer_blocks_without_strong_capability_or_requested_effort(self):
        for runtime in (
            {"models": [{"id": "deep-only", "capabilities": ["deep"], "efforts": ["high"]}]},
            {"models": [{"id": "strong-test", "capabilities": ["bounded"], "efforts": ["medium"]}]},
            {"models": [{"id": "strong-test", "capabilities": ["strong"], "efforts": ["low"]}]},
        ):
            with self.subTest(runtime=runtime):
                selected = cost.route("reviewer", runtime=runtime)
                self.assertIsNone(selected["model"])
                self.assertEqual(selected["RESOLUTION_STATUS"], "BLOCKED")
        self.assertEqual(cost.route("reviewer")["RESOLUTION_STATUS"], "RUNTIME_REQUIRED")

    def test_runtime_quality_effort_and_suitability_are_explicit(self):
        runtime = self.runtime()
        runtime["models"][-1]["capabilities"] = ["strong"]
        self.assertEqual(
            cost.route("planner", decision_complexity="DEEP", runtime=runtime)["model"],
            "strong-test",
        )
        runtime["models"].append(
            {"id": "best-deep", "capabilities": ["deep"], "efforts": ["high"], "priority": 5}
        )
        self.assertEqual(
            cost.route("planner", decision_complexity="DEEP", runtime=runtime)["model"], "best-deep"
        )
        only_bounded = {"models": [self.runtime()["models"][0]]}
        self.assertEqual(
            cost.route("reviewer", runtime=only_bounded)["RESOLUTION_STATUS"], "BLOCKED"
        )
        no_medium = {
            "models": [{"id": "strong-test", "capabilities": ["strong"], "efforts": ["low"]}]
        }
        self.assertIsNone(cost.route("reviewer", runtime=no_medium)["model"])
        for invalid in (
            {"models": "all"},
            {"models": [{"id": "unknown"}]},
            {"models": self.runtime()["models"] * 2},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cost.route("executor", runtime=invalid)

    def test_read_only_cli_routes_five_scenarios(self):
        scenarios = [
            (
                "healthcheck",
                "SIMPLE",
                "ROUTINE",
                "ROUTINE",
                "medium",
                ["low", "low", "medium", "low"],
            ),
            (
                "feature",
                "SIMPLE",
                "NORMAL",
                "NORMAL",
                "medium",
                ["low", "medium", "medium", "medium"],
            ),
            (
                "payment_local",
                "SIMPLE",
                "NORMAL",
                "NORMAL",
                "high",
                ["low", "medium", "medium", "high"],
            ),
            (
                "payment_state",
                "SIMPLE",
                "HARD",
                "COMPLEX",
                "high",
                ["low", "high", "medium", "high"],
            ),
            ("bonus", "COMPLEX", "DEEP", "COMPLEX", "high", ["medium", "high", "medium", "high"]),
        ]
        requests = []
        for name, coord, decision, review, risk, efforts in scenarios:
            requests.extend(
                {
                    "role": role,
                    "task_class": name,
                    "risk": risk,
                    "coordination_complexity": coord,
                    "decision_complexity": decision,
                    "review_complexity": review,
                }
                for role in ("coordinator", "planner", "executor", "reviewer")
            )
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            runtime_file, request_file = root / "runtime.json", root / "request.json"
            runtime_file.write_text(json.dumps(self.runtime()))
            request_file.write_text(json.dumps(requests))
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            command = [
                sys.executable,
                str(ROOT / "scripts/devcrew_cost.py"),
                "--runtime",
                str(runtime_file),
                "--request",
                str(request_file),
            ]
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            routes = json.loads(result.stdout)
            self.assertEqual(len(routes), 20)
            for index, scenario in enumerate(scenarios):
                rows = routes[index * 4 : index * 4 + 4]
                self.assertEqual([row["effort"] for row in rows], scenario[-1])
                self.assertEqual(
                    [row["model"] for row in rows],
                    [
                        "strong-test",
                        "deep-test" if scenario[0] == "bonus" else "strong-test",
                        "bounded-test",
                        "strong-test",
                    ],
                )
                self.assertTrue(all(row["ROUTE_APPLIED"] is False for row in rows))
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
            runtime_file.write_text('{"models": []}')
            blocked = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(blocked.returncode, 2)
            self.assertTrue(
                all(row["RESOLUTION_STATUS"] == "BLOCKED" for row in json.loads(blocked.stdout))
            )

    def test_review_pass_and_failure_bound(self):
        self.assertEqual(cost.review_action(pass_state="digest", current_state="digest"), "commit")
        self.assertEqual(cost.review_action(pass_state="old", current_state="new"), "review")
        self.assertEqual(cost.review_action(failed_cycles=1), "review")
        self.assertEqual(cost.review_action(failed_cycles=2), "blocked")
        self.assertEqual(cost.review_action(failed_cycles=2, new_evidence=False), "blocked")

    def test_repeat_fail_can_continue_with_concrete_assessment_but_not_a_boolean(self):
        progress = {
            "reason": "A targeted test disproves the reviewer assumption",
            "next_action": "Recheck only that finding",
            "expected_result": "Resolve the conflict",
            "cost_rationale": "One bounded check reuses the existing candidate",
            "evidence_records": ["checks/proof.json"],
        }
        self.assertEqual(cost.review_action(failed_cycles=2, continuation=progress), "review")
        for missing in progress:
            with self.subTest(missing=missing):
                incomplete = {key: value for key, value in progress.items() if key != missing}
                self.assertEqual(
                    cost.review_action(failed_cycles=2, continuation=incomplete), "blocked"
                )
        self.assertEqual(
            cost.review_action(failed_cycles=2, continuation=progress, new_evidence=False),
            "blocked",
        )
        self.assertEqual(
            cost.review_action(pass_state="same", current_state="same", loop_detected=True),
            "blocked",
        )
        self.assertEqual(
            cost.review_action(continuation=progress, owner_decision_required=True), "blocked"
        )

    def test_full_suite_reuse_needs_matching_verified_tree_and_command(self):
        state = {"git_head": "abc", "worktree_fingerprint": {"digest": "same"}}
        record = {
            "status": "passed",
            "exit_code": 0,
            "command": ["pytest"],
            "git_head": "abc",
            "git_head_before": "abc",
            "worktree_fingerprint_before": state["worktree_fingerprint"],
            "worktree_fingerprint_after": state["worktree_fingerprint"],
        }
        self.assertTrue(cost.reusable_check(record, state, ["pytest"]))
        self.assertFalse(cost.reusable_check(record, state | {"git_head": "def"}, ["pytest"]))
        self.assertFalse(cost.reusable_check(record, state, ["pytest", "-q"]))
        self.assertFalse(cost.reusable_check(record | {"status": "failed"}, state, ["pytest"]))
        self.assertFalse(
            cost.reusable_check(
                record | {"worktree_fingerprint_before": {"digest": "old"}}, state, ["pytest"]
            )
        )

    def test_reuse_requires_matching_execution_root_for_split_receipts(self):
        state = {"git_head": "abc", "worktree_fingerprint": {"digest": "same"}, "cwd": "/task/A"}
        record = {
            "status": "passed",
            "exit_code": 0,
            "command": ["pytest"],
            "git_head": "abc",
            "git_head_before": "abc",
            "worktree_fingerprint_before": state["worktree_fingerprint"],
            "worktree_fingerprint_after": state["worktree_fingerprint"],
            "execution_root": "/task/A",
        }
        self.assertTrue(cost.reusable_check(record, state, ["pytest"]))
        self.assertFalse(cost.reusable_check(record, state | {"cwd": "/task/B"}, ["pytest"]))
        self.assertFalse(
            cost.reusable_check(record | {"execution_root": "/task/B"}, state, ["pytest"])
        )

    def test_compact_handoff_and_unknown_telemetry(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory) / "repo"
            project.mkdir()

            def git(*args):
                return subprocess.run(
                    ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
                )

            git("init", "-q")
            git("config", "user.name", "Test")
            git("config", "user.email", "test@example.com")
            (project / "app.py").write_text("base\n")
            git("add", "app.py")
            git("commit", "-qm", "base")
            base_sha = git("rev-parse", "HEAD").stdout.strip()
            created = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/devcrew.py"),
                    "task-worktree",
                    "create",
                    "--worktree-request",
                    "Use a worktree for this task",
                    "--project",
                    str(project),
                    "--task-id",
                    "T1",
                    "--base-sha",
                    base_sha,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(created.returncode, 0, created.stderr)
            task_worktree = json.loads(created.stdout)["task_worktree_root"]
            receipt = context.devcrew_worktree.resolve(project, "T1")
            planner = phase_review.issue_planner_receipt(
                project, "T1", base_sha, "planner payload", "planner-T1"
            )
            manifest = {
                "schema_version": 1,
                "task_id": "T1",
                "task_mode": "SINGLE_PHASE",
                "semantic_contract": {"capabilities": ["ship app behavior"]},
                "worktree": {
                    "repository_id": receipt["repository_id"],
                    "task_worktree_root": receipt["task_worktree_root"],
                    "branch": receipt["branch"],
                    "base_sha": receipt["expected_head"],
                },
                "current_phase_id": "APP",
                "phases": [
                    {
                        "phase_id": "APP",
                        "goal": "Implement app behavior",
                        "depends_on": [],
                        "owned_scope": ["app.py"],
                        "acceptance": ["app behavior works"],
                        "verification": ["app tests pass"],
                        "commit_boundary": "App behavior capability",
                        "commit_subject": "type(T1): implement app behavior",
                        "status": "PLANNED",
                    }
                ],
            }
            manifest["planner_dispatch"] = {
                "spawn_receipt_id": planner["SPAWN_RECEIPT_ID"],
                "route_id": planner["ROUTE_ID"],
                "agent_id": planner["AGENT_ID"],
                "model": planner["RESOLVED_MODEL"],
                "effort": planner["REASONING_EFFORT"],
                "spawn_mode": planner["SPAWN_MODE"],
            }
            devcrew_phase.save_manifest(
                devcrew_phase.manifest_path(project, "T1"), manifest, receipt
            )
            handoff = context.prepare_handoff(
                {
                    "task_id": "T1",
                    "role": "executor",
                    "runtime": self.runtime()
                    | {
                        "explicit_model_effort": True,
                        "roles": {
                            "executor-agent": {"available": True, "supports_overrides": True}
                        },
                    },
                },
                {
                    "GOAL": "ship",
                    "RELEVANT_FILES": ["app.py"],
                    "PROJECT_ROOT": str(project),
                    "TASK_WORKTREE_ROOT": task_worktree,
                    "BASE_SHA": base_sha,
                    "OWNED_SCOPE": ["app.py"],
                    "ACCEPTANCE": ["ship works"],
                    "VERIFICATION": ["run app tests"],
                },
                {
                    "DONE": "RED",
                    "TESTS": "one test",
                    "NEXT_ACTION": "GREEN",
                    "CURRENT_STATE": "old verbose state",
                },
                "next-agent",
            )
        self.assertEqual(handoff["spawn"]["fork_turns"], "none")
        self.assertEqual(set(handoff["handoff"]), set(context.HANDOFF_FIELDS))
        self.assertNotIn("old verbose state", handoff["spawn"]["message"])
        report = cost.task_report(
            "T1",
            risk="low",
            executor_routes=[cost.route("executor", runtime=self.runtime())],
            reviewer_routes=[cost.route("reviewer", runtime=self.runtime())],
            targeted_test_runs=2,
            full_suite_runs=1,
        )
        self.assertEqual(report["token_telemetry"], "unavailable")
        self.assertEqual(report["strong_model_escalations"], 0)

    def test_log_summary_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            (root / "check.log").write_text("\n".join(str(n) for n in range(100)))
            record = root / "check.json"
            record.write_text(
                json.dumps(
                    {"command": ["test"], "exit_code": 1, "status": "failed", "log": "check.log"}
                )
            )
            summary = cost.check_summary(record, tail_lines=3)
            self.assertEqual(summary["failures"], ["97", "98", "99"])
            self.assertEqual(summary["summary"], "99")
            self.assertNotIn("0", summary["failures"])

    def test_cli_reads_project_mode(self):
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
                        "cost": {"cost_mode": "balanced"},
                    }
                )
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/devcrew.py"),
                    "cost-route",
                    "--project",
                    str(project),
                    "--role",
                    "reviewer",
                    "--risk",
                    "medium",
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["cost_mode"], "balanced")
            self.assertEqual(json.loads(result.stdout)["effort"], "medium")

    def test_reuse_check_validates_current_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            source = project / "tracked.txt"
            source.write_text("initial\n")
            command = [sys.executable, "-c", "print('ok')"]
            run = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/devcrew.py"),
                    "run-check",
                    "--project",
                    str(project),
                    "--name",
                    "suite",
                    "--",
                    *command,
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(run.returncode, 0, run.stderr)
            record = run.stdout.splitlines()[0]
            reuse = [
                sys.executable,
                str(ROOT / "scripts/devcrew.py"),
                "reuse-check",
                "--record",
                record,
                "--",
                *command,
            ]
            self.assertEqual(subprocess.run(reuse, capture_output=True).returncode, 0)
            source.write_text("changed\n")
            self.assertNotEqual(subprocess.run(reuse, capture_output=True).returncode, 0)


if __name__ == "__main__":
    unittest.main()
