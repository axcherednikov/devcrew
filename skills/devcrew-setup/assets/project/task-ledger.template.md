# Task card

Copy this template into a task-specific card in the configured tasks directory. Fill it with observed facts. An empty list means no entry has been recorded; it cannot establish that a check passed. Update the state after each significant stage and before handoff. Preserve existing entries and agreed requirements.

```yaml
task: null
status: planned
updated_at: null
workspace: null
branch: null
git_head: null
state:
  done: []
  in_progress: []
  next: null
  key_files: []
blockers: []
evidence: []
```

`status` accepts planned / in_progress / blocked / review / done. Use a timestamp with a time zone for `updated_at` and an absolute working directory for `workspace`. If Git is unavailable, `branch` and `git_head` may be null. In `done`, list completed stages and their results. Use `in_progress` for unfinished work, `next` for one concrete action, and `key_files` for paths with their purpose. For each blocker, record its cause, the work it blocks, and who needs to act. In `evidence`, record paths to CLI records and reports, what state they checked, and what they covered. The done status alone does not prove acceptance.

## Goal and scope

Describe the expected outcome, included and excluded changes, and dependencies. Reference the brief and sources of decisions without duplicating the full package.

## Acceptance criteria

For each criterion, record an observable check and link to its result. Identify anything unverified and explain why.

## Ownership and stages

Identify the implementer, directory, owned paths, and dependencies for each part. An unfinished part blocks any dependent wave. A wave's checks must cover its integrated tree.

## Decisions and deviations

Record significant decisions and their sources, scope changes, and reasons for skipping any applicable checks.

## Resume

Read `state`, blockers, and linked files. Verify the directory, branch, HEAD, local diff, and freshness of the relevant records. If the code changed, retain earlier results as history, mark affected checks stale, and update `next`. Use the existing card for continuity; chat recollection cannot establish status.
