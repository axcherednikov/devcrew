# Choose the execution strategy

Classify the work before substantive repository investigation. The execution
mode is determined by observable task properties, not by whether the host
believes it can finish in one pass.

## DIRECT

DIRECT is allowed only when **all** of these are true:

- The change is a local, bounded mechanical edit.
- The exact target and intended before/after behavior are explicitly requested
  or established by a verified existing contract.
- Existing context is sufficient: no substantive flow/caller investigation or
  material technical choice is needed. Bounded inspection to validate known
  context is allowed.
- The change is low risk and reversible.
- A deterministic local acceptance or regression check exists.

An observable behavior change alone does not disqualify DIRECT. Examples include
a known local boundary fix whose expected result is documented and covered by a
focused test, or disabling one explicitly unused metrics cron when its shared
configuration, consumers, and impact are already established. This does not
make cron or flag changes safe by default: payment jobs, audit/security
monitoring, cleanup jobs, and jobs with unknown consumers remain
AGENT EXECUTION. A DTO rename is DIRECT only when the complete local edit and
all affected uses are already known, with no compatibility uncertainty.

These boundaries remain AGENT EXECUTION regardless of edit size: data mutation
or migrations; API/compatibility changes; authentication/authorization/security;
payments; tenant isolation; external integration; concurrency, retry or
idempotency; failure recovery; and complex state transitions.

If relevant facts are missing, broader effects or callers emerge, or a real
technical decision is needed, stop before editing and reclassify as
AGENT EXECUTION. Bounded inspection may validate known context, but it must not
be used to investigate substantive unknown callers or effects. If any DIRECT
condition is false or cannot be established, classify as AGENT EXECUTION.

For DIRECT, preserve the current-checkout policy, load applicable requirements,
and follow the shared engineering policy for meaningful targeted tests
(test-first when applicable), safety, reporting, and mandatory project checks.
The host may inspect the bounded target, make the change, inspect the diff
inline, and run the relevant deterministic checks. No planner/executor/reviewer
lifecycle or records are required solely because the task is DIRECT. Explicit
user-requested delegation remains allowed. DIRECT does not authorize production
deployment. If inspection or implementation reveals an excluded risk or
material uncertainty, stop the direct edit and route the affected work through
AGENT EXECUTION before continuing.

## AGENT EXECUTION

AGENT EXECUTION is required if any DIRECT condition is false. The host is the
coordinator. It may read only the minimal request/project context needed to
classify the work, identify relevant roles, verify runtime capability, and
check whether an existing plan is ready for execution. It must not perform the
task's substantive investigation, choose a material technical solution, write
production code, or repair production code after review. Once work is delegated,
the host must not duplicate that work in its own context.

The coordinator must establish real separate-context dispatch before starting
AGENT EXECUTION. See [dispatch preflight](dispatch-preflight.md). A skill call,
role prompt applied in the host, or sequential self-prompt is not an agent.

### Plan selection

If an existing brief/implementation plan is current and contains confirmed
acceptance and scope, verified project/API facts, no material open decisions,
bounded implementation steps, and verification requirements, reuse its content.
For a large task, also validate the short brief below. Do not repeat planning
without a concrete gap.

Direct executor dispatch requires an existing validated phase manifest with a
persisted planner receipt for the same task and original base SHA **for formal
schema-v2 work**. Resume it
against a fresh `task-checkout resolve` receipt, revalidate the persisted planner
receipt, and dispatch only `CURRENT_PHASE` when its lifecycle permits execution.
Do not run `phase init` again for an existing manifest or bypass a validation
error by recreating it.

An external ready plan without that manifest first goes to a routed planner for
bounded validation, not planning from scratch **for formal work**. Supply the existing plan and its
evidence; check only current facts, acceptance, scope, open decisions, phase
boundaries, and verification requirements. Do not repeat research unless a
concrete gap requires it. The planner returns `PLAN_READY` with the formal phase
plan and stops. Record its actual spawn receipt for the same task and original
base SHA, initialize the manifest with `phase init --spawn-receipt-id ID`, then
dispatch the executor. A ready plan alone cannot replace a planner receipt.

Otherwise, identify the missing facts or decision. Dispatch only bounded
research roles that answer concrete questions and add independent evidence,
specialization, or useful parallelism. A decision/planner agent then synthesizes
the evidence into a bounded technical decision and executor-ready plan. The
host does not substitute its own technical decision for this role. There is no
minimum or fixed number of agents; use only roles with a stated contribution.
Unresolved product, compatibility, acceptance, or owner decisions go to the
user before dependent implementation.

