import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills/devcrew-handoff"


class HandoffContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (SKILL_DIR / "SKILL.md").read_text()
        cls.audiences = (SKILL_DIR / "references/audiences.md").read_text()
        cls.template = (SKILL_DIR / "references/template.md").read_text()

    def test_one_public_skill_exists_and_is_read_only(self):
        self.assertTrue((SKILL_DIR / "SKILL.md").is_file())
        for name in ("devcrew-qa-handoff", "devcrew-client-handoff", "devcrew-customer-handoff"):
            self.assertFalse((ROOT / "skills" / name).exists())
        self.assertIn("always read-only", self.skill)
        self.assertIn("does not implement, fix, review", self.skill)
        self.assertIn("publish comments", self.skill)

    def test_full_template_has_four_audience_distinct_sections_in_order(self):
        russian = (SKILL_DIR / "references/ru/template.md").read_text()
        for template, headers in (
            (self.template, ("## Private summary", "## QA", "## Frontend / client", "## For the customer")),
            (russian, ("## Кратко для меня", "## QA", "## Команде клиентского приложения", "## Для заказчика")),
        ):
            positions = [template.index(header) for header in headers]
            self.assertEqual(positions, sorted(positions))
        self.assertIn("never Tracker/MR/customer copy", self.audiences)

    def test_private_summary_is_short_and_includes_limits_and_technical_context(self):
        self.assertIn("2–5 concise bullets", self.audiences)
        self.assertIn("15–30 second read", self.audiences)
        self.assertIn("blockers/risks/follow-up", self.audiences)
        self.assertIn("Class/service/test names are allowed", self.audiences)
        self.assertIn("only checks actually run", self.template)

    def test_adaptive_audiences_support_only_and_default_private_summary(self):
        self.assertIn("Only QA", self.skill)
        self.assertIn("without the private summary", self.skill)
        self.assertIn("Never emit unrequested audience blocks", self.skill)
        for audience in ("QA", "CLIENT", "CUSTOMER", "ALL"):
            self.assertIn(audience, self.skill)

    def test_qa_is_behavioral_and_actionable_but_bounded(self):
        for phrase in ("Describe user/system behavior, not implementation structure", "Precondition", "Action", "Expected result"):
            self.assertIn(phrase, self.audiences)
        self.assertIn("usually 1–3 scenarios", self.skill)
        self.assertIn("project-wide regression plan", self.skill)
        self.assertIn("critical evidenced edge cases", self.audiences)
        self.assertIn("Avoid internal class names", self.audiences)
        self.assertIn("Call out critical scenarios separately", self.skill)

    def test_client_contract_only_and_backend_internal_change_is_explicit(self):
        self.assertIn("externally observable contract", self.audiences)
        self.assertIn("backend-only", self.skill)
        self.assertIn("client changes are not required", self.skill)
        self.assertIn("State compatibility only if verified", self.audiences)

    def test_customer_is_outcome_oriented_and_omits_engineering_tools(self):
        self.assertIn("business outcomes", self.audiences)
        self.assertIn("Omit source code names, architecture, test tools/counts", self.audiences)
        customer = self.audiences.split("## Customer / PM", 1)[1]
        self.assertNotIn("PHPUnit", customer)
        self.assertNotIn("PHPStan", customer)

    def test_claims_require_evidence_and_unknown_is_not_instruction(self):
        for marker in ("REQUIREMENT → IMPLEMENTED BEHAVIOR → VERIFICATION EVIDENCE → AUDIENCE",
                       "CONFIRMED", "UNKNOWN", "NOT_APPLICABLE",
                       "do not turn them into QA/client/customer instructions"):
            self.assertIn(marker, self.skill)

    def test_failure_states_block_success_claims(self):
        for state in ("SOURCE_UNAVAILABLE", "IMPLEMENTATION_NOT_FOUND", "INSUFFICIENT_EVIDENCE", "HANDOFF_BLOCKED"):
            self.assertIn(state, self.skill)
        self.assertIn("Do not produce successful customer completion text", self.skill)

    def test_task_review_chaining_and_cost_policy(self):
        self.assertIn("completed verification", self.skill)
        self.assertIn("blocking review finding", self.skill)
        self.assertIn("must not generate one automatically", self.skill)
        self.assertIn("Prefer a bounded model", self.skill)
        self.assertIn("do not fan out audience agents", self.skill)

    def test_review_and_fix_router_negatives_remain_distinct(self):
        routing = (ROOT / "skills/devcrew/references/routing.md").read_text()
        evals = (ROOT / "tests/routing-evals.md").read_text()
        self.assertIn("Review/check/inspect an MR by itself remains `REVIEW`", routing)
        self.assertIn("“Проверь MR”; “Исправь баг”", evals)


if __name__ == "__main__":
    unittest.main()
