# Project integration sync preflight

Every substantive DevCrew workflow that depends on project-local integration
checks this contract once the target repository is known and before dispatch,
review, diagnosis, or implementation. This is a deterministic local CLI check;
it does not call a model or scan the repository. The plugin package is the
source of templates, while the project's `.codex/agents/` files are the
runtime role configuration.

Run the installed plugin helper against the target project:

```text
python3 <plugin-root>/scripts/devcrew.py sync-status --project PROJECT
```

Interpret the JSON `status`:

- `SYNC_OK`: continue. Plugin version differences alone do not require project
  writes when the project schema and managed integration are current.
- `SYNC_REQUIRED` with `conflicts: []`: automatically run
  `python3 <plugin-root>/scripts/devcrew.py sync --project PROJECT`, without
  asking the user to approve this internal maintenance. The helper plans
  changes first and applies only missing managed roles, proven DevCrew
  managed-field updates with allowed local overrides, the project sync state,
  and future schema migrations, plus the root `.gitignore` runtime rule. If successful, continue the original user
  request with its original scope and acceptance; do not ask the user to repeat
  it. This automatic authorization covers only local DevCrew maintenance,
  never a later implementation commit for the user's task. Sync never stages
  files or creates a Git commit; project-local `.codex/` must remain untracked.
- `SYNC_CONFLICT`: stop work that depends on the project integration. Ask the
  user to resolve the supplied paths/reasons and preserve project files. This
  is the sync decision point requiring user input. Never fall back to an
  unconfigured role or silently use the host model for a role decision.
- `RUNTIME_RESTART_RECOMMENDED`: a new Codex task/session may load updated
  typed role definitions. It does not stop the current task when a distinct
  generic agent context is available: route the role with the current model
  policy, pass the current plugin role contract and explicit model/effort, and
  continue the original request. Stop at the dependent stage only if generic
  dispatch or its required route is unavailable.
- `PROJECT_NOT_INITIALIZED`: do not initialize a project implicitly. Follow
  the owning workflow's policy: DIRECT may proceed if it does not rely on
  project-local roles. AGENT EXECUTION requires explicit setup or must report
  `AGENT_RUNTIME_REQUIRED`; do not fall back to host implementation.

Before the first task/session write, also apply [project state layout](../../../docs/project-state-layout.md).
Read the configured task/session paths; use ignored `.codex/tasks/` and
`.codex/sessions/` when no paths are configured. For legacy configured paths
outside `.codex/`, at a quiescent boundary inspect
`python3 <plugin-root>/scripts/devcrew.py migrate-state --project PROJECT --dry-run`,
then apply the same command without `--dry-run` when its plan is conflict-free.
This is bounded local state maintenance; it does not authorize a Git commit,
changing acceptance, or moving arbitrary functional documentation. Preserve
task history and reconcile card/manifest links and evidence before resume.
With active writers, defer dependent state writes until the boundary is safe;
do not move their state underneath them. A migration conflict preserves the
sources and stops dependent writes for resolution. Read-only work needs no
state migration.

For AGENT EXECUTION, this sync status is not proof that a role is loaded into
the active runtime. Check and report these separately for each required role:

- `ROLE_FILE_EXISTS`: whether the role TOML exists in the target project or
  subproject.
- `ROLE_RUNTIME_AVAILABLE`: whether the active runtime exposes that role or
  can start a generic distinct agent context with its exact role contract.

A TOML found under a subproject does not imply that a task opened from a root
workspace loaded the role. If a required role is not runtime-available, return
`AGENT_RUNTIME_REQUIRED` with `MISSING_CAPABILITY` and `BLOCKED_STAGE`. A generic
spawn is a fallback only when it creates a real separate context; never use the
host as an unreported worker. Do not expand project sync behavior to infer
runtime role loading from file presence.

Integration and synchronization belong to `HOST_PROJECT_ROOT`, the configured
project that owns DevCrew state. A durable task worktree is an execution
checkout, not a second DevCrew installation: do not sync it, install task-local
role files, or require task-local roles before using a generic distinct
executor context. Keep `PROJECT_ROOT` as the host integration root and pass
`TASK_WORKTREE_ROOT` separately as the executor's only production write root.

The `sync` command updates local runtime files without changing Git HEAD or
the index. It uses Git's effective ignore rules for `.codex/`, including global
excludes, `.git/info/exclude` and negation precedence. An already ignored directory
requires no `.gitignore` creation or edit, regardless of the matching rule's
position. Otherwise it appends `/.codex/`, preserving existing rules and leaving
that edit unstaged. A Git ignore-query error blocks the dependent write; non-Git
projects retain the conservative trailing-literal-rule fallback. It requires no Git author
identity and invokes no commit hooks. Existing checks for unsafe Git operations
and pre-existing changes to tracked managed paths remain fail-closed. Unrelated
staged and dirty project files remain intact. Already tracked runtime files
must be removed from tracking separately while preserving their working copies;
sync does not change their index entries or rewrite existing history.
A failed/conflicting preflight blocks dependent substantive work; do not
continue against a partially applied integration.

