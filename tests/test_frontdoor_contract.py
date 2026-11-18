import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills" / "devcrew"


class FrontDoorContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (SKILLS / "SKILL.md").read_text()
        cls.routing = (SKILLS / "references/routing.md").read_text()
        cls.sources = (SKILLS / "references/source-resolution.md").read_text()
        cls.handoff = (SKILLS / "references/handoff.md").read_text()
        cls.evals = (ROOT / "tests/routing-evals.md").read_text()

    def test_public_skill_is_packaged_and_specialized_skills_remain(self):
        self.assertTrue((SKILLS / "agents/openai.yaml").is_file())
        self.assertTrue(self.skill.startswith("---\nname: devcrew\n"))
        for name in (
            "devcrew-task",
            "devcrew-autopilot",
            "devcrew-brief",
            "devcrew-review",
            "devcrew-diagnose",
            "devcrew-architecture",
            "devcrew-product-onboard",
            "devcrew-session",
            "devcrew-coordinate",
            "devcrew-setup",
            "devcrew-skill-doctor",
            "devcrew-small-talk",
            "devcrew-handoff",
        ):
            with self.subTest(skill=name):
                self.assertTrue((ROOT / "skills" / name / "SKILL.md").is_file())
                self.assertIn(name, self.routing)

    def test_router_is_thin_and_runtime_honest(self):
        self.assertIn("thin router", self.skill)
        self.assertIn("not as an implementation workflow", self.skill)
        self.assertIn("RUNTIME_CAPABILITY_MISSING", self.skill)
        self.assertIn("LARGE", self.routing)
        self.assertIn("Prompt length and risk alone do not justify autopilot", self.routing)
        self.assertIn("bounded model at low effort", self.routing)
        for intent in (
            "IMPLEMENT",
            "DIAGNOSE",
            "REVIEW",
            "HANDOFF",
            "ARCHITECTURE",
            "BRIEF",
            "PRODUCT_ONBOARD",
            "SESSION_CONTROL",
            "READ_ONLY_ANALYSIS",
        ):
            self.assertIn(f"`{intent}`", self.routing)
        self.assertIn("POLICY_CANDIDATE", self.routing)

    def test_source_resolution_is_provider_neutral_and_precedes_dispatch(self):
        self.assertIn("Before deciding the workflow", self.sources)
        self.assertIn("MCP/connectors/integrations", self.sources)
        self.assertIn("do not hardcode a tracker", self.sources)
        self.assertIn("A reference alone never determines intent", self.sources)
        self.assertIn("SOURCE_UNAVAILABLE", self.sources)
        self.assertIn("WORKSPACE_AMBIGUOUS", self.sources)

    def test_handoff_is_bounded_and_prevents_loops(self):
        for field in (
            "ORIGINAL_INTENT",
            "SOURCE_REFERENCE",
            "ACCEPTANCE_CRITERIA",
            "RELEVANT_EVIDENCE_REFERENCES",
        ):
            self.assertIn(field, self.handoff)
        self.assertIn("full transcript", self.handoff)
        self.assertIn("router → owner → router → owner", self.handoff)
        self.assertIn("Small implementation stays", self.handoff)

    def test_default_prompt_and_readme_use_public_entrypoint(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertIn("$devcrew", manifest["interface"]["defaultPrompt"][0])
        self.assertIn("$devcrew", (ROOT / "README.md").read_text())

    def test_routing_evals_cover_positive_and_negative_cases(self):
        for case in (
            "router-tracker-implement",
            "router-tracker-status",
            "router-fix-unknown-cause",
            "router-root-cause-only",
            "router-existing-pr-review",
            "router-architecture",
            "router-bounded-task",
            "router-coordinated-epic",
            "router-brief-unresolved",
            "router-grill",
            "router-product-onboard",
            "router-simple-read-only",
            "router-explicit-override",
            "router-source-unavailable",
            "router-outcome-tricky",
            "router-session-resume",
            "router-design-plus-implementation",
            "router-handoff-qa",
            "router-handoff-client",
            "router-handoff-customer",
            "router-handoff-all",
            "router-implement-then-handoff",
        ):
            with self.subTest(case=case):
                self.assertIn(case, self.evals)


if __name__ == "__main__":
    unittest.main()
