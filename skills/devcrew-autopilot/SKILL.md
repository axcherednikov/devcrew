---
name: devcrew-autopilot
description: "Execute the requested phase of an existing task graph with separate executor and reviewer contexts: select a ready task, delegate implementation, verify it, review it, and commit it atomically. For individual changes or new plans, use the corresponding workflow."
---

# DevCrew — multi-agent autopilot

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

For each task in the requested phase, apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) through the owning workflow; preserve its existing execution and review gates.

Apply the mandatory [loop guard](../devcrew-task/references/loop-guard.md): on `LOOP_DETECTED`, stop dependent automatic work, preserve evidence and resolve the technical blocker internally when possible. Ask the user only for a concrete owner decision, missing data/access or material work/cost authorization.

For collaboration-agent dispatch and completion, follow the shared [agent waiting procedure](../devcrew-task/references/agent-waiting.md). Keep the coordinator active through result delivery and the existing verification and review gates.

Use for an authorized autonomous run of an existing project task graph. Finish one task at a time within the requested phase. This is an in-session multi-agent orchestrator, not a background scheduler. It does not authorize publishing, pushing, deployment, new native Codex tasks, or a new architecture. A prohibition on commits conflicts with this workflow: stop for that decision before execution rather than running a commitless queue.

Apply [task checkout policy](../devcrew-task/references/checkout-policy.md): the current working directory is the default. Worktrees and parallel writers require an explicit user instruction; this workflow does not grant it. Keep an existing working branch; on the primary branch automatically branch from the current HEAD, preserving all local changes. Do not ask the user to choose or confirm a branch.

After resolving the target project and before reading the task graph or dispatching agents, apply the shared [project sync preflight](../devcrew-setup/references/project-sync-preflight.md). `SYNC_REQUIRED` with no conflicts is automatically synchronized locally without staging or a Git commit, and this run continues. `RUNTIME_RESTART_RECOMMENDED` does not stop the run when a generic role context can be routed with the current plugin contract and explicit model/effort. `SYNC_CONFLICT` stops the run for user resolution; a missing generic context or required route blocks only its dependent stage.

## Task routing: implementation or diagnosis

Choose the workflow from the requested outcome, not merely from uncertainty about the cause:

- A requested fix belongs to [devcrew-task](../devcrew-task/SKILL.md). This includes an obvious bug, a bug with an unknown cause when investigation is part of delivering the fix, and a fix after a diagnosis has already established its cause. Ordinary diagnosis stays inside that implementation workflow.
- Use [devcrew-diagnose](../devcrew-diagnose/SKILL.md) only when the root cause is materially unknown, proving it is a distinct necessary task, and implementation should not begin as the immediate next step. Typical cases are hard-to-reproduce or intermittent failures, regressions, performance investigations, and unexplained races when the requested outcome is evidence rather than a fix.
- A request to find and prove root cause without fixing selects `devcrew-diagnose`. A request to review a completed fix selects [devcrew-review](../devcrew-review/SKILL.md).
- An architecture audit or assessment of an existing system selects [devcrew-architecture](../devcrew-architecture/SKILL.md) only when architectural evidence and candidate directions are the primary outcome. Do not add an architecture audit to ordinary implementation, diagnosis, review, or feature design.
- Examples: “assess payment modularity/testability/refactoring opportunities” → `devcrew-architecture`; “why does payment sometimes double charge?” → `devcrew-diagnose`; “add a payment provider” → `devcrew-task`; “design a new payment subsystem” → `devcrew-brief`; “review this payment refactor diff” → `devcrew-review`.
- Never make diagnosis a mandatory stage of every bugfix. Do not route solely by words such as “unknown”, “intermittent”, or “performance”; honor a requested fix when diagnosis is simply part of completing it.
- Requests to audit or repair agent skills, skill routing, metadata, references, or their helper infrastructure select [devcrew-skill-doctor](../devcrew-skill-doctor/SKILL.md). Examples: “Why do devcrew-task and diagnose conflict?” and “Optimize routing of my skills.” Ordinary code review remains `devcrew-review`, an application architecture assessment remains `devcrew-architecture`, and an application bug investigation remains `devcrew-diagnose`. Skill Doctor is never an automatic post-task or autopilot stage.