Role transitions require a new coordinator dispatch with a role-matched
contract. A scout or analyst returns sourced findings to the coordinator and
cannot promote itself to planner or executor. A decision/planner agent returns
`PLAN_READY` to the coordinator and stops; it never continues into production
implementation. The coordinator creates a separate executor dispatch from the
accepted plan. The coordinator itself never writes delegated production code.

### Short brief for large tasks

For a product task, prefer a first phase that delivers one minimal working
scenario across the components needed for that scenario, when feasible; later
phases extend the working result. Describe the scenario's input or trigger, key
steps, observable result, and the checks that demonstrate it. Keep it within the
accepted task scope: a backend scenario may end at an observable API or system
result and does not imply adding a UI.

An independent technical task may end with its own verified capability and
does not need a user-facing flow or a later product scenario. For example, a
database schema change, infrastructure update, or race-condition fix can be a
complete task when its own observable acceptance and verification are met.
When a product scenario has a real prerequisite, such as a migration or
preparatory infrastructure, explain the dependency and why it must come first;
the following scenario phase should deliver the working result. A migration
may be included in the scenario phase when scope and verification remain clear.
The presence of a migration alone does not justify a layer-by-layer plan.

A large task spans several semantic capabilities, requires coordinated changes
across components, or changes a non-trivial end-to-end user/system flow. It may
be `SINGLE_PHASE` or `MULTI_PHASE`; phase count alone does not determine size.
Before implementation, the planner's `PLAN_READY` must include a short brief
with these five points:

1. **Goal** — who benefits and what outcome they need.
2. **What must change** — the intended observable behavior and in-scope changes.
3. **Out of scope** — explicit exclusions and their destination when relevant.
4. **Main user or system scenario** — the trigger/input,
   key steps, and observable result of the main flow.
5. **How to verify completion** — observable acceptance criteria and the relevant
   tests/checks that demonstrate them.

Keep this brief in the existing planner result or accepted task/brief artifact.
Reuse a current brief that covers all five points after validating its facts,
scope, and acceptance; reference it rather than rewriting it. Missing or stale
points are a concrete plan gap: the planner fills only those gaps before
`PLAN_READY`. Material unanswered requirements remain owner questions and block
dependent implementation. This is part of the existing planner contract, not
a mandatory separate `devcrew-brief` invocation, planning stage, or new document.
Small tasks and DIRECT do not acquire this five-point requirement.

### Implementation and review

When preparing a phase manifest, preserve the agreed phase goal in `goal` and
its main user/system scenario in `main_scenario`: trigger/input, key steps and
observable result, including a technical capability when applicable. Use the
current phase portion of the accepted brief; do not add later-phase work or
invent an unconfirmed scenario. Executor and reviewer receive the same goal,
scenario, acceptance, scope and constraints. Existing manifests without
`main_scenario` remain resumable; their dispatch field is null and the reviewer
reports that evidence limit. This does not require a new brief document or stage.

The executor-agent owns substantive production implementation, tests, and its
assigned checks under the accepted bounded plan and exact file ownership. It
may make local implementation choices within that plan, but does not redesign
architectural seams, domain models, confirmed contracts, or accepted strategy.
If the plan is insufficient, return `REPLAN_REQUEST` to the decision/planner;
if the slice is too large, return it to coordination/planning for smaller implementation
slices. Local implementation difficulty stays with the executor while
within the plan; if it requires a material decision, return `REPLAN_REQUEST`.
If evidence invalidates a material plan assumption, it stops affected work and returns `REPLAN_REQUEST` under the
[technical replan protocol](replan-protocol.md).

After the executor returns and deterministic checks run, an independent,
read-only reviewer-agent in a separate context is mandatory. It cannot be the
executor. Route `LOCAL_FIX` to the same executor, then rerun affected checks
and review. Route `SYSTEMIC_REPLAN` to the decision/planner agent, then send
the accepted updated plan to the executor and repeat verification and review.
Route `OWNER_DECISION_REQUIRED` to the user. The reviewer never edits code or
dispatches agents.

AGENT EXECUTION requires real runtime capability for each role at the stage it
is needed. If required dispatch or role capability is unavailable, stop before
the dependent stage and return `AGENT_RUNTIME_REQUIRED` with `MISSING_CAPABILITY`
and `BLOCKED_STAGE`. Do not fall back to host implementation. A generic spawned
agent is acceptable only when it runs in a distinct context and receives the
exact role contract.

## Agent use

Agent count is not a goal or quota. Dispatch an additional role only when it
adds independent research, specialization, useful parallelism, or independent
review. Do not introduce parallel writer waves or change model/cost routing
through this strategy.

Keep one writer per checkout. Use bounded ownership, dispatch preflight and
postflight, integrated checks, and the [engineering policy](engineering-policy.md).
Do not weaken acceptance, confirmed owner decisions, safety/security
obligations, scope ownership, evidence requirements, or verification honesty.
