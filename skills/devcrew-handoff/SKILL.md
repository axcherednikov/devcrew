---
name: devcrew-handoff
description: "Prepare a read-only handoff for the user, QA, frontend or mobile developers, API consumers, or customers from verified implementation evidence."
---

# DevCrew handoff

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) when scoping the requested audience handoff; report only outcomes and checks supported by evidence.

Prepare audience-specific delivery text for work that already has implementation evidence. This workflow is always read-only: it does not implement, fix, review, change criteria, create tasks/commits, or publish comments. Explicit `$devcrew-handoff` and router intent `HANDOFF` select this skill.

After resolving a local target project and before preparing the handoff, apply the shared [project sync preflight](../devcrew-setup/references/project-sync-preflight.md). Any required project maintenance happens before this workflow's read-only evidence work; do not initialize an uninitialized project implicitly.

## Resolve and qualify evidence

Follow the public router's provider-neutral [source resolution](../devcrew/references/source-resolution.md) and [bounded handoff contract](../devcrew/references/handoff.md). Read only the referenced tracker/task, MR/PR, commit/branch/diff/worktree, task/spec, attachment, or completed task/session evidence needed for the requested outputs. If a tracker issue is provided, obtain its context and follow available links to implementation evidence. Never claim completion from a task description alone.

Build a small internal map: `REQUIREMENT → IMPLEMENTED BEHAVIOR → VERIFICATION EVIDENCE → AUDIENCE`. Ground each factual statement in requirements, implementation/diff, API/schema/config, tests, accepted decisions, or confirmed review results. Mark unresolved facts `UNKNOWN`; do not turn them into QA/client/customer instructions. Separate `CONFIRMED`, `UNKNOWN`, and `NOT_APPLICABLE`.

Return the relevant explicit failure state instead of a success handoff:

- `SOURCE_UNAVAILABLE` — referenced external source cannot be retrieved.
- `IMPLEMENTATION_NOT_FOUND` — task exists but no linked or supplied implementation evidence is available.
- `INSUFFICIENT_EVIDENCE` — implementation is located but evidence cannot support the requested claims.
- `HANDOFF_BLOCKED` — confirmed blocking defect/review finding prevents representing work as ready.

Explain the evidence gap or blocker briefly. Do not produce successful customer completion text in these states.

## Select audiences and write

Use audience selectors `QA`, `CLIENT`, `CUSTOMER`, or `ALL`. Use the user's language, concise Markdown, and copy/paste-ready audience blocks. Unless the user asks to omit it, include the **private summary** plus the requested audience blocks. A generic/full handoff selects all four. “Only QA” or “without the private summary” suppresses the private summary. If only one audience is requested without “only”, include private summary and that audience. Never emit unrequested audience blocks.

Use [audience rules](references/audiences.md) and the [English](references/template.md) or [Russian](references/ru/template.md) template for the requested audience. Keep each audience's purpose distinct: engineering-oriented private summary, behavior-oriented QA, externally observable contract for clients, and outcome-oriented customer/PM. Technical appendix is opt-in only.

For QA, prioritize evidenced changed behavior, failure paths, state transitions, permissions/isolation, money/external effects and nearby regressions; usually 1–3 scenarios for a small change and 3–7 for a medium change. Call out critical scenarios separately when evidenced high-risk behavior is affected. Do not turn this into a project-wide regression plan.

For client output, assert contract stability only after checking relevant public API/schema/config and diff. A confirmed backend-only change must explicitly say client changes are not required; otherwise say `UNKNOWN` rather than infer. Customer text omits internal names and tooling.

## Chaining and cost

When requested with implementation, wait until `devcrew-task` or `devcrew-autopilot` has completed verification, then use only confirmed implementation and verification evidence. When requested after review, use confirmed review evidence; a blocking review finding returns `HANDOFF_BLOCKED`. Ordinary task/autopilot/review completion may offer handoff, but must not generate one automatically.

Read evidence once and generate the requested audiences in this workflow; do not fan out audience agents. Prefer a bounded model under the shared [cost policy](../../docs/cost-policy.md): routine routing/read-only synthesis uses low effort, medium only for genuine ambiguity; high model/effort is not required.
