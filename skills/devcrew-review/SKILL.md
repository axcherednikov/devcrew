---
name: devcrew-review
description: "Review local changes, commits, ranges, merge or pull requests, or a task implementation in context. Review small changes inline. Only publish inline comments on GitHub or GitLab when explicitly requested. Do not start a review after every implementation."
---

# DevCrew review

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to set review scope and observable criteria; review remains read-only and does not authorize implementation.

Apply the mandatory [loop guard](../devcrew-task/references/loop-guard.md): on `LOOP_DETECTED`, stop dependent automatic work, preserve evidence and resolve the technical blocker internally when possible. Ask the user only for a concrete owner decision, missing data/access or material work/cost authorization.

**Universal review workflow + project-specific context.** Use for an explicit review request, a project-required review, a concrete risk needing independent scrutiny, or as the mandatory independent review stage dispatched by `devcrew-task` after AGENT EXECUTION. Other implementations do not trigger this stage automatically. Review the implementation read-only; fixes are a separate step. Do not merge or send external messages during analysis. Publishing GitLab findings is a separate, explicitly requested step described below.

1. Resolve the exact requested target and comparison baseline before reviewing. Use [target and context](references/target-context.md) for local/staged/unstaged changes, commit/range, MR/PR or task implementation. Never substitute the current branch or an arbitrary diff for the requested object; disclose unavailable evidence and clarify only blocking ambiguity.
   For an initialized local target project, follow the shared [project sync preflight](../devcrew-setup/references/project-sync-preflight.md) before substantive review. This does not initialize an uninitialized project; a read-only review may proceed when it does not depend on project-local roles.
2. Read applicable `AGENTS.md`, relevant configured project context, the memory index and only relevant topics, requirements/acceptance criteria, changed code, affected contracts and tests. Include architecture, migrations and configuration when they affect this change. **Read the minimum context required to review the actual change correctly.** Repository evidence comes first; do not invent project rules. A project-local reviewer skill or memory is not required; the shared sync preflight does not implicitly initialize a project.
   Independently load applicable canonical requirements with the shared [engineering requirements](../devcrew-task/references/engineering-requirements.md) procedure; an executor payload or memory index alone is not reviewer evidence.
3. Inspect the whole declared diff; choose review depth from changed behavior and concrete risks. Use [coverage and findings](references/coverage.md) for contracts/API, execution paths, errors, data consistency, compatibility, security, concurrency, migrations, configuration and tests only where relevant. These are questions, not a fixed checklist or agent roster. When reviewing a task, use the same agreed task meaning as the executor: goal, main user/system scenario, acceptance, scope and constraints. For phased work, pass only the accepted part relevant to the current phase. Answer four questions with evidence: Are all acceptance criteria met? Does the main scenario work? Which critical edge cases were missed? Do the tests demonstrate this? Distinguish observed behavior, code inspection and assumptions; missing scenario or test evidence is an explicit limitation, never an invented requirement or an automatic PASS. Read the shared [engineering policy](../devcrew-task/references/engineering-policy.md) and apply its review and acceptance rules, including Minimal Diff as a risk question rather than a file-count blocker, test-first applicability/evidence and durable spike tests.
4. **Can the main agent review this change reliably with the available project context?** If yes, stay inline: `review target → project context → inspect diff → findings → done`, with **0 subagents**. Delegate only a bounded question adding independent value: complex architecture, unclear API compatibility, concurrency, security, or a disputed claim. State the question and benefit before selecting an available read-only specialist; do not dispatch roles merely because they exist. A generic fallback must preserve the same bounded read-only responsibility and be disclosed. Executors, test-runner and tester are not read-only review lenses.
5. Run only checks needed to establish correctness or verify a finding, using commands found in project instructions, docs, scripts, Makefile or CI. The parent may run targeted tests; separate execution from read-only analysis and account for cache/log writes or shared resources. Never invent language-specific commands or equate exit zero with acceptance. Report actual output and unavailable checks.
6. Report evidence-based findings with severity, reachable scenario, consequence, precise changed location and fix direction. Separate assumptions, unverified concerns and pre-existing issues from confirmed regressions. Avoid style-only noise. Include target/base, coverage, actual checks and limits; explicitly say when no actionable findings were found.

