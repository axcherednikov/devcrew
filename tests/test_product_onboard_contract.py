import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "devcrew-product-onboard"
SKILL = SKILL_DIR / "SKILL.md"
README = ROOT / "README.md"
TASK = ROOT / "skills/devcrew-task/SKILL.md"
ARCHITECTURE = ROOT / "skills/devcrew-architecture/SKILL.md"
BRIEF = ROOT / "skills/devcrew-brief/SKILL.md"
SESSION = ROOT / "skills/devcrew-session/SKILL.md"
AUTOPILOT = ROOT / "skills/devcrew-autopilot/SKILL.md"
EVALS = ROOT / "tests/routing-evals.md"


class ProductOnboardContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = SKILL.read_text()
        cls.readme = README.read_text()
        cls.task = TASK.read_text()
        cls.architecture = ARCHITECTURE.read_text()
        cls.brief = BRIEF.read_text()
        cls.session = SESSION.read_text()
        cls.autopilot = AUTOPILOT.read_text()
        cls.evals = EVALS.read_text()

    def test_skill_is_packaged_and_frontmatter_matches(self):
        self.assertTrue(SKILL.is_file())
        self.assertTrue((SKILL_DIR / "agents/openai.yaml").is_file())
        self.assertIn("name: devcrew-product-onboard\n", self.skill)

    def test_explicit_existing_project_target_and_greenfield_boundary(self):
        self.assertIn("explicitly asks to understand an existing product", self.skill)
        self.assertIn("existing, initialized DevCrew target", self.skill)
        self.assertIn("Do not use this for greenfield projects", self.skill)
        self.assertIn("Never run automatically after setup", self.skill)

    def test_lenses_and_evidence_classification_are_complete(self):
        for lens in ("Product map and user flows", "Actors, permissions and visibility",
                     "Domain glossary", "Product signals", "Product materials"):
            with self.subTest(lens=lens):
                self.assertIn(lens, self.skill)
        for status in ("CONFIRMED", "INFERRED", "UNKNOWN"):
            with self.subTest(status=status):
                self.assertIn(f"**{status}**", self.skill)
        self.assertIn("Every durable observed product claim carries a precise source", self.skill)
        self.assertIn("Product decisions belong to their existing owner mechanism", self.skill)

    def test_absent_analytics_is_white_spot_not_invented_metric(self):
        self.assertIn("If analytics is absent, record a white spot; never invent metrics.", self.skill)
        self.assertIn("An absent analytics substrate", self.skill)

    def test_memory_is_configured_indexed_and_separate_from_engineering_memory(self):
        for phrase in ("memory directory configured by `.codex/devcrew.json`",
                       "existing memory index/map", "Do not assume the directory is literally `memory/`",
                       "Keep product context separate from engineering conventions",
                       "Do not append a second copy on rerun"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)
        for section in ("Product map / flows", "Actors / permissions / visibility",
                        "Domain glossary", "Signals / analytics / flags / plans",
                        "Unknowns / assumptions"):
            with self.subTest(section=section):
                self.assertIn(f"`{section}`", self.skill)

    def test_readers_are_read_only_and_minimum_sufficient_bounded_models(self):
        self.assertIn("minimum sufficient read-only researcher(s)", self.skill)
        self.assertIn("Default researchers to a bounded model at low/medium effort", self.skill)
        self.assertIn("read-only", self.skill)
        self.assertIn("No model or high effort is mandatory", self.skill)
        self.assertNotRegex(self.skill, r"(?i)always (?:use|spawn) (?:four|five|4|5)")

    def test_material_fanout_requires_scope_approval_only(self):
        self.assertIn("substantial fan-out", self.skill)
        self.assertIn("wait for approval before that material cost", self.skill)
        self.assertIn("Ordinary bounded read-only inspection needs no approval ceremony", self.skill)

    def test_downstream_context_is_selective_and_integrated(self):
        for consumer in ("devcrew-brief", "devcrew-task", "devcrew-review",
                         "devcrew-architecture", "devcrew-autopilot", "devcrew-session"):
            with self.subTest(consumer=consumer):
                self.assertIn(consumer, self.skill)
        self.assertIn("only matching sections", self.skill)
        self.assertIn("payment changes need the payment flow, relevant actors and glossary", self.skill)
        self.assertIn("an internal refactor with no product behavior needs no product context", self.skill)
        self.assertIn("load only sections relevant to the task's user-visible behavior", self.task)
        self.assertIn("`devcrew-brief` uses relevant product facts as evidence", self.skill)

    def test_no_automatic_autopilot_or_session_fact_promotion(self):
        self.assertIn("never invokes this skill automatically", self.skill)
        self.assertNotIn("devcrew-product-onboard", self.autopilot)
        self.assertIn("does not promote observations to product facts", self.skill)
        self.assertIn("does not automatically require a session workflow", self.session)

    def test_refresh_revalidates_sources_and_preserves_decision_owner(self):
        for phrase in ("revalidate each old source and claim", "remove claims no longer supported",
                       "Never let an inference overwrite an accepted decision",
                       "missing or moved evidence makes the claim stale"):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, self.skill)

    def test_routing_examples_separate_onboard_brief_and_adjacent_skills(self):
        for case in ("product-onboard-explicit", "product-onboard-vs-brief",
                     "product-onboard-no-autostart", "product-onboard-existing-and-greenfield",
                     "product-onboard-route-overlap"):
            with self.subTest(case=case):
                self.assertIn(case, self.evals)
        self.assertIn("devcrew-product-onboard", self.readme)
        self.assertIn("devcrew-product-onboard", self.architecture)
        self.assertIn("devcrew-setup", self.skill)

    def test_unrelated_dirty_worktree_is_preserved(self):
        self.assertIn("Preserve unrelated worktree changes", self.skill)
        self.assertIn("Do not stage or commit", self.skill)
        self.assertIn("or modify production files", self.skill)


if __name__ == "__main__":
    unittest.main()
