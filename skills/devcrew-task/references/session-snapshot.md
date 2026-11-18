# Session snapshot

For a supervised full-brief chat, the session narrative points to the existing coordinate inventory/action ledger rather than creating a second ledger. On resume reconcile its real host/project/thread IDs and recent messages with stable action ID, actual receipt, cursor, original source and human approval references, and current checkout/writer/revision. A pending or uncertain action is not replayed until native state establishes whether it was delivered.

For collaboration-agent continuation, include the pending task/agent identity, role and stage, original dispatch receipt, expected result, any result already received, and next action. Link the shared [agent waiting procedure](agent-waiting.md). On resume, reconcile received results before dispatching or consuming a result again.

Write a compact snapshot in the configured session directory when work spans turns. Include the current Git revision, task and project state, changed files, decisions, checks actually run with exact outcomes, open questions, and the next bounded action. A snapshot is a handoff record, not a claim that unrecorded work happened.

At a stage boundary or before handoff, update the card's `updated_at`, workspace, branch, Git HEAD, `state.done`, `state.in_progress`, `state.next`, `state.key_files`, blockers, and evidence links. Use the [task card template](../../devcrew-setup/assets/project/task-ledger.template.md), also provisioned by init as `card.template.md` in the configured tasks directory. Preserve existing card content when adding fields.

After a context reset, read the card and latest snapshot first. Check the actual workspace, branch, HEAD, local diff, and freshness of the evidence needed for the next action. A changed tree does not erase completed history; mark affected evidence stale, record what changed, and continue from the next bounded action. Ask only about unresolved information that actually blocks correct work.

Finish card/document edits before capturing state for a review or running final checks. Keep machine review reports and wave results in host checks state. Configured checks are excluded from the fingerprint only when the host and execution roots are the same; split-root captures fingerprint the full task tree. If a later card edit changes the fingerprint, do not manufacture a fresh snapshot for an old review: inspect the documentation delta and record its coverage before recapturing. A narrative session snapshot is not the `capture-state` object required by the review gate.

For AGENT EXECUTION, keep the durable task worktree root and task ID in the
snapshot. Checks are launched with host project state and explicit task
execution identity; receipts and logs remain in host checks state while their
fingerprints describe the task worktree. Resume with the same expected root and
task ID when verifying or reusing a receipt.

If a confirmed host spawn reaches the agent thread limit, preserve its capacity
receipt in the host's configured sessions state. The receipt records the
blocked role, stage, attempt, resolved route and both project roots; the task
checkout root may be null before task preparation. Preserve the current-directory
default and any explicit user worktree authorization under
[task checkout policy](checkout-policy.md). Record any already-completed
role result in the task card/session narrative before manual resume so the host
continues only the blocked role and stage. Do not treat an interrupt as proof
that the runtime freed a slot.
