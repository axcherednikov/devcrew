import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCTOR = ROOT / "skills/devcrew-skill-doctor/SKILL.md"
AUTOPILOT = ROOT / "skills/devcrew-autopilot/SKILL.md"
README = ROOT / "README.md"
EVALS = ROOT / "tests/routing-evals.md"
METADATA = ROOT / "skills/devcrew-skill-doctor/agents/openai.yaml"


class SkillDoctorContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.doctor = DOCTOR.read_text()
        cls.autopilot = AUTOPILOT.read_text()
        cls.evals = EVALS.read_text()

    def test_explicit_skill_maintenance_routes_to_doctor(self):
        for phrase in (
            "audit",
            "troubleshoot",
            "optimize",
            "repair",
            "migrate",
            "devcrew-skill-doctor",
            "devcrew-task and diagnose conflict",
            "Optimize routing of my skills",
        ):
            self.assertIn(phrase, self.doctor + self.autopilot)

    def test_scope_boundaries_route_application_work_elsewhere(self):
        for skill in (
            "devcrew-review",
            "devcrew-architecture",
            "devcrew-diagnose",
            "devcrew-task",
            "devcrew-setup",
        ):
            self.assertIn(skill, self.doctor)
        for case in (
            "skill-negative-boundaries",
            "skill-audit",
            "skill-routing-conflict",
            "skill-stale-helper",
            "skill-duplicated-policy",
        ):
            self.assertIn(case, self.evals)

    def test_audit_is_read_only_and_repair_requires_explicit_intent(self):
        self.assertIn("read-only audit", self.doctor)
        self.assertIn("authorizes the smallest necessary local repairs", self.doctor)
        self.assertIn("Do not ask for permission again", self.doctor)
        self.assertIn("skill-audit-read-only", self.evals)
        self.assertIn("skill-repair-authorized", self.evals)

    def test_evidence_classification_prevents_false_proven_findings(self):
        for label in ("PROVEN DEFECT", "STATIC FINDING", "UNTESTED HYPOTHESIS", "PREFERENCE"):
            self.assertIn(label, self.doctor)
        self.assertIn("A proven defect needs direct evidence", self.doctor)

    def test_model_incident_does_not_add_duplicate_rule(self):
        self.assertIn("do not add a duplicate rule", self.doctor)
        self.assertIn("instruction, routing, helper, or model behavior", self.doctor)
        self.assertIn("skill-correct-rule-model-incident", self.evals)

    def test_cache_boundary_and_verification_are_explicit(self):
        self.assertIn("Never edit an installed plugin cache as source", self.doctor)
        self.assertIn("package validation", self.doctor)
        self.assertIn("git diff --check", self.doctor)
        self.assertIn("skill-cache-not-source", self.evals)
        self.assertIn("skill-post-repair-verification", self.evals)

    def test_bounded_models_and_no_automatic_live_eval(self):
        self.assertIn("Prefer a bounded model", self.doctor)
        self.assertIn("bounded/low", self.doctor)
        self.assertIn("bounded/medium", self.doctor)
        self.assertIn("Do not start live/model evaluations automatically", self.doctor)
        self.assertIn("skill-cost-routing", self.evals)

    def test_stale_helpers_and_unproven_findings_have_scenarios(self):
        self.assertIn("skill-stale-helper", self.evals)
        self.assertIn("skill-unproven-finding", self.evals)

    def test_not_an_automatic_autopilot_stage_and_catalogued(self):
        self.assertIn("never an automatic post-task or autopilot stage", self.autopilot)
        self.assertIn("devcrew-skill-doctor", README.read_text())

    def test_skill_identity_and_ui_metadata_match(self):
        self.assertTrue(self.doctor.startswith("---\nname: devcrew-skill-doctor\n"))
        metadata = METADATA.read_text()
        self.assertIn('display_name: "DevCrew — Skill Doctor"', metadata)
        self.assertIn("$devcrew-skill-doctor", metadata)


if __name__ == "__main__":
    unittest.main()
