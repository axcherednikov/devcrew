# DevCrew cost policy

For native collaboration-agent waits, use the shared [bounded waiting procedure](../skills/devcrew-task/references/agent-waiting.md). Virtual-time wake counts are simulation only; actual-run observations use the coordinator's existing useful waiting activity, and token telemetry remains `UNKNOWN` without a trustworthy runtime measurement.

Select work → required capability → available runtime model. Cost is measured
for the whole task: stronger planning can cost less than an inadequate solution,
reimplementation and repeated review. No numeric cost formula is needed.
`economy` remains the default project `cost_mode`; `balanced` and `quality` are
accepted for compatibility and do not override the independent routing axes or
reduce review requirements. `scripts/devcrew_cost.py` implements this policy.
Spend strong-model tokens on decisions, architecture, systemic replanning and
independent review. Use a bounded model for implementation, tests and local fixes;
optimize the total cost of a sound task outcome, not one call in isolation.

## Independent assessments

| Assessment | Values and meaning |
|---|---|
| COORDINATION_COMPLEXITY | SIMPLE: one bounded flow. COMPLEX: several agents/projects, dependencies, stages or ambiguous dispatch. |
| DECISION_COMPLEXITY | ROUTINE: applicable precedent and obvious local choice. NORMAL: bounded choice in an existing model. HARD: several connected technical decisions or unresolved transitions/invariants. DEEP: new domain/system model, interdependent rules, states, storage and API, multiple viable architectures and costly foundational mistakes. |
| IMPLEMENTATION_COMPLEXITY | MECHANICAL: explicitly identified simple bounded operation. NORMAL: ordinary implementation of an accepted plan. COMPLEX: algorithm, concurrency implementation, heavy legacy refactor or complex data transformation. |
| REVIEW_COMPLEXITY | ROUTINE: obvious implementation. NORMAL: ordinary nontrivial review. COMPLEX: difficult independent correctness analysis. |
| RISK | LOW: limited, reversible impact. MEDIUM: material behavior/contracts with bounded recovery. HIGH: money, security/auth, tenant isolation, concurrency, critical state transitions or risky data migration. |

Risk determines protective checks, review depth and reviewer effort. Risk alone
never increases decision complexity, planner effort, or executor model/effort.
A known local payment fix is NORMAL decision + HIGH risk. A payment bug with
unresolved linked transitions/retries/late events is HARD + HIGH. Designing a new versioned file format with interdependent compatibility rules
and competing recovery strategies can warrant DEEP planning. The facts about the technical
choice determine complexity; size and risk labels alone do not imply DEEP.

| Work | Required capability / effort |
|---|---|
| Simple / complex coordinator | strong / low or medium |
| ROUTINE / NORMAL / HARD planner | strong / low, medium, high respectively |
| DEEP planner | deep / high, with an explicit strong / high fallback |
| NORMAL / COMPLEX AGENT EXECUTION executor | bounded / medium |
| Explicit mechanical bounded executor | bounded / low |
| Routine / normal / complex reviewer | strong / low, medium, high |
| HIGH risk reviewer | strong / high |
| Bounded factual research | bounded / low or medium |
| Research requiring independent complex inference | strong or deep / high |

An already-running coordinator cannot necessarily change its own model. Select
its route before session start when supported; otherwise record the limitation.
Planner and reviewer are selected independently. A sound accepted plan avoids
redundant planning; an absent or insufficient plan requires the existing
AGENT EXECUTION planner stage. This policy does not alter DIRECT or dispatch
semantics, agent counts, workspace ownership or fan-out.

Every production executor must advertise the bounded capability and the requested
effort. Complexity, risk and retries never escalate executor capability. Every reviewer
must advertise the strong capability and the requested effort. A model may qualify
for more than one role. Always start the reviewer in a fresh, independent context.
If the required capability or effort is unavailable, block that dispatch.

Project role templates inherit the host's model and supply initial effort defaults.
Owners can configure a local model pin. Synchronization removes superseded package
defaults and preserves user overrides. Start a new session to load the updated roles.
A manual capacity retry uses the exact issued route and payload of the original
attempt. A new model selection requires a fresh route.

