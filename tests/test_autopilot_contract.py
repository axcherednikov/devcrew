import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
AUTOPILOT = ROOT / "skills" / "devcrew-autopilot" / "SKILL.md"


class AutopilotContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = AUTOPILOT.read_text()

    def test_requires_real_subagent_execution_and_a_hard_stop_without_it(self):
        self.assertIn("REAL SUBAGENT EXECUTION NOT AVAILABLE", self.text)
        self.assertIn("spawn_agent", self.text)
        self.assertIn("Do not simulate delegation", self.text)

    def test_executor_and_reviewer_are_separate_fresh_agent_contexts(self):
        self.assertIn("executor-agent", self.text)
        self.assertIn("separate fresh agent context", self.text)
        self.assertIn("must not review its own work", self.text)

    def test_defines_bounded_executor_and_reviewer_payloads(self):
        for field in (
            "TASK ID",
            "GOAL",
            "IN SCOPE",
            "OUT OF SCOPE",
            "ACCEPTANCE",
            "DEPENDENCIES",
            "RELEVANT FILES",
            "VERIFICATION STRATEGY",
            "FILES_CHANGED",
            "ACCEPTANCE_RESULTS",
            "DIFF_SUMMARY",
            "BLOCKING_FINDINGS",
            "NON_BLOCKING_FINDINGS",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.text)

    def test_progressive_context_and_cost_routing_are_explicit(self):
        for level in ("Level 1", "Level 2", "Level 3", "Level 4"):
            with self.subTest(level=level):
                self.assertIn(level, self.text)
        self.assertIn("capability/cost class", self.text)
        self.assertIn("Do not hardcode model names", self.text)

    def test_dynamic_routing_records_independent_axes_and_explicit_spawn(self):
        for field in ("COORDINATION_COMPLEXITY", "DECISION_COMPLEXITY", "IMPLEMENTATION_COMPLEXITY",
                      "REVIEW_COMPLEXITY", "RISK", "RESOLVED_MODEL", "REASONING_EFFORT", "SELECTION_REASON"):
            self.assertIn(field, self.text)
        self.assertIn("explicit `model` and `reasoning_effort`", self.text)
        self.assertIn("exact role contract", self.text)
        self.assertIn("Risk, task size, retries and repeated failures never escalate executor capability", self.text)
        self.assertIn("bounded capability at medium for NORMAL/COMPLEX work", self.text)
        self.assertIn("SYSTEMIC_REPLAN routes back to planner", self.text)
        self.assertNotIn("preserve role defaults and report the limitation", self.text)

    def test_economy_path_keeps_independent_review_and_no_llm_verifier(self):
        self.assertIn("Economy is the autopilot default", self.text)
        self.assertIn('fork_turns: "none"', self.text)
        self.assertIn("Do not dispatch a reasoning model merely to wait", self.text)
        self.assertIn("send only the blocking findings", self.text)
        self.assertIn("If the corrected candidate fails again, assess evidence, the bounded next action and cost", self.text)
        self.assertIn("Stop repeated actions without discriminating evidence or oscillation", self.text)
        self.assertIn("A PASS on the unchanged captured HEAD and fingerprint remains valid", self.text)


if __name__ == "__main__":
    unittest.main()
