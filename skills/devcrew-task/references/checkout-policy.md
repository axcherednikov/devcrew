# Task checkout policy

Work in the current working directory by default, in DIRECT, AGENT EXECUTION,
autopilot and coordination. Agent delegation, task complexity, a task graph,
or multiple requests do not authorize a worktree or parallel writers.

Create or use a separate worktree only when the user explicitly asks for a
worktree, parallel isolated work, or to do the task in a separate branch in
parallel (for example, “let’s work on this task separately on its own branch”). Record
the actual user request; never manufacture it. A task-number branch required
by this policy is not itself permission to create a worktree. Existing explicit
authorization for the same task persists on resume; an unrelated task does not
inherit it. Without that authorization, serialize writers in the current
checkout; do not create a worktree as a fallback for dirty files or collisions.

Inspect the current branch first. Keep an existing non-primary branch and its
current HEAD, including staged, unstaged, untracked and ignored files. When on
the primary branch, automatically create a suitable task branch from that current
HEAD and carry the same files and index forward. Do not ask the user to choose
or confirm a branch. Commit, merge into the primary branch and push only when
the user requests those actions; existing authorization remains valid.

The local Git setting `devcrew.primaryBranch` overrides discovery. Otherwise use the default branch advertised by the local remote HEAD (prefer origin), then an existing `init.defaultBranch`, a unique main/master, or the only local branch. In multi-branch repositories without a clear primary, configure it explicitly for linked worktrees, for example `git config devcrew.primaryBranch trunk`.

For a task with a known identifier, register the current checkout using
`python3 scripts/devcrew.py task-checkout create --project PROJECT_ROOT --task-id ID`.
On the primary branch the helper creates `codex/tasks/ID`, choosing an unused numeric
suffix on a branch-name collision. On a working branch it registers that branch
without switching. An explicit base must match the current HEAD. Dirty state
alone is not a blocker and never grants permission to stash, reset or clean.
If the task has no number, use a meaningful task slug without a branch-choice
question. Read-only requests never create or switch branches. Resolve an existing
task with `task-checkout resolve`; creation never resumes or resets it. Detached
HEAD or conflicting lifecycle ownership requires diagnosis, not a guessed branch.

For explicitly requested parallel work, add `--worktree-request TEXT` containing
the actual user instruction. With no `--base-sha`, this uses the local primary
head. An explicit full `--base-sha` must match that head in linked mode. The
legacy `task-worktree create` command also requires
`--worktree-request`; it cannot silently create a worktree. Resume preserves the
saved checkout mode. Legacy worktrees without a saved request cannot be used
automatically; after
the user explicitly authorizes that task, record it with
`task-checkout resolve --worktree-request TEXT`. Missing linked checkouts may
be recreated only with a persisted explicit request and the lifecycle's
existing clean-state proof.

Require one writer before changing branches. Preserve staged, unstaged,
untracked and ignored files; automatic branching uses `--no-overwrite-ignore`
from the current HEAD, without checking out a different tree. Conflicting
lifecycle ownership or unsafe paths stop dependent work for diagnosis. Never
stash, reset, clean, copy edits, or create a worktree to bypass the conflict.

`TASK_WORKTREE_ROOT` and manifest `worktree` remain compatibility field names
for the assigned execution checkout. In current-directory mode,
`TASK_WORKTREE_ROOT == PROJECT_ROOT`; in authorized worktree mode they differ.
Both modes preserve durable receipts and branch/HEAD validation. In AGENT
EXECUTION they also require the same phase plan, scope ownership, routed
dispatch, checks and independent review. One writer
owns a checkout at a time. Current-directory lifecycle and phase state live
under its Git directory in `devcrew-checkouts/`, outside the production tree.
