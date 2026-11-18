---
name: devcrew-task
description: "Implement or fix a concrete task using DIRECT or AGENT EXECUTION. Use devcrew-diagnose for an investigation that explicitly excludes a fix."
---

# DevCrew task

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](references/task-readiness.md) during the owning workflow's targeted research; use its source-resolution and uncertainty guidance without adding another gate.

Apply the mandatory [loop guard](references/loop-guard.md): on `LOOP_DETECTED`, stop dependent automatic work, preserve evidence and resolve the technical blocker internally when possible. Ask the user only for a concrete owner decision, missing data/access or material work/cost authorization.

For collaboration-agent mailbox waits, follow the shared [agent waiting procedure](references/agent-waiting.md), including bounded waits and identity-based result reconciliation.

Use this skill for implementation, diagnosis with a requested fix, or a multi-step repository task. When the user asks for a fix, ordinary diagnosis is part of this workflow even when the cause is not yet known: use test-first → fix → verification when applicable. A separate investigation whose primary outcome is proven root cause without an immediate implementation belongs to `devcrew-diagnose`. Read applicable `AGENTS.md` files first. Consult `.codex/devcrew.json` and configured context only when relevant; lack of DevCrew state does not require setup for a simple task.

Apply [task checkout policy](references/checkout-policy.md): the current working directory is the default. Worktrees and parallel writers require an explicit user instruction; this workflow does not grant it. Keep an existing working branch; on the primary branch automatically branch from the current HEAD, preserving all local changes. Do not ask the user to choose or confirm a branch.

Once the target project is known and before substantive work or agent dispatch, follow the shared [project sync preflight](../devcrew-setup/references/project-sync-preflight.md). Reuse a fresh status already supplied by the front door for the same project/task.

First classify the request using [the execution strategy](references/implementation-strategy.md). In DIRECT, the host inspects the known bounded target and verifies its mechanical edit. In AGENT EXECUTION, the host reads only minimum routing context; assigned research agents investigate task behavior and callers, and a decision/planner agent owns any material technical decision. The host must not do the substantive investigation in parallel or instead of those roles.

**Repository evidence before user questions. User questions before unsupported assumptions.** If code, tests or project documentation reliably answer the question, use that evidence without asking again. In AGENT EXECUTION, assigned research roles gather substantive repository evidence and the planner chooses technical solutions within confirmed constraints; the host uses their sourced findings to identify owner questions. Evidence of current behavior is not permission to invent a new requirement; for restoration, apply the confirmed original behavior baseline in the shared engineering policy. If evidence is insufficient, continue targeted research; do not guess or turn a guess into a test expectation. Ask one short, concrete question only when a material requirement remains unresolved, available research cannot close a material evidence gap, or a choice carries a material unresolved risk to data, security or significant irreversible consequences. Include the known facts, risk, recommended option and rationale; use `$devcrew-brief` when substantial uncertainty needs requirements formalization. Wait for the answer before dependent implementation, while continuing independent work when useful. A brief organizes unresolved questions; it does not answer them on the user's behalf. Internal technical decisions follow existing role boundaries and replan routes, rather than becoming user choices automatically.

Use relevant research to distinguish a supported engineering choice from an unresolved owner question before implementation, then run targeted verification. Never use `request → unsupported assumption → implementation`. Do not invoke a brief when repository evidence is sufficient, ask ceremonial questions or automatically dispatch agents for clarification.

Apply [the execution strategy](references/implementation-strategy.md); do not use a self-completion judgment to choose DIRECT. Apply the shared [engineering policy](references/engineering-policy.md) in the correct role: executor/research/planning roles own substantive flow discovery and solution selection in AGENT EXECUTION; the host enforces acceptance, safety, evidence and verification gates as coordinator. In DIRECT, the host applies the policy to choose the smallest complete solution for the bounded edit. Its Minimal Diff, test-first, safety, reporting and verification rules apply to both modes.

For code/config tasks, establish the affected language/version profile from the shared policy before implementation. Limit it to touched languages and include evidence paths; resolve material conflicts as blockers. Concrete paths, versions, and commands in handoffs must be evidence-backed or explicitly marked for runtime verification.

Apply the shared [engineering requirements](references/engineering-requirements.md) workflow when explicit durable feedback arrives and before DIRECT edits. Assess one clear user requirement after its first observation, index an existing canonical source by reference, and load the current applicable context before each new bounded edit pass. Refresh after new feedback. Do not persist one-off or unresolved proposals.

