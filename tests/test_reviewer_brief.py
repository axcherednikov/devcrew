import copy
import pathlib
import sys
import tomllib
import unittest
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import devcrew_context
import devcrew_phase
from test_task_phases import plan


class ReviewerBriefTests(unittest.TestCase):
    def setUp(self):
        self.manifest = plan("MULTI_PHASE")
        self.receipt = {"owner": "DEVCREW", "state": "RESOLVED", "repository_id": "repo",
                        "task_id": "T01", "task_worktree_root": "/tmp/T01",
                        "branch": "codex/tasks/T01", "expected_head": "a" * 40}

    def review_target(self, manifest):
        value = copy.deepcopy(manifest)
        value["phases"][0].update(status="COMMITTED", candidate_commit={
            "base": "a" * 40, "sha": "b" * 40})
        return devcrew_phase.review_target(value, "P1")

    def test_executor_and_reviewer_receive_same_current_phase_meaning(self):
        scenario = "GET /health invokes the health handler and returns the service status."
        self.manifest["phases"][0]["main_scenario"] = scenario
        self.manifest["phases"][1]["main_scenario"] = "Later readiness scenario"
        validated = devcrew_phase.validate_manifest(self.manifest, self.receipt)
        executor = devcrew_phase.executor_input(validated, pathlib.Path("/tmp/T01"))
        reviewer = self.review_target(validated)
        self.assertEqual(executor["main_scenario"], scenario)
        self.assertEqual(reviewer["MAIN_SCENARIO"], executor["main_scenario"])
        self.assertEqual(reviewer["GOAL"], executor["goal"])
        for key in ("ACCEPTANCE", "VERIFICATION", "OWNED_SCOPE", "KNOWN_CONTRACTS", "DO_NOT_CHANGE"):
            self.assertEqual(reviewer[key], executor[key.lower()])
        message = devcrew_context._reviewer_contract(
            "T01", "P1", "a" * 40, "b" * 40, reviewer, ["checks/health.json"])
        self.assertIn(scenario, message)
        self.assertNotIn("Later readiness scenario", message)

    def test_legacy_manifest_preserves_explicit_missing_scenario(self):
        validated = devcrew_phase.validate_manifest(self.manifest, self.receipt)
        executor = devcrew_phase.executor_input(validated, pathlib.Path("/tmp/T01"))
        reviewer = self.review_target(validated)
        self.assertIsNone(executor["main_scenario"])
        self.assertIsNone(reviewer["MAIN_SCENARIO"])
        devcrew_context._reviewer_contract(
            "T01", "P1", "a" * 40, "b" * 40, reviewer, ["checks/health.json"])
        self.assertNotIn("main_scenario", validated["phases"][0])

    def test_present_scenario_must_be_nonempty_text(self):
        for invalid in (None, "", "  ", 1, [], {}):
            with self.subTest(value=invalid):
                self.manifest["phases"][0]["main_scenario"] = invalid
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase.validate_manifest(self.manifest, self.receipt)
                self.assertEqual(caught.exception.code, "INVALID_MAIN_SCENARIO")

    def test_reviewer_rejects_invalid_goal_or_scenario(self):
        target = self.review_target(self.manifest)
        for field, invalids in (("GOAL", (None, "", "  ", [])),
                                ("MAIN_SCENARIO", ("", "  ", 1, []))):
            for invalid in invalids:
                with self.subTest(field=field, value=invalid):
                    altered = dict(target, **{field: invalid})
                    with self.assertRaises(ValueError):
                        devcrew_context._reviewer_contract(
                            "T01", "P1", "a" * 40, "b" * 40, altered, ["checks/health.json"])

    def test_negative_evidence_guidance_keeps_static_findings_and_risk_based_mutation(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        coverage = (root / "skills/devcrew-review/references/coverage.md").read_text()
        gate = (root / "skills/devcrew-task/references/verify-gate.md").read_text()
        for text in (coverage, gate):
            self.assertIn("same run", text)
            self.assertIn("stale", text)
            self.assertIn("truncated", text)
            self.assertTrue("command acceptance" in text or "command event proves" in text)
            self.assertIn("mixed", text)
            self.assertIn("static", text.casefold())
            self.assertIn("targeted mutation", text)
        self.assertIn("not required for every finding", coverage)
        self.assertIn("not a blanket obligation", coverage)
        self.assertIn("rather than requiring mutation for each change", gate)

    def test_final_v2_reviewer_contract_carries_canonical_original_intent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            receipt = dict(self.receipt, task_worktree_root=str(root))
            manifest = copy.deepcopy(self.manifest)
            manifest["task_mode"] = "SINGLE_PHASE"
            manifest["semantic_contract"] = {"capabilities": ["health"]}
            manifest["phases"] = [manifest["phases"][0]]
            manifest["schema_version"] = 2
            source = devcrew_phase.bind_intent_source(root, receipt, {
                "mode": "unavailable", "source_type": "user_prompt", "origin": "request",
                "reason": "prompt capture unavailable"})
            manifest["original_intent"] = {
                "sources": [source], "requirements": [], "decisions": [],
                "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                "task_acceptance": {"status": "NOT_ASSESSED", "review": None}}
            manifest["phases"][0].update(status="COMMITTED", candidate_commit={"base": "a" * 40, "sha": "b" * 40})
            target = devcrew_phase.review_target(manifest, "P1")
            intent = target["ORIGINAL_INTENT"]
            self.assertEqual(intent["plan_sha256"], devcrew_phase.intent_plan_digest(manifest))
            self.assertEqual(intent["sources"], [source])
            self.assertEqual(intent["phase_id"], "P1")
            message = devcrew_context._reviewer_contract(
                "T01", "P1", "a" * 40, "b" * 40, target, ["checks/health.json"])
            self.assertIn("ORIGINAL_INTENT", message)

    def test_review_workflow_and_role_require_four_evidenced_questions(self):
        root = pathlib.Path(__file__).resolve().parents[1]
        workflow = (root / "skills/devcrew-review/SKILL.md").read_text()
        role = tomllib.loads((root / "skills/devcrew-setup/assets/agents/reviewer-agent.toml").read_text())
        for instructions in (workflow, role["developer_instructions"]):
            for question in ("Are all acceptance criteria met?", "Does the main scenario work?",
                             "Which critical edge cases were missed?", "Do the tests demonstrate this?"):
                with self.subTest(question=question):
                    self.assertIn(question, instructions)
            self.assertIn("with evidence", instructions)
            self.assertIn("accepted part relevant to the current phase", instructions)
            self.assertIn("explicit limitation", instructions)


if __name__ == "__main__":
    unittest.main()
