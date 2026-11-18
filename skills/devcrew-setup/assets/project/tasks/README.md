# DevCrew task state

Place the task index and per-task cards in the task directory configured for this repository. Keep status, scope, acceptance, findings, and links to task artifacts in the card.

The default is ignored `.codex/tasks/`. Store per-task brief, plan, drafts, implementation report and working handoff in a task ID/slug subdirectory here. Preserve only compact confirmed behavior/decisions in durable docs, reusable lessons in memory, and accepted project invariants in rules; do not copy the full task package into those directories.

Use `card.template.md` for a new card. Update its structured state at stage boundaries and before handoff. Existing cards remain valid: add missing fields when continuing that task, preserving its history and project conventions. Store machine gate reports under the configured checks directory and link them from the card.
