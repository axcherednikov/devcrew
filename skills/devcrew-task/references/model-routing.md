# Per-slice model and context choice

Use models actually exposed by the host and preserve explicit user choices.
Apply the shared [cost policy](../../../docs/cost-policy.md) to every spawn in
AGENT EXECUTION, including initial planner, executor, reviewer, research and
replacement contexts. This skill permits explicit model/effort selection for
its subagents; it does not authorize global settings or model changes to a
user-owned task. DIRECT and AGENT EXECUTION classification stays unchanged.

Assess COORDINATION_COMPLEXITY, DECISION_COMPLEXITY,
IMPLEMENTATION_COMPLEXITY, REVIEW_COMPLEXITY and RISK independently. Simple
coordination requires strong capability/low effort; complex coordination needs
medium effort. Planner uses ROUTINE/low, NORMAL/medium, HARD/high strong
reasoning, or DEEP/high deep reasoning with a declared strong fallback.
An executor requires bounded capability at medium for NORMAL/COMPLEX work.
Use low effort for explicitly mechanical slices. The independent reviewer
requires strong capability at low, medium or high effort for routine, normal or
complex review respectively, and high effort for HIGH RISK. If no observed
model advertises the role's capability and effort, block dispatch. Select
among qualified models by runtime priority and then inventory order.
Factual research uses bounded low/medium.

Risk, size and retries never escalate executor capability. SYSTEMIC_REPLAN
returns to the technical decision layer before another executor dispatch.
PLAN_INSUFFICIENT returns to the planner and TASK_TOO_LARGE to coordination
for smaller slices. LOCAL_IMPLEMENTATION_DIFFICULTY stays with the executor
while within agreed acceptance and scope.

Before dispatch, use `devcrew_cost.route` or the snapshot-aware read-only CLI
`python3 scripts/devcrew_cost.py --runtime RUNTIME.json --request REQUEST.json`.
Supply current runtime availability, capability suitability, supported efforts,
loaded roles, and explicit model/effort override support. Do not infer any of
these from a TOML file or the model name. A route with no resolved model blocks
dispatch. Retain the complete route record (ROLE, TASK_CLASS, all four complexity
fields, RISK, MODEL_CLASS, RESOLVED_MODEL, REASONING_EFFORT, SELECTION_REASON);
unrelated dimensions may be null. Record compact reasons, never reasoning history.

Before any actual spawn, build and validate the complete payload with
`devcrew_context.routed_spawn` (or the reviewer/replacement builder that uses
it). Inspect `dispatch.SPAWN_MODE` and `dispatch.SPAWN_REASON`, then submit the
returned `spawn` object using its exact `agent_type`, `model` and
`reasoning_effort`. Use the returned typed/generic selection directly; do not
try the typed role first or create a probe agent to discover override support.
Python constructs requests only and never launches or probes agents. The
reviewer payload carries the same selection in its `DISPATCH_SELECTION`
message record. Generic fallbacks receive explicit controls and the exact role
contract. No spawn may silently inherit the coordinator or runtime model.

`SPAWN_MODE=typed` requires an available role with matching model/effort
(`typed_model_effort_match`) or explicit evidence that this particular loaded
role supports overrides (`typed_supported_override`). The snapshot's
`roles[agent_type].supports_overrides=true` must represent that role-specific
runtime guarantee; model/effort fields on the generic spawn tool only establish
`explicit_model_effort`, never typed-role override support. Absent or unknown
role-specific override support is not permission to test a conflicting role.
`SPAWN_MODE=generic` records `pinned_model_mismatch`, `pinned_effort_mismatch`,
`unavailable_role` or `unconfirmed_role_override` before any runtime call.

Custom role files are defaults and may pin model/effort. Use a typed role only
when the runtime confirms it can honor the selected parameters, including a
matching pinned configuration. On conflicting pins or unavailable typed role,
use a distinct generic agent with the exact supplied role contract and explicit
model/effort controls, or return `AGENT_RUNTIME_REQUIRED`. Never silently accept
a different route or rewrite project TOMLs. In particular, a local
high-effort executor pin cannot override a selected bounded/medium payload. Full-history
forks may prohibit overrides; use `fork_turns="none"` for these bounded spawns.

Prepared requests remain `ROUTE_APPLIED=false`. Record effective runtime
model/effort separately, using `devcrew_cost.confirm_route` only with an actual
effective observation. Missing confirmation stays UNCONFIRMED; mismatches stop
dependent work. Neither a role-file name nor a successful payload build proves
the route was applied. Follow the active host schema for supported controls.

Every prepared route has a unique `ROUTE_ID`. The coordinator may not call the
runtime directly: it must submit the exact builder's spawn payload and then
record the observed `AGENT_ID`, spawn mode, task/phase identity, and fresh
review context in `capacity-report`. A successful call persists a spawn
receipt linked to the route; a direct or unreceipted spawn is not a valid
DevCrew dispatch and cannot close a role gate. Receipts preserve the full
machine-readable route, including null values for unrelated complexity
dimensions.

Keep the parent's context compact: exact file sections, shared invariants, accepted decisions, output contract and next action. Use scoped read-only digests for large logs. Persist the card at stage boundaries; do not prescribe a context-token handoff threshold without an available measurement. A user-requested single uninterrupted run remains valid. Limit concurrency to exposed capacity and the shared resource budget; reuse a relevant agent for follow-ups rather than spawning duplicates after an ambiguous status.
