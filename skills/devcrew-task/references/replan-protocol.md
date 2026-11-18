# Technical replan and escalation protocol

This protocol is used by `devcrew-task` AGENT EXECUTION, autopilot, and
`devcrew-review`. It changes the working technical hypothesis only. Acceptance
criteria, safety rules, confirmed owner/product decisions, and scope
authorization remain hard constraints. `ACCEPTANCE_IMPACT` in a
`REPLAN_REQUEST` must be `NO`; the `REPLAN_REQUEST` cannot change acceptance
criteria. Changing acceptance requires an owner decision.

Apply the mandatory [loop guard](loop-guard.md) before classification or dispatch.
`LOOP_DETECTED` takes precedence over all continuation routes below; technical
classification alone is not evidence of progress or authority to bypass an owner boundary.

## Executor receipt

When evidence invalidates a material part of the accepted strategy, the
executor stops affected work and returns this compact handoff rather than
silently changing architecture, continuing the invalid plan, or waiting for
review:

```text
STATUS: REPLAN_REQUEST
PLAN_ASSUMPTION: <material assumption in the accepted decision>
OBSERVED_EVIDENCE: <what code, tests, or contracts establish>
CONFLICT: <why the current plan is unsafe or no longer reasonable>
PROPOSED_ALTERNATIVE: <technical alternative, or none>
AFFECTED_SCOPE: <affected bounded paths/behavior>
UNAFFECTED_SCOPE: <scope that remains valid and may continue>
ACCEPTANCE_IMPACT: NO
OWNER_DECISION_NEEDED: YES | NO
EVIDENCE_REFS: <minimal file:line/test references>
```

The receipt cannot redefine scope, acceptance, or confirmed decisions. If
`OWNER_DECISION_NEEDED: YES`, state the concrete owner choice and stop dependent
work. A coordinator may classify an obviously local defect as `LOCAL_FIX`
without creating a replan receipt.

The coordinator owns role transitions. A planner returns `PLAN_READY` and ends
its assignment; the coordinator creates a new executor dispatch from that
plan. Scouts and analysts return evidence and cannot promote themselves to
planner or executor. Role changes never happen silently inside one agent
context.

For executor difficulty, preserve the existing receipt classifications:
`PLAN_INSUFFICIENT` is a material gap and returns `REPLAN_REQUEST` for
decision/planner; `TASK_TOO_LARGE` returns to coordination/planning to split
the work into bounded implementation slices; `LOCAL_IMPLEMENTATION_DIFFICULTY` stays
with the executor while it remains within the accepted plan, and returns
`REPLAN_REQUEST` only if continuing requires a material technical decision.
Independent first `LOCAL_FIX` findings can remain executor work within one bounded correction. Further confirmed technical errors may be corrected autonomously with an evidence-backed continuation assessment: what was learned, bounded next action, expected result/check and proportionate cost. A shared invalid seam is `SYSTEMIC_REPLAN`; recurrence following a correction is not by itself a loop or an owner question.

For review findings, the first independent local finding may be classified as
`LOCAL_FIX`. A root-cause or invariant class returning after correction, including
a prior fix covering only one analogous path, requires assessment of actual
progress. Stop with `LOOP_DETECTED` for repeated actions without new discriminating
evidence, oscillation or an unchanged dispute. Technical replans remain internal
when acceptance and owner boundaries are unchanged. Reconsider a disproven review
through `phase review-reconsider --continuation FILE`; preserve its report and
obtain a new independent review of the same candidate before acceptance.
Preserve one bounded impact inventory and reuse
it for one closure pass followed by one independent review; do not restart the
inventory for each analogous symptom.

Every candidate/final commit, including a `LOCAL_FIX` replacement, requires a fresh successful full test suite on the current tree. Developers may run targeted checks during implementation; DevCrew does not classify or prove targeted coverage. A systemic replan may broaden checks only when its impact inventory identifies the affected surface.

## Coordinator classification and recipient

Classify by root cause, using supplied evidence:

| Classification | Meaning | Next recipient/action |
| --- | --- | --- |
| `LOCAL_FIX` | Accepted plan and seam remain valid; a bounded implementation defect remains. | In AGENT EXECUTION, return the finding to the same executor for correction, then rerun affected checks and independent review. In DIRECT, the host may make the local correction. |
| `TECHNICAL_REPLAN` | Acceptance and owner decisions remain fixed, but a material technical assumption, seam, ownership, state transition, storage/dependency choice, external technical contract, or strategy is invalid. | Dispatch internal `decision-agent` with bounded context. A first engineering decision does not require a user question unless the loop guard has fired. |
| `OWNER_DECISION_REQUIRED` | Viable outcomes differ in product/business behavior, external contract, confirmed owner decision, acceptance, or authorization. | Ask the user/owner the specific unresolved choice; do not dispatch the executor to choose. Use `devcrew-brief` only for genuine requirements or product uncertainty, not routine technical replans. |

