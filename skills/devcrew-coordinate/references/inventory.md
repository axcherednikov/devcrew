# Campaign inventory

For the approved full-brief handoff, keep one supervised child entry and link the exact brief/source identity and human approval reference/scope. Record real host/project/thread IDs, actual create receipt, returned title and native status separately from acceptance. Keep `clientThreadId` marked setup-only; never use it with read/send/wait. Record latest wait cursor, action/request ID, message/evidence references, delivered revision/fingerprint, verification and review coverage, acceptance status, and bounded next action. Do not copy full transcripts. A prepared action without a receipt and an ambiguous timeout remain `uncertain` until native state and messages reconcile them; never create or send a duplicate while uncertain.

For this one supervised child, the parent coordinator is the sole writer of this
configured sessions inventory and may persist it while the child runs. The
child owns its task card and checkout; the parent never writes those files.
Create the child only after material owner decisions are resolved. Record
nonblocking hypotheses as hypotheses, not as confirmed decisions.

Create a Markdown file only when coordinating an actual campaign; do not prepopulate fictitious tasks. Record the user objective, authorization scope, coordinator workspace, updated-at timestamp, and inventory owner. Use project-native structure where it exists.

For each task record:

- Native host and thread ID, exact returned title, project ID and resolved checkout path.
- Expected deliverable, dependencies, assigned scope and acceptance criteria.
- Observed native status and separate acceptance status: queued, running, needs-fix, awaiting-review, accepted or blocked. Record when each observation was made.
- Active writer and shared resource names plus common lock directory. Writers in different paths that resolve to the same checkout still conflict.
- Latest cursor, last request identifier, request text/artifact link, expected reply and observed reply reference.
- Delivered revision/fingerprint, integration revision, actual execution records, review report and covered scope; missing evidence stays explicitly missing.
- Blocker, owner and next bounded action. Do not infer acceptance from a task's terminal status.

Maintain an action ledger with stable ID, target, exact intended effect, authorization source/scope, precondition revision, state (prepared, sent, confirmed, uncertain), receipt and next reconciliation step. This is a human-maintained record, not a transaction engine. A local sent flag alone cannot prove a remote action happened.

On resume verify IDs and paths, live ownership, current revisions and freshness before sending anything. Do not overwrite an executor's own card with coordinator guesses. Store copied reports as referenced evidence, not additional authoritative state. A campaign is complete only when all in-scope acceptance criteria are verified or the user explicitly changes the scope; report unresolved work instead of marking it done.