Diagnosis is read-only with respect to production implementation. On completion it returns `ROOT_CAUSE`, `CLASSIFICATION`, `EVIDENCE`, `MINIMIZED_REPRODUCTION`, `REPRODUCTION_RATE` (when intermittent), `BASELINE` (for performance), `REGRESSION_TEST_RECOMMENDATION`, `FIX_ACCEPTANCE_CRITERIA`, and `LIMITATIONS`. If a fix is requested or accepted, route `IMPLEMENTATION_REQUIRED` to the ordinary `devcrew-task` executor workflow with this compact handoff; do not fix within `devcrew-diagnose`:

```text
STATUS: IMPLEMENTATION_REQUIRED
ROOT_CAUSE:
CLASSIFICATION:
EVIDENCE:
MINIMIZED_REPRODUCTION:
REPRODUCTION_RATE: failures / attempts | not applicable
BASELINE: measurements and method | not applicable
REGRESSION_TEST_RECOMMENDATION:
FIX_ACCEPTANCE_CRITERIA:
LIMITATIONS:
```

## Optional brief grill

Autopilot does not run grill automatically for every task. For a complex brief whose interdependent decisions or material risk profile justify stress-testing, it may use the existing optional [devcrew-brief grill mode](../devcrew-brief/references/grill.md), or recommend that the user run it before implementation. The brief thread performs the critique itself: do not create a Grill agent, fan out solely for grill, add a mandatory stage, or alter the shared dynamic cost/model-routing policy. Grill does not resolve owner decisions without owner input; unresolved dependent tasks stay blocked under the existing brief contract.

An architecture assessment is also read-only. If the user selects a candidate for implementation, preserve the evidence and uncertainty in this handoff and route clear requirements to `devcrew-task`, substantial unresolved requirements to `devcrew-brief`, or a specific failure-cause investigation to `devcrew-diagnose`:

```text
ARCHITECTURE_CANDIDATE:
GOAL:
EVIDENCE:
PROPOSED_DIRECTION:
ACCEPTANCE_INTENT:
RISKS:
RELEVANT_FILES:
OPEN_DECISIONS:
```

The parent is only the orchestrator. It may inspect compact planning state, choose one READY task, construct bounded agent payloads, run deterministic gates through tools, decide transitions, stage, and commit. It must not implement the selected task, perform a repository-wide audit, run long evaluations itself, or review its own candidate.

## Runtime capability gate

Before reading implementation code or dispatching work, inspect the active runtime tool schema and available role definitions. Real delegation requires a tool that starts an agent in a distinct model context and returns its result, such as Codex `spawn_agent` plus the matching wait/message controls. A skill invocation, role prompt applied inline, native task creation, or sequential self-prompt is not delegation.

If the active runtime has no real subagent execution mechanism, stop before implementation and report exactly:

```text
REAL SUBAGENT EXECUTION NOT AVAILABLE
```

Name the missing capability. Do not simulate delegation by applying `devcrew-task`, `devcrew-review`, or role instructions sequentially in the parent context. Do not create user-owned Codex tasks as a substitute for subagents.

When real delegation exists:

- dispatch implementation with `spawn_agent` (or the runtime's documented equivalent) to `executor-agent`; if that custom role is unavailable, use a generic writing agent with the exact executor contract below and disclose the fallback;
- dispatch review to `reviewer-agent` in a different, separate fresh agent context after the candidate and evidence are stable; if the role is unavailable, use a generic read-only agent with the reviewer contract below;
- the executor must not review its own work, and the parent must not review it inline;
- reuse the same executor agent for in-scope fixes when the runtime supports follow-ups, sending only the review findings and any newly relevant evidence;
- dispatch executor and reviewer with `fork_turns: "none"` when the runtime exposes it. If the runtime cannot bound inherited history, state that limitation and still send only the bounded task payload.

Every planner, executor, reviewer, and decision spawn in this workflow must be
prepared through the routed spawn builder and carry its `ROUTE_ID`. Direct
runtime calls without a route are contract violations and cannot satisfy a
task gate. After each actual spawn, record `AGENT_ID`, `FORK_TURNS`, and the
exact route/dispatch through `capacity-report`; only its persisted matching
spawn receipt proves the route was applied. A phase reviewer is authoritative
only after `review-start`, against its exact base/target snapshot, with a fresh
context and a persisted matching report. An unrouted or inherited-context
review may be retained as advisory context only.

Agent capacity is a hard resource limit. Run executor and reviewer sequentially because review depends on the final candidate. A coordinator agent is optional: the parent should cheaply compute READY itself unless dependency reconciliation is genuinely complex, in which case it may dispatch one bounded read-only coordinator context.

## Cost and model policy

Route by the active runtime's capability/cost class using the shared [cost policy](../../docs/cost-policy.md). Do not hardcode model names in this skill. Economy is the autopilot default. Assess coordination, technical decision, implementation and review complexity separately from risk. Resolve every planner/executor/reviewer/replacement route from an explicit runtime snapshot with `devcrew_cost.route` or `scripts/devcrew_cost.py --runtime RUNTIME.json --request REQUEST.json`.

Simple coordination uses strong/low; complex coordination strong/medium. Planner ROUTINE/NORMAL/HARD uses strong low/medium/high, while DEEP uses the best confirmed deep/high model or an explicit strong/high fallback. An executor requires bounded capability at medium for NORMAL/COMPLEX work, or low effort for an explicitly mechanical slice. Risk, task size, retries and repeated failures never escalate executor capability. Independent review requires strong capability at low/medium/high effort for routine/normal/complex review, always high for HIGH RISK. Block dispatch when the required capability or effort is unavailable. Generic fallback preserves the model and effort selected from the runtime inventory. Narrow factual research uses bounded low/medium. A missing or insufficient accepted plan requires the existing planner stage; adequate plans avoid redundant planning.

Keep problems at the correct layer under the accepted plan: `PLAN_INSUFFICIENT` returns to planner; `TASK_TOO_LARGE` returns to coordination/planning for smaller slices; `LOCAL_IMPLEMENTATION_DIFFICULTY` stays with the executor while it remains within the accepted plan. A material decision or disproved assumption requests replan through the existing protocol. Local review fixes remain with the executor within the loop guard; recurrence stops for a user answer. Strong model budget is for decision, architecture, systemic replan and independent review; the executor handles bounded implementation, tests and local fixes to minimize total task cost while preserving quality.

Persist ROLE, TASK_CLASS, COORDINATION_COMPLEXITY, DECISION_COMPLEXITY, IMPLEMENTATION_COMPLEXITY, REVIEW_COMPLEXITY, RISK, MODEL_CLASS, RESOLVED_MODEL, REASONING_EFFORT and SELECTION_REASON. Unrelated complexity fields are null. Keep reasons compact; do not store chain-of-thought. SYSTEMIC_REPLAN routes back to planner, then an independently selected executor.

Use the context helper's spawn builders and pass the returned explicit `model` and `reasoning_effort` into the actual `spawn_agent` call. These Python helpers only prepare payloads. Project role definitions hold defaults; if a typed role pins conflicting settings, use an available generic agent with the exact role contract and explicit model/effort, or report `AGENT_RUNTIME_REQUIRED`. Never preserve a conflicting default silently. The historical local executor default cannot override the selected route. A prepared route remains unapplied until an effective runtime observation confirms it; record missing confirmation separately and stop on a mismatch.

The skill cannot downgrade or replace the already-running parent model. Its routing authority is limited to subagent spawn controls exposed by the host; record this as a runtime limitation when the parent's cost class cannot be selected before the run.

## Agent context lifecycle

Apply the shared [agent context policy](../../docs/agent-context.md) to the orchestrator, executors, and reviewers. Read optional `agent_context` from the existing project `.codex/devcrew.json`. Record runtime-supplied observations with unknown fields as null. Run `scripts/devcrew.py context-evaluate --project PROJECT --agent AGENT.json` at safe stage boundaries and before a large new step when a fresh observation exists. Do not infer context from cumulative tokens, transcript length, a model name, or Codex allowance. When no trustworthy context measurement exists, record it unavailable and use the cost policy's bounded logical-stage handoff for long tasks.

The phase lifecycle is independent of telemetry: `phase resume` exposes `executor_lifecycle: NEW_CONTEXT_REQUIRED` for independent executor work, including when measurements are null. Each independent phase receives a new actual executor with `fork_turns: "none"`. Active same-phase repair reports `LOCAL_FIX_CONTINUATION_ALLOWED` and may retain an executor only through the linked continuation receipt protocol. If a required fresh context cannot be started, stop under the required-handoff/runtime/capacity blocker; do not reuse the prior executor or change its route.

If the current runtime exposes `get_usage_limits`, use its current result only for separately configured allowance guardrails; the helper can map that result when supplied in the observation. If no such tool exists, record `CODEX_ALLOWANCE_TELEMETRY_NOT_AVAILABLE`. Never scrape the account UI or treat allowance as agent context.

On `HANDOFF_RECOMMENDED`, let the agent finish its current safe atomic step and return a structured state. If more work remains and automatic handoff is enabled, replace it before the next substantial step. On `HANDOFF_REQUIRED`, forbid new substantial work; finish only an indivisible operation whose interruption is riskier, then replace the agent. A hard signal on the first measurement requires immediate handoff. If a required replacement cannot be spawned, stop with `AGENT_HANDOFF_REQUIRED_BUT_UNAVAILABLE`.

Use `context-handoff` to form the compact package with the original task contract and task ID, then call `spawn_agent` with the returned explicit `model`, `reasoning_effort` and `fork_turns: "none"`. Do not fork the old executor or reviewer history. The replacement continues the same task and does not create a commit. Repeat this procedure for multiple handoffs. Spawn each reviewer with a fresh context, and hand off a reviewer that reaches a threshold before it starts another review pass. Do not raise model class merely because context grew; follow the existing cost policy. After each task, use `context-report` for a best-effort usage summary, preserving unknown values.

## Progressive context

The orchestrator reads context progressively and stops as soon as the next decision is grounded:

- **Level 1:** card/EPIC, task index, statuses, dependency graph, run inventory, and working-tree ownership state.
- **Level 2:** only the selected current task and its acceptance criteria.
- **Level 3:** only linked ADR or brief sections required by that task; do not reread an entire brief after every commit.
- **Level 4:** exact implementation files, contracts, analogous code, and tests needed by the executor. Prefer named paths and section ranges over whole documents.

The parent usually stops at Level 3. Level 4 belongs in the executor payload and the executor reads those repository paths in its own context. Large logs remain local; return command, exit code, concise result, and evidence path. After each task persist only the short state defined below, so the next agent receives state rather than previous reasoning history.

## Freeze the run boundary

Read applicable `AGENTS.md`, relevant `.codex/devcrew.json` paths, and the Level 1 artifacts. Follow the project's existing layout and vocabulary; do not initialize a graph. Determine `requested phase` from the user's explicit boundary, otherwise the authoritative EPIC/card. Record its exact identity and task IDs. Ambiguous or conflicting phase information requires a user decision. Freeze this boundary; a later phase becoming READY is not authorization to enter it.

Apply [devcrew-coordinate](../devcrew-coordinate/SKILL.md) for dependency truth, ownership, inventory, and resume reconciliation, but keep the parent read to compact artifacts. Brief/ADR requirements outrank task details. Never rewrite acceptance criteria to fit implementation.

Before selection and each commit, inspect HEAD, index, tracked/untracked changes, and active writers. Unknown dirty state or unexplained HEAD/index changes are a hard stop: preserve them; do not stash, reset, clean, stage, or commit them. On resume, recognize owned changes only from the recorded baseline and exact diff, and reconcile any already-created commit before retrying.

## Compute READY and choose one task

Map project statuses without rewriting them and reconcile dependency disagreements before relying on them.

`READY(T) = T belongs to requested phase AND status permits execution AND every dependency is accepted/completed with required evidence AND no unresolved blocker AND ownership/resources permit execution`.

A label alone, a completed chat, an unverified DONE label, a missing dependency, or a cycle cannot satisfy READY. Select exactly one READY task by declared priority/order, with stable task ID as the tie-breaker. Leave all other READY tasks untouched until the current cycle commits. Recompute from actual artifacts after each completed cycle.

For a task-local blocker, record reason, evidence, and unblock condition, then search for another READY task. If a blocker appears after edits, do not mix partial work into the next task. Reverse only precisely owned changes and verify the baseline; if ownership or safe restoration is uncertain, stop for a user decision. When no READY task remains, a small separate read-only investigation agent may inspect repository evidence for the blocker. Ask the user only when an actual product, architecture, or safety decision is still required.

## Build the executor package

Run [dispatch preflight](../devcrew-task/references/dispatch-preflight.md), capture the baseline, and assign explicit file ownership. Send one bounded package, not the parent history or whole repository context:

```text
TASK ID:
GOAL:
TYPE:
IN SCOPE:
OUT OF SCOPE:
ACCEPTANCE:
DEPENDENCIES:
RELEVANT FILES:
RELEVANT ADR/BRIEF SECTIONS:
VERIFICATION STRATEGY:
OWNED PATHS:
BASELINE/EVIDENCE PATHS:
LANGUAGE_PROFILE: (when relevant: primary/affected languages, supported version/range, runtime/framework, relevant library versions, evidence paths)
CONFIRMED CONTEXT (include confirmed decisions as facts):
REPOSITORY INSTRUCTIONS:
RELEVANT ADR:
SAFETY BOUNDARIES:
DELIVERABLES:
OPEN DECISIONS / BLOCKERS (list only dependent task IDs; independent DAG branches remain eligible):
```

Keep this package self-contained for an executor with `fork_turns: "none"`:
include the confirmed task contract, scope, acceptance, relevant ADR, exact
files/instructions, verification, safety boundaries, deliverables, and blockers.
Do not include conversation history, chain of thought, unrelated task files,
full logs, or a whole brief when linked sections suffice. Include
`LANGUAGE_PROFILE` only when the task needs it. Build it once from verified
repository evidence; the executor need not reread all manifests/CI unless the
evidence is stale or conflicts with the checkout. Every concrete path,
command, version, or resource name must be verified or labelled for runtime
verification. Existing task/handoff artifacts remain the source of truth; do
not create a mandatory implementation-prompt file.

When a brief has unresolved material questions, block only tasks whose scope
depends on those answers; continue independent READY branches. Pass confirmed
decisions as facts and include only ADRs relevant to the task scope. A brief
recommendation without owner confirmation is not a requirement. Handoffs
contain the final confirmed decisions and relevant ADR references, never the
question-and-answer transcript.

The executor follows [devcrew-task](../devcrew-task/SKILL.md) and the shared [engineering policy](../devcrew-task/references/engineering-policy.md), including its Minimal Diff discipline. This remains part of the ordinary implementation task: do not add a Minimal Diff agent or workflow stage. It implements only this task, uses test-first for testable IMPLEMENTATION behavior, does not select the next task, expand scope, alter acceptance, or commit, and returns:

```text
STATUS: DONE | BLOCKED | NEEDS_INPUT | REPLAN_REQUEST
FILES_CHANGED:
TESTS: command, exit code, concise result, evidence path
ACCEPTANCE_RESULTS:
BLOCKERS:
DIFF_SUMMARY:
REPLAN_REQUEST: <required structured fields from shared protocol when status is REPLAN_REQUEST>
```

The orchestrator then applies [dispatch postflight](../devcrew-task/references/dispatch-postflight.md), inspects scope/evidence, and rejects unexpected paths or unresolved deviations. This is orchestration acceptance, not an inline code review.

### Replan and escalation

Use the shared [technical replan and escalation protocol](../devcrew-task/references/replan-protocol.md). Executor receipts may return `STATUS: REPLAN_REQUEST`; accept and classify them before considering the task complete. `LOCAL_FIX` returns to the executor, `TECHNICAL_REPLAN` invokes the internal decision-agent, and `OWNER_DECISION_REQUIRED` pauses dependent work for the owner. On technical replan, the current task remains incomplete: no commit and the DAG must not advance. After `APPROVED_REPLAN` or `KEEP_CURRENT_PLAN`, send the executor an updated bounded package and rerun affected verification and applicable gates before review/commit. Do not weaken acceptance or owner decisions.

## Deterministic verification

Run declared deterministic checks directly through the runtime's command tool, preferably with `scripts/devcrew.py run-check` when recorded evidence is required. Do not dispatch a reasoning model merely to wait for PHPUnit, lint, static analysis, `git diff --check`, or a benchmark command. Keep full logs in evidence files and retain only command, exit code, concise result, and record path in the orchestration context.

The executor may run targeted RED/GREEN checks while implementing. After its return, the orchestrator runs the required final checks on the settled tree. Follow the shared [review repair and verification scope](../devcrew-task/references/replan-protocol.md#review-handoff): Every candidate/final commit, including a `LOCAL_FIX` replacement, requires a fresh successful full test suite on the current tree. Targeted checks remain available during development; DevCrew does not classify them; a repeated failure requires evidence-backed progress and cost assessment rather than an automatic owner question; reuse one bounded impact inventory. Use `check-summary` to keep command output bounded. A failed, skipped, stale, or unavailable required check cannot certify success. Long benchmark or live evaluation work belongs to the executor or a purpose-built runner agent with bounded output, never the parent context.

## Independent reviewer package

After verification succeeds, dispatch a reviewer in a separate fresh agent context from both executor and orchestrator. The autopilot requirement overrides `devcrew-review`'s ordinary permission to stay inline. Supply only:

- the task contract and relevant ADR/brief sections;
- baseline and final git diff (or exact commands/paths to obtain them);
- verification evidence and TDD history where applicable;
- concise implementation summary and the cost-policy risk classification;
- explicit read-only scope.

Do not send executor reasoning or the whole conversation. The reviewer applies [devcrew-review](../devcrew-review/SKILL.md) to correctness, acceptance, architecture compliance, scope drift, test quality, backward compatibility, security, and applicable TDD evidence, then returns:

```text
REVIEW: PASS | FAIL
REVIEW_CLASSIFICATION: LOCAL_FIX | SYSTEMIC_REPLAN | OWNER_DECISION_REQUIRED | NONE
BLOCKING_FINDINGS:
NON_BLOCKING_FINDINGS:
EVIDENCE_LIMITS:
```

Scope drift, future-task implementation, changed acceptance, or a brief/ADR violation is FAIL even when tests pass. Classify by root cause, not finding count: `LOCAL_FIX` findings return to the executor; `SYSTEMIC_REPLAN` goes to decision-agent before executor work resumes; `OWNER_DECISION_REQUIRED` pauses dependent work for the owner. On FAIL, send only the blocking findings and necessary bounded evidence to the relevant next recipient. Reviewer remains read-only and coordinator owns dispatch. Then rerun affected verification and dispatch a new fresh reviewer context over the final candidate. If the corrected candidate fails again, assess evidence, the bounded next action and cost before continuing internally. Stop repeated actions without discriminating evidence or oscillation with `LOOP_DETECTED`; ask the user only for a concrete owner boundary. Reconsider a disproven review on the same candidate with preserved history and fresh independent review. Never reuse a reviewer that saw or produced the implementation, and never commit on FAIL. The reviewer uses existing verification evidence and performs only targeted spot checks when needed.

## Final gate, commit, and next task

Prepare task/card/index completion updates before final verification/review. A PASS on the unchanged captured HEAD and fingerprint remains valid through staging. Semantic edits require affected checks and independent review of the changed diff; a mechanical formatting/metadata fix requires relevant deterministic checks and an explicit semantic-diff comparison. Run declared pre-commit checks directly; if none exist, use `git diff --check` plus complete staged-diff inspection. Formatter or hook mutations invalidate affected evidence. Never bypass hooks.

After fresh verification and REVIEW PASS, the orchestration layer stages explicit task paths/hunks and creates exactly one atomic commit. Never use `git add -f`/`--force` to stage ignored files or the whole working tree; `.codex/` is reinstallable runtime state and must not enter a commit. Verify its SHA and diff. No agent may force push, use `reset --hard`, amend another author's commit, perform an unapproved destructive migration, or enter another phase. Executor and reviewer never commit.

Reconcile state, recompute READY, and persist:

```text
DONE:
COMMIT:
VERIFICATION:
BLOCKED:
IMPORTANT DECISIONS:
DISCOVERED WORK:
NEXT READY:
```

Also persist the compact cost-routing report defined in the shared cost policy, including unknown token telemetry rather than estimated counts.

Stop when the requested phase is complete, no READY tasks remain, a user decision is required, dirty-state ownership is unknown, a new ADR is needed, or an unapproved destructive operation is required. Distinguish phase completion from blocked/cyclic/missing-dependency work. Never automatically advance to the next phase.

At phase/session close, the coordinator may recommend or perform the optional [evidence-driven session retro](../devcrew-task/references/session-harvest.md) only for a completed significant epic or complex session with bounded task, review, diff, and verification evidence. Never run it after every task or for trivial/incomplete work. Reuse the coordinator/session workflow and existing memory model; do not create a Retro agent, fan out, add a mandatory stage, or make closure depend on retro. Keep the same shared dynamic cost/model-routing policy and inspect only current-scope evidence.