When execution exposes a problem, route by cause within the accepted plan: `PLAN_INSUFFICIENT` returns to the decision/planner stage;
`TASK_TOO_LARGE` returns to coordination/planning for smaller slices;
`LOCAL_IMPLEMENTATION_DIFFICULTY` stays with the executor while it is
local and within the accepted plan. A material new decision or disproved plan
assumption requests replan. Local review fixes remain with the executor within the
shared [loop guard](../skills/devcrew-task/references/loop-guard.md); repeated
failure requires evidence-backed assessment of progress and proportionate cost,
not an automatic owner question or model escalation. A first systemic
review finding uses the existing planner/replan protocol. Do not change that
protocol's receipt classification or semantics.
SYSTEMIC_REPLAN/TECHNICAL_REPLAN selects planner (at least HARD), then returns
the accepted plan to an independently selected executor. The route API accepts
`task_class=REPLAN_REQUEST` only for the technical decision dispatch after the
coordinator classifies the receipt; it does not classify receipts or replace
the shared replan protocol. Local fixes remain with executor and owner choices
remain with the owner.

## Runtime snapshot and route record

Pass an explicit snapshot from the active tool schema/authoritative runtime
observation. Use the identifiers reported by that host. The identifiers below illustrate the
snapshot schema. Replace them with real runtime observations:

```json
{
  "models": [
    {"id": "example-bounded", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
    {"id": "example-strong", "capabilities": ["strong"], "efforts": ["low", "medium", "high"]},
    {"id": "example-deep", "capabilities": ["deep", "strong"], "efforts": ["high"]}
  ],
  "explicit_model_effort": true,
  "generic_available": true,
  "roles": {
    "executor-agent": {"available": true, "supports_overrides": false,
                       "model": "example-strong", "reasoning_effort": "high"}
  }
}
```

Only list actually available models and suitable capability classes. Optional
integer `priority` ranks suitability within a capability tier (higher first).
Among equally ranked models the host's inventory order breaks ties. For planning
and research the resolver may use another suitable tier, marking it `FALLBACK`.
Executor and reviewer require their own capability class. Their role, effort and
fresh-context requirements remain in force across retries and cost modes.
Unsupported requested effort or absent capability produces `BLOCKED`, not a
silent downgrade. A missing snapshot gives `RUNTIME_REQUIRED` and a null
resolved model.

Every route records `ROLE`, `TASK_CLASS`, `COORDINATION_COMPLEXITY`,
`DECISION_COMPLEXITY`, `IMPLEMENTATION_COMPLEXITY`, `REVIEW_COMPLEXITY`, `RISK`,
`MODEL_CLASS`, `RESOLVED_MODEL`, `REASONING_EFFORT`, `SELECTION_REASON`.
Irrelevant complexity dimensions are null. Reasons are short facts such as
`routine_decision`, `normal_decision`, `architectural_complexity`,
`deep_system_design`, `systemic_replan`, `high_risk_review`,
`complex_coordination`, `normal_implementation` and
`mechanical_implementation`; never store chain-of-thought. Executor selection
reasons never encode risk- or retry-based model escalation.
Legacy `model`, `effort`, `reason` aliases remain available.

## Dispatch and read-only dry-run

```text
python3 scripts/devcrew_cost.py --runtime RUNTIME.json --request REQUEST.json
```

A request is one object or a list, for example
`{"role":"planner","risk":"high","decision_complexity":"NORMAL"}`.
The command only prints calculated routes; it never spawns agents or runs model
evaluations. Exit 2 indicates invalid input or an unresolved route. The legacy
`devcrew.py cost-route` CLI still accepts project cost mode and old arguments,
but without a runtime snapshot its result is an unresolved policy recommendation.
Use the snapshot-aware command for dispatch preparation.

Use `devcrew_context.routed_spawn` for a bounded initial dispatch,
`reviewer_spawn` for review, or `prepare_handoff` for a replacement. These build
payloads only; the coordinator actually calls the host's `spawn_agent` with the
returned `model` and `reasoning_effort` and `fork_turns="none"`. They fail before
producing a payload when no explicit route can be honored. Every returned
message contains the route record. The builders validate the route against the
current snapshot, preventing stale or manually substituted model settings.

In AGENT EXECUTION, `HOST_PROJECT_ROOT` owns project integration, sync,
routing, and check evidence. `TASK_WORKTREE_ROOT` is the explicit execution
root in the exact generic or typed executor contract. It equals PROJECT_ROOT
by default; only an explicit user request permits a separate worktree. Task
worktrees do not need copied role files or a separate sync before dispatch.

