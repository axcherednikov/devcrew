"""Immutable technical evidence is not host lifecycle evidence."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests"))
import devcrew_phase
import test_pre_candidate_reapproval as fixtures


class PreCandidateDomainContractTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.PreCandidateReapprovalTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.plan["semantic_contract"]["technical_decision"] = {
            "evidence": "Verified native instructions at 0x08002256.",
            "details": {"evidence": ["unchanged source hash"]},
        }

    def initialize(self, *, schema_v2=False):
        f = self.fixture
        plan = f.v2_plan() if schema_v2 else json.loads(json.dumps(f.plan))
        planner = fixtures.planner_receipt(f.host, "T01", f.base, "immutable technical evidence")
        plan.pop("planner_dispatch", None)
        for phase in plan["phases"]:
            phase["verification_bindings"] = devcrew_phase.verification_bindings(
                phase["verification"], phase["verification_bindings"])
        assessment = ({"assessment": f.v2_assessment(plan), "decisions": []}
                      if schema_v2 else None)
        path = f.host / ("domain-evidence-v2.json" if schema_v2 else "domain-evidence-v1.json")
        initial = devcrew_phase.initialize_manifest(path, plan, f.receipt, f.host,
            planner["SPAWN_RECEIPT_ID"], intent_assessment=assessment)
        return path, plan, initial

    def test_unchanged_technical_evidence_reapproves_and_resumes(self):
        for schema_v2 in (False, True):
            with self.subTest(schema_v2=schema_v2):
                path, replacement, initial = self.initialize(schema_v2=schema_v2)
                f = self.fixture
                replacement["phases"][0]["goal"] = "correct the confirmed runtime seam"
                assessment = None
                if schema_v2:
                    replacement["original_intent"] = json.loads(json.dumps(initial["original_intent"]))
                    assessment = {"assessment": f.v2_assessment(replacement,
                        previous=devcrew_phase.intent_plan_digest(initial)), "decisions": []}
                planner = fixtures.planner_receipt(f.host, "T01", f.base, "technical evidence reapproval")
                updated = devcrew_phase.reapprove_pre_candidate(path, replacement, f.receipt,
                    f.host, "P1", planner["SPAWN_RECEIPT_ID"], intent_assessment=assessment)
                self.assertEqual(updated["semantic_contract"], initial["semantic_contract"])
                self.assertEqual(updated["contract_generation"], 1)
                resumed = devcrew_phase.load_for_resume(path, f.receipt, f.host)
                self.assertEqual(resumed["semantic_contract"], initial["semantic_contract"])
                path.unlink()

    def test_technical_evidence_cannot_change_during_reapproval(self):
        path, replacement, initial = self.initialize()
        f = self.fixture
        replacement["semantic_contract"]["technical_decision"]["evidence"] = "changed evidence"
        planner = fixtures.planner_receipt(f.host, "T01", f.base, "reject changed immutable evidence")
        before = path.read_bytes()
        with self.assertRaisesRegex(devcrew_phase.PhaseError,
                                    "PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH"):
            devcrew_phase.reapprove_pre_candidate(path, replacement, f.receipt, f.host,
                "P1", planner["SPAWN_RECEIPT_ID"])
        self.assertEqual(path.read_bytes(), before)

    def test_host_lifecycle_injection_outside_semantic_contract_stays_rejected(self):
        path, replacement, initial = self.initialize()
        f = self.fixture
        planner = fixtures.planner_receipt(f.host, "T01", f.base, "reject host lifecycle injection")
        before = path.read_bytes()
        for location in ("root", "phase", "nested_phase"):
            with self.subTest(location=location):
                proposed = json.loads(json.dumps(replacement))
                if location == "root":
                    proposed["evidence"] = []
                elif location == "phase":
                    proposed["phases"][0]["candidate_commit"] = {"sha": "f" * 40}
                else:
                    proposed["phases"][0]["known_contracts"] = [{"evidence": []}]
                with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_PLANNER_OUTPUT"):
                    devcrew_phase.reapprove_pre_candidate(path, proposed, f.receipt, f.host,
                        "P1", planner["SPAWN_RECEIPT_ID"])
                self.assertEqual(path.read_bytes(), before)
