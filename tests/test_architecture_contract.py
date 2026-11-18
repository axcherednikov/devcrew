import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
ARCHITECTURE = ROOT / "skills" / "devcrew-architecture" / "SKILL.md"
AUTOPILOT = ROOT / "skills" / "devcrew-autopilot" / "SKILL.md"
EVALS = ROOT / "tests" / "routing-evals.md"


class ArchitectureContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.architecture = ARCHITECTURE.read_text()
        cls.autopilot = AUTOPILOT.read_text()
        cls.evals = EVALS.read_text()

    def test_autopilot_routes_architecture_outcomes_and_not_adjacent_work(self):
        for phrase in (
            "architecture audit or assessment of an existing system selects",
            "only when architectural evidence and candidate directions are the primary outcome",
            "Do not add an architecture audit to ordinary implementation",
            "assess payment modularity/testability/refactoring opportunities",
            "why does payment sometimes double charge?",
            "add a payment provider",
            "design a new payment subsystem",
            "review this payment refactor diff",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.autopilot)

    def test_routing_eval_covers_required_architecture_intents(self):
        for case in (
            "architecture-explicit-audit",
            "architecture-modularity",
            "architecture-testability",
            "architecture-refactoring",
            "architecture-routing-boundaries",
        ):
            with self.subTest(case=case):
                self.assertIn(case, self.evals)

    def test_architecture_is_read_only_and_separate_from_adjacent_skills(self):
        self.assertIn("focused, read-only investigation", self.architecture)
        self.assertIn("does not change production code", self.architecture)
        for name in ("devcrew-review", "devcrew-diagnose", "devcrew-task", "devcrew-brief"):
            with self.subTest(name=name):
                self.assertIn(name, self.architecture)

    def test_candidates_require_evidence_and_calibrated_confidence(self):
        for phrase in (
            "Evidence:",
            "Current friction:",
            "Proposed direction:",
            "Expected leverage:",
            "Compatibility / migration risk",
            "Reversibility",
            "Confidence:",
            "Do not label an evidence-free candidate `strong`",
            "do not present them as established defects or recommendations",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.architecture)

    def test_adr_conflicts_and_speculative_abstractions_are_explicitly_bounded(self):
        self.assertIn("If evidence conflicts with an ADR, report the ADR", self.architecture)
        self.assertIn("Do not declare the ADR obsolete", self.architecture)
        self.assertIn("An abstraction justified only by hypothetical future variation is not a recommendation", self.architecture)
        self.assertIn("Do not recommend repositories, interfaces, factories, or adapters merely as a pattern", self.architecture)

    def test_candidate_handoff_preserves_uncertainty_and_routes_by_remaining_work(self):
        for field in (
            "ARCHITECTURE_CANDIDATE:", "GOAL:", "EVIDENCE:", "PROPOSED_DIRECTION:",
            "ACCEPTANCE_INTENT:", "RISKS:", "RELEVANT_FILES:", "OPEN_DECISIONS:",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.architecture)
                self.assertIn(field, self.autopilot)
        self.assertIn("Route clear requirements to `devcrew-task`", self.architecture)
        self.assertIn("Route substantial unresolved product or architecture requirements to `devcrew-brief`", self.architecture)
        self.assertIn("Route a request to prove the cause of a specific failure to `devcrew-diagnose`", self.architecture)

    def test_shared_policies_and_bounded_models_cost_routing_are_referenced(self):
        self.assertIn("shared [cost policy]", self.architecture)
        self.assertIn("[engineering policy]", self.architecture)
        self.assertIn("bounded/low", self.architecture)
        self.assertIn("bounded/medium", self.architecture)
        self.assertIn("Escalate to a stronger model only for a concrete evidence-based reason", self.architecture)


if __name__ == "__main__":
    unittest.main()
