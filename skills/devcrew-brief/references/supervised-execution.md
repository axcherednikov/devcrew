# Supervised execution after a full brief

This is a bounded handoff protocol for a full, approved brief. It is not a new
task runtime and it does not replace the owning implementation workflow, its
stage gates, independent review, or source-trust checks.

## Approval boundary

1. Before starting any full brief, including a read-only brief, confirm that
   the user has chosen a strong model appropriate to this work. If the choice
   is unknown or insufficient, ask the owner to choose and wait; do not change
   the model or increase cost automatically. For execution briefs, the human
   approval for child-chat creation remains a separate gate. This applies to
   full briefs routed from `devcrew-task` because material uncertainty remains.
2. Complete relevant repository/source research and resolve answerable facts.
3. Resolve material owner decisions that affect child scope or permissions.
   Keep nonblocking hypotheses explicitly labeled and bounded.
4. Present the complete brief, its exact version or stable reference, remaining
   open questions, scope, acceptance criteria, and checks.
5. Ask for explicit human approval covering (a) creation of one visible Codex
   chat and (b) subsequent messages that keep to this brief's scope. Silence,
   an agent's recommendation, or approval to discuss/prepare the brief is not
   authorization. If approval is absent, stop after the read-only brief.
6. If the user changes
   scope, risk, required permission, or an unresolved
   product decision, return to the owner. Do not treat the initial approval as
   broader than its stated scope.

## Prepare and create

Before an external action, discover the actual native schemas available in the
current Codex host. The supported capability set for this flow is
`list_projects`, `create_thread`, `list_threads`, `read_thread`,
`send_message_to_thread`, and `wait_threads`; names here are contracts to
discover, not permission to invent a shell/API bridge. If any required tool or
argument is unavailable, record the exact capability gap and stop before the
dependent side effect. Do not substitute an internal subagent for the visible
user-owned chat.

Use `list_projects` to identify the project's real ID and repository/local
checkout identity. Reconcile the current host, project, working directory,
branch, revision, dirty paths and active writer. The default target is the
project-local chat. Do not select a similarly titled project or a different
checkout. Confirm that the exact original request/attachment can be carried
through a supported native attachment/source binding or independently
verified descriptor in the owning workflow. A parent-authored summary is not
the original source. If the original or approval reference cannot be carried
and verified, report that specific blocker.

The parent coordinator is the sole writer of its configured sessions Markdown
inventory, including while the supervised child runs. It may update only that
ledger; it must not write child-owned files or task cards. This rule resolves
the campaign-inventory exception for this one supervised child and does not
grant the parent ownership of the child's checkout.

Use the configured sessions Markdown inventory described in
[`devcrew-coordinate/references/inventory.md`](../../devcrew-coordinate/references/inventory.md).
Before `create_thread`, write one `prepared` action entry with a stable action
ID, exact target/effect, exact brief/source identity, human approval reference
and scope, current checkout/revision precondition, and reconciliation step.
This is a human-maintained ledger, not a transaction or idempotency service.

Never dispatch an executor from the parent before approval; the child owns its
own implementation workflow and dispatches only after verifying the approved
brief and its normal gates. Create only one child. Omit model and thinking/effort arguments unless the user
explicitly requested an override and the native schema supports it. The chat's
model does not set the executor or reviewer model; those continue to follow the
owning workflow's independent role/cost policy. The initial prompt is a
synthetic handoff and must say so. Include the action ID, exact brief version,
original-source identity, approval reference/scope, confirmed decisions,
acceptance/checks, relevant evidence links and boundaries. Keep the original
source and human approval verifiable independently rather than describing the
prompt as the source or approval itself.

After create returns, record the actual receipt and real `threadId`, host,
project, returned title, status, and any `clientThreadId` separately. A
`clientThreadId` is setup-only: never pass it to read, send, or wait. Resolve
the real thread identity using available native state before continuing. If
create times out or the receipt is missing, mark the action `uncertain`,
reconcile native thread inventory and correlation marker, and do not create a
second child while the outcome is ambiguous.

Before child writes, the child must check its actual workspace root, branch,
revision, dirty state/single-writer ownership, source and approval access, and
that the brief's permissions are sufficient. A mismatch is a blocker, not a
reason to silently switch checkout or broaden authority.

## Observe, assess and correct

Use bounded `wait_threads` calls only while this parent session is active,
passing real host/thread IDs and the saved cursor. Persist the returned cursor
and observed status. This is active-session polling; it is not continuous
background monitoring. After a pause, reconcile the saved IDs against native
thread inventory and recent messages, action ID, current writer/checkout/branch/
revision, and current diff/evidence before taking another action.

Native `completed` or a child message saying “done” is not acceptance. Inspect
the actual diff/artifacts and executed checks against the original source, the
approved brief and its criteria. Keep native status separate from acceptance
status. The owning workflow still requires its normal verification and
independent review.

An in-scope correction may be sent under the original approval. Include the
action/request ID, concrete finding and evidence, expected result, affected
check, and relevant correction history/cost guard. Reconcile recent messages
before sending. A changed requirement, scope, risk, or required permission
returns to the owner for a decision.

The transport does not guarantee idempotency or exactly-once delivery. Record
each action as `prepared`, `sent`, `confirmed`, or `uncertain`, with its receipt,
cursor/evidence and next action. A confirmed action is never replayed. A
prepared action without a receipt or an ambiguous timeout remains uncertain
until reconciled; only evidence that delivery did not occur permits a bounded
retry with the same action identity and scope. If the outcome cannot be safely
resolved, stop the dependent side effect and report the concrete blocker.

## Resume and close

On resume, inspect ledger plus native identity/status/recent messages, then
revalidate project/root/branch/revision, active writer, source and approval,
and evidence freshness. Do not infer delivery from a local `sent` flag or
absence of a recent response. Continue only from the reconciled next action.
Record actual IDs, receipts, status/cursor observations, evidence references,
acceptance status, blockers and next action in the existing inventory. Store
references/digests and concise evidence, not full transcripts.

Report verified criteria, missing evidence and checks, native status, separate
acceptance status, any neighboring findings, and how to resume. Do not claim
continuous monitoring or completion from a chat status alone.
