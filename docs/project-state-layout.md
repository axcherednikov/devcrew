# Project state layout

[Russian version](ru/project-state-layout.md)

DevCrew stores the state it needs to run a task under the Git-ignored `.codex/` directory. Durable knowledge belongs in the project's established `memory/`, `docs/`, and `rules/` locations. Inspect repository conventions before choosing these paths.

## Initialization

Before running `init`:

1. Read the root `AGENTS.md` and any applicable nested `AGENTS.md` files.
2. Follow their references to other rules and indexes, such as `rules/index.md`.
3. Inspect existing `docs/`, `memory/`, `rules/`, `.codex/agents/`, task logs, and naming conventions.
4. Select paths for durable knowledge that follow project conventions. Put technical task, session, and check directories under `.codex/`.
5. Run `init` with the paths you identified.

When the project has no specific conventions, use the defaults:

| Purpose | Path |
| --- | --- |
| Project context | `docs/context.md` |
| Task cards | `.codex/tasks/` |
| Session snapshots, handoff, and dispatch/capacity receipts | `.codex/sessions/` |
| Durable memory | `memory/` |
| Check records and logs | `.codex/checks/` |
| Path configuration | `.codex/devcrew.json` |
| Project integration version and sync metadata | `.codex/devcrew-state.json` |
| Project roles | `.codex/agents/` |

## Task working files and durable knowledge

DevCrew writes task-specific materials to the configured task directory, which defaults to `.codex/tasks/<task-id-or-slug>/`. This includes the brief, draft decisions and ADRs, plan, task card, implementation report, findings, and working handoff. Session snapshots and the coordinator inventory belong in `.codex/sessions/`. Machine-readable reports and full logs belong in `.codex/checks/`. Do not create `docs/<task>/` or a separate top-level `sessions/` for this material.

At a meaningful task boundary, carry forward only confirmed knowledge that remains useful after the task:

- `memory/`: record a reusable lesson with a specific `TRIGGER → ACTION`, the scope in which it applies, and a verifiable source. Keep one-off observations in the task card.
- `docs/`: summarize final behavior, a public contract, or an accepted long-term architectural decision. Update the existing document or ADR rather than copying the entire brief, plan, or report.
- `rules/`: record a confirmed, mandatory project invariant only if the current rules do not already cover it. Follow the project's process for changing rules.

Before writing, check the existing document or index for duplicates. Record the fact or decision, why or when it applies, and a durable reference such as code, a test, a commit, or a maintained document. An ignored task or session file cannot be the only evidence for a durable lesson. Leave full logs, conversation history, working hypotheses, status reports, and next steps in the working records. Keep `docs/context.md` for confirmed project context and `.codex/` for the state of ongoing work. Small tasks that yield no new durable knowledge need no additional record.

This procedure supplements [session harvest](../skills/devcrew-task/references/session-harvest.md) and the [memory lifecycle](../skills/devcrew-session/references/memory-lifecycle.md). Their requirements for evidence and applicability still apply.

## Configuring paths

Specify state and documentation paths relative to the project root:

```text
python3 scripts/devcrew.py init \
  --project PATH \
  --context rules/devcrew-context.md \
  --tasks-dir .codex/tasks \
  --sessions-dir .codex/sessions \
  --memory-dir memory \
  --checks-dir .codex/checks
```

In this example, the project keeps its context in `rules/` and technical data in `.codex/`. Choose new technical layouts within `.codex/`. Existing configured paths remain available for safe reads and resume operations. Migrate legacy task and session paths with `migrate-state` before writing new records.

The helper stores the selected paths in `.codex/devcrew.json`:

```json
{
  "paths": {
    "checks": ".codex/checks",
    "context": "rules/devcrew-context.md",
    "memory": "memory",
    "sessions": ".codex/sessions",
    "tasks": ".codex/tasks"
  },
  "schema_version": 1
}
```

`doctor`, `run-check`, `verify-check`, and `flaky-hunt` use this configuration. Running `init` again without new flags keeps the existing paths. Supplying different paths over an existing configuration returns an error and leaves the files in place.

