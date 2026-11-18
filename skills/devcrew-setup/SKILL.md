---
name: devcrew-setup
description: "Create or verify project-specific DevCrew state and the roles in .codex/agents/. Preserve existing user changes."
---

# DevCrew setup

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to the requested setup outcome; preserve existing state and do not expand the task into an audit.

Use this skill when a repository needs DevCrew project state or when setup health is uncertain. Before creating anything, read the applicable `AGENTS.md` files, then inspect the repository's existing `docs/`, `memory/`, `rules/`, agent configuration, manifests, and task conventions. Reuse the project's established locations; do not impose a `docs/devcrew/` subtree. Initialization is project-local and idempotent.

The helper defaults to `docs/context.md`, `.codex/tasks/`, `.codex/sessions/`, `memory/`, and `.codex/checks/`. Pass `--context`, `--tasks-dir`, `--sessions-dir`, `--memory-dir`, and `--checks-dir` when discovery shows different project conventions. The selected layout is recorded in `.codex/devcrew.json` so all helpers resolve the same paths. Explicit setup provisions the complete role template set into `.codex/agents/`, writes `.codex/devcrew-state.json` with the current project schema, and installs `card.template.md` in the configured tasks directory, preserving existing documents. Existing task cards are extended when resumed, not rewritten by init. The plugin directory is never treated as an automatically loaded agent registry.

Do not overwrite user-authored files. If a target exists with different content, preserve it and report a conflict for review. Do not write installed caches, global settings, marketplace files, or external services. Setup does not copy user-specific invariants from the source project: discover them from the target repository and record unknowns as open questions.

Follow [project state layout](../../docs/project-state-layout.md): new technical task/session/check paths belong inside ignored `.codex/`; durable project knowledge stays in the established `memory/`, `docs/`, and `rules/`. `init` appends the runtime ignore rule without removing existing Git rules. For legacy configured task/session paths outside `.codex/`, inspect `migrate-state --dry-run` at a quiescent boundary, then migrate before new writes. Preserve task history and reconcile references/evidence after the move; never blindly relocate functional documentation.

Select the language for new project documents from an explicit deliverable preference or the user’s language. Pass `--language ru` for Russian, otherwise use English. Other reusable project templates follow the same mapping: `assets/project/` for English and `assets/project/ru/` for Russian. Existing documents retain their contents. Agent instructions remain in English for both languages.

Use the local helper:

```text
python3 <plugin-root>/scripts/devcrew.py init --project PATH [--language en|ru] [--context FILE] [--tasks-dir DIR] [--sessions-dir DIR] [--memory-dir DIR] [--checks-dir DIR]
python3 <plugin-root>/scripts/devcrew.py migrate-state --project PATH [--dry-run]
python3 <plugin-root>/scripts/devcrew.py sync-status --project PATH
python3 <plugin-root>/scripts/devcrew.py sync --project PATH
python3 <plugin-root>/scripts/devcrew.py doctor --project PATH
```

`doctor` is read-only. It checks required state and role TOML structure. Valid project customizations are allowed; a difference from a shipped template alone is not an error. It reports actionable diagnostics rather than silently repairing files. Read `assets/` for project templates and [project onboarding](../devcrew-task/references/project-onboard.md) for discovery order.

For automatic, bounded project integration checks before substantive workflows, follow the shared [project sync preflight](references/project-sync-preflight.md). Setup writes current role templates and schema state for new projects; `sync` migrates legacy initialized projects locally, preserves the runtime ignore rule, and never stages files or creates a Git commit.

Project roles are TOML files with `name`, `description`, and `developer_instructions`. They inherit the host model and declare `model_reasoning_effort`. A project owner may configure a local `model` or adjust effort. Read-only role templates may declare `sandbox_mode = "read-only"`; that declaration describes intended behavior and does not override the parent session's policy.

Specialist roles are dispatched only for a concrete need. The internal `decision-agent` role receives bounded context and is used for an initial technical decision when AGENT EXECUTION has no sufficient accepted plan, or for an evidence-backed `TECHNICAL_REPLAN`. A sufficient current plan does not require a decision-agent call. Reviewer dispatch requires the model and effort selected at runtime by the shared [cost policy](../../docs/cost-policy.md), using supported runtime overrides when necessary. A project may override a role in `.codex/agents/` and should record the reason in its configured context file. The sync helper preserves local customizations and reports a conflict when a required managed-role update cannot be applied safely.

Start a new Codex task in the initialized project to load custom roles. The helper cannot reload roles into the current session.
