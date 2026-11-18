"""Check that published workflow guidance preserves the original-intent gate."""

import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class OriginalIntentDocumentationContract(unittest.TestCase):
    def setUp(self):
        self.task = (ROOT / "skills/devcrew-task/SKILL.md").read_text()
        self.review = (ROOT / "skills/devcrew-review/SKILL.md").read_text()
        self.session = (ROOT / "skills/devcrew-session/SKILL.md").read_text()
        self.runtime = (ROOT / "docs/runtime-contract.md").read_text()
        self.workflows = (ROOT / "docs/en/workflows.md").read_text()
        self.decision = (
            ROOT / "skills/devcrew-setup/assets/agents/decision-agent.toml"
        ).read_text()
        self.executor = (
            ROOT / "skills/devcrew-setup/assets/agents/executor-agent.toml"
        ).read_text()
        self.reviewer = (
            ROOT / "skills/devcrew-setup/assets/agents/reviewer-agent.toml"
        ).read_text()
        self.evaluation = (ROOT / "evaluation/original-intent.md").read_text()

    def test_capture_and_planning_guidance_keeps_source_provenance_and_authority_separate(self):
        for text in (self.task, self.runtime, self.workflows):
            with self.subTest(source=text[:40]):
                self.assertIn("phase source-bind", text)
                self.assertIn("--intent-assessment", text)
                self.assertIn("attachment", text.casefold())
        for text in (self.task, self.runtime):
            self.assertIn("unavailable", text.casefold())
        for text in (self.task, self.decision):
            self.assertIn("complete", text.casefold())
            self.assertIn("matrix", text.casefold())
            self.assertIn("explicit", text.casefold())
        self.assertIn(
            "do not add a mandatory preliminary planning reviewer",
            " ".join(self.task.casefold().split()),
        )
        self.assertIn(
            "do not add a mandatory preliminary planning-reviewer dispatch",
            self.decision.casefold(),
        )

    def test_final_review_and_task_status_require_independent_semantic_proof(self):
        for text in (self.task, self.review, self.reviewer, self.runtime):
            with self.subTest(source=text[:40]):
                self.assertIn("INTENT_REVIEW", text)
        for text in (self.review, self.reviewer, self.runtime):
            self.assertIn("TARGET_SHA", text)
        for text in (self.task, self.runtime):
            self.assertIn("TASK_DONE", text)
            self.assertIn("PHASE_ACCEPTED", text)
        self.assertIn("omitted from that matrix", self.review)
        self.assertIn("UNVERIFIED_LEGACY", self.task)
        self.assertIn("PHASE_NOT_REVIEWABLE", self.runtime)

    def test_session_and_evaluation_preserve_history_and_leave_real_runs_unverified(self):
        for reference in (
            "source snapshots",
            "decision identities",
            "final intent-review report",
            "task_status",
            "UNVERIFIED_LEGACY",
        ):
            self.assertIn(reference, self.session)
        for scenario in range(1, 11):
            self.assertIn(f"R{scenario} —", self.evaluation)
        for marker in (
            "NOT RUN",
            "NOT VERIFIED",
            "Probe A",
            "Probe B",
            "Desktop A",
            "Desktop B",
            "fork_turns=none",
            "test_final_v2_pass_rejects_unmapped_source_omission_despite_complete_matrix",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.evaluation)

    def test_direct_diagnosis_and_business_decision_limits_remain_explicit(self):
        for text in (self.task, self.decision, self.review, self.executor):
            self.assertIn("diagnosis", text.casefold())
            self.assertIn("business", text.casefold())
        self.assertIn("lightweight", self.task)
        self.assertIn("lightweight", self.decision)
        self.assertIn("owner", self.executor.casefold())


if __name__ == "__main__":
    unittest.main()