The host runtime calls `spawn_agent`; these Python payload builders cannot
catch its runtime exceptions. After the call, the host reports the observed
result through the capacity postflight contract. A confirmed exact thread-limit
rejection blocks the current role. It does not trigger model/effort escalation,
another-role fallback, automatic retry, or task-worktree mutation. Manual
resume uses the saved route and is one explicit host spawn attempt.

TOML contains a role contract and safe defaults. Explicit spawn routing takes
precedence only if the runtime confirms override support. A matching pinned
role is usable; a conflicting or unavailable typed role requires a generic
spawn with the exact supplied `role_contract` and explicit model/effort. If
that is unavailable, report `AGENT_RUNTIME_REQUIRED`. Never silently preserve
a conflicting role default. For example, a high-effort local pin cannot silently replace a medium-effort route. No role TOML is migrated by routing.

A prepared payload has `ROUTE_APPLIED=false`, even when parameters are explicit.
After dispatch, `confirm_route(selection, observation)` compares runtime's
effective `model` and `reasoning_effort`: exact agreement yields `CONFIRMED`
and `ROUTE_APPLIED=true`; differing values yield `MISMATCH`; missing evidence
stays `UNCONFIRMED`. Stop dependent work on a mismatch; retain unavailable
confirmation as a runtime limitation. A constructed payload is not proof of an
actual launch. This Python helper has no desktop spawn API or live model eval.

Use one executor at a time and one fresh reviewer after the candidate settles.
The parent computes simple READY state and runs deterministic verification. A
review FAIL is classified by root cause: local findings go to the executor;
systemic technical causes go through the conditional internal decision-agent;
owner/product choices go to the owner. This conditional decision call uses the independently selected planner route
and adds no work to a normal implementation path with an adequate plan.
The mandatory loop guard overrides these continuation routes. Repeated FAIL alone does not require an owner question. Continue confirmed
technical corrections only with evidence of progress, a bounded next action,
expected result/check and proportionate cost. Repeated actions without new
discriminating evidence, oscillation or unresolved repeated disputes return
`LOOP_DETECTED`: stop dependent automatic work and report the technical blocker.
Ask the owner only for a concrete owner choice, missing data/access or material
work/cost authorization. Existing allowance guardrails still apply; never invent
measured usage. Changing model/effort or rewording a finding is not progress. A PASS
belongs to the captured HEAD and worktree fingerprint: unchanged state goes
straight to commit after the pre-commit deterministic checks. If state changes,
review the changed semantic diff; a mechanical formatting or metadata fix with
no semantic change needs only affected deterministic checks and a recorded
comparison of the semantic diff.

Use targeted RED/GREEN tests while implementing. Every candidate/final commit, including a `LOCAL_FIX` replacement, requires a fresh successful full test suite on the current tree. Run every ordered verification binding before candidate creation; do not defer the full suite. DevCrew does not classify targeted checks. Store evidence against the exact tree and reject stale or failed records.

Store full command logs in `run-check` evidence. Use `check-summary --record`
for command, exit code, final line, failure tail and paths; inspect further
only when needed. The reviewer gets the task contract, relevant ADR sections,
final diff, implementation summary, and compact verification evidence. The
executor gets one task and referenced paths, not the run history, full brief,
unrelated ADRs or logs. Shared policy stays in this file; dynamic task data
stays in the dispatch package. This may help prompt caching, but the runtime
does not expose cache hits.

When context telemetry is unavailable, do not infer token thresholds. A long
multi-stage executor may finish a logical stage and hand off with TASK_ID,
GOAL, DONE, REMAINING, FILES_CHANGED, TESTS, FAILURES, DECISIONS and NEXT_ACTION.
Use `fork_turns: "none"` for the replacement. Do not split small tasks merely
to rotate agents. Save a compact per-task routing report with task, risk,
executor/reviewer model and effort, executor count/handoffs, review cycles,
full-suite and targeted-run counts, strong-model reasons, and token telemetry
availability. The helper `devcrew_cost.task_report` defines the report shape.

The desktop spawn schema exposes model/effort and `fork_turns`; custom roles
may override selected values. It does not expose per-agent token use, context
size/limit, scoped tool grants, or a Fast Mode switch to this skill. Keep
autopilot cost-oriented and do not claim to disable Fast Mode or scope tools.
