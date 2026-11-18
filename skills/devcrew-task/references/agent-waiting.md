# Waiting for collaboration agents

This procedure applies to native collaboration agents started with `spawn_agent` and coordinated with mailbox tools such as `wait_agent`. It is distinct from user-owned native Codex tasks, which use `wait_threads`, and from operating-system processes, which need their own process completion or monitoring interface. The word “agent” alone does not establish which runtime is active; follow the tool used to start the work.

## Keep useful work in the foreground

After dispatch, retain the task, agent, role, stage, route and receipt identity. Continue useful independent foreground work while the child runs. Once that work is complete, wait for mailbox updates with `wait_agent`, using a timeout no greater than 60,000 ms and no greater than the remaining user-update budget. Respect the tool's runtime minimum; if the remaining budget is below that minimum, continue useful foreground work or return a truthful status rather than hiding a longer wait.

A timeout means only that no mailbox update arrived during that interval. It does not mean the child is hung, failed, or finished. Do not follow an empty timeout with `clock.sleep`, repeated `list_agents`, transcript/status reads, or other polling solely to detect progress. Do not treat a final-answer boundary as a background-resume mechanism. Stay in the active coordinator turn until expected results arrive or a real blocker requires a user decision. If the remaining update budget is shorter than the tool's minimum wait, send concise progress commentary, renew that budget, and continue useful foreground work while the dispatch remains pending.

## Reconcile every delivery by identity

Reconcile updates using task ID, agent ID, role, stage and dispatch/receipt identity. A message or progress note is not a terminal result, and a terminal result does not satisfy acceptance, verification, review or phase gates. Record steering against the same pending stage. For multiple agents, retain separate pending identities and associate every update with its sender and assignment.

When a terminal success, failure or blocker arrives, record it once against the matching dispatch. A duplicate delivery is not a second result. On resume or replacement, carry forward the pending stage, original dispatch receipt, expected result, any result already received, and the next action. Reconcile stored results before dispatching; do not duplicate a dispatch or consume one result twice. A terminal result resumes the existing checks and independent-review lifecycle; it does not bypass them.

## Conditional fallback

Use a fallback only when the required native mailbox capability is actually unavailable. If an already-observed status/result tool is available, do useful foreground work and take at most one status snapshot after each 60 seconds of that work, subject to the remaining update budget; do not add sleep or poll more frequently. If no such tool is available, report the delivery limitation in concise commentary and continue useful foreground work until expected results arrive or a real blocker requires owner input. Stop snapshots once every pending dispatch has a reconciled terminal result. This fallback cannot claim native mailbox wake semantics, guaranteed completion delivery, or background continuation. Do not create a scheduler, daemon, waiter agent, hidden process, or host configuration change as a substitute.

For reproducible schedule comparisons, use the offline virtual-time evaluation at [`evaluation/agent-waiting.md`](../../../evaluation/agent-waiting.md). Its wake counts are simulations, not historical observations or billing measurements. Actual useful-run observations belong to the coordinator's existing waiting activity; never add a delay or agent solely to collect measurements. Report token telemetry as `UNKNOWN` when the runtime supplies no trustworthy measurement.
