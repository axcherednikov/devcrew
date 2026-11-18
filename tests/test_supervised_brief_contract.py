import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class SupervisedBriefContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.brief = (ROOT / "skills/devcrew-brief/SKILL.md").read_text()
        cls.supervision = (ROOT / "skills/devcrew-brief/references/supervised-execution.md").read_text()
        cls.coordinate = (ROOT / "skills/devcrew-coordinate/SKILL.md").read_text()
        cls.procedure = (ROOT / "skills/devcrew-coordinate/references/procedure.md").read_text()
        cls.inventory = (ROOT / "skills/devcrew-coordinate/references/inventory.md").read_text()
        cls.session = (ROOT / "skills/devcrew-session/SKILL.md").read_text()
        cls.snapshot = (ROOT / "skills/devcrew-task/references/session-snapshot.md").read_text()
        cls.adr = (ROOT / "docs/brief-supervision-adr.md").read_text()
        cls.routing = (ROOT / "tests/routing-evals.md").read_text()

    def test_approval_precedes_any_create_or_execution(self):
        self.assertIn("Until that approval exists", self.brief)
        self.assertIn("do not create a thread, dispatch an executor, or start implementation", self.brief)
        self.assertIn("Agent messages are not human approval", self.brief)

    def test_model_and_material_decisions_gate_full_execution_brief(self):
        self.assertIn("Before starting any full brief", self.supervision)
        self.assertIn("including a read-only brief", self.supervision)
        self.assertIn("ask the owner to choose", self.supervision)
        self.assertIn("the model or increase cost automatically", self.supervision)
        self.assertIn("approval for child-chat creation remains a separate gate", self.supervision)
        self.assertIn("Resolve material owner decisions that affect child scope or permissions", self.supervision)
        self.assertIn("full briefs routed from `devcrew-task`", self.supervision)

    def test_parent_owns_only_supervised_child_inventory_and_not_preapproval_dispatch(self):
        self.assertIn("sole writer of its configured sessions Markdown", self.supervision)
        self.assertIn("must not write child-owned files or task cards", self.supervision)
        self.assertIn("Never dispatch an executor from the parent before approval", self.supervision)
        self.assertIn("Before creating the supervised child, confirm all material owner decisions are resolved", self.coordinate)

    def test_native_tool_contract_and_project_identity_are_explicit(self):
        for name in ("list_projects", "create_thread", "list_threads", "read_thread", "send_message_to_thread", "wait_threads"):
            with self.subTest(tool=name):
                self.assertIn(name, self.supervision)
        self.assertIn("project's real ID", self.supervision)
        self.assertIn("branch", self.supervision)
        self.assertIn("active writer", self.supervision)
        self.assertIn("Do not substitute an internal subagent", self.supervision)

    def test_ledger_precedes_effect_and_tracks_identity(self):
        self.assertIn("Before `create_thread`", self.supervision)
        self.assertIn("stable action", self.supervision)
        self.assertIn("actual create receipt", self.inventory)
        self.assertIn("clientThreadId", self.inventory)

    def test_uncertain_transport_requires_reconciliation(self):
        self.assertIn("does not guarantee idempotency", self.supervision)
        self.assertIn("same action identity", self.supervision)
        self.assertIn("do not create a\nsecond child", self.supervision)
        self.assertIn("native state", self.snapshot)
        self.assertIn("recent messages", self.session)

    def test_parent_assesses_artifacts_and_keeps_acceptance_separate(self):
        self.assertIn("actual diff/artifacts", self.supervision)
        self.assertIn("acceptance status", self.supervision)
        self.assertIn("independent review", self.supervision)
        self.assertIn("An in-scope correction may be sent", self.supervision)

    def test_adr_records_reused_native_approach_and_limits(self):
        self.assertIn("Markdown inventory/action ledger", self.adr)
        self.assertIn("Python bridge, daemon, or new persistent schema", self.adr)
        self.assertIn("exactly-once", self.adr)

    def test_public_guides_cover_approval_and_recovery_in_both_languages(self):
        readme = (ROOT / "README.md").read_text()
        cases = {
            "en": ("fresh project session", "explicitly approve", "Without approval", "reconciling the existing chat"),
            "ru": ("новую сессию проекта", "явного одобрения", "Без одобрения", "сверьте существующий чат"),
        }
        for language, phrases in cases.items():
            path = f"docs/{language}/workflows.md"
            with self.subTest(language=language):
                self.assertIn(path, readme)
                guide = (ROOT / path).read_text()
                for phrase in phrases:
                    self.assertIn(phrase, guide)

    def test_supervised_regression_scenarios_have_oracles(self):
        section = self.routing.split("## Full-brief supervised visible-chat cases", 1)[1].split("## Coordinator vs agent execution policy cases", 1)[0]
        cases = (
            "brief-supervision-clear-task", "brief-supervision-accessible-answer",
            "brief-supervision-single-question", "brief-supervision-material-brief",
            "brief-supervision-no-approval", "brief-supervision-approved-create",
            "brief-supervision-bounded-correction", "brief-supervision-scope-change",
            "brief-supervision-adjacent-finding", "brief-supervision-direct-skill",
            "brief-supervision-resume-confirmed", "brief-supervision-tool-unavailable",
            "brief-supervision-model-boundary", "brief-supervision-readonly-loop",
            "brief-supervision-local-attachment", "brief-supervision-attachment-no-retry-loop",
            "brief-supervision-model-not-chosen", "brief-supervision-task-routed-material-uncertainty",
            "brief-supervision-model-readonly",
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertIn(f"| {case} |", section)
        self.assertEqual(section.count("Static ") + section.count("Live-host evidence"), 19)


if __name__ == "__main__":
    unittest.main()
