# Working with DevCrew

[Русский](../ru/workflows.md) · [Installation and local CLI](guide.md)

## Give the task an observable outcome

In the target project's Codex chat, start with `$devcrew` and describe the outcome. For example, “Fix the CSV export so an empty result still has a header row. Run the export tests.” Include the branch, file, issue or pull request when it determines what to inspect. A tracker reference needs a connected tracker with read access.

| I want to… | Skill |
|---|---|
| Prepare a repository or understand the product already in it | `$devcrew-setup`, `$devcrew-product-onboard` |
| Agree on the scope of a change, implement it, or run part of an existing task graph | `$devcrew-brief`, `$devcrew-task`, `$devcrew-autopilot` |
| Find the cause of a bug or assess an existing architecture | `$devcrew-diagnose`, `$devcrew-architecture` |
| Review a diff or exercise a running local service | `$devcrew-review`, `$devcrew-qa` |
| Coordinate existing tasks, save a session, or hand results to another team | `$devcrew-coordinate`, `$devcrew-session`, `$devcrew-handoff` |
| Draft a commit message, maintain skills, or request concise replies | `$devcrew-commit-message`, `$devcrew-skill-doctor`, `$devcrew-small-talk` |

## Implement a change

A bounded fix with a clear contract and a deterministic check can run directly. Higher-risk or uncertain changes use separate execution and review. The agent inspects the current branch, preserves existing changes and follows the target project's verification instructions. Ask explicitly when you want a commit, merge, push or separate worktree. The existing-graph autopilot has its own per-task commit procedure; check the selected phase before starting it.

Formal phased tasks bind a verified file or attachment with `phase source-bind`. If the task originates in chat, the alternative is a structured, user-confirmed Intent Contract. Review its goal, requirements, acceptance and non-goals before confirming the digest. The host supplies `--intent-assessment` when initializing a schema-v2 phase. A prompt digest alone cannot supply the original request. At the end, a fresh reviewer checks the candidate against the bound source; `phase resume` reports `TASK_DONE` only after all required evidence is valid. [Runtime reference](../runtime-contract.md#original-intent-and-task-completion)

## Review and verify

For a review, identify the target: “Review the staged diff” or “Review this PR against its base.” Expect findings with a file, a reachable scenario and the consequence. External comments require an explicit request. For a QA run, identify a local test service and a specific user flow. Use synthetic data. Browser actions require host support; reading an existing QA report reads saved evidence.

To check a saved command record, use `verify-check --record PATH`. Read its log to see what actually ran. A passing unit suite, a model routing evaluation and a live Codex run answer different questions. [CLI contracts](../runtime-contract.md) · [Routing scenarios](../../tests/routing-evals.md)

## Supervise a full brief

When the host supports visible project chats, a parent chat can hand an approved brief to a child and observe its progress. Verify this in a fresh project session. In the positive case, explicitly approve creation and watch one child appear. Without approval, verify that no child or message is created. If interrupted, resume by reconciling the existing chat and last action before retrying. This avoids duplicating a task after an uncertain send. [Handoff design](../brief-supervision-adr.md)

## Find the evidence

Project conventions and durable lessons belong in the configured context and memory directories. Look under `.codex/tasks/` for working task records, `.codex/sessions/` for handoff state and `.codex/checks/` for execution logs. These locations can be configured per project. Check `.codex/devcrew.json` before moving data. [Storage layout](../project-state-layout.md) · [SSH profile storage](../server-profiles.md)
