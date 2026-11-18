# Accepting a delegated implementation

Read the actual diff and evidence before accepting the worker's summary. Run:

For phase-based AGENT EXECUTION, compare the returned work against the saved
`CURRENT_PHASE` contract only. The coordinator inspects actual verification
output against acceptance, records `READY_FOR_CLOSE` with one or more verified
host `run-check` records and the exact `--spawn-receipt-id` from the current
executor cycle, then creates the immutable candidate with the
manifest's ordered verification bindings. Dispatch a separate reviewer using
the `review-start` contract; that reviewer reads only `BASE_SHA..TARGET_SHA`.
Save the exact report under the host project and submit it with `review-report`.
A PASS report must be explicitly accepted; acceptance requires formal
`review-start`, an exact routed reviewer spawn receipt linked to task/phase/base/
target and actual reviewer agent, a persisted matching report, candidate SHA,
tree, parent, branch/HEAD, clean worktree, and verification receipts. Only then
may the coordinator advance the next sequential phase. Reports without that
route are advisory and cannot accept or advance the phase. FAIL classifications
halt without automatic repair. Apply [loop guard](loop-guard.md) before repair:
a failed corrected candidate requires coordinator assessment and an evidence-backed
`--continuation FILE` before another correction. It is not an automatic owner
question. Reconsider a disproven review with `review-reconsider` on the same SHA.
Within that boundary,
`LOCAL_FIX` resumes the same phase with the executor
and only the saved findings, phase base, rejected candidate SHA, owned scope,
acceptance, and verification. Start that explicit cycle with
`local-fix-start`; it preserves the rejected candidate/report and resets the
task checkout to the unchanged phase base. Reverify from scratch, make a new
candidate, and dispatch a new review against its exact SHA. Each rejection
creates another visible cycle. `SYSTEMIC_REPLAN` stops for replan and
`OWNER_DECISION_REQUIRED` stops for the owner. Resume during review uses the
same candidate SHA; an inconsistent or dirty writer checkout fails closed.
Scope or requirement changes return through replan.

```text
python3 scripts/devcrew.py phase transition --project HOST --task-id ID --phase-id CURRENT_PHASE --status READY_FOR_CLOSE --spawn-receipt-id EXECUTOR_RECEIPT --evidence-record HOST_CHECK_RECORD [--evidence-record HOST_CHECK_RECORD ...]
python3 scripts/devcrew.py phase candidate-commit --project HOST --task-id ID --verification-bindings BINDINGS.json
python3 scripts/devcrew.py phase review-start --project HOST --task-id ID --phase-id CURRENT_PHASE
python3 scripts/devcrew.py phase review-report --project HOST --task-id ID --report HOST_REVIEW_REPORT.json
python3 scripts/devcrew.py phase local-fix-start --project HOST --task-id ID --phase-id CURRENT_PHASE
python3 scripts/devcrew.py phase accept --project HOST --task-id ID --phase-id CURRENT_PHASE
python3 scripts/devcrew.py phase advance --project HOST --task-id ID --phase-id NEXT_PHASE
python3 scripts/devcrew.py phase resume --project HOST --task-id ID
```

```text
python3 <plugin-root>/scripts/devcrew.py scope-check --project HOST [--execution-root TASK] --before BEFORE.json --files OWNED.json
```

The baseline is the `capture-state --include-files` result taken before this slice; OWNED.json is an explicit array of execution-root-relative files. In task-worktree mode, keep the snapshot and ownership file in host checks state. This detects additions, deletions, content and mode changes relative to preexisting work. It is a diagnostic, not a sandbox: ignored files, same-root configured check outputs and external effects are outside its coverage. A changed HEAD requires inspection. Do not erase someone else's changes to make the gate pass.

Then verify the parts a path comparison cannot establish:

