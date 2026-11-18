"""Workflow obligations for engineering requirement intake and delivery."""
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


class RequirementWorkflowContract(unittest.TestCase):
    def assert_any(self, text, *alternatives):
        normalized = " ".join(text.casefold().split())
        self.assertTrue(any(value.casefold() in normalized for value in alternatives),
                        f"expected one of {alternatives!r}")

    def test_task_direct_review_and_session_paths_share_canonical_requirements(self):
        task = (ROOT / "skills/devcrew-task/SKILL.md").read_text()
        review = (ROOT / "skills/devcrew-review/SKILL.md").read_text()
        lifecycle = (ROOT / "skills/devcrew-session/references/memory-lifecycle.md").read_text()
        guide = (ROOT / "skills/devcrew-task/references/engineering-requirements.md").read_text()
        self.assertIn("before DIRECT edits", task)
        self.assertIn("engineering-requirements.md", task)
        self.assertIn("independently", review.casefold())
        self.assertIn("engineering requirements", review.casefold())
        self.assertIn("separate", lifecycle.casefold())
        self.assertIn("user engineering requirements", lifecycle.casefold())
        self.assertIn("engineering-requirements.json", guide)
        for term in ("distinct", "equivalent", "task-local", "one-time", "unresolved",
                     "requirements-context", "direct", "stale", "reviewer", "source"):
            self.assertIn(term, guide.casefold())
        self.assert_any(guide, "cannot control a model", "already running")

    def test_runtime_contract_documents_resource_bounds_and_read_only_diagnosis(self):
        contract = (ROOT / "docs/runtime-contract.md").read_text()
        for term in ("requirements-record", "requirements-context", "requirements-diagnose",
                     "512 KiB", "32 KiB", "stale issued context", "requirements-scope", "owned_scope", "legacy empty-registry", "recovery guidance"):
            self.assertIn(term.casefold(), contract.casefold())
        self.assert_any(contract, "read-only", "read only")


if __name__ == "__main__":
    unittest.main()