Classify by root cause, not by finding count. Independent local defects can
remain `LOCAL_FIX` with a verified bounded continuation; multiple symptoms caused by
one wrong architectural seam are `TECHNICAL_REPLAN`.

## Decision-agent handoff and result

Invoke the internal role only after classifying `TECHNICAL_REPLAN`. Send only
the original accepted technical decision and bounded plan, the
`REPLAN_REQUEST` or grouped systemic review evidence, the file/line/test
evidence needed to decide, and unchanged acceptance/scope/owner decisions. Do
not send the full transcript, unrelated task artifacts, or request an
automatic repository-wide reread. The agent may selectively open or request
additional sources when evidence is insufficient.

For initial planning, when no accepted sufficient plan exists, the decision-agent
returns the bounded executor-ready contract below after receiving the task
acceptance, research evidence, and relevant source references:

```text
OUTCOME: PLAN_READY
TECHNICAL_DECISION: <evidence-backed choice and rationale>
EXECUTOR_STEPS: <bounded implementation steps>
ACCEPTED_SCOPE: <owned paths/behavior>
UNCHANGED_SCOPE: <confirmed boundaries>
REQUIRED_VERIFICATION: <observable acceptance checks>
OPEN_OWNER_DECISIONS: <none or concrete blocking questions>
EVIDENCE_REFS: <file:line/test references>
```

The coordinator must not add a technical decision or implementation detail
that is absent from this plan; material gaps go back to decision/planning or
the owner before executor dispatch. `PLAN_READY` is the planner's terminal
receipt for that assignment: the planner does not write production code or
continue as executor.

For replans, it returns one compact outcome:

```text
OUTCOME: APPROVED_REPLAN
UPDATED_DECISION: <bounded technical decision>
UPDATED_PLAN: <bounded executor steps>
AFFECTED_SCOPE: <paths/behavior>
UNCHANGED_SCOPE: <still-valid paths/behavior>
REQUIRED_VERIFICATION: <checks for affected scope>
```

```text
OUTCOME: KEEP_CURRENT_PLAN
RATIONALE: <brief evidence-based reason the strategy remains valid>
REQUIRED_EXECUTOR_ACTION: <bounded local continuation/fix>
```

```text
OUTCOME: OWNER_DECISION_REQUIRED
OWNER_QUESTION: <specific choice and behavior/contract impact>
BLOCKED_SCOPE: <dependent work only>
```

The decision-agent cannot change acceptance or owner decisions. The coordinator
routes `APPROVED_REPLAN` and `KEEP_CURRENT_PLAN` to the executor in a new,
role-matched dispatch with an updated bounded package; owner outcome goes to
the user. If evidence does not establish a need to change strategy, keep the
plan and continue.

## Review handoff

Review is read-only. Return `REVIEW_CLASSIFICATION: LOCAL_FIX | SYSTEMIC_REPLAN |
OWNER_DECISION_REQUIRED` with root cause and evidence. `LOCAL_FIX` findings go
to the executor. `SYSTEMIC_REPLAN` goes to the decision-agent as
`TECHNICAL_REPLAN` only while the loop guard permits continuation; do not split
a shared root cause into per-finding fixes.
`OWNER_DECISION_REQUIRED` goes to the owner. Reviewer does not edit, implement,
or dispatch agents; the coordinator owns transitions.

## Execution boundaries

For AGENT EXECUTION, accept `REPLAN_REQUEST`, classify it, and continue only
after technical decision and affected-scope verification. The coordinator
routes the accepted updated plan to the executor; it never takes over
implementation. The DIRECT path uses ordinary host reasoning and does not
create an artificial `REPLAN_REQUEST` to itself or dispatch a planner-agent.

For autopilot, replan leaves the current task incomplete. Do not commit or
advance the DAG while replan is pending. After decision, send the executor the
updated bounded package, rerun verification for affected scope plus applicable
task gates, and resume review as needed. Commit and DAG advance remain behind
green implementation, verification, and review gates.

Replan is conditional, so the normal path has no decision-agent call or extra
context cost. Context is limited to the accepted decision, new evidence,
conflict, proposed alternative, affected scope, unchanged acceptance, and
selectively requested sources.

This stage does not add parallel autopilot, task fan-out, worktree waves, or a
new general model-routing strategy. Existing sequential task execution and
cost-routing policy remain in force.
