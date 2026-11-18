import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
POLICY = ROOT / "skills/devcrew-task/references/engineering-policy.md"
TASK = ROOT / "skills/devcrew-task/SKILL.md"
REVIEW = ROOT / "skills/devcrew-review/SKILL.md"
AUTOPILOT = ROOT / "skills/devcrew-autopilot/SKILL.md"


class MinimalDiffContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = POLICY.read_text()
        cls.task = TASK.read_text()
        cls.review = REVIEW.read_text()
        cls.autopilot = AUTOPILOT.read_text()

    def test_understanding_precedes_solution_selection(self):
        for requirement in (
            "trace the\naffected behavioral flow",
            "inspect likely change sites",
            "find analogous project\nprecedents",
            "understand affected layers/contracts",
            "separate confirmed\nrequirements from speculative improvements",
        ):
            with self.subTest(requirement=requirement):
                self.assertIn(requirement, self.policy)

    def test_ladder_prefers_existing_helper_and_standard_library(self):
        self.assertIn("Reuse an existing project helper", self.policy)
        self.assertIn("Use the language standard library instead of a wrapper", self.policy)

    def test_ladder_prefers_framework_database_and_direct_dependency_primitives(self):
        self.assertIn("Use the framework, platform, or database primitive", self.policy)
        self.assertIn("already-installed **direct** dependency", self.policy)
        self.assertIn("never rely on an incidental\n   transitive package", self.policy)

    def test_abstraction_needs_evidence_beyond_hypothetical_future(self):
        self.assertIn("One\nhypothetical future implementation is not evidence", self.policy)
        self.assertIn("Require evidence of a real boundary", self.policy)

    def test_protected_validation_security_and_acceptance_cannot_be_reduced(self):
        for obligation in (
            "trust-boundary, user-input, webhook or\nexternal-API validation",
            "security,\nauthentication, authorization or tenant isolation",
            "approved acceptance criteria",
            "required\naccessibility",
            "data integrity",
        ):
            with self.subTest(obligation=obligation):
                self.assertIn(obligation, self.policy)
        self.assertIn("do not mark a reduced-scope task done", self.policy)

    def test_marker_requires_ceiling_trigger_and_action_without_backlog(self):
        self.assertIn("`minimal-diff: <observable ceiling> — <verifiable trigger and\naction>`", self.policy)
        self.assertIn("does not create a backlog item automatically", self.policy)

    def test_minimal_diff_uses_existing_test_first_policy(self):
        self.assertIn("use the existing test-first policy below", self.policy)
        self.assertIn("Verification policy\nremains the source of truth", self.policy)
        self.assertIn("Do not create another TDD\nworkflow", self.policy)

    def test_root_seam_is_preferred_without_file_count_goal(self):
        self.assertIn("Prefer a correct root seam\nover duplicated caller patches", self.policy)
        self.assertIn("file count alone is not a goal", self.policy)

    def test_review_treats_minimal_diff_as_risk_not_file_count_gate(self):
        self.assertIn("never a file-count blocker", self.policy)
        self.assertIn("Pure excess may be a non-blocking recommendation", self.policy)
        self.assertIn("file-count blocker", self.review)

    def test_autopilot_has_no_separate_agent_or_stage(self):
        self.assertIn("do not add a Minimal Diff agent or workflow stage", self.autopilot)
        self.assertIn("shared [engineering policy]", self.autopilot)
        self.assertNotIn("minimal-diff-agent", self.autopilot)

    def test_task_points_to_owning_policy(self):
        self.assertIn("smallest complete solution", self.task)
        self.assertIn("shared [engineering policy]", self.task)

    def test_cost_policy_keeps_dynamic_routing_and_context_minimal(self):
        cost = (ROOT / "docs/cost-policy.md").read_text()
        self.assertIn("NORMAL / COMPLEX AGENT EXECUTION executor | bounded / medium |", cost)
        self.assertIn("Explicit mechanical bounded executor | bounded / low |", cost)
        self.assertIn("never escalate executor capability", cost)
        self.assertIn("If the required capability or effort is unavailable", cost)
        self.assertIn("not the run history, full brief", cost)

    def test_shared_routing_guidance_has_no_strong_executor_escalation(self):
        routing = (ROOT / "skills/devcrew-task/references/model-routing.md").read_text()
        autopilot = AUTOPILOT.read_text()
        for content in (routing, autopilot):
            self.assertIn("bounded capability at medium", content)
            self.assertIn("TASK_TOO_LARGE", content)
            self.assertIn("PLAN_INSUFFICIENT", content)
            self.assertIn("LOCAL_IMPLEMENTATION_DIFFICULTY", content)
            self.assertNotIn("complex implementation needs a concrete reason for stronger", content)


if __name__ == "__main__":
    unittest.main()