## Migrating existing runtime state

At a point when no process or agent is writing project state, preview the migration and then apply it:

```text
python3 scripts/devcrew.py migrate-state --project PATH --dry-run
python3 scripts/devcrew.py migrate-state --project PATH
```

The command moves only the configured `tasks` and `sessions` directories located outside `.codex/`, to `.codex/tasks/` and `.codex/sessions/`, respectively. It preserves their contents, extra fields in `.codex/devcrew.json`, and the configured context, memory, and checks paths. Configured directories already inside `.codex/` stay in place. The helper validates all sources and destinations before writing. An existing destination, symlink, or special file blocks migration. A write failure rolls back completed moves. `--dry-run` writes nothing. The command does not scan arbitrary `docs/<task>/` directories. Move an unconfigured brief directory separately after confirming its task ownership and preserving final project documentation.

Migration leaves evidence, digests, and saved review/dispatch payloads as historical records. Check references in task cards, manifests, and handoffs. Obtain fresh check or dispatch records for the new location before continuing work. Moving a record does not revalidate an earlier PASS. The command leaves the Git index unchanged and creates no commit. If Git already tracks runtime files, remove only verified technical paths from tracking separately and retain durable documentation. Ignore rules do not affect files Git already tracks.

The plugin package manifest holds the plugin version. The monotonically increasing project integration schema version is stored separately in `.codex/devcrew-state.json`. A plugin version change without a schema migration requires neither project file changes nor a commit. The shared [sync preflight](../skills/devcrew-setup/references/project-sync-preflight.md) checks the schema before substantive DevCrew workflows. Sync installs missing roles, updates only roles it can safely identify as managed, and records the time and version of the sync.

The state records the plugin version, project schema, sync timestamp, and hashes of the templates and installed managed roles. Sync updates local files only. It adds `/.codex/` to the root `.gitignore` only if Git's effective ignore rules do not already exclude the directory. The Git index and HEAD stay unchanged. Both `sync_commit` and `sync_commit_id` are `null` in the result. Historical commit IDs remain readable in older state files. Custom roles and other files in `.codex/` are outside sync's ownership.

## Limits and data preservation

- `init` preserves user-authored documents.
- Paths must be relative, disjoint, and contained within the project.
- The helper does not write through symlinks in managed paths.
- Initialization checks every destination before creating any files.
- `init`, `sync`, and `migrate-state` use Git to check whether `.codex/` is ignored. This accounts for `.gitignore`, `.git/info/exclude`, global ignore rules, and the order of negations. If the directory is already ignored, the root `.gitignore` stays untouched, regardless of the position of the matching rule. If the directory is not ignored or the rule is negated, the helper appends `/.codex/`. A Git ignore-check error stops the dependent write. Outside Git, the helper checks for the trailing local ignore rule. Keep `.codex/` untracked. Do not force-add its contents. Remove already tracked runtime files from tracking separately while keeping them on disk. Sync does not change the index or rewrite history.
- The working-tree fingerprint excludes the checks directory regardless of its configured location.
- Project roles are created separately in `.codex/agents/`. Open a new Codex task to load them.

## QA session state

Local QA profiles and immutable scenario snapshots are stored below the
configured task root at `qa/`; immutable run results/evidence and the discovery
index are stored below the configured checks root at `qa/`. Paths are resolved
through the same project-relative safe destination checks as other runtime
state. A run ID names its immutable snapshots and evidence. The index is only a
catalog; reports verify the result, evidence, snapshots, project identity, and
digests before returning a result. Do not treat historical status text alone as
current PASS evidence.

