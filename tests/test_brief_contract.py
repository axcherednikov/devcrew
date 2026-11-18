import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
BRIEF = ROOT / "skills" / "devcrew-brief"
AUTOPILOT = ROOT / "skills" / "devcrew-autopilot" / "SKILL.md"


class BriefContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.skill = (BRIEF / "SKILL.md").read_text()
        cls.questions = (BRIEF / "references" / "questions.md").read_text()
        cls.adr = (BRIEF / "references" / "adr.md").read_text()
        cls.workflow = (BRIEF / "references" / "workflow.md").read_text()
        cls.handoff = (BRIEF / "references" / "template-main.md").read_text()
        cls.autopilot = AUTOPILOT.read_text()
        cls.grill = (BRIEF / "references" / "grill.md").read_text()

    def test_independent_questions_share_a_frontier_round(self):
        self.assertIn("Ask independent frontier questions in one compact round", self.questions)

    def test_working_brief_package_uses_ignored_task_directory(self):
        self.assertIn(".codex/tasks/<slug>/", self.workflow)
        self.assertIn("Do not create a `docs/<task>/` directory for these files", self.workflow)
        self.assertIn("project-state-layout.md", self.workflow)

    def test_dependent_question_waits_for_known_prerequisites(self):
        self.assertIn("Wait for prerequisites before asking dependent questions", self.questions)

    def test_frontier_is_recomputed_after_each_answer(self):
        self.assertIn(
            "After each answer, record the confirmed decision, recompute the graph", self.questions
        )

    def test_irrelevant_follow_up_is_removed(self):
        self.assertIn("remove questions that no longer apply", self.questions)

    def test_repository_evidence_precedes_user_questions(self):
        self.assertIn(
            "First check code, tests, manifests, configuration, ADRs, domain documentation, CI and existing contracts",
            self.questions,
        )
        self.assertIn("Repository evidence before user questions", self.skill)

    def test_recommendation_needs_confirmation(self):
        self.assertIn("A recommendation remains a recommendation", self.questions)
        self.assertIn(
            "recommendation without owner confirmation is not a requirement", self.autopilot
        )

    def test_independent_planning_and_autopilot_branches_continue(self):
        self.assertIn("Continue independent planning work", self.questions)
        self.assertIn("continue independent READY branches", self.autopilot)

    def test_material_question_format_is_compact(self):
        for field in ("Why this decision matters:", "Options:", "Recommendation:", "Trade-off:"):
            with self.subTest(field=field):
                self.assertIn(field, self.questions)

    def test_high_impact_long_lived_tradeoff_recommends_adr(self):
        self.assertIn("ADR_RECOMMENDED", self.adr)
        self.assertIn("long-term contract or architectural boundary", self.adr)
        self.assertIn("costly or risky to change", self.adr)

    def test_small_local_choices_do_not_get_adrs(self):
        self.assertIn("minor implementation details", self.adr)
        self.assertIn("an inexpensive reversible choice", self.adr)

    def test_existing_adr_is_reused(self):
        self.assertIn("do not duplicate a recorded decision", self.adr)

    def test_unconfirmed_choice_is_not_accepted_adr(self):
        self.assertIn("never record an unconfirmed choice as accepted", self.adr)
        self.assertIn("ADR STATUS", self.handoff)

    def test_executor_gets_decisions_not_qa_history(self):
        self.assertIn("never the\nquestion-and-answer transcript", self.autopilot)
        self.assertIn("CONFIRMED CONTEXT (include confirmed decisions as facts)", self.autopilot)

    def test_open_question_blocks_only_dependent_tasks(self):
        self.assertIn("block only tasks whose scope", self.autopilot)
        self.assertIn("independent READY branches", self.autopilot)

    def test_final_brief_has_explicit_decision_sections(self):
        for section in (
            "CONFIRMED DECISIONS",
            "OPEN MATERIAL QUESTIONS",
            "IMPLEMENTATION ASSUMPTIONS",
            "ADR STATUS",
        ):
            with self.subTest(section=section):
                self.assertIn(section, self.skill)
                self.assertIn(section, self.handoff)

    def test_autopilot_preserves_cost_routing_contract(self):
        self.assertIn("Economy is the autopilot default", self.autopilot)
        self.assertIn("Do not hardcode model names", self.autopilot)
        cost_policy = (ROOT / "docs" / "cost-policy.md").read_text()
        self.assertIn("NORMAL / COMPLEX AGENT EXECUTION executor | bounded / medium |", cost_policy)

    def test_grill_is_optional_mode_inside_existing_brief_skill(self):
        self.assertIn("optional", self.skill.lower())
        self.assertIn("grill mode", self.skill)
        self.assertNotIn("name: devcrew-grill", self.grill)
        self.assertIn("not a new discovery phase, skill, agent", self.grill)

    def test_grill_is_reserved_for_nontrivial_material_risk(self):
        self.assertIn("Do not use it for trivial edits", self.grill)
        self.assertIn("costly or hard-to-reverse choices", self.grill)

    def test_full_execution_brief_confirms_model_before_work_and_supervises_every_route(self):
        self.assertIn("confirm the user has already selected a strong model", self.skill)
        self.assertIn("including one routed here from `devcrew-task`", self.skill)
        self.assertIn("Never override the model automatically", self.skill)
        self.assertIn("parent never dispatches that executor", self.skill)

    def test_model_choice_precedes_read_only_and_execution_briefs(self):
        self.assertIn("Before starting any full brief", self.skill)
        self.assertIn("including a read-only brief", self.skill)
        self.assertIn("visible child-chat approval are a separate gate", self.skill)

    def test_grill_interview_asks_exactly_one_question_per_turn(self):
        self.assertIn("ask **exactly one question per turn**", self.grill)

    def test_grill_next_question_uses_answer_and_updated_plan(self):
        self.assertIn("After each answer, record the owner's confirmed choice", self.grill)
        self.assertIn("update the plan and dependency graph", self.grill)

    def test_grill_retrieves_repository_evidence_before_owner_question(self):
        self.assertIn("Before each question, retrieve targeted evidence", self.grill)
        self.assertIn("Do not ask about facts evidence can establish", self.grill)

    def test_grill_recommendation_is_not_an_owner_decision(self):
        self.assertIn("Clearly label the recommendation as unconfirmed", self.grill)
        self.assertIn("Recommendations never become requirements by implication", self.grill)

    def test_grill_obeys_existing_dependency_frontier(self):
        self.assertIn("existing dependency-aware frontier", self.grill)
        self.assertIn("never ask a dependent question before its prerequisites", self.grill)

    def test_grill_prioritizes_highest_risk_unresolved_frontier_item(self):
        self.assertIn("Select the highest-risk unresolved decision", self.grill)

    def test_grill_stops_when_plan_stabilizes_or_decisions_remain_open(self):
        self.assertIn("Stop when further answers no longer materially change the plan", self.grill)
        self.assertIn("explicitly retained as `OPEN MATERIAL QUESTIONS`", self.grill)

    def test_self_play_is_read_only_and_leaves_unsupported_decisions_open(self):
        self.assertIn("conduct a read-only critique", self.grill)
        self.assertIn("Do not make product or owner decisions", self.grill)
        self.assertIn("Unsupported choices remain open questions", self.grill)

    def test_autopilot_does_not_dispatch_a_grill_agent_or_run_everywhere(self):
        self.assertIn("does not run grill automatically for every task", self.autopilot)
        self.assertIn("do not create a Grill agent", self.autopilot)
        self.assertIn("fan out solely for grill", self.autopilot)

    def test_grill_preserves_dynamic_model_routing(self):
        self.assertIn("alter the shared dynamic cost/model-routing policy", self.autopilot)
        cost_policy = (ROOT / "docs" / "cost-policy.md").read_text()
        self.assertIn("Simple / complex coordinator | strong / low or medium |", cost_policy)

    def test_grill_reuses_existing_adr_and_decision_record(self):
        self.assertIn("existing ADR mechanism", self.grill)
        self.assertIn("do not create a second decision log", self.grill)

    def test_existing_brief_question_and_adr_contracts_remain(self):
        self.assertIn("Ask independent frontier questions in one compact round", self.questions)
        self.assertIn("never record an unconfirmed choice as accepted", self.adr)
        self.assertIn("ADR STATUS", self.handoff)

    def test_full_brief_execution_requires_explicit_visible_chat_approval(self):
        supervision = (BRIEF / "references" / "supervised-execution.md").read_text()
        self.assertIn("explicit human approval", self.skill)
        self.assertIn("creating one visible Codex", self.skill)
        self.assertIn("Until that approval exists", self.skill)
        self.assertIn("Agent messages are not human approval", self.skill)
        self.assertIn("clientThreadId", supervision)
        self.assertIn("prepared", supervision)
        self.assertIn("uncertain", supervision)

    def test_ordinary_brief_step_eight_remains_read_only_default(self):
        self.assertIn("ordinary/read-only briefs", self.workflow)
        self.assertIn("do not authorize execution", self.workflow)


if __name__ == "__main__":
    unittest.main()
