import pathlib
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / "skills" / "devcrew-task" / "references" / "replan-protocol.md"


class ReplanContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.protocol = " ".join(PROTOCOL.read_text().split())
        cls.task = (ROOT / "skills/devcrew-task/SKILL.md").read_text()
        cls.autopilot = (ROOT / "skills/devcrew-autopilot/SKILL.md").read_text()
        cls.review = (ROOT / "skills/devcrew-review/SKILL.md").read_text()
        cls.brief = (ROOT / "skills/devcrew-brief/SKILL.md").read_text()

    def test_executor_replan_has_required_evidence_and_cannot_change_acceptance(self):
        for field in (
            "PLAN_ASSUMPTION",
            "OBSERVED_EVIDENCE",
            "CONFLICT",
            "PROPOSED_ALTERNATIVE",
            "AFFECTED_SCOPE",
            "UNAFFECTED_SCOPE",
            "ACCEPTANCE_IMPACT",
            "OWNER_DECISION_NEEDED",
            "EVIDENCE_REFS",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.protocol)
        self.assertIn("ACCEPTANCE_IMPACT: NO", self.protocol)
        self.assertIn("cannot change acceptance criteria", self.protocol)
        self.assertIn("REPLAN_REQUEST", self.task)

    def test_plan_ready_and_role_changes_end_at_coordinator_dispatch(self):
        strategy = (ROOT / "skills/devcrew-task/references/implementation-strategy.md").read_text()
        preflight = (ROOT / "skills/devcrew-task/references/dispatch-preflight.md").read_text()
        decision = (ROOT / "skills/devcrew-setup/assets/agents/decision-agent.toml").read_text()
        self.assertIn("PLAN_READY", self.protocol)
        self.assertIn("terminal receipt", self.protocol)
        self.assertIn("does not write production code", self.protocol)
        self.assertIn("new coordinator dispatch", strategy)
        self.assertIn("new coordinator dispatch", preflight)
        self.assertIn("new role-matched dispatch", decision)

    def test_difficulty_routes_without_changing_replan_receipt_semantics(self):
        for route in ("PLAN_INSUFFICIENT", "TASK_TOO_LARGE", "LOCAL_IMPLEMENTATION_DIFFICULTY"):
            self.assertIn(route, self.protocol)
        self.assertIn("bounded implementation slices", self.protocol)
        self.assertIn(
            "Independent first `LOCAL_FIX` findings can remain executor work within one bounded correction",
            self.protocol,
        )
        self.assertIn("A shared invalid seam is `SYSTEMIC_REPLAN`", self.protocol)
        self.assertIn(
            "recurrence following a correction is not by itself a loop or an owner question",
            self.protocol,
        )
        self.assertIn(
            "Further confirmed technical errors may be corrected autonomously", self.protocol
        )
        self.assertIn(
            "REPLAN_REQUEST` only if continuing requires a material technical decision",
            self.protocol,
        )

    def test_coordinator_routes_local_technical_and_owner_decisions(self):
        for route in ("LOCAL_FIX", "TECHNICAL_REPLAN", "OWNER_DECISION_REQUIRED"):
            self.assertIn(route, self.protocol)
        self.assertIn("does not require a user question", self.protocol)
        self.assertIn("to the owner", self.protocol)

    def test_internal_decision_agent_is_bounded_and_has_three_outcomes(self):
        template_path = ROOT / "skills/devcrew-setup/assets/agents/decision-agent.toml"
        role = tomllib.loads(template_path.read_text())
        self.assertEqual(role["name"], "decision-agent")
        self.assertIn("APPROVED_REPLAN", role["developer_instructions"])
        self.assertIn("KEEP_CURRENT_PLAN", role["developer_instructions"])
        self.assertIn("OWNER_DECISION_REQUIRED", role["developer_instructions"])
        self.assertIn("full transcript", role["developer_instructions"])

    def test_task_and_autopilot_have_replan_loop_without_acceptance_or_dag_advance(self):
        self.assertIn("devcrew-task` AGENT EXECUTION", self.protocol)
        self.assertIn("DIRECT path uses ordinary host reasoning", self.protocol)
        self.assertIn("does not create an artificial", self.protocol)
        for phrase in (
            "REPLAN_REQUEST",
            "APPROVED_REPLAN",
            "KEEP_CURRENT_PLAN",
            "DAG must not advance",
            "no commit",
        ):
            self.assertIn(phrase, self.autopilot)

    def test_review_classifies_root_cause_read_only_and_owner_questions_use_brief(self):
        for route in ("LOCAL_FIX", "SYSTEMIC_REPLAN", "OWNER_DECISION_REQUIRED"):
            self.assertIn(route, self.protocol)
        self.assertIn("root cause, not by finding count", self.protocol)
        self.assertIn("review is read-only", self.protocol.lower())
        self.assertIn("genuine requirements or product uncertainty", self.brief)
        self.assertIn("SYSTEMIC_REPLAN", self.review)

    def test_existing_safety_cost_and_sequential_execution_boundaries_remain(self):
        for invariant in (
            "safety rules",
            "confirmed owner/product decisions",
            "scope authorization",
            "no decision-agent call or extra context cost",
            "does not add parallel autopilot",
            "task fan-out",
            "worktree waves",
            "new general model-routing strategy",
        ):
            with self.subTest(invariant=invariant):
                self.assertIn(invariant, self.protocol)
        self.assertIn("one task at a time", self.autopilot)
        self.assertIn("decision-agent before executor work resumes", self.autopilot)

    def test_model_policy_keeps_systemic_replan_in_decision_layer(self):
        policy = (ROOT / "docs/cost-policy.md").read_text()
        self.assertIn("SYSTEMIC_REPLAN/TECHNICAL_REPLAN selects planner", policy)
        self.assertIn("Local fixes remain with executor", policy)
        self.assertIn("Risk alone", policy)

    def test_routing_eval_covers_requested_executor_review_and_planner_scenarios(self):
        evals = (ROOT / "tests/routing-evals.md").read_text()
        for case in (
            "replan-local-fix",
            "replan-invalid-assumption",
            "replan-owner-outcomes",
            "review-independent-findings",
            "review-wrong-seam",
            "replan-keep-plan",
            "replan-approved-alternative",
        ):
            self.assertIn(case, evals)


if __name__ == "__main__":
    unittest.main()
