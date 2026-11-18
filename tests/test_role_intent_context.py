"""Bound role delivery without weakening stored original-intent certification."""
import copy
import hashlib
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_phase
import test_original_intent as fixtures


class RoleIntentContextTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.IntentTransitionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.manifest, original, _answer = self.fixture.answered_manifest()
        self.fixture.complete_assessment(self.manifest, original)
        devcrew_phase.validate_manifest(self.manifest, self.fixture.receipt)

    def test_role_view_keeps_sources_obligations_decisions_and_exact_state_digest(self):
        before = copy.deepcopy(self.manifest)
        full = devcrew_phase.original_intent_context(self.manifest, "P1")
        bounded = devcrew_phase._role_intent_context(self.manifest, "P1")
        state = bounded["planning_assessment"]
        self.assertEqual(state, {
            "status": "ASSESSED",
            "state_sha256": hashlib.sha256(
                devcrew_phase._canonical_json(full["planning_assessment"])).hexdigest(),
        })
        for key in full.keys() - {"planning_assessment"}:
            self.assertEqual(bounded[key], full[key], key)
        self.assertEqual(self.manifest, before)
        self.assertEqual(
            bounded["effective_requirements"]["R1"]["statement"],
            "Recommend a different payment card when SBP is blocked.")
        bounded["sources"].clear()
        bounded["decisions"].clear()
        self.assertEqual(self.manifest, before)

    def test_large_coordinator_rationale_does_not_expand_role_payload(self):
        first = devcrew_phase._role_intent_context(self.manifest, "P1")
        original_digest = devcrew_phase.intent_plan_digest(self.manifest)
        assessment = self.manifest["original_intent"]["planning_assessment"]["assessment"]
        assessment["technical_change_rationale"] += " bounded coordinator explanation" * 20000
        devcrew_phase.validate_manifest(self.manifest, self.fixture.receipt)
        second = devcrew_phase._role_intent_context(self.manifest, "P1")
        self.assertEqual(devcrew_phase.intent_plan_digest(self.manifest), original_digest)
        self.assertEqual(len(json.dumps(first)), len(json.dumps(second)))
        self.assertNotEqual(
            first["planning_assessment"]["state_sha256"],
            second["planning_assessment"]["state_sha256"])
        full = devcrew_phase.original_intent_context(self.manifest, "P1")
        self.assertIn("assessment", full["planning_assessment"])
        self.assertGreater(len(json.dumps(full)), len(json.dumps(second)) + 100000)

    def test_executor_initial_and_repair_views_are_bounded(self):
        for repair in (False, True):
            with self.subTest(repair=repair):
                manifest = copy.deepcopy(self.manifest)
                if repair:
                    manifest["phases"][0]["active_local_fix"] = {
                        "rejected_candidate_sha": "b" * 40, "findings": ["Local finding"]}
                result = devcrew_phase.executor_input(manifest, self.fixture.task_root)
                self.assertEqual(
                    result["original_intent"],
                    devcrew_phase._role_intent_context(manifest, "P1"))
                self.assertNotIn("assessment", result["original_intent"]["planning_assessment"])

    def test_final_review_keeps_independent_sources_but_not_host_judgment_text(self):
        result = devcrew_phase.original_intent_review_context(self.manifest, "P1")
        self.assertEqual(result, devcrew_phase._role_intent_context(self.manifest, "P1"))
        self.assertEqual(result["requirements"], self.manifest["original_intent"]["requirements"])
        self.assertEqual(result["sources"], self.manifest["original_intent"]["sources"])
        self.assertEqual(result["decisions"], self.manifest["original_intent"]["decisions"])
        self.assertNotIn("assessment", result["planning_assessment"])
        self.manifest["phases"].append({"phase_id": "P2"})
        self.assertIsNone(devcrew_phase.original_intent_review_context(self.manifest, "P1"))

    def test_role_digest_cannot_replace_full_manifest_approval(self):
        before = copy.deepcopy(self.manifest)
        state = devcrew_phase._role_intent_context(self.manifest, "P1")["planning_assessment"]
        self.manifest["original_intent"]["planning_assessment"] = state
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_INTENT_ASSESSMENT"):
            devcrew_phase.validate_manifest(self.manifest, self.fixture.receipt)
        devcrew_phase.validate_manifest(before, self.fixture.receipt)

    def test_schema_one_has_no_original_intent_delivery(self):
        manifest = {"schema_version": 1}
        self.assertIsNone(devcrew_phase._role_intent_context(manifest, "P1"))


if __name__ == "__main__":
    unittest.main()
