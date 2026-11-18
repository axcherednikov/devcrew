import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SESSION = (ROOT / "skills/devcrew-session/SKILL.md").read_text()
HARVEST = (ROOT / "skills/devcrew-task/references/session-harvest.md").read_text()
RETRO = (ROOT / "skills/devcrew-task/references/task-retro.md").read_text()
AUTOPILOT = (ROOT / "skills/devcrew-autopilot/SKILL.md").read_text()
POLICY = (ROOT / "skills/devcrew-task/references/engineering-policy.md").read_text()


class SessionRetroContract(unittest.TestCase):
    def test_session_state_is_private_and_knowledge_retention_is_minimal(self):
        self.assertIn("`.codex/sessions/`", SESSION)
        self.assertIn("`memory/`, `docs/`, and `rules/`", SESSION)
        self.assertIn("project-state-layout.md", SESSION)
        self.assertIn("Cite durable code/test/commit/document sources", HARVEST)

    def test_no_standalone_retro_skill_or_agent(self):
        self.assertFalse((ROOT / "skills/devcrew-retro").exists())
        self.assertIn("do not create a Retro agent", AUTOPILOT)

    def test_retro_is_optional_for_trivial_tasks_and_can_run_for_completed_epics(self):
        for text in (SESSION, HARVEST, RETRO):
            self.assertIn("trivial", text.lower())
            self.assertRegex(text.lower(), r"unfinished|incomplete")
        self.assertIn("completed epic", HARVEST)
        self.assertIn("completed significant epic", AUTOPILOT)
        self.assertIn("Retro is not a closure gate", SESSION)

    def test_claims_require_evidence_and_bounded_sources(self):
        self.assertIn("Do not invent a retro file", HARVEST)
        self.assertIn("Do not search the repository", HARVEST)
        for source in (
            "task or epic plan/card",
            "session snapshot",
            "review reports",
            "verification/check records",
            "verified diff",
            "confirmed blockers",
        ):
            with self.subTest(source=source):
                self.assertIn(source, HARVEST)

    def test_recurring_classes_require_two_observations(self):
        self.assertIn("at least twice", HARVEST)
        self.assertIn("A single episode is an observation", HARVEST)

    def test_loops_require_cycles_and_evidence_backed_cause(self):
        self.assertIn("observed cycles", HARVEST)
        for cause in (
            "unclear requirement",
            "missed invariant",
            "handoff gap",
            "weak self-check",
            "wrong seam",
            "insufficient test",
            "noisy reviewer rule",
        ):
            with self.subTest(cause=cause):
                self.assertIn(cause, HARVEST)

    def test_lessons_require_trigger_action(self):
        self.assertIn("`TRIGGER → ACTION`", HARVEST)
        self.assertIn("A durable lesson must state", HARVEST)

    def test_memory_dedupes_policy_instructions_and_existing_lessons(self):
        for term in (
            "configured memory index",
            "engineering policy",
            "applicable project instructions",
            "existing lessons",
            "not already covered",
        ):
            with self.subTest(term=term):
                self.assertIn(term, HARVEST)
        self.assertIn("memory lifecycle", HARVEST)

    def test_one_off_and_policy_candidates_are_not_promoted(self):
        self.assertIn("Keep one-off details in the task card", HARVEST)
        self.assertIn("`POLICY_CANDIDATE`", HARVEST)
        self.assertIn("separate policy task", HARVEST)

    def test_marker_search_is_scoped_to_verified_current_diff(self):
        self.assertIn("verified final diff", HARVEST)
        self.assertIn("current epic/task scope", HARVEST)
        self.assertIn("recorded baseline", HARVEST)
        self.assertIn("Do not search the repository", HARVEST)

    def test_marker_states_have_distinct_followup_behavior(self):
        for state in (
            "`TRIGGERED`",
            "`NOT_TRIGGERED`",
            "`UNKNOWN`",
            "`MALFORMED`",
            "`PROTECTED_VIOLATION`",
        ):
            with self.subTest(state=state):
                self.assertIn(state, HARVEST)
        triggered = HARVEST.split("`TRIGGERED`:", 1)[1].split("\n- `NOT_TRIGGERED`:", 1)[0]
        for state in ("NOT_TRIGGERED", "UNKNOWN"):
            section = HARVEST.split(f"`{state}`:", 1)[1].split("\n- `", 1)[0]
            self.assertIn("no backlog item", section)
        self.assertIn("actionable follow-up candidate", triggered)
        self.assertIn("recommend correcting the marker", HARVEST)
        self.assertIn("never acceptable technical debt", HARVEST)

    def test_protected_obligations_match_minimal_diff_policy(self):
        self.assertIn("validation, security, integrity, acceptance, an invariant", HARVEST)
        self.assertIn("Minimal Diff must never defer or remove", POLICY)

    def test_no_automatic_external_backlog_or_scope_expansion(self):
        for forbidden in (
            "never creates external issues/tracker tasks",
            "expands an epic/product scope",
            "starts implementation",
            "creates a follow-up agent",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertIn(forbidden, HARVEST)
        self.assertIn("explicit request", HARVEST)

    def test_policy_candidate_does_not_edit_global_policy(self):
        self.assertIn("recommendation only", HARVEST)
        self.assertIn("Do not edit", RETRO)

    def test_autopilot_does_not_run_retro_per_task_or_create_agent(self):
        self.assertIn("Never run it after every task", AUTOPILOT)
        self.assertIn("do not create a Retro agent", AUTOPILOT)
        self.assertNotIn("retro-agent", AUTOPILOT)

    def test_dynamic_routing_and_bounded_context_remain_in_effect(self):
        self.assertIn("same shared dynamic cost/model-routing policy", AUTOPILOT)
        self.assertIn("inspect only current-scope evidence", AUTOPILOT)
        self.assertIn("Do not search the repository", HARVEST)

    def test_role_lessons_are_selective_and_use_existing_model(self):
        self.assertIn("If role-specific memory/context already exists", HARVEST)
        self.assertIn("load only relevant lessons downstream", HARVEST)
        self.assertIn("Do not create a parallel memory model", HARVEST)

    def test_trivial_session_closes_without_retro(self):
        self.assertIn("Trivial tasks and sessions close normally without retro", SESSION)
        self.assertIn("Retro is not a closure gate", SESSION)


if __name__ == "__main__":
    unittest.main()