- Each acceptance criterion has an implementation and an observed test/result, or an explicit unresolved gap. Compare outcomes, not lines of prose between the plan and report.
- Apply the [engineering policy's review and acceptance rules](engineering-policy.md#review-and-acceptance) to the returned tests, verification history and any spike/evaluation artifacts.
- Producer and consumer signatures, exports, configuration and wiring agree on the integrated tree. Find callers of newly added symbols before calling them unused; planned later consumers must have an assigned dependent slice.
- Transaction boundaries, authorization, retry/idempotency and numeric behavior follow relevant project conventions. Recheck assumptions that drifted during implementation.
- For risky conditions, perform the targeted test-quality check in [review coverage](../../devcrew-review/references/coverage.md). Never mutate the shared tree while another writer or test run owns it.

Return accepted / needs-fix with changed paths, exact evidence, deviations and unresolved findings. For delegated AGENT EXECUTION, route `LOCAL_FIX` to the same executor, `SYSTEMIC_REPLAN` to decision-agent and then executor, and `OWNER_DECISION_REQUIRED` to the user; the coordinator does not edit delegated production code. For DIRECT, the host may correct its own mechanical change. Rerun affected checks before a dependent wave. Escalate changes to agreed external contracts or scope; do not ask permission for ordinary in-scope fixes. A clean postflight does not substitute for final integrated tests or review.

## Reporting a host spawn result

The host runtime performs each `spawn_agent` call. A Python helper that prepares
the dispatch cannot catch an exception raised by that runtime. After each
actual call, the host must report its observed result explicitly as
`SPAWN_SUCCESS` or `SPAWN_FAILED`; include the exact runtime error text when a
call fails. Only a confirmed `SPAWN_FAILED` whose `RUNTIME_ERROR` is exactly
`agent thread limit reached` is a capacity blocker. Do not infer capacity from
an interrupted call, missing result, timeout, or another error.

Successful calls must also include the actual `AGENT_ID`, `FORK_TURNS`,
`SPAWN_MODEL`, `SPAWN_REASONING_EFFORT`, and `SPAWN_MODE`. The final payload
builder issues and durably records the route, dispatch selection, role identity,
and canonical spawn payload before the runtime call. Postflight must carry that
exact prepared route and payload; a missing, fabricated, changed, or already
consumed issuance is rejected. A confirmed capacity retry reuses the same
issued payload and route. Planner reports
bind `BASE_SHA` to the original task base. Executor reports bind `PHASE_ID`,
`BASE_SHA`, `WRITER_CYCLE`, and, for repair cycles, `REJECTED_CANDIDATE_SHA`.
Reviewers bind the exact candidate `TARGET_SHA` and use `FORK_TURNS=none`.
`capacity-report` persists a spawn receipt matching the exact prepared route,
model/effort, spawn mode and task/phase identity after verifying the issued route. A
direct or unreceipted spawn
is `ROUTING_DEVIATION`; its result cannot close a required role gate.

For executor roles, every independent phase requires a new actual agent and
`FORK_TURNS=none`; receipt recording and verification reject an agent already
recorded for another phase in the same task. Do not use `FOLLOWUP_SUCCESS` as
`SPAWN_SUCCESS`. For a same-phase local repair only, after observing a successful
followup, call
`devcrew_dispatch.record_executor_continuation(PROJECT, report)` with
`STATUS=FOLLOWUP_SUCCESS`, `REQUESTED_ROLE=executor`, the original
`ORIGIN_SPAWN_RECEIPT_ID`, actual `AGENT_ID`, exact `TASK_ID`, `PHASE_ID`,
`BASE_SHA`, current positive `WRITER_CYCLE`, exact `REJECTED_CANDIDATE_SHA`,
and the unchanged `PREPARED`, `SPAWN_PAYLOAD`, payload digest, model, effort and
spawn mode. The helper stores a continuation receipt linked to the original
receipt only when the manifest still carries that active repair cycle. The
origin's fork value is retained as history, not treated as evidence of a new
spawn. Stale cycle, rejected SHA, phase, agent, or route data fails closed.

Report the result through `capacity-report` with task ID, requested role,
current stage, task root (null before planner creates it), actual-spawn attempt
number, and the prepared route and dispatch. The helper stores a confirmed
capacity blocker in the host's configured sessions directory. A successful
planner, executor, or reviewer spawn clears that active blocker while
preserving the success report. Other failures keep their original status.

For a manual resume, first reconcile completed planner/executor/reviewer
results from the task card or session context. Then run `capacity-resume` once
and make at most one `spawn_agent` call for the returned blocked role and
stage, using the saved model, effort, and dispatch. Report that actual call
with the next attempt number. Do not restart earlier completed roles, retry
automatically, change route/model/effort, use another task's agent, or alter
the task worktree to address host capacity. Interrupting an agent does not
establish that a runtime slot has been freed.
# Review repair and verification scope

Apply the mandatory [loop guard](loop-guard.md) before any continuation route.
Apply the [technical replan protocol](replan-protocol.md) when classifying
review findings. The first independent local finding may remain `LOCAL_FIX`;
a root-cause/invariant returning after correction requires assessment of progress.
`LOOP_DETECTED` stops repeated actions without discriminating evidence; confirmed
technical errors remain internal when a bounded continuation is justified. Reuse one bounded impact inventory for
one closure pass and one review. Every candidate/final commit, including a `LOCAL_FIX` replacement, requires a fresh successful full test suite on the current tree. Targeted checks remain available during development; DevCrew does not classify them.