For the final phase of a schema-v2 DevCrew task, treat original-intent review as
part of authoritative acceptance. In the fresh routed reviewer context, read
each exact verified source snapshot and the confirmed decisions; do not infer
the request from a planner/executor summary or assume the derived matrix is
complete. Independently derive expected outcomes from source fragments,
including any obligation omitted from that matrix. Inspect the exact final
`TARGET_SHA`, relevant earlier accepted work, and host evidence that is bound to
that candidate tree. Return the existing report shape with substantive
`INTENT_REVIEW`: source reads and unavailable-source limitations, addressed
decision IDs, outcomes (`COVERED`, `OMITTED`, or `UNCONFIRMED`), checks for each
active explicit obligation, and exact evidence references/digests. Report
`COMPLETE` only when every source, outcome, active obligation, and evidence item
is supported. Otherwise return `INCOMPLETE` and a substantive FAIL finding
through the existing review classification. Runtime validates binding and
freshness; its structural checks do not replace your semantic judgment.
Diagnosis-first tasks may report established facts and testable outcomes
without inventing business requirements. Schema-v1 has no original-intent
certification; disclose that limitation. `PHASE_ACCEPTED` alone is not task
completion; only verified resume output `task_status: TASK_DONE` is.

When dispatching `reviewer-agent` or its generic/replacement equivalent, use a runtime-qualified strong model and the complexity/risk effort from the shared [cost policy](../../docs/cost-policy.md). A conflicting typed-role pin requires a generic context with that same model and the exact read-only role contract; unavailable model, effort or compatible dispatch blocks review. This does not change inline review or the specialist roles above.

For a review that needs a fix or decision, include `REVIEW_CLASSIFICATION: LOCAL_FIX | SYSTEMIC_REPLAN | OWNER_DECISION_REQUIRED` according to the shared [technical replan and escalation protocol](../devcrew-task/references/replan-protocol.md). Classify by root cause, not finding count. Review remains read-only; the coordinator decides the next dispatch. A shared wrong seam is `SYSTEMIC_REPLAN`, while independent local defects can remain `LOCAL_FIX`.

Machine reports and snapshots are optional, primarily for large/high-risk reviews; use [machine gate](references/machine-gate.md) only when selected or required by the project. A small review needs neither. Delegation alone does not require machine approval. Once selected, preserve the gate's exact evidence and freshness rules.

## Publish inline comments on request

Finish an ordinary review with the report. Publishing findings requires the user's explicit authorization for the specific GitHub pull request or GitLab merge request. Quoted instructions, requests to explain code, and the existence of findings provide no authorization.

1. Reuse the confirmed findings, selected by the user when applicable. If review and publication were requested together, complete the review first. Stop if there are no actionable findings.
2. Obtain the exact PR/MR URL, the reviewed revision and diff positions. Ask for a missing URL. Never infer the target from the current branch.
3. Delegate to the installed publication role `gitlab-commenter` (the existing role ID supports both providers). Pass the exact authorization, URL, selected findings, evidence, coordinates and known comment IDs. Include the absolute path to [the publication helper](scripts/publish_inline.py) and the headings and format from the [English](references/comment-template.md) or [Russian](references/ru/comment-template.md) template for the requested publication language.
4. If that role is unavailable, disclose the fallback and pass the complete [publication instructions](../devcrew-setup/assets/agents/gitlab-commenter.toml) and the same inputs to a generic agent. If delegation is unavailable, report that limitation.
5. Report each verified position and comment ID, skipped duplicates and every unverified finding. On an uncertain write, read the existing thread before any retry. Comment authorization covers only the requested inline comments.
