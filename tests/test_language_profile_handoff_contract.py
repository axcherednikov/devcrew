import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class LanguageProfileHandoffContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.policy = (ROOT / "skills/devcrew-task/references/engineering-policy.md").read_text()
        cls.task = (ROOT / "skills/devcrew-task/SKILL.md").read_text()
        cls.brief = (ROOT / "skills/devcrew-brief/SKILL.md").read_text()
        cls.autopilot = (ROOT / "skills/devcrew-autopilot/SKILL.md").read_text()

    def test_php_profile_uses_version_evidence(self):
        self.assertIn("primary and affected languages", self.policy)
        self.assertIn("manifests, lock files", self.policy)
        self.assertIn("supported\nversion/range", self.policy)
        self.assertIn("evidence paths", self.policy)
        self.assertIn("LANGUAGE_PROFILE", self.autopilot)

    def test_polyglot_profile_is_limited_to_affected_languages(self):
        self.assertIn("affected languages", self.policy)
        self.assertIn(
            "Do not inventory unrelated parts of a polyglot repository",
            " ".join(self.policy.split()),
        )

    def test_supported_range_sets_minimum_compatibility(self):
        self.assertIn("target its minimum", self.policy)
        self.assertIn("newer than confirmed support", self.policy)

    def test_conflicting_authoritative_versions_are_a_blocker(self):
        self.assertIn("Conflicting authoritative evidence", self.policy)
        self.assertIn("is a blocker", self.policy)

    def test_executor_package_is_self_contained(self):
        for field in (
            "CONFIRMED CONTEXT",
            "IN SCOPE",
            "OUT OF SCOPE",
            "ACCEPTANCE",
            "RELEVANT ADR",
            "RELEVANT FILES",
            "REPOSITORY INSTRUCTIONS",
            "VERIFICATION STRATEGY",
            "SAFETY BOUNDARIES",
            "DELIVERABLES",
            "OPEN DECISIONS / BLOCKERS",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.autopilot)

    def test_executor_package_excludes_conversation_history(self):
        self.assertIn("Do not include conversation history, chain of thought", self.autopilot)

    def test_unknown_commands_must_not_be_invented(self):
        self.assertIn("instead of inventing it", self.policy)

    def test_verified_paths_and_commands_can_be_handed_off(self):
        self.assertIn("verify it from repository evidence", self.policy)
        self.assertIn("BASELINE/EVIDENCE PATHS", self.autopilot)

    def test_pure_docs_can_omit_language_profile(self):
        self.assertIn("Omit the profile", self.policy)
        self.assertIn("only when the task needs it", self.autopilot)

    def test_brief_records_only_material_version_constraints(self):
        self.assertIn("materially depends on language/runtime compatibility", self.brief)
        self.assertIn("Omit version inventory when it does not affect the decision", self.brief)

    def test_no_mandatory_implementation_prompt_artifact(self):
        self.assertIn("mandatory implementation-prompt file", self.autopilot)

    def test_existing_task_artifacts_remain_source_of_truth(self):
        self.assertIn("Existing task/handoff artifacts remain the source of truth", self.autopilot)

    def test_cost_policy_keeps_dynamic_routing_and_context_minimal(self):
        cost = (ROOT / "docs/cost-policy.md").read_text()
        self.assertIn("`economy` remains the default", cost)
        self.assertIn("NORMAL / COMPLEX AGENT EXECUTION executor | bounded / medium |", cost)
        self.assertIn("not the run history, full brief", cost)
        self.assertIn("unrelated ADRs or logs", cost)


if __name__ == "__main__":
    unittest.main()