Only work satisfying every DIRECT condition may be implemented by the host. A behavior change, one-method fix, or test-driven fix is not automatically AGENT EXECUTION: apply the exact-target, verified-behavior, sufficient-context, low-risk/reversible and deterministic-check criteria in the execution strategy. DIRECT keeps the existing checkout policy, applicable requirements, meaningful targeted tests (test-first when applicable), safety boundaries, inline diff review and mandatory project checks; it adds no planner/executor/reviewer lifecycle or records solely for DIRECT. If missing context, broader effects, or an excluded risk emerges, stop the direct edit and reclassify before continuing. Explicit user-requested delegation remains allowed.

For AGENT EXECUTION, the host is coordinator-only: check required role/runtime availability, validate an existing plan or dispatch bounded research and decision/planning, then send the accepted plan to executor-agent. The executor owns production code. After deterministic checks, a separate read-only reviewer-agent is mandatory. Agent count is driven by specific value, never a minimum/maximum quota. If dispatch is unavailable, return `AGENT_RUNTIME_REQUIRED` with `MISSING_CAPABILITY` and `BLOCKED_STAGE`; do not implement inline.

For **formal schema-v2** AGENT EXECUTION, the planner must classify the task as `SINGLE_PHASE` or
`MULTI_PHASE` by semantic capability boundaries and return a formal phase plan,
including dependencies, owned scope, observable acceptance, verification, and
commit boundary for every phase. Persist the validated manifest in host state
before writer dispatch. Resume a valid manifest without replanning. Dispatch
only its `CURRENT_PHASE`; the phase payload replaces task-wide narrative and
scope. A missing or mismatched manifest fails closed as
`PHASE_MANIFEST_REQUIRED` or a machine-readable phase error. Initialize the
manifest with `phase init --spawn-receipt-id ID` only when no manifest exists;
it never replaces an existing plan. Init requires the routed planner receipt
for the same task and original base SHA, and resume revalidates it. Owned scope
paths must be normalized repository-relative paths.

