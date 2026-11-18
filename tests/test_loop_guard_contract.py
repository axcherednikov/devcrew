from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LoopGuardContractTests(unittest.TestCase):
    def test_all_active_workflows_load_the_guard(self):
        for name in (
            "devcrew",
            "devcrew-task",
            "devcrew-autopilot",
            "devcrew-diagnose",
            "devcrew-review",
            "devcrew-skill-doctor",
            "devcrew-coordinate",
            "devcrew-brief",
            "devcrew-architecture",
        ):
            with self.subTest(skill=name):
                self.assertIn("loop-guard.md", (ROOT / "skills" / name / "SKILL.md").read_text())

    def test_stop_requires_question_and_preserves_history_without_false_red_trigger(self):
        policy = " ".join(
            (ROOT / "skills/devcrew-task/references/loop-guard.md").read_text().split()
        )
        for rule in (
            "A repeated FAIL alone is not a loop",
            "A → B → A",
            "discriminating evidence",
            "same unresolved work",
            "context compaction",
            "A technical blocker is not automatically an owner question",
            "Wait for the answer",
            "Silence",
            "bounded next action",
            "cost is proportionate",
            "Expected test-first RED",
            "planned reproduction samples",
            "--owner-decision",
            "--continuation FILE",
            "phase review-reconsider",
            "model's opinion is not proof",
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, policy)

    def test_typed_core_roles_stop_without_automatic_escalation(self):
        for name in ("executor-agent", "reviewer-agent", "decision-agent"):
            with self.subTest(role=name):
                contract = (
                    ROOT / "skills/devcrew-setup/assets/agents" / (name + ".toml")
                ).read_text()
                self.assertIn("STATUS: LOOP_DETECTED", contract)
                self.assertIn("ask the user only for a concrete owner choice", contract)
                self.assertIn("Repeated FAIL alone is not a loop", contract)

    def test_cost_policy_no_longer_escalates_failed_loops(self):
        policy = (ROOT / "docs/cost-policy.md").read_text()
        self.assertIn("mandatory loop guard overrides", policy)
        self.assertNotIn(
            "Two unsuccessful review/fix cycles permit an escalated fresh review", policy
        )
