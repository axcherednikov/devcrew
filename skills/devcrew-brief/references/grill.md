# Optional plan grill

Grill is an optional stress-test of an already sufficiently formed, non-trivial brief, plan, or ADR. Use it when a consequential decision or risk profile justifies critique: for example, interdependent decisions, costly or hard-to-reverse choices, migrations, external contracts, or complex payment/state/concurrency flows. A direct request to “grill the plan”, “stress-test the plan”, or ask about weak points activates it. Do not use it for trivial edits, obvious local fixes, or routine CRUD. Grill is not a new discovery phase, skill, agent, or ADR/memory mechanism.

Before grilling, understand the current plan and its confirmed and unresolved decisions. Critique it as a skeptic: identify the first likely failure, hidden assumptions and indirectly affected invariants, and only relevant failure/retry, duplicate, partial-success, rollback/recovery, scale, concurrency, stale-event, dependency-degradation, authorization, migration, or observability risks. This is a selective prompt, not a universal checklist or license to expand scope.

## Interview mode

When the decision owner is available, ask **exactly one question per turn**. Before each question, retrieve targeted evidence from relevant repository files, code, tests, docs, memory, git history, and existing decisions. Do not ask about facts evidence can establish. Select the highest-risk unresolved decision that can change the plan materially, and obey the existing dependency-aware frontier in [questions.md](questions.md): never ask a dependent question before its prerequisites are resolved. After each answer, record the owner's confirmed choice (or correction), update the plan and dependency graph, remove questions made irrelevant, and recalculate the frontier. Do not repeat closed questions or jump chaotically between branches.

Where useful, present one concise question, a recommended answer, and a short reason. Clearly label the recommendation as unconfirmed; the owner may accept, change, or reject it. Do not act on the decision before the owner answers. Recommendations never become requirements by implication.

Stop when further answers no longer materially change the plan and blocking branches are resolved, or when remaining decisions are explicitly retained as `OPEN MATERIAL QUESTIONS` / owner decisions. Do not question for volume.

## Read-only self-play

When the decision owner is unavailable, conduct a read-only critique against targeted evidence. Test the plan's first failure point, hidden invariants, x10 scale where relevant, rollback/recovery, existing project behavior, and assumptions not supported by evidence. Do not make product or owner decisions to defend the plan. Unsupported choices remain open questions; propose evidence-backed alternatives as recommendations only.

## Record

Use the existing brief and, when its normal threshold is met, the existing ADR mechanism. Record confirmed decisions, decisions changed after grill, remaining open questions, and material residual risks in the final brief. Update the existing ADR through its owning process if grilling surfaces a decision; do not create a second decision log.
