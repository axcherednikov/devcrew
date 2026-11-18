---
name: devcrew-session
description: "Save context on request, when handing work over, or when resuming a long multi-stage task. Ordinary implementation does not require a session snapshot."
---

# DevCrew session

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

When recovering a supervised visible-chat handoff, reconcile its existing coordinate inventory against native thread IDs/status/recent messages, action identity, current writer and checkout/branch/revision, source/approval references, and evidence freshness before another action. Preserve prepared/sent/confirmed/uncertain state and actual receipts; never blindly repeat a create or message after an ambiguous result.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to bound the requested session handoff or recovery and preserve its existing task authority.

Use this skill for requested context persistence, a durable handoff, or a substantial multi-stage run that needs recovery. Spanning turns or completing an ordinary implementation does not automatically require a session workflow; record a reusable lesson only when it has concrete cross-task value. Read applicable `AGENTS.md`, then `.codex/devcrew.json` when present, the configured context, task card, and latest session record. Write only to repository-approved project-local locations; do not rely on hidden built-in memory, global project stores, or installed caches.

Keep task status, session snapshots, and durable cross-task lessons in their configured locations (defaults: `.codex/tasks/`, `.codex/sessions/`, and `memory/`). A role may propose a lesson, but the parent session decides whether it is durable and records the destination. Do not turn a one-off implementation detail into a universal rule. Include the current Git revision, changed files, checks actually run, exact outcomes, open questions, and next bounded action.

At stage boundaries and before handoff, update the structured task card using [session snapshot](../devcrew-task/references/session-snapshot.md). On resume, reconcile the card with the actual checkout and evidence before continuing `state.next`; preserve completed history and identify stale checks. The [session harvest](../devcrew-task/references/session-harvest.md) procedure handles optional evidence-driven retro for significant completed work only. Trivial tasks and sessions close normally without retro; unfinished work gets a normal handoff, not a premature retro. Retro is not a closure gate. These procedures do not authorize external communication or global writes.

For schema-v2 task handoffs, preserve references to the immutable original
source snapshots and their provenance, active requirement IDs, confirmed
decision identities, planning-assessment history, final intent-review report,
and the existing evidence records that bind to the candidate. State which
sources or evidence remain unavailable and keep `task_status` distinct from a
phase status. Revalidate identities and report freshness on resume; a summary
or copied digest is not a substitute for the original source. Never claim
exact prompt text was recovered from session memory. An attachment remains
identified as an attachment. Schema-v1 context carries its explicit
`UNVERIFIED_LEGACY` limitation; `PHASE_ACCEPTED` alone does not establish
`TASK_DONE`.

When maintaining durable lessons or correcting estimates, apply [memory lifecycle](references/memory-lifecycle.md) and [estimate calibration](references/estimate-calibration.md). Follow [project state layout](../../docs/project-state-layout.md) for ignored runtime state and minimal durable updates to `memory/`, `docs/`, and `rules/`; do not copy full session/task history into those directories. Use measured project observations and preserve original estimates; inventory warnings do not authorize automatic deletion.
