from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / "skills/devcrew-task/references/task-readiness.md"
ENTRYPOINTS = (
    "skills/devcrew/SKILL.md",
    "skills/devcrew-task/SKILL.md",
    "skills/devcrew-autopilot/SKILL.md",
    "skills/devcrew-brief/SKILL.md",
    "skills/devcrew-review/SKILL.md",
    "skills/devcrew-diagnose/SKILL.md",
    "skills/devcrew-architecture/SKILL.md",
    "skills/devcrew-product-onboard/SKILL.md",
    "skills/devcrew-handoff/SKILL.md",
    "skills/devcrew-session/SKILL.md",
    "skills/devcrew-coordinate/SKILL.md",
    "skills/devcrew-setup/SKILL.md",
    "skills/devcrew-skill-doctor/SKILL.md",
)


class TaskReadinessContract(unittest.TestCase):
    def test_shared_rule_covers_outcome_scope_checks_uncertainty_and_source_routing(self):
        text = SHARED.read_text()
        for expected in (
            "requested outcome", "scope and behavior", "observable checks",
            "material unresolved decisions", "Do not demand absolute certainty",
            "Silence is not approval", "Diagnosis, review, research and planning remain read-only",
            "phase source-bind --source-spec FILE", "ordinary-source-gap FILE",
            "loop guard", "adjacent finding",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, text)

    def test_router_and_every_direct_substantive_entrypoint_reference_shared_rule(self):
        for relative in ENTRYPOINTS:
            with self.subTest(path=relative):
                self.assertIn("task-readiness.md", (ROOT / relative).read_text())

    def test_router_remains_thin_and_owner_workflow_owns_research(self):
        router = (ROOT / "skills/devcrew/SKILL.md").read_text()
        self.assertIn("thin router", router)
        self.assertIn("selected owning workflow", router)
        readiness = SHARED.read_text()
        self.assertIn("The owning workflow does the research", readiness)
        self.assertIn("without waiting for ceremonial confirmation", readiness)

    def test_public_and_direct_routes_treat_authorized_local_attachments_consistently(self):
        router = (ROOT / "skills/devcrew/SKILL.md").read_text()
        direct = (ROOT / "skills/devcrew-task/SKILL.md").read_text()
        for text in (router, direct):
            self.assertIn("authorized local attachment", text)
            self.assertIn("ordinary-source-gap FILE", text)
            self.assertIn("exact", text)
            self.assertIn("attachment", text)
        self.assertNotIn("not a source-bind fallback", router)
        self.assertNotIn("not a source-bind fallback", direct)

    def test_ordinary_requires_exact_read_and_source_rule_has_authorization_question(self):
        direct = (ROOT / "skills/devcrew-task/SKILL.md").read_text()
        ordinary = direct.split("### Ordinary current-chat tasks", 1)[1].split("## ", 1)[0]
        self.assertIn("exact complete message bytes", ordinary)
        self.assertIn("every attached source", ordinary)
        self.assertIn("Verify the message and attachment digests", ordinary)
        self.assertIn("descriptor alone never establishes readability", ordinary)

        source_rule = SHARED.read_text().split("## Resolve the original source before dispatch", 1)[1]
        self.assertIn("If the exact local", source_rule)
        self.assertIn("binding is not yet authorized, ask one", source_rule)
        self.assertIn("whether to authorize binding that exact file", source_rule)
        self.assertIn("route can read the exact bytes", source_rule)
        self.assertIn("report the missing reader/access", source_rule)

    def test_read_only_skills_retain_their_authority_boundary(self):
        for relative, phrase in (
            ("skills/devcrew-review/SKILL.md", "review remains read-only"),
            ("skills/devcrew-diagnose/SKILL.md", "diagnosis remains read-only"),
            ("skills/devcrew-architecture/SKILL.md", "analysis remains read-only"),
        ):
            with self.subTest(path=relative):
                self.assertIn(phrase, (ROOT / relative).read_text())


if __name__ == "__main__":
    unittest.main()