The single machine-readable state is `.codex/devcrew-state.json`. It separates
`plugin_version` from `project_schema_version`, records the synchronization
time, role template/installed hashes, field ownership and local overrides.
New sync state has `sync_commit_id: null`; the CLI's `sync_commit` result is
also null. Historical commit IDs remain readable for compatibility.
`.codex/devcrew.json` remains the separate versioned
state-path configuration. `AGENTS.md` is not a version store.

Only these paths are sync-managed:

- `.codex/devcrew-state.json`;
- `.codex/agents/<name>.toml` for names in the helper's shipped `ROLE_NAMES`.

The only project-file edit outside these runtime paths is the root
`.gitignore` rule that keeps `.codex/` ignored.

Other `.codex` files, custom role names, source files, and project documents
are outside the sync ownership contract. The role contract explicitly separates:

- `MANAGED_FIELDS`: `name`, `description`, `developer_instructions`.
- Optional managed `sandbox_mode`: only when the current or recognized
  historical DevCrew template for that role defines it. It is never a local
  override. Roles such as executor-agent do not gain this field implicitly.
- `LOCAL_OVERRIDE_FIELDS`: `model`, `model_reasoning_effort`.

A local model/effort change is accepted and recorded; managed-field edits and
unknown fields fail closed with `SYNC_CONFLICT` before any writes. Only known
contract string fields are rendered, so unknown settings cannot disappear.
The allowlist is a versioned implementation contract, not a universal TOML
schema. Updating it requires an explicit migration decision.

Each role state stores `template_sha256` (raw shipped bytes),
`installed_sha256` (actual installed bytes), `managed_fields` (ordered field
names), `managed_fields_sha256`, `local_overrides` and
`override_fields_sha256` (per-field hashes of installed model/effort values).
`locally_modified` reflects the presence of allowed overrides. Semantic hashes
use SHA-256 of UTF-8 JSON with sorted keys, `ensure_ascii=False`, and compact
`,`/`:` separators. No complete role TOML is stored in state.

The per-field baseline hashes distinguish a user changing a model/effort from
an unchanged value inherited from a previous template. An inherited value
follows new template defaults; an existing local override survives template
updates. Changing a field locally to the current template default clears its
override. A formatting-only change to a recorded role without an allowed value
change is preserved as a conflict rather than silently rewritten.

A project containing known managed role files can migrate without either
state file; an empty project remains `PROJECT_NOT_INITIALIZED`. Legacy
provenance comes from the shipped
[historical role catalog](../assets/legacy-managed-role-fields.json), not from
Git history at the destination. Exact historical template bytes inherit new
defaults. Otherwise, recognized managed-field projections allow preservation
of differing model/effort values. Unknown managed instructions conflict. Old
whole-file state can migrate only when it proves an unchanged installed
baseline or its unchanged installed role matches recognized managed fields.
This first sync creates `.codex/devcrew-state.json`; it does not create the
separate state-path configuration or project documents.

All role destinations, parsed candidates, and state are planned before writes.
Files use same-directory temporary writes and atomic replacement. Caught
write failures restore the prior role, state and `.gitignore` bytes and remove
newly installed files. No Git staging, reset or commit is performed.
This is not a crash-atomic multi-file transaction: process termination or a
machine failure between replacements can leave mixed generations. The next
preflight detects inconsistent managed hashes and refuses dependent work.
Recovery then requires explicit resolution; an interrupted sync must not be
reported as successful.

The setup helper reconciles to the current desired role/schema state rather
than running an open-ended migration framework. Increment the project schema
only when project-local integration changes are required. A higher recorded
schema than the installed plugin supports is a conflict. The fast path checks
the small role set and state file only.

## User-visible sync confirmation

The packaged `project_sync_notice.py` hook checks the current project on
`UserPromptSubmit` and `Stop`. It emits a localized `systemMessage` (English or Russian, selected from the submitted text or process locale when text is unavailable) only when
the installed helper reports `SYNC_OK` and the sync-state bytes remain stable.
A package version difference alone does not require project writes or prevent
confirmation when the project schema, managed roles, and integration are
already current. The message confirms the verified project state and role files
on disk, not that updated instructions or roles are already loaded into the
current session. Preserved local model/effort overrides are named explicitly.

The hook never performs sync, edits project files, blocks a turn, or contacts an
external service. It deduplicates confirmations for each project by the pair of
installed plugin version and sync-state digest in runtime-supplied
`PLUGIN_DATA`, with a lock for concurrent hooks. A new plugin version emits one
confirmation even when state bytes are unchanged; the same version/state pair
stays silent. A legacy marker without a version emits once and is upgraded.
Missing data access, invalid state, pending updates or conflicts produce no
success message. The hook cannot attest that the UI displayed it. Codex
requires trust for the current bundled hook definition before it runs. See
[Codex hooks](https://learn.chatgpt.com/docs/hooks).
