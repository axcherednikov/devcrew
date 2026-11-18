import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
ROUTER = SKILLS / "devcrew" / "SKILL.md"
ROUTING = SKILLS / "devcrew" / "references" / "routing.md"
SOURCES = SKILLS / "devcrew" / "references" / "source-resolution.md"
HANDOFF = SKILLS / "devcrew" / "references" / "handoff.md"
EVALS = ROOT / "tests" / "routing-evals.md"


class RouterContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.router = ROUTER.read_text()
        cls.routing = ROUTING.read_text()
        cls.sources = SOURCES.read_text()
        cls.handoff = HANDOFF.read_text()
        cls.evals = EVALS.read_text()

    def test_public_router_exists_and_specialized_workflows_remain_available(self):
        self.assertTrue(ROUTER.is_file())
        self.assertIn("thin router", self.router)
        for skill in (
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
            "devcrew-handoff",
            "devcrew-skill-doctor",
            "devcrew-qa",
            "devcrew-commit-message",
        ):
            with self.subTest(skill=skill):
                self.assertTrue((SKILLS / skill / "SKILL.md").is_file())
        self.assertIn("respect that override", self.router)

    def test_router_resolves_sources_before_outcome_dispatch(self):
        self.assertLess(
            self.routing.index("Resolve a referenced external source"),
            self.routing.index("Route by the requested outcome"),
        )
        for source in (
            "tracker/task ID",
            "PR/MR",
            "commit",
            "branch",
            "diff/working tree",
            "task/spec file",
            "repository path",
            "attachment",
        ):
            with self.subTest(source=source):
                self.assertIn(source, self.sources)
        self.assertIn("A reference alone never determines intent", self.sources)
        self.assertIn("provider-neutral capability", self.sources)
        self.assertIn("SOURCE_UNAVAILABLE", self.sources)
        self.assertIn("WORKSPACE_AMBIGUOUS", self.sources)

    def test_outcome_routing_and_task_topology_are_canonical(self):
        for intent in (
            "devcrew-review",
            "devcrew-diagnose",
            "devcrew-handoff",
            "HANDOFF",
            "devcrew-qa",
            "`QA`",
            "devcrew-architecture",
            "devcrew-product-onboard",
            "devcrew-brief",
            "devcrew-task",
            "devcrew-autopilot",
            "READ_ONLY",
        ):
            with self.subTest(intent=intent):
                self.assertIn(intent, self.routing)
        self.assertIn("A requested fix belongs to", self.routing)
        self.assertIn("Choose `devcrew-task` for a bounded change", self.routing)
        self.assertIn("Prompt length and risk alone do not justify autopilot", self.routing)
        self.assertIn("Grill/stress-test", self.routing)
        self.assertIn("simple read-only question directly", self.router)

    def test_diagnose_requires_an_application_problem_without_requested_change(self):
        rule = next(
            line
            for line in self.routing.splitlines()
            if line.strip().endswith("→ `devcrew-diagnose`.")
        )
        for condition in (
            "materially unknown cause",
            "application failure or malfunction",
            "performance or concurrency problems",
            "with no requested change",
        ):
            with self.subTest(condition=condition):
                self.assertIn(condition, rule)
        self.assertNotIn("or explanation only", rule)
        direct_answer = next(
            line
            for line in self.routing.splitlines()
            if "Otherwise answer a simple read-only question" in line
        )
        self.assertIn("code or concept explanations", direct_answer)
        self.assertIn("A requested fix belongs to `devcrew-task`", self.routing)

    def test_explanation_failure_and_fix_fixtures_keep_distinct_outcomes(self):
        rows = {
            parts[1].strip(): parts[3].strip()
            for line in self.evals.splitlines()
            if line.startswith("| router-")
            for parts in [line.split("|")]
        }
        cases = {
            "router-simple-read-only": (
                "`READ_ONLY_ANALYSIS`",
                "direct explanation",
                "MUST NOT invoke `devcrew-diagnose`",
            ),
            "router-root-cause-only": (
                "`DIAGNOSE`",
                "`devcrew-diagnose`",
                "MUST NOT edit production code",
            ),
            "router-fix-conditional": (
                "IMPLEMENT",
                "`devcrew-task`",
                "MUST NOT route diagnose-only",
            ),
        }
        for case, requirements in cases.items():
            for requirement in requirements:
                with self.subTest(case=case, requirement=requirement):
                    self.assertIn(requirement, rows[case])

    def test_local_qa_reads_and_replays_stay_distinct_from_handoff_review_and_fix(self):
        self.assertIn("Requests to run local QA, find or read a saved QA attempt", self.router)
        self.assertIn("route to `devcrew-qa`", self.router)
        self.assertIn("Run local QA, find or read saved QA evidence", self.routing)
        self.assertIn("Finding or opening a saved report is read-only", self.routing)
        self.assertIn("Preparing delivery text for QA remains `HANDOFF`", self.routing)
        rows = {
            parts[1].strip(): parts[3].strip()
            for line in self.evals.splitlines()
            if line.startswith("| router-")
            for parts in [line.split("|")]
        }
        self.assertIn("`QA` → `devcrew-qa`", rows["router-local-qa"])
        self.assertIn("MUST NOT start a browser", rows["router-qa-find-read"])
        self.assertIn("historical PASS is not transferable", rows["router-qa-find-read"])
        self.assertIn("`HANDOFF / QA` → `devcrew-handoff`", rows["router-qa-handoff-distinct"])
        self.assertIn("`devcrew-qa`", rows["router-qa-handoff-distinct"])

    def test_handoff_is_bounded_and_prevents_router_loops(self):
        for field in (
            "ORIGINAL_INTENT:",
            "SOURCE_REFERENCE:",
            "CONFIRMED_SOURCE_SUMMARY:",
            "WORKSPACE:",
            "INTENT:",
            "ACCEPTANCE_CRITERIA:",
            "RELEVANT_EVIDENCE_REFERENCES:",
        ):
            with self.subTest(field=field):
                self.assertIn(field, self.handoff)
        self.assertIn("Do not pass the full transcript", self.handoff)
        self.assertIn("Dispatch exactly one owning workflow", self.handoff)
        self.assertIn("Never create `router → owner → router → owner` loops", self.handoff)
        self.assertIn("Prefer a bounded model", self.handoff)

    def test_router_does_not_require_expensive_routing_or_multi_agent_execution(self):
        self.assertIn("low reasoning", self.routing)
        self.assertIn("No model or high effort is mandatory", self.routing)
        self.assertIn("routing alone never creates an agent team", self.handoff)
        self.assertIn("RUNTIME_CAPABILITY_MISSING", self.router + self.sources)
        self.assertNotIn("must spawn", self.router.lower())

    def test_routing_eval_suite_covers_positive_and_negative_cases(self):
        cases = (
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
            "router-session-resume",
            "router-local-spec",
            "router-source-unavailable",
            "router-small-task-cost",
            "router-handoff-qa",
            "router-local-qa",
            "router-qa-find-read",
            "router-qa-handoff-distinct",
            "router-handoff-client",
            "router-handoff-customer",
            "router-handoff-all",
            "router-implement-then-handoff",
        )
        for case in cases:
            with self.subTest(case=case):
                self.assertIn(case, self.evals)


if __name__ == "__main__":
    unittest.main()