An external ready plan without an existing validated manifest first receives
bounded planner validation under [plan selection](references/implementation-strategy.md#plan-selection)
for a formal task.
Reuse its content and evidence; repeat research only for concrete gaps. The
planner returns the formal phase plan and stops; record its matching spawn
receipt before initialization. Only an existing validated manifest with its
persisted planner receipt may skip a new planner dispatch.

For formal product tasks, prefer a first phase that delivers one minimal working
scenario across the components needed for that scenario, when feasible; later
phases can extend that result. Plan independent technical tasks around their
own observable capability and verification, without requiring a user flow.
Explain any real dependency that requires preparation before the scenario.

Formal `READY_FOR_CLOSE` is a stop at the close boundary, never acceptance and never
authority to start the next phase. Enter it only with one or more repeatable
current executor `--spawn-receipt-id ID` and one or more repeatable
`--evidence-record` paths to host `run-check` records verified for the host
project, task ID, and task execution root. The records' logs and digests are
verified, then normalized host-relative record references are stored in the
manifest. Executor receipts must match phase/base and current writer cycle;
repair receipts also bind the rejected candidate SHA. Candidate-commit checks
the persisted executor receipt again. On resume, records are verified again.
This does not bind evidence to a task HEAD or tree SHA. Scope expansion or changed requirements
return through the replan protocol. DIRECT remains unchanged.

Each independent formal phase requires a new actual executor spawn with
`fork_turns="none"`; `phase advance` grants no executor authority by itself.
`phase resume` exposes `executor_lifecycle: NEW_CONTEXT_REQUIRED` independently
of telemetry. Only an observed same-phase `LOCAL_FIX` followup may retain its
executor, and then it must be recorded through
`devcrew_dispatch.record_executor_continuation` with the origin receipt,
current writer cycle, rejected candidate SHA, exact issued payload, and
observed `FOLLOWUP_SUCCESS`. This continuation is not a new spawn receipt and
cannot authorize another phase. Missing telemetry keeps its existing unknown
result. If the required new context cannot be started, use the existing
required-handoff blocker rather than reusing the old executor.

The host runtime performs each `spawn_agent` call; DevCrew Python cannot catch
its runtime exception. Explicitly postflight every observed call through
`capacity-report`. Only a confirmed `SPAWN_FAILED` with exact error text
`agent thread limit reached` becomes `AGENT_CAPACITY_BLOCKED`; stop at that
role/stage. Do not fall back to the host, change role/model/effort, reuse
another task's agent, or automatically retry. On an explicit manual resume,
reconcile completed role results and dispatch only the saved blocked role and
stage with its prepared route, once. Interrupting an agent does not prove a
runtime slot was freed. See [dispatch postflight](references/dispatch-postflight.md).

For AGENT EXECUTION, use the shared [technical replan and escalation protocol](references/replan-protocol.md). An executor may return `REPLAN_REQUEST` when new evidence invalidates a material technical assumption; it must not silently alter a material decision or continue affected work. Route `LOCAL_FIX` back to the same executor; route `SYSTEMIC_REPLAN` to decision/planner and then executor; send `OWNER_DECISION_REQUIRED` to the user. The first independent local review finding may remain `LOCAL_FIX`; a repeated failure requires an evidence-backed progress and cost assessment, not an automatic owner question. Stop repeated actions without new discriminating evidence; ask the owner only for an actual owner boundary. Every candidate/final commit, including a `LOCAL_FIX` replacement, requires a fresh successful full test suite on the current tree. Targeted checks remain available during implementation; DevCrew does not classify them. See the protocol for the reusable impact inventory and bounded closure pass. DIRECT work uses ordinary local reasoning and does not manufacture an agent receipt.

Every AGENT EXECUTION planner, executor, reviewer, and decision dispatch must
use `devcrew_context.routed_spawn` or its routed task/reviewer builder. A
direct `spawn_agent`/collaboration spawn without that route is a DevCrew
contract violation and cannot satisfy a role gate. Keep the returned route and
dispatch together; after the actual runtime call, report the exact result
through `capacity-report`, including `AGENT_ID` and `FORK_TURNS`, so DevCrew
can persist a matching spawn receipt. A prepared route is not applied until
that observed spawn receipt exists. An unrouted agent result is advisory only.

An authoritative **formal** phase review additionally requires a committed candidate,
formal `phase review-start`, an exact routed reviewer spawn receipt bound to
task, phase, base SHA and target SHA, verification evidence, and a persisted
matching report. The reviewer receives only the bounded review contract and
must use a fresh context (`fork_turns="none"`). An unrouted, stale, or
inherited-history review is advisory and cannot accept or advance the phase.

### Ordinary current-chat tasks

When the front door has a sufficient current user message and the host has
actually read the exact complete message bytes, plus every attached source
through its supported reader, it may select the ordinary current-chat lifecycle
before formal source binding. Verify the message and attachment digests; a host
descriptor alone never establishes readability. If the exact source is not
readable through ordinary, follow the shared source-resolution rule below to
bind an authorized attachment formally, ask for the missing authorization, or
report the concrete access limitation. The host derives bounded requirement and
acceptance statements and event/message digests without writing prompt bytes.
It routes planner, executor
and reviewer through the ordinary dispatch builder; each actual spawn must be
recorded after the host observes it. The planner first returns a persisted
technical plan with requirement coverage, steps, owned scope and verification;
the coordinator accepts its exact digest before executor dispatch. The executor
and reviewer receive that accepted plan, never an unsaved planner receipt alone.
The reviewer uses the exact `mcp__codex_app__read_thread` parameters in its
fresh host access descriptor (and every authorized attachment reader), verifies
the message digest, then reports independently derived outcomes and omissions.
A truncated/summary-only thread, inaccessible attachment or unavailable host
reader is `ORDINARY_SOURCE_CONTEXT_REQUIRED`, not a PASS. This host-trusted
semantic report is not machine proof of the read.

The ordinary candidate is `{base SHA, current HEAD, worktree fingerprint,
verified check-record digests}`. It never stages files, creates a commit, or
updates refs. Every receipt binds its canonical payload digest and the current
lifecycle/source/matrix identity; an old or altered issuance cannot be used
after the candidate changes. The host also persists the observed risk and
separate decision, implementation and review complexities; it routes each role
through the shared cost policy without treating risk as deeper planning or
escalating the bounded executor. A fresh independent reviewer receipt, full
requirement-ID coverage, PASS, current evidence and an unchanged fingerprint
are required for `TASK_DONE`. A changed tree invalidates review and acceptance.
Resume verifies matrix, accepted-plan, check-evidence and review-report
integrity; any missing/replaced evidence or report is a concrete invalid state,
not `ACCEPTED`. If current-message host access is unavailable, return the
concrete context gap rather than inventing requirements. The ordinary lifecycle
cannot start in a checkout containing a rejected formal manifest, and it never
reads, rewrites or accepts schema-v1/v2 formal manifests.

Resolve the actual source format before dispatch. The ordinary attachment
reader accepts only Page references; a host descriptor, summary or local path
does not prove readable bytes. When an authorized local attachment or
unsupported ordinary format cannot be read through ordinary, use
`phase source-bind --source-spec FILE` on a fresh task. If a source-format gap
is discovered after an ordinary task exists, use the explicit
`phase source-bind --ordinary-source-gap FILE` contract to bind the exact
attachment to that same task, base, checkout and preserved history. The writer
must be quiescent; sealing blocks later ordinary dispatch and mutations.
Formal planning remains blocked until the linked exact source and history
verify. Never reconstruct the source from a summary or treat ordinary receipts
as formal authority. If the authorized attachment cannot be read through
ordinary, the formal source-binding route is the supported recovery; if neither
route can independently read the exact bytes, report the missing access and
the smallest action that restores it.

An ordinary FAIL or stale candidate is recoverable through `ordinary continue`,
not by deleting state or reusing a receipt. The command seals the prior cycle
with its candidate, plan, report, evidence bindings and chained digest, then
creates a new cycle. Its continuation names `LOCAL_FIX`, `SYSTEMIC_REPLAN` or
the classified `STALE_CANDIDATE`, concrete current evidence, bounded next
action, expected result and cost rationale. `LOCAL_FIX` and systemic replan
both require a current accepted plan and new candidate/review artifacts;
systemic replan receives a fresh planner route. A same-executor local followup
is permitted only as an observed `FOLLOWUP_SUCCESS` bound to the exact
followup payload and origin receipt; it is not a fabricated new spawn. Foreign
file changes remain blocked for owner/external reconciliation. Repeated failures
need a new candidate/evidence signature and do not manufacture an owner
question. Archived cycles remain readable and resume validates their digest
chain, while every active gate rejects archived payloads, receipts, evidence
and reports. A v0.18 record that predates routing observations remains readable;
attach its assessment explicitly with `ordinary attach-routing` instead of
silently assigning low risk, then preserve its existing plan/history.

Plan reuse resolves the exact planner receipt named by the accepted report and
walks archived ancestry to its original author. Repeated local fixes preserve
that original origin; recovery first verifies the candidate, saved checks and
any review before it seals history. A stale candidate's checks are compared
with its captured state. Legacy v0.18 records with no file baseline cannot be
classified as stale. Legacy FAIL reports without a classification use the
explicit continuation action only for the exact cycle-zero legacy shape; their
reports and receipts are not rewritten. A stale legacy record without a file
baseline gets a specific rejection.

When independent review finds source omissions, continue only through
`SYSTEMIC_REPLAN`. The runtime appends deterministic requirement IDs with
review and omission provenance to the effective matrix, preserving the
original source and matrix unchanged. Planner, executor and reviewer receive
the effective requirements; the accepted plan and final review cover all of
them. Duplicate omission statements are deduplicated. A PASS with omissions,
or a local fix that retains the incomplete plan, is rejected. The host's
semantic trust boundary and owner decision requirements remain in force. An
already saved `LOCAL_FIX` with omissions can be explicitly continued as
`SYSTEMIC_REPLAN`; `OWNER_DECISION_REQUIRED` cannot be upgraded.

### Original intent and task completion

For schema-v2 AGENT EXECUTION tasks, bind a verified source **before launching
the planner**. Use `phase source-bind` for an exact attachment, ticket,
document, confirmed external answer, or a separately user-confirmed structured
Intent Contract (`mode: contract`). Read the exact schema and confirmation
procedure in [runtime contract](../../docs/runtime-contract.md#original-intent-and-task-completion).
The hook keeps event digests/confirmation metadata only; they cannot prove a
readable original message. Full prompt snapshot binding (`mode: capture`) is
disabled. Never copy chat text into a file source to bypass this restriction,
reconstruct exact prompt text, or label a coordinator draft as a human source.
When source access is unavailable, show a structured contract and obtain the
user's explicit digest-bound confirmation; do not manufacture confirmation.
`mode: unavailable` is a limitation, not permission to start the planner or
certify completion. Planner preflight enforces this before route issuance.
On resume use existing bound immutable sources and confirmations; source
binding does not replace any existing phase, assessment or review gate.

Before accepting a schema-v2 plan, the coordinator reads every available
source and submits its planning assessment separately from planner output via
`phase init --intent-assessment FILE` (or the same option on an authorized
pre-candidate reapproval or systemic replan). Identify stable requirement IDs,
source fragments, explicit obligations versus facts and technical decisions,
and their phase/acceptance references. A complete derived matrix does not prove
that all source requirements were found. Do not add a mandatory preliminary
planning reviewer. Preserve confirmed obligations through autonomous technical
replans; only a verified, explicit decision naming the affected requirement
may authorize a business change. Technical rewording and verification remain
autonomous. Direct work keeps its existing lightweight procedure; short
diagnosis-first work may establish facts and later testable outcomes without
inventing product choices or a full upfront specification.

For the final schema-v2 phase, a fresh, routed independent reviewer must read
the immutable original sources and confirmed decisions, independently derive
expected outcomes (including outcomes absent from the requirement matrix),
and inspect the exact final candidate tree plus relevant accepted predecessor
work and existing host evidence. It returns the existing phase report with
`INTENT_REVIEW` containing the current `plan_sha256`, exact source reads or
unavailability, addressed decision IDs, source outcomes, obligation checks,
and digests of referenced evidence. The runtime checks identity, completeness,
evidence and freshness; the reviewer owns the semantic judgment and must FAIL
with the existing review classification when an obligation is missing or
unconfirmed. A complete matrix or assessment by the planner/executor cannot
substitute for this review.

`PHASE_ACCEPTED` means the current phase passed review. It does not by itself
mean `TASK_DONE`: report completion only when resume returns
`task_status: TASK_DONE` after all phases, verified sources/decisions, a fresh
complete final intent review, and exact final candidate/evidence validation.
Pending intent remains incomplete; schema-v1 remains `UNVERIFIED_LEGACY` and
cannot be certified by this path. Do not reopen an accepted phase to recover a
late omission: report the existing `PHASE_NOT_REVIEWABLE` limitation and leave
the task incomplete for coordinator handling. Carry source, requirement,
decision, review-report and evidence references through resumes and handoffs.

Each role transition requires a new coordinator dispatch using that role's contract. Scouts and analysts return findings; they do not promote themselves to planner or executor. A planner returns `PLAN_READY` and stops; the coordinator dispatches the executor separately. In AGENT EXECUTION the host remains coordinator-only and never writes delegated production code. The executor implements the accepted bounded plan without redesigning its architectural seam, domain model, or confirmed contracts; material plan gaps return through the existing replan protocol.

For AGENT EXECUTION read [model and context routing](references/model-routing.md) and [dispatch preflight](references/dispatch-preflight.md); for long or shared runs use [run control](references/run-control.md). Give agents only the bounded role brief and relevant evidence. A generic fallback is allowed only as a distinct spawned context with the exact role contract, and must be disclosed. Role-file existence does not establish runtime availability.

One writer owns a checkout at a time. Only explicitly user-authorized parallel independent writers use distinct worktrees and are integrated before verification; otherwise serialize writers in the current directory. A dependent wave stops when any expected slice is missing, failed, or reports unresolved problems. Never treat a command mention as execution evidence: inspect actual output and confirm acceptance. Delegated implementation checks, waves and machine-gated reviews use the helper's recorded evidence; direct simple work may use the repository's check command without extra records. Read-only analysis returns sourced findings; it does not need an execution record unless it actually runs a check.

For recorded evidence, scope checks, waves or machine-gated reviews, use the local helper:

```text
python3 <plugin-root>/scripts/devcrew.py run-check --project HOST [--execution-root TASK] [--task-id ID] --name NAME [--timeout SECONDS] -- COMMAND ARG...
python3 <plugin-root>/scripts/devcrew.py verify-check --record PATH [--execution-root TASK] [--task-id ID]
python3 <plugin-root>/scripts/devcrew.py reuse-check --record PATH [--execution-root TASK] [--task-id ID] -- COMMAND ARG...
python3 <plugin-root>/scripts/devcrew.py capture-state --project HOST [--execution-root TASK]
python3 <plugin-root>/scripts/devcrew.py capture-state --project HOST [--execution-root TASK] --include-files
python3 <plugin-root>/scripts/devcrew.py scope-check --project HOST [--execution-root TASK] --before PATH --files PATH
python3 <plugin-root>/scripts/devcrew.py wave-gate --project HOST [--execution-root TASK] [--task-id ID] --plan PATH --results PATH --wave N
python3 <plugin-root>/scripts/devcrew.py review-gate --project HOST [--execution-root TASK] --report PATH
python3 <plugin-root>/scripts/devcrew.py flaky-hunt --project PATH --name NAME --max-runs N --timeout SECONDS [--max-seconds SECONDS] -- COMMAND ARG...
```

`run-check` prints the unique JSON record path followed by a summary; the record and full log are under the host's configured checks directory (default `.codex/checks/`). In split-root mode, commands run and freshness is calculated against `TASK`, while evidence stays under `HOST`. The record contains command argv, resolved working directory, exit code, timestamps, status, Git revision when available, before/after worktree fingerprints, and a SHA-256 log digest. This is local execution evidence, not a tamper-proof security attestation. Verification and reuse compare against the same execution-tree fingerprint recorded by `run-check`. Host checks output is excluded only when the host and execution roots are the same; split-root freshness includes tracked task files under the task's own checks directory. `flaky-hunt` prints its observed run count and record paths, with exit 0 for `not_caught`, exit 1 for a caught test failure, and exit 2 for timeout or executable errors.

For the task sequence and reusable procedures, read the relevant files under `references/`. The package keeps those procedures as documentation; the executable helper is `scripts/devcrew.py` at the plugin root and its public CLI tests are under `tests/`.

For product discovery in an existing project, route an explicit request to [devcrew-product-onboard](../devcrew-product-onboard/SKILL.md); do not run it for ordinary implementation or after setup. When the configured project memory index points to product context, load only sections relevant to the task's user-visible behavior and cite applicable confirmed invariants/owner decisions. Internal work with no product behavior need not load it. Do not turn inference into a requirement.

Before writing delegation read [dispatch preflight](references/dispatch-preflight.md); after return use [dispatch postflight](references/dispatch-postflight.md) to compare actual changes and behavior with the accepted brief. For high-risk behavior, schedule the first meaningful live or negative test once the path works, with fixture/resource ownership, and retain final integrated verification.

Before releasing a dependent wave, integrate the preceding wave and run its acceptance checks on the receiving checkout without concurrent writers. Each completed wave's result includes a nonempty `checks` list of execution-record paths from that checkout. The latest completed wave must match its current state; earlier waves retain historical integrity checks. A check that changes files or HEAD cannot certify a completed wave: rerun it after those changes settle. See [the runtime contract](../../docs/runtime-contract.md) for the report format and migration from status-only reports.

An independent reviewer-agent is mandatory after deterministic checks for every AGENT EXECUTION implementation. DIRECT work needs only ordinary inline diff inspection. Keep machine gate reports in the configured checks directory. Use a task card and [session snapshot](references/session-snapshot.md) for a durable handoff or long multi-stage run; reconcile existing state with Git and evidence on resume. Do not automatically invoke `$devcrew-brief`, `$devcrew-session` or `$devcrew-coordinate` for implementation.

Before finishing, run the narrowest meaningful checks and inspect the diff inline in DIRECT. In AGENT EXECUTION, confirm integrated scope/evidence, deterministic check outcomes, and independent reviewer disposition; this coordinator acceptance does not replace reviewer review. Report changed files, exact commands and outcomes, deviations, and unresolved risks. Persist these in configured task/session locations only when the workflow or project requires durable state. Apply [project state layout](../../docs/project-state-layout.md): working task packages belong under ignored `.codex/tasks/`, snapshots under `.codex/sessions/`; keep only compact confirmed knowledge in `memory/`, `docs/`, and `rules/`.

Command arguments and full output are stored locally without automatic secret redaction. Do not put credentials in argv; use the project’s established secret handling. Execution evidence currently rejects projects containing Git submodules or nonignored untracked nested Git repositories; run each nested repository as its own project. A verified zero exit code still requires inspection of the semantic output.

For user-requested coordination of multiple existing tasks, use [DevCrew coordinate](../devcrew-coordinate/SKILL.md); a single implementation remains in this workflow.