Managed QA environment ownership and recovery records live under the configured
checks root at `qa-environments/runs/`; the resource and port claim index and
admission lock share that root. Records retain the profile path and digest,
startup intent, verified process identity, preparation and cleanup results,
readiness addresses, the run data record, and cleanup failures. Verified
identity includes process start time, process group, and session, so a reused
PID is not treated as the original service. A record marked `READY` or
`CLEANUP_FAILED` must carry all of these fields with the types the recorder
writes; a missing, empty, or wrongly typed field is refused before any state
write, data removal, or signal. A `ready` port is also claimed in
the index to keep a second DevCrew run away; the record stores it under
`port_claims` and the index marks the claim kind. A per-run state lock under
`qa-environments/locks/` serializes that run's start and stop transitions, while
the project resource-index lock is held only for short claim reads and writes.

Preparation and declared cleanup records also keep the process group created for
each command. A recorded result only stays settled while that group is empty;
surviving descendants are terminated when the group can still be tied to the
confirmed leader, and otherwise the outcome becomes `EFFECT_UNKNOWN` with its
claims held. A leader whose current identity cannot be read is never signalled.
Declared cleanup runs only after confirmed data ownership, preparation, or a
service launch could have produced a side effect, so a refusal before that point
leaves the profile's cleanup commands unstarted; a command that failed before its
process was spawned counts as no side effect. The profile's cleanup commands run
in their declared order, and each record keeps its index for recovery. The
recorded data path is
revalidated immediately before every new cleanup command starts, on every stop,
including when an earlier refusal left the data record `EFFECT_UNKNOWN`; a
replaced directory or a symlinked parent blocks them, just as it blocks the
built-in removal. A started cleanup step is
reconciled on every stop, including after its data directory is already gone, so
a saved failure or unknown outcome keeps its claims held.

A profile may declare a project-relative `data_dir` outside the checks root.
Start records the creation intent before creating it and marks the directory
owned only after writing a `.devcrew-qa-owner.json` marker naming the run and
project; stop renames the marked directory before deleting it, and refuses to
touch a directory whose marker does not match. An intent without confirmed
ownership neither deletes data nor runs the profile's cleanup commands. Stop revalidates the recorded
path against the current project layout before removal: a path outside the
project, under the checks root, or behind a symlinked component is left in place
and reported as incomplete cleanup. After the rename, the pending-removal stage
and the renamed path stay recorded until that directory is gone, so a repeated
stop retries it instead of treating the missing original path as success. A start
intent, `EFFECT_UNKNOWN` outcome,
failed cleanup, or unconfirmed data record keeps its claims until an operator
resolves it; a new start does not adopt a process or erase an unresolved claim. Use the matching `qa environment-status` and `qa
environment-stop` commands to inspect or release only verified owned processes,
marked data, and their claims.

Agent-driven web QA attempts live under the configured checks root at
`qa-web/runs/`, with their `index.json` and admission lock beside them. Scenario
and profile snapshots live under configured `tasks/qa/snapshots/RUN_ID/`.
Records retain the run token, declared viewports, per-viewport observations,
expected and actual check values, PNG screenshots with digests and dimensions,
the fresh backend readback status and field match booleans, and the linked
managed-environment cleanup result. Screenshots are copied into the run
directory and are never overwritten; missing, changed, or wrongly sized images
cannot support `PASS`. A web run is bound to the saved environment run ID and
requires `READY` at start. Recording performs the fresh backend reads first,
then stops that managed environment and requires confirmed `STOPPED` cleanup.
Discovery, listing, and report reading open no lock and create no file, so they
also work when saved artifacts are read-only.

Replay attempts create another independent run directory and scenario snapshot;
the source run is read-only. Before a replay starts, the bound environment is
re-verified live: project identity, a different environment than the source
run's, matching process identities for every service, and a base URL equal to a
recorded readiness address. A stale `READY` record, a dead service, a foreign
address, or an environment already bound to another attempt fails before a new
run exists. The binding is written with the new run directory for every attempt,
including a plain start, so an unfinished attempt still owns its environment. The new result and evidence keep `replay`
provenance (source run, source digests, source status, selection, isolated flag)
and the project index records `replay_of` for discovery. Defects are derived
from saved failed checks and readbacks, not stored as writable history. An
incomplete source has no result digest and can only be replayed as an explicitly
isolated attempt.
