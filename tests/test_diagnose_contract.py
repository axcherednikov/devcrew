import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
DIAGNOSE = ROOT / "skills" / "devcrew-diagnose" / "SKILL.md"
TASK = ROOT / "skills" / "devcrew-task" / "SKILL.md"
REVIEW = ROOT / "skills" / "devcrew-review" / "SKILL.md"
AUTOPILOT = ROOT / "skills" / "devcrew-autopilot" / "SKILL.md"


class DiagnoseContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.diagnose = DIAGNOSE.read_text()
        cls.task = TASK.read_text()
        cls.review = REVIEW.read_text()
        cls.autopilot = AUTOPILOT.read_text()

    def test_diagnose_is_separate_investigation_and_task_owns_requested_fix(self):
        self.assertIn("primary outcome is an evidence-backed root cause, not a production fix", self.diagnose)
        self.assertIn("If the user asks for a fix and diagnosis is part of completing it, use", self.diagnose)
        self.assertIn("ordinary diagnosis is part of this workflow", self.task)
        self.assertIn("test-first → fix → verification", self.task)

    def test_autopilot_routes_by_outcome_not_cause_keywords(self):
        for phrase in (
            "A requested fix belongs to",
            "an unknown cause when investigation is part of delivering the fix",
            "A request to find and prove root cause without fixing selects",
            "hard-to-reproduce or intermittent failures",
            "performance investigations",
            "A request to review a completed fix selects",
            "Never make diagnosis a mandatory stage of every bugfix",
            "Do not route solely by words such as",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.autopilot)

    def test_diagnosis_returns_compact_implementation_handoff(self):
        for field in (
            "IMPLEMENTATION_REQUIRED",
            "ROOT_CAUSE:",
            "CLASSIFICATION:",
            "EVIDENCE:",
            "MINIMIZED_REPRODUCTION:",
            "REPRODUCTION_RATE:",
            "BASELINE:",
            "REGRESSION_TEST_RECOMMENDATION:",
            "FIX_ACCEPTANCE_CRITERIA:",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.diagnose)
                self.assertIn(field, self.autopilot)
        self.assertIn("route the compact report to the normal", self.diagnose)
        self.assertIn("do not fix within `devcrew-diagnose`", self.autopilot)

    def test_diagnose_never_implements_production_fix_and_review_stays_separate(self):
        self.assertIn("Do not change production implementation, apply a fix", self.diagnose)
        self.assertIn("does not write production code", self.diagnose)
        self.assertIn("review a completed fix", self.diagnose)
        self.assertIn("fixes are a separate step", self.review)

    def test_evidence_rules_prevent_suspicion_from_becoming_root_cause(self):
        for phrase in (
            "at most 3–5 active, falsifiable hypotheses",
            "Suspicious code, temporal correlation, or a plausible mechanism alone is not causal evidence",
            "discriminating check connects the cause to the observed failure",
            "record attempts and reproduction rate",
            "capture a comparable baseline before",
            "If a second consecutive probe cycle produces no new discriminating evidence",
            "return `LOOP_DETECTED`, stop probing and report the technical blocker",
            "ask the user only for a concrete owner choice",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.diagnose)

    def test_shared_policies_and_economy_routing_are_referenced(self):
        self.assertIn("engineering policy", self.diagnose)
        self.assertIn("cost policy", self.diagnose)
        self.assertIn("prefer a bounded model", self.diagnose)
        self.assertIn("deterministic tools", self.diagnose)
        self.assertIn("compact context", self.diagnose)
        self.assertIn("shared [context handoff policy]", self.diagnose)


if __name__ == "__main__":
    unittest.main()
