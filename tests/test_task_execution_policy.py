import pathlib
import json
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import devcrew_context
import devcrew_cost
import devcrew_dispatch
import devcrew_phase
import test_phase_review_lifecycle as phase_review


class TaskExecutionPolicyContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def read(path):
            return " ".join((ROOT / path).read_text().split())

        cls.strategy = read("skills/devcrew-task/references/implementation-strategy.md")
        cls.engineering = read("skills/devcrew-task/references/engineering-policy.md")
        cls.task = read("skills/devcrew-task/SKILL.md")
        cls.preflight = read("skills/devcrew-task/references/dispatch-preflight.md")
        cls.replan = read("skills/devcrew-task/references/replan-protocol.md")
        cls.project_preflight = read("skills/devcrew-setup/references/project-sync-preflight.md")
        cls.brief = read("skills/devcrew-brief/SKILL.md")
        cls.review = read("skills/devcrew-review/SKILL.md")
        cls.decision = read("skills/devcrew-setup/assets/agents/decision-agent.toml")
        cls.executor = read("skills/devcrew-setup/assets/agents/executor-agent.toml")
        cls.setup = read("skills/devcrew-setup/SKILL.md")
        cls.postflight = read("skills/devcrew-task/references/dispatch-postflight.md")
        cls.evals = (ROOT / "tests/routing-evals.md").read_text()
        cls.autopilot = read("skills/devcrew-autopilot/SKILL.md")

    def test_phase_dispatch_is_manifest_bound_and_stops_at_close_boundary(self):
        for required in ("`SINGLE_PHASE`", "`MULTI_PHASE`", "semantic capability",
                         "`CURRENT_PHASE`", "`PHASE_MANIFEST_REQUIRED`", "`READY_FOR_CLOSE`",
                         "DIRECT remains unchanged"):
            with self.subTest(required=required):
                self.assertIn(required, self.task)
        self.assertIn("fresh `task-checkout resolve` receipt", self.preflight)
        self.assertIn("only `CURRENT_PHASE`", self.preflight)
        self.assertIn("never acceptance", self.task)
        self.assertIn("actual verification output", self.postflight)

    def test_planning_prefers_a_minimal_product_scenario_and_preserves_technical_capabilities(self):
        policy = " ".join((self.task, self.strategy, self.decision)).lower()
        policy = " ".join(policy.split())
        clauses = {
            "product applicability": re.compile(r"(?:product tasks|для продуктовых задач)", re.I),
            "feasibility qualifier": re.compile(r"(?:where feasible|when feasible|when possible|по возможности)", re.I),
            "first minimal working scenario": re.compile(
                r"(?:first phase|initial phase|первая фаза).{0,100}"
                r"(?:minimal|smallest|минимальн\w*).{0,60}"
                r"(?:working scenario|end.to.end scenario|работающ\w* сценар\w*)", re.I),
            "later scenario expansion": re.compile(
                r"(?:later phases|subsequent phases|последующ\w* фаз\w*).{0,100}"
                r"(?:expand|extend|расшир\w*)", re.I),
            "independent technical capability": re.compile(
                r"(?:independent technical|standalone technical|самостоятельн\w* техническ\w*).{0,120}"
                r"(?:capabilit\w*|capabilities|возможност\w*)", re.I),
            "no required user flow for technical work": re.compile(
                r"(?:without|does not require|need not|не обяз\w*).{0,60}"
                r"(?:user flow|user-facing flow|пользовательск\w* flow|пользовательск\w* сценар\w*)", re.I),
        }
        for name, pattern in clauses.items():
            with self.subTest(clause=name):
                self.assertRegex(policy, pattern)

        supported_formulations = (
            (
                "For product tasks, when possible, the initial phase delivers a smallest working scenario; later phases extend it.",
                ("product applicability", "feasibility qualifier", "first minimal working scenario", "later scenario expansion"),
            ),
            (
                "Для продуктовых задач по возможности первая фаза завершает минимальный работающий сценарий, а последующие фазы расширяют его.",
                ("product applicability", "feasibility qualifier", "first minimal working scenario", "later scenario expansion"),
            ),
            (
                "Independent technical capabilities can stand alone and need not include a user-facing flow.",
                ("independent technical capability", "no required user flow for technical work"),
            ),
            (
                "Самостоятельная техническая capability не обязана включать пользовательский сценарий.",
                ("independent technical capability", "no required user flow for technical work"),
            ),
        )
        for fixture, (formulation, applicable_clauses) in enumerate(supported_formulations):
            normalized_formulation = " ".join(formulation.lower().split())
            for name in applicable_clauses:
                with self.subTest(supported_formulation=fixture, clause=name):
                    self.assertRegex(normalized_formulation, clauses[name])

        clause_fixtures = {
            "product applicability": "For product tasks, use a small scenario.",
            "feasibility qualifier": "When possible, use a small scenario.",
            "first minimal working scenario": "The initial phase delivers a smallest working scenario.",
            "later scenario expansion": "Later phases extend the result.",
            "independent technical capability": "Independent technical capabilities stand alone.",
            "no required user flow for technical work": "Technical work need not include a user-facing flow.",
        }
        complete_fixture = " ".join(clause_fixtures.values())
        for name, removed_clause in clause_fixtures.items():
            with self.subTest(removed_clause=name):
                without_clause = complete_fixture.replace(removed_clause, "")
                self.assertNotRegex(without_clause, clauses[name])

        preparation_contract = re.compile(
            r"(?=.*(?:prerequisit\w*|prepar\w*|migration|миграц\w*|подготов\w*))"
            r"(?=.*(?:dependenc\w*|зависимост\w*))"
            r"(?=.*(?:explain\w*|state\w*|указ\w*|объясн\w*))"
            r"(?=.*(?:why.{0,40}(?:must|should|need).{0,20}(?:come first|go first|run first)|"
            r"reason.{0,80}(?:must|should|need).{0,20}(?:come first|go first|run first)|"
            r"почему.{0,50}(?:должн\w*|следует).{0,30}(?:раньше|сначала)|"
            r"причин\w*.{0,50}(?:раньше|сначала)))", re.I)

        def sentences(content):
            return re.split(r"(?<=[.!?])\s+", " ".join(content.split()))

        def has_justified_prerequisite(content):
            return any(preparation_contract.search(sentence) for sentence in sentences(content))

        for name, content in (("strategy", self.strategy), ("decision agent", self.decision)):
            with self.subTest(preparation_source=name):
                self.assertTrue(has_justified_prerequisite(content))

        combined = " ".join((self.strategy, self.decision))
        actual_clauses = [sentence for sentence in sentences(combined)
                          if preparation_contract.search(sentence)]
        self.assertTrue(actual_clauses)
        without_actual_clauses = " ".join(
            sentence for sentence in sentences(combined)
            if not preparation_contract.search(sentence)
        )
        self.assertFalse(has_justified_prerequisite(without_actual_clauses))

        unrelated = (
            "A migration is preparation. Its dependency is tracked in a separate note. "
            "Another phase may run first for an unrelated reason."
        )
        self.assertFalse(has_justified_prerequisite(unrelated))
        translated = "Если необходима миграция до сценария, объясните зависимость и причину, почему она должна пройти раньше."
        self.assertTrue(has_justified_prerequisite(translated))

    def test_direct_is_all_conditions_and_does_not_require_agents(self):
        for condition in (
            "local, bounded mechanical edit",
            "exact target and intended before/after behavior",
            "no substantive flow/caller investigation or material technical choice is needed",
            "low risk and reversible",
            "deterministic local acceptance or regression check",
        ):
            with self.subTest(condition=condition):
                self.assertIn(condition.lower(), self.strategy.lower())
        for safety_boundary in (
            "data mutation or migrations", "API/compatibility",
            "authentication/authorization/security", "payments", "tenant isolation",
            "external integration", "concurrency, retry or idempotency",
            "failure recovery", "complex state transitions",
        ):
            with self.subTest(safety_boundary=safety_boundary):
                self.assertIn(safety_boundary.lower(), self.strategy.lower())
        self.assertIn("An observable behavior change alone does not disqualify DIRECT", self.strategy)
        self.assertIn("disabling one explicitly unused metrics cron", self.strategy)
        self.assertIn("payment jobs, audit/security monitoring, cleanup jobs", self.strategy)
        self.assertIn("If relevant facts are missing, broader effects or callers emerge", self.strategy)
        self.assertIn("Bounded inspection may validate known context", self.strategy)
        self.assertIn("current-checkout policy", self.strategy)
        self.assertIn("applicable requirements", self.strategy)
        self.assertIn("test-first when applicable", self.strategy)
        self.assertIn("mandatory project checks", self.strategy)
        self.assertIn("production deployment", self.strategy)
        self.assertIn("if any direct condition is false or cannot be established", self.strategy.lower())
        direct = self.strategy.split("## DIRECT", 1)[1].split("## AGENT EXECUTION", 1)[0]
        self.assertIn("No planner/executor/reviewer lifecycle or records are required solely because the task is DIRECT", direct)

    def test_behavioral_api_and_payment_work_are_agent_execution(self):
        self.assertIn("AGENT EXECUTION is required if any DIRECT condition is false", self.strategy)
        for signal in ("API/compatibility", "payment", "failure recovery", "complex state transitions"):
            self.assertIn(signal.lower(), self.strategy.lower())
        self.assertIn("an observable behavior change alone does not disqualify direct", self.strategy.lower())

    def test_registered_plan_goes_to_executor_without_replanning(self):
        self.assertIn("If an existing brief/implementation plan is current", self.strategy)
        self.assertIn("Do not repeat planning without a concrete gap", self.strategy)
        self.assertIn("Direct executor dispatch requires an existing validated phase manifest", self.strategy)
        self.assertIn("persisted planner receipt for the same task and original base SHA", self.strategy)
        self.assertIn("fresh `task-checkout resolve` receipt", self.strategy)
        self.assertIn("Do not run `phase init` again", self.strategy)
        self.assertIn("Only an existing validated phase manifest with a matching persisted planner receipt", self.preflight)
        self.assertIn("send it directly to executor", self.preflight)
        self.assertIn("do not replace the executor", self.brief)

    def test_external_ready_plan_requires_bounded_planner_validation(self):
        self.assertIn("An external ready plan without that manifest first goes to a routed planner", self.strategy)
        self.assertIn("bounded validation, not planning from scratch", self.strategy)
        self.assertIn("Do not repeat research unless a concrete gap requires it", self.strategy)
        self.assertIn("planner returns `PLAN_READY` with the formal phase plan and stops", self.strategy)
        self.assertIn("phase init --spawn-receipt-id ID", self.strategy)
        self.assertIn("A ready plan alone cannot replace a planner receipt", self.strategy)
        self.assertIn("An external ready plan without that manifest requires bounded planner validation", self.preflight)
        self.assertIn("An external ready plan without an existing validated manifest", self.task)
        self.assertIn("bounded planner validation and registration", self.brief)
        for case in ("registered-plan-resume", "external-ready-plan", "registered-plan-invalid-receipt"):
            self.assertIn(f"| {case} |", self.evals)

    def test_missing_plan_uses_research_and_decision_layer_before_executor(self):
        self.assertIn("bounded research roles", self.strategy)
        self.assertIn("A decision/planner agent then synthesizes", self.strategy)
        self.assertIn("host does not substitute its own technical decision", self.strategy.lower())
        self.assertIn("decision/planning, then send the accepted plan to executor-agent", self.task)
        self.assertIn("executor-ready", self.decision)

    def test_host_is_coordinator_and_executor_owns_production_changes(self):
        for prohibition in (
            "must not perform the task's substantive investigation",
            "choose a material technical solution",
            "write production code",
            "repair production code after review",
        ):
            with self.subTest(prohibition=prohibition):
                self.assertIn(prohibition, self.strategy)
        self.assertIn("executor-agent owns substantive production implementation", self.strategy)
        self.assertIn("must not duplicate that work", self.strategy)
        self.assertIn("coordinator checks the plan and evidence references", self.engineering)

    def test_role_transitions_are_explicit_and_planner_stops_at_plan_ready(self):
        for content in (self.strategy, self.task, self.preflight, self.replan):
            self.assertIn("new", content.lower())
        self.assertIn("PLAN_READY", self.replan)
        self.assertIn("terminal receipt", self.replan)
        self.assertIn("never continues into production", self.strategy)
        self.assertIn("cannot promote itself to planner or executor", self.strategy)
        self.assertIn("new coordinator dispatch", self.preflight)
        self.assertIn("new role-matched dispatch", self.decision)
        self.assertIn("do not change roles", self.executor)

    def test_executor_implements_bounded_plan_without_redesigning_contracts(self):
        for content in (self.strategy, self.task, self.executor):
            self.assertIn("accepted", content.lower())
            self.assertIn("bounded", content.lower())
        for boundary in ("architectural seams", "domain models", "confirmed contracts"):
            self.assertIn(boundary, self.executor)
        self.assertIn("host remains coordinator-only", self.task)
        self.assertIn("never writes delegated production code", self.task)

    def test_agent_execution_requires_independent_reviewer_and_routes_local_fix(self):
        self.assertIn("mandatory", self.strategy)
        self.assertIn("separate context", self.strategy)
        self.assertIn("same executor", self.strategy)
        self.assertIn("rerun affected checks", self.strategy)
        self.assertIn("mandatory independent review stage", self.review)
        self.assertIn("In AGENT EXECUTION, return the finding to the same executor", self.replan)

    def test_systemic_replan_and_owner_decision_routes_are_preserved(self):
        self.assertIn("SYSTEMIC_REPLAN", self.strategy)
        self.assertIn("decision/planner", self.strategy)
        self.assertIn("OWNER_DECISION_REQUIRED", self.strategy)
        self.assertIn("Ask the user/owner", self.replan)
        self.assertIn("cannot change acceptance criteria", self.replan)

    def test_unavailable_runtime_is_a_hard_stop_not_host_fallback(self):
        self.assertIn("AGENT_RUNTIME_REQUIRED", self.strategy)
        self.assertIn("MISSING_CAPABILITY", self.strategy)
        self.assertIn("BLOCKED_STAGE", self.strategy)
        self.assertIn("Do not fall back to host implementation", self.strategy)
        self.assertIn("distinct context", self.preflight)
        self.assertIn("REAL SUBAGENT EXECUTION NOT AVAILABLE", self.autopilot)

    def test_subproject_role_file_is_not_runtime_availability(self):
        for field in ("ROLE_FILE_EXISTS", "ROLE_RUNTIME_AVAILABLE"):
            self.assertIn(field, self.preflight)
            self.assertIn(field, self.project_preflight)
        self.assertIn("root workspace", self.preflight)
        self.assertIn("subproject", self.project_preflight)

    def test_preserves_safety_and_does_not_expand_routing_or_parallelism(self):
        for invariant in ("scope ownership", "evidence requirements", "verification honesty"):
            self.assertIn(invariant, self.strategy)
        for unchanged in ("model/cost routing", "parallel writer waves"):
            self.assertIn(unchanged, self.strategy)
        self.assertIn("Do not weaken acceptance", self.strategy)
        self.assertIn("one task at a time", self.autopilot)

    def test_executor_is_required_for_agent_execution(self):
        self.assertIn("require executor-agent dispatch", self.preflight)
        self.assertIn("executor-agent owns substantive production implementation", self.strategy)

    def test_reviewer_is_required_after_checks_for_agent_execution(self):
        self.assertIn("separate reviewer context after checks", self.preflight)
        self.assertIn("After the executor returns and deterministic checks run", self.strategy)

    def test_routed_receipt_is_mandatory_for_devcrew_dispatch_and_phase_acceptance(self):
        self.assertIn("direct `spawn_agent`/collaboration spawn without that route", self.task)
        self.assertIn("matching spawn receipt", self.task)
        self.assertIn("`ROUTE_ID`", self.preflight)
        self.assertIn("capacity-report` persists a spawn receipt", self.postflight)
        self.assertIn("fork_turns=\"none\"", self.preflight)
        self.assertIn("Direct runtime calls without a route are contract violations", self.autopilot)

    def test_direct_skips_executor_planner_and_independent_reviewer(self):
        direct = self.strategy.split("## DIRECT", 1)[1].split("## AGENT EXECUTION", 1)[0]
        for role in ("executor", "planner", "reviewer"):
            with self.subTest(role=role):
                self.assertIn(role, direct)

    def test_planner_is_conditional_on_plan_sufficiency(self):
        self.assertIn("If an existing brief/implementation plan is current", self.strategy)
        self.assertIn("Otherwise", self.strategy)
        self.assertIn("Require decision/planner dispatch", self.preflight)

    def test_large_task_planner_requires_main_scenario_and_acceptance_brief(self):
        for point in ("Goal", "What must change", "Out of scope",
                      "Main user or system scenario",
                      "How to verify completion"):
            with self.subTest(point=point):
                self.assertIn(point, self.strategy)
                self.assertIn(point, self.decision)
        self.assertIn("trigger/input, key steps and observable result", self.decision)
        self.assertIn("observable acceptance and relevant tests/checks", self.decision)
        self.assertIn("short-brief-for-large-tasks", self.preflight)
        self.assertIn("Missing or stale points go back to the planner before writer dispatch", self.preflight)

    def test_current_large_task_brief_is_reused_without_an_extra_stage(self):
        self.assertIn("Reuse a current brief that covers all five points", self.strategy)
        self.assertIn("reference it rather than rewriting it", self.strategy)
        self.assertIn("fill only missing or stale points", self.decision)
        self.assertIn("complete current brief needs no repeated planning", self.preflight)
        self.assertIn("Do not require a separate devcrew-brief invocation, planning stage or new document", self.decision)

    def test_short_brief_is_not_a_gate_for_small_tasks_or_owner_decision_substitute(self):
        self.assertIn("phase count alone does not determine size", self.strategy)
        self.assertIn("Small tasks and DIRECT do not acquire this five-point requirement", self.strategy)
        self.assertIn("Material open requirements remain owner questions and block dependent implementation", self.decision)

    def test_executor_replan_preserves_receipt_and_acceptance_guards(self):
        for field in ("REPLAN_REQUEST", "PLAN_ASSUMPTION", "OBSERVED_EVIDENCE", "AFFECTED_SCOPE"):
            with self.subTest(field=field):
                self.assertIn(field, self.replan)
        self.assertIn("cannot change acceptance criteria", self.replan)

    def test_review_role_is_read_only_and_not_executor_context(self):
        reviewer_role = (ROOT / "skills/devcrew-setup/assets/agents/reviewer-agent.toml").read_text()
        self.assertIn("separate read-only context from the executor", reviewer_role)
        self.assertIn("Do not edit files", reviewer_role)
        self.assertIn("Review is read-only", self.replan)

    def test_local_fix_and_systemic_replan_have_distinct_recipients(self):
        self.assertIn("same executor", self.strategy)
        self.assertIn("decision/planner agent", self.strategy)
        self.assertIn("updated plan to the executor", self.strategy)

    def test_owner_decision_is_never_resolved_by_planner_or_executor(self):
        self.assertIn("Unresolved product, compatibility, acceptance, or owner decisions go to the user", self.strategy)
        self.assertIn("do not dispatch the executor to choose", self.replan)

    def test_missing_required_role_blocks_at_its_stage(self):
        self.assertIn("for each role at the stage it is needed", self.strategy)
        self.assertIn("BLOCKED_STAGE", self.strategy)
        self.assertIn("before the dependent stage", self.strategy)

    def test_safe_sync_continues_original_task_with_routed_generic_context(self):
        self.assertIn("automatically run", self.project_preflight)
        self.assertIn("continue the original user request", self.project_preflight)
        self.assertIn("RUNTIME_RESTART_RECOMMENDED", self.project_preflight)
        self.assertNotIn("RUNTIME_RESTART_REQUIRED", self.project_preflight + self.autopilot)
        for required in ("PROJECT_ROOT", "original user request", "acceptance", "model", "reasoning_effort",
                         "current plugin role contract"):
            self.assertIn(required.lower(), (self.preflight + self.project_preflight).lower())
        self.assertIn("Do not ask the user to approve safe sync", self.preflight)
        self.assertIn("never a later implementation commit", self.project_preflight)
        self.assertIn("SYNC_CONFLICT", self.project_preflight)

    def test_stale_schema_implementation_scenario_auto_syncs_then_executes(self):
        self.assertIn("implementation task in a project with stale devcrew schema", self.evals.lower())
        self.assertIn("AUTO_SYNC → local runtime update → AGENT EXECUTION", self.evals)
        self.assertIn("without user approval", self.evals)
        self.assertIn("never repeats the request", self.evals)

    def test_generic_fallback_is_a_real_distinct_spawn(self):
        self.assertIn("generic spawned agent", self.strategy)
        self.assertIn("distinct context", self.strategy)
        self.assertIn("exact role contract", self.preflight)

    def test_old_subjective_inline_and_parent_executor_rules_are_absent(self):
        for obsolete in ("Can I safely complete this task myself", "parent or one executor implements",
                         "self-completion check"):
            with self.subTest(obsolete=obsolete):
                self.assertNotIn(obsolete, self.strategy + self.preflight + self.task)

    def test_routing_examples_obey_direct_vs_agent_execution_boundary(self):
        rows = {line.split("|", 2)[1].strip(): line for line in self.evals.splitlines()
                if line.startswith("|") and line.count("|") >= 3}
        for case in ("method-test", "rest-known", "race"):
            with self.subTest(case=case):
                self.assertIn("AGENT EXECUTION", rows[case])
                self.assertIn("executor", rows[case].lower())
                self.assertIn("independent reviewer", rows[case].lower())
        for case in ("task-agent-api-tests", "task-agent-payment-state"):
            with self.subTest(case=case):
                self.assertIn("AGENT EXECUTION", rows[case])
                self.assertIn("executor", rows[case].lower())
                self.assertIn("independent reviewer", rows[case].lower())
        for case in ("validation", "bug-failing-test", "task-direct-metrics-cron",
                     "task-direct-local-boundary"):
            with self.subTest(case=case):
                acceptance = rows[case].split("|")[3].strip()
                self.assertRegex(acceptance, r"^DIRECT(?:\b|:|;)")
        for case in ("small-bug", "task-direct-metrics-cron-ambiguous"):
            with self.subTest(case=case):
                self.assertIn("AGENT EXECUTION", rows[case])
        for case in ("task-agent-payment-cron", "task-agent-security-cron",
                     "task-agent-data-api-concurrency", "task-direct-scope-escalation"):
            with self.subTest(case=case):
                self.assertIn("AGENT EXECUTION", rows[case])
        self.assertIn("DIRECT", rows["task-direct-mechanical"])
        self.assertIn("no executor/planner/reviewer required", rows["task-direct-mechanical"])

    def test_decision_agent_is_initial_planner_only_when_plan_is_insufficient(self):
        self.assertIn("initial technical decision when AGENT EXECUTION has no sufficient accepted plan", self.setup)
        self.assertIn("sufficient current plan does not require a decision-agent call", self.setup)
        self.assertIn("For initial planning", self.replan)
        self.assertIn("TECHNICAL_REPLAN", self.replan)

    def test_postflight_routes_local_fix_without_coordinator_implementation(self):
        self.assertIn("`LOCAL_FIX` to the same executor", self.postflight)
        self.assertIn("`SYSTEMIC_REPLAN` to decision-agent and then executor", self.postflight)
        self.assertIn("`OWNER_DECISION_REQUIRED` to the user", self.postflight)
        self.assertIn("coordinator does not edit delegated production code", self.postflight)
        self.assertIn("For DIRECT, the host may correct its own mechanical change", self.postflight)
        self.assertIn("Python helper that prepares the dispatch cannot catch an exception", self.postflight)
        self.assertIn("RUNTIME_ERROR` is exactly", self.postflight)
        self.assertIn("Interrupting an agent does not", self.postflight)

    def test_task_executor_payload_is_bound_to_task_worktree_and_preserves_bounded_medium_route(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        project = pathlib.Path(directory.name) / "repo"
        project.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(project), *args], check=True,
                                  capture_output=True, text=True)
        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        (project / "owned.py").write_text("base\n")
        git("add", "owned.py")
        git("commit", "-qm", "base")
        base_sha = git("rev-parse", "HEAD").stdout.strip()
        result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "task-worktree", "create",
                                     "--worktree-request", "Use a worktree for this task",
                                 "--project", str(project), "--task-id", "T07", "--base-sha", base_sha],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        task_worktree = receipt["task_worktree_root"]
        receipt = devcrew_context.devcrew_worktree.resolve(project, "T07")
        manifest = {
            "schema_version": 1, "task_id": "T07", "task_mode": "SINGLE_PHASE",
            "semantic_contract": {"capabilities": ["payment amount behavior"]},
            "worktree": {"repository_id": receipt["repository_id"],
                         "task_worktree_root": receipt["task_worktree_root"],
                         "branch": receipt["branch"], "base_sha": receipt["expected_head"]},
            "current_phase_id": "AMOUNT",
            "phases": [{"phase_id": "AMOUNT", "goal": "Correct amount behavior",
                        "depends_on": [], "owned_scope": ["owned.py"],
                        "acceptance": ["amount behavior works"],
                        "verification": ["amount tests pass"],
                        "commit_boundary": "Amount behavior capability", "commit_subject": "type(T07): correct amount behavior", "status": "PLANNED"}],
        }
        planner = phase_review.issue_planner_receipt(project, "T07", base_sha,
                                                     "planner payload", "planner-T07")
        manifest["planner_dispatch"] = {"spawn_receipt_id": planner["SPAWN_RECEIPT_ID"],
                                       "route_id": planner["ROUTE_ID"], "agent_id": planner["AGENT_ID"],
                                       "model": planner["RESOLVED_MODEL"], "effort": planner["REASONING_EFFORT"],
                                       "spawn_mode": planner["SPAWN_MODE"]}
        devcrew_phase.save_manifest(devcrew_phase.manifest_path(project, "T07"), manifest, receipt)
        runtime = {"models": [
            {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
            {"id": "strong-test", "capabilities": ["strong"], "efforts": ["low", "medium", "high"]}],
            "roles": {"executor-agent": {"available": True, "model": "strong-test",
                                             "reasoning_effort": "high"}},
            "generic_available": True, "explicit_model_effort": True}
        route = devcrew_cost.route("executor", runtime=runtime,
                                   implementation_complexity="NORMAL", task_class="implementation")
        payload = devcrew_context.routed_task_spawn(
            "executor", "Implement the accepted task", route, runtime,
            task_id="T07", project_root=str(project), task_worktree_root=task_worktree,
            base_sha=base_sha, owned_scope=["owned.py"], acceptance=["behavior works"],
            verification=["python -m unittest tests.test_task"],
            receipt=receipt,
            role_contract="Exact executor-agent role contract")
        spawn = payload["spawn"]
        self.assertEqual(spawn["agent_type"], "default")
        self.assertEqual((spawn["model"], spawn["reasoning_effort"]), ("bounded-test", "medium"))
        for field in ("TASK_ID", "PROJECT_ROOT", "TASK_WORKTREE_ROOT", "TASK_WORKTREE_RECEIPT", "BASE_SHA", "OWNED_SCOPE",
                      "ACCEPTANCE", "VERIFICATION"):
            self.assertIn(field, spawn["message"])
        self.assertIn(task_worktree, spawn["message"])
        self.assertEqual(payload["dispatch"]["SPAWN_REASON"], "pinned_model_mismatch")

    def test_task_dispatch_keeps_host_integration_separate_from_task_roles(self):
        self.assertIn("synchronization belong to `HOST_PROJECT_ROOT`", self.project_preflight)
        self.assertIn("task-local role files and sync are not prerequisites", self.preflight)
        self.assertIn("`PROJECT_ROOT`, `TASK_WORKTREE_ROOT`, `TASK_ID`", self.preflight)
        self.assertIn("`TASK_WORKTREE_ROOT`", self.preflight)

    def test_executor_role_supports_assigned_current_root_and_forbids_fallback_and_commit(self):
        for required in ("only production write root", "TASK_WORKTREE_ROOT",
                         "When `TASK_WORKTREE_ROOT == PROJECT_ROOT`",
                         "When they differ, treat `PROJECT_ROOT` as read-only",
                         "Do not switch branches", "create or remove worktrees", "Do not commit",
                         "TASK_WORKTREE_BOUNDARY_VIOLATION"):
            self.assertIn(required, self.executor)

    def test_direct_policy_keeps_existing_checkout_behavior(self):
        direct = self.strategy.split("## DIRECT", 1)[1].split("## AGENT EXECUTION", 1)[0]
        self.assertNotIn("TASK_WORKTREE_ROOT", direct)
        self.assertNotIn("task-worktree create", direct)

    def test_first_independent_local_review_finding_may_remain_local_fix(self):
        self.assertIn("first independent local finding may be classified as", self.replan)
        self.assertIn("`LOCAL_FIX`", self.replan)

    def test_recurrent_root_review_findings_stop_before_systemic_replan(self):
        self.assertIn("Independent first `LOCAL_FIX` findings", self.replan)
        self.assertIn("evidence-backed continuation assessment", self.replan)
        self.assertIn("Stop with `LOOP_DETECTED` for repeated actions", self.replan)
        self.assertIn("Technical replans remain internal", self.replan)
        self.assertIn("one analogous path", self.replan)

    def test_every_candidate_and_final_commit_requires_fresh_full_suite(self):
        rule = "Every candidate/final commit, including a `LOCAL_FIX` replacement, requires a fresh successful full test suite on the current tree."
        for document in (self.replan, self.postflight, self.autopilot):
            self.assertIn(rule, document)
            self.assertIn("DevCrew does not classify", document)

    def test_systemic_replan_reuses_one_bounded_impact_inventory(self):
        self.assertIn("one bounded impact inventory", self.replan)
        self.assertIn("reuse it for one closure pass", self.replan)


if __name__ == "__main__":
    unittest.main()
