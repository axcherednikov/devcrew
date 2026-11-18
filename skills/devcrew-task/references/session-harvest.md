# Session harvest and evidence-driven retro

Run retro only at a completed session/task boundary when the work was significant: a completed epic or large multi-step task, a meaningful implementation → verification → review cycle, multiple fix/review loops, or a substantial session with repeated lessons. Do not run it for trivial fixes, renames, small local changes, every ordinary task, or unfinished work without enough evidence. Retro is optional and never blocks normal session close or handoff.

Use the existing owning artifacts, bounded to the current task/epic: task or epic plan/card, session snapshot, implementation and review reports (including rejected findings), verification/check records, bug/fix evidence, scoped task history/handoffs, changed files and verified diff, relevant brief/ADR changes, confirmed blockers, and commit/task evidence when available. Do not invent a retro file, reconstruct full transcripts, scan the whole repository/history, or treat impressions as facts. State unavailable evidence and limits.

Report briefly, at most 3–5 valuable lessons: (1) recurring problem classes, counted only when the same evidenced class appears at least twice; (2) loops, naming the affected slice, observed cycles, and an evidence-backed cause such as unclear requirement, missed invariant, handoff gap, weak self-check, wrong seam, insufficient test, or noisy reviewer rule; (3) practices that demonstrably reduced risk/iterations; and (4) repeated noise with examples. Do not use generic advice such as “be more careful.” A single episode is an observation, not a durable lesson.

Before persisting a lesson, inspect the configured memory index and relevant sources, engineering policy, applicable project instructions, and existing lessons. Classify candidates as task fact, project invariant, role lesson, or `POLICY_CANDIDATE`. Persist only reusable, evidence-backed project/role lessons that are not already covered by policy/instructions or equivalent memory. Replace a stale lesson only when current evidence demonstrates it is superseded, preserving its history per the existing memory lifecycle. Keep one-off details in the task card. A durable lesson must state a concrete `TRIGGER → ACTION`; otherwise do not write it. Do not turn a single project observation into global engineering policy. `POLICY_CANDIDATE` is a recommendation only and requires a separate policy task.

## Minimal Diff marker harvest

Inspect `minimal-diff:` markers only in the verified final diff for the current epic/task scope, bounded by its recorded baseline and changed/in-scope paths. Do not search the repository or attribute pre-existing markers to this work. For each marker, retain its exact text/location and classify from evidence:

- `TRIGGERED`: the stated observable trigger is evidenced as having occurred; record an actionable follow-up candidate in the existing task/session memory.
- `NOT_TRIGGERED`: the trigger was checked and has not occurred; report only, with no backlog item.
- `UNKNOWN`: evidence cannot determine trigger state; report only, with no backlog item.
- `MALFORMED`: lacks a measurable/observable trigger or concrete action; recommend correcting the marker, without inventing a follow-up.
- `PROTECTED_VIOLATION`: defers validation, security, integrity, acceptance, an invariant, accessibility, or another protected obligation; report as a contract problem, never acceptable technical debt.

A marker is a deferral note, not an automatic backlog command. Retro never creates external issues/tracker tasks, expands an epic/product scope, starts implementation, or creates a follow-up agent. Record a triggered follow-up candidate in existing DevCrew/session memory; external work requires the user's explicit request or the established approved planning workflow.

If role-specific memory/context already exists, attach an evidenced lesson to the owning role and load only relevant lessons downstream. Do not create a parallel memory model or copy full retro history into agent context. Follow [project state layout](../../../docs/project-state-layout.md) for minimal durable updates: reusable lessons in memory, confirmed behavior/accepted long-term decisions in docs, and approved project invariants in rules. Cite durable code/test/commit/document sources rather than relying only on ignored task/session files. Keep the final report short: recurring classes, loops and causes, what worked, noise, marker classifications, memory changes, actionable follow-ups, policy candidates, and evidence limits.

An explicit user requirement is a separate intake path: one justified assessment can activate it without a retro or recurrence count. Retro-derived lessons still require the evidence threshold above. Use the configured canonical requirements registry and source-reference rules in [engineering requirements](engineering-requirements.md).
