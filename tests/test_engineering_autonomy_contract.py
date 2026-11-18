import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASK = ROOT / "skills/devcrew-task"
BRIEF = ROOT / "skills/devcrew-brief"


def read(path):
    return " ".join(path.read_text().split())


class EngineeringAutonomyContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = read(TASK / "references/engineering-policy.md")
        cls.task = read(TASK / "SKILL.md")
        cls.brief = read(BRIEF / "SKILL.md")
        cls.questions = read(BRIEF / "references/questions.md")
        cls.provenance = read(BRIEF / "references/provenance-check.md")
        cls.communication = read(ROOT / "skills/devcrew-small-talk/SKILL.md")

    def test_sufficient_facts_lead_to_an_internal_choice_preserving_original_behavior(self):
        for phrase in (
            "confirmed facts",
            "engineering conclusions",
            "unverified hypotheses",
            "research feasible options, choose an evidence-backed solution",
            "Do not delegate an internal implementation choice to the user",
            "confirmed original behavior is the compatibility baseline",
            "RAM-only locks do not restore confirmed persistence across reboot",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.policy)

    def test_insufficient_facts_lead_to_research_without_invented_evidence(self):
        self.assertIn("continue available targeted research", self.policy)
        self.assertIn("do not fill evidence gaps with invented facts", self.policy)
        self.assertIn("continue targeted research", self.task)
        self.assertNotIn("ambiguity detected → focused question OR brief", self.task)
        self.assertIn("continue the available targeted investigation", self.questions)

    def test_material_uncertainty_or_risk_stops_dependent_work_for_a_recommended_question(self):
        for phrase in (
            "unresolved requirements",
            "data preservation",
            "security",
            "significant irreversible consequences",
            "stop dependent work",
            "explain the concrete risk",
            "ask a specific question with a recommended option",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.policy)
        self.assertIn("significant unresolved risk", self.questions)
        self.assertIn("recommended option", self.questions)
        self.assertIn(
            "data preservation, security, or significant irreversible consequences",
            self.communication,
        )
        self.assertNotIn("only for a matter of product preference", self.communication)

    def test_planning_and_communication_do_not_promote_engineering_choices_to_user_requirements(
        self,
    ):
        self.assertIn("engineering-policy.md", self.brief)
        self.assertIn(
            "An engineering recommendation is not a confirmed user requirement", self.policy
        )
        self.assertIn("A recommendation remains a recommendation", self.questions)
        self.assertIn("engineering conclusion", self.provenance)
        self.assertIn("does not become a user requirement", self.provenance)
        self.assertIn("hypothesis", self.communication)
        adr = read(BRIEF / "references/adr.md")
        self.assertIn("Architectural significance alone does not require a user question", adr)
        self.assertIn("The project's ADR approval process determines acceptance", adr)
        self.assertNotIn("if it materially changes the product or the architecture", adr)
        template = read(BRIEF / "references/template-main.md")
        self.assertIn("the facts, the engineering conclusion, and its rationale", template)
        self.assertNotIn("Only immaterial, safe assumptions", template)
        self.assertNotIn(
            "security or architectural decisions are an open question for the user", self.provenance
        )
        self.assertNotIn("security or an architectural decision, do not guess", self.task)

    def test_autonomy_preserves_existing_safety_acceptance_and_role_boundaries(self):
        self.assertIn(
            "does not override safety, scope, role boundaries or the loop guard", self.policy
        )
        self.assertIn("Minimal Diff must never defer or remove", self.policy)
        self.assertIn("necessary data-loss prevention", self.policy)
        self.assertIn("do not guess or turn a guess into a test expectation", self.task)
        self.assertIn("User questions before unsupported assumptions", self.brief)
        self.assertIn("A function's existence does not establish its behavior", self.provenance)
        self.assertIn("Wait for the answer before implementing dependent behavior", self.provenance)
        replan = read(TASK / "references/replan-protocol.md")
        self.assertIn("cannot change acceptance criteria", replan)
        self.assertIn("do not dispatch the executor to choose", replan)
        guard = read(TASK / "references/loop-guard.md")
        self.assertIn(
            "It takes precedence over automatic local-fix, replan, escalation and retry rules",
            guard,
        )


if __name__ == "__main__":
    unittest.main()
