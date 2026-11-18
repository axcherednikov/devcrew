# Runtime contract

The helper is invoked as `python3 <plugin-root>/scripts/devcrew.py <subcommand>`. This document defines the data contracts and observable records used by the declarative skills.

## Wave gate

`plan` is an object with `waves`, where each wave has an `id` string and `slices`. Each wave also requires ordered `verification` text and `verification_bindings` using the phase binding format below. The planner derives these commands from project rules and the accepted plan before execution. Each slice has an `id` string and `files`, an explicit list of relative file paths. Paths are normalized, must remain inside the repository, and may not be absolute or contain `*`/`?` selectors. Bracket-named paths such as `[id].tsx` are literal filenames, never expanded. Collision aliases are normalized before ownership is checked; conflicting ownership within one wave is rejected. The same file may appear in sequential waves.

`results` is an object with `waves`, where each wave has an `id`, `slices`, and `checks`. Each result slice has an `id`, boolean `done`, and `problems`, a list of strings. `checks` is a nonempty list of `run-check` JSON record paths for the integrated wave on the receiving checkout. Paths are absolute or relative to `--project`; they resolve inside that project's configured checks directory. Duplicate records, including aliases, are rejected.

`wave-gate --project HOST [--execution-root TASK] [--task-id ID] --plan PLAN --results RESULTS --wave N` rejects unknown or duplicate results. `N` is the requested next wave and is zero-based: wave zero validates plan/result structure and persists the accepted plan, execution root, task ID, approval time and environment digest under host checks state, before commands run. Subsequent calls for that task/execution root require that same plan; renaming or copying the plan file cannot reset approval. Previously approved plans are immutable through `wave-gate`, including at wave zero. `--replan-reason TEXT` is only a wave-zero request for separate plan reconsideration: it returns nonzero with `details.status: REPLAN_APPROVAL_REQUIRED` and `replan_requested: true`, and never creates, overwrites or refreshes approval. Plan changes without the flag also return `REPLAN_APPROVAL_REQUIRED`, with `replan_requested: false`. Text such as `owner-approved`, role names and locally issued dispatch receipts do not prove independent authorization to change wave bindings. The repository has no trusted wave-reapproval boundary, so changed plans remain rejected pending separate trusted reconciliation; this CLI does not implement that approval operation. Repeating wave zero with an unchanged plan and no request is idempotent and preserves the original approval time/environment and existing valid evidence. Later waves require every expected slice in every previous wave to be done with no problems and all its records to be valid successful executions covering every agreed binding by exact argv, timeout, idle timeout and inherited-environment digest. Check names, verification descriptions and exit codes alone cannot satisfy a binding. Records within a wave must describe the same execution root, task ID, HEAD and fingerprint. Split-root receipts require the expected `--execution-root`; optional `--task-id` binds receipts to the task. A check that changed HEAD or the fingerprint during execution cannot certify a wave; run it again after changes settle.

The latest completed wave (`N - 1`) must match the current checkout. Older completed waves retain schema, project, success and log-integrity verification without requiring their historical fingerprint or HEAD to equal today's tree. Otherwise normal work on the next wave would invalidate every previous wave. A failure gate stops dependent work. The helper does not perform integration or determine whether the chosen commands cover acceptance; the parent must inspect the combined diff and semantic output.

Example completed-wave entry (replace the record path with actual `run-check` output):

```json
{"id": "foundation", "slices": [{"id": "schema", "done": true, "problems": []}], "checks": [".codex/checks/actual-record.json"]}
```

## Review gate

The report is an object with `state`, `expected_lenses` (a list of lens IDs), `lenses`, and boolean `approved`. Each lens has an `id`, `status` of `complete` or `failed`, and `findings`. Each finding has `severity` `P0`, `P1`, `P2`, or `P3`, `verification` `confirmed`, `rejected`, or `unverified`, and a non-empty `summary`.

`review-gate --project HOST [--execution-root TASK] --report REPORT` approves only when the exact expected lens set is present, every lens is complete, the report is structurally valid, `approved` is true, no confirmed or unverified P0/P1 finding remains, and `state` matches the execution checkout. Missing, malformed, failed, stale, or unresolved evidence fails closed. The report remains host-owned.

### Review state and report storage

`capture-state --project HOST [--execution-root TASK]` is read-only and prints one JSON object: integer `schema_version: 1`, absolute resolved execution-root `cwd`, `git_head` (string or null), and `worktree_fingerprint` with the same shape as execution evidence. It neither creates a directory nor writes a record. In split-root use, host remains the evidence/configuration root and the task execution tree is fingerprinted in full. Capture before review starts and embed the returned object unchanged in the host report's `state`. All lenses must cover that state. Stop concurrent writers during capture, checks and review; hashing is not an atomic filesystem snapshot.

Keep machine review reports and wave results under host checks state. In same-root mode, that configured directory is excluded from fingerprints; split-root operations fingerprint the complete task tree. Finish task-card and other documentation edits before capture/checks. Later changes to any included file, including documentation, or to HEAD invalidate review. Review the delta, record its coverage, and only then adopt a new state; simply recapturing state cannot refresh an old verdict. The gate verifies consistency of supplied evidence, not the quality or actual performance of a review.

### Compatibility

Both gates now require `--project`. Status-only review reports without `state`, and completed-wave results without `checks`, are rejected. Recheck the current tree rather than fabricating evidence for old reports. Old wave plans without ordered verification bindings and prior wave-zero approval fail closed. Newly generated execution records add `git_head_before`, `timeout_seconds`, `idle_timeout_seconds` and `environment_sha256`; completed-wave checks require this field. Existing version-1 execution records remain readable by `verify-check` without it. Re-run historical checks with the updated helper when migrating an unfinished wave plan that has no such records. `init` adds a missing `card.template.md` in the configured tasks directory while preserving existing templates and cards.

## Execution evidence

`init --language en|ru` selects the templates for newly created documents. The CLI defaults to English, preserves existing documents, and installs the same English agent roles in either mode.

`init` records the selected project-state paths in `.codex/devcrew.json`. Projects default to `docs/context.md`, `.codex/tasks/`, `.codex/sessions/`, `memory/`, and `.codex/checks/`; path flags may select repository-native alternatives. The discovery order, configuration example, and safety rules are documented in [project state layout](project-state-layout.md).

`run-check` executes the supplied argv directly with no shell and persists full output plus structured evidence: command, cwd, exit code, start and end timestamps, Git revision when available, before/after fingerprints, wall/idle timeouts, a SHA-256 digest of the actual inherited environment, and a log digest. The child receives the exact environment snapshot that was hashed; environment values are not persisted. The digest excludes only the exact `CODEX_TOOL_CALL_ID` key; its value still reaches the child and is never written to the receipt. Every other environment key and value remains part of the digest. It writes a unique log and atomically written JSON sidecar under the configured host checks directory; schema version 1 records status as `passed`, `failed`, `timeout`, or `error`. For split host/task execution, `--project HOST_PROJECT_ROOT --execution-root TASK_WORKTREE_ROOT --task-id TASK_ID` stores the receipt and log in host checks state while running the command and calculating its Git HEAD/fingerprints in the task worktree. Receipts include `host_project_root`, `execution_root`, `execution_head_sha`, and optional `task_id`; their existing `cwd` identifies the host receipt directory. Pass expected `--execution-root` and optional `--task-id` to `verify-check` and `reuse-check`; mismatches fail even if HEAD/fingerprint match. New and old schema-version-1 records without split-root fields continue to use `cwd` as their single root. `verify-check` rejects missing, failed, timed-out, inconsistent, stale, or nonzero records. It compares the current execution-tree fingerprint, including tracked and nonignored untracked files; configured host checks are excluded only when host and execution roots are the same. The record is local evidence, not a tamper-proof attestation against a process that can edit all files. A non-Git project reports that freshness uses the local fingerprint only.

`flaky-hunt` repeats the same real execution with `--max-runs` and `--timeout`, optionally bounded by finite positive `--max-seconds`. It stops on the first nonzero test exit (`caught`, exit 1), reports all-success at a limit as `not_caught` (exit 0), and reports timeout or executable errors as `environment_error` (exit 2). These outcomes describe observed runs and do not prove stability or acceptance.

## Limits and command results

Python 3.11+ is required. `run-check` defaults to a 300-second command timeout; positive finite `--timeout` overrides it. Timeouts terminate the spawned process group on POSIX or the owned Job Object on Windows. Logs merge stdout/stderr directly into a unique file, and contain unredacted command output. Avoid secrets in argv and use established project secret handling.

Exit codes: `init` returns 0 on success, 2 on invalid input or conflicting state; `doctor` returns 0 for valid state, 1 for missing/invalid state, 2 for an invalid project argument. `run-check` returns 0 for a passed command, 1 for a recorded failure/timeout/execution error, 2 for invalid input or an environment failure before a record can be written. Records preserve the actual subprocess exit code. `verify-check`, `wave-gate`, and `review-gate` return 0 only when their checks pass, otherwise nonzero. Argument parsing errors return 2. `flaky-hunt` returns 0 for not_caught, 1 for caught, 2 for an environment/input error.

Fingerprints include file contents and modes, tracked deletions and nonignored untracked files; checks output is excluded. They do not capture ignored files, external services, environment variables, or dependency state outside the project. Git submodules (including uninitialized gitlinks) and nonignored untracked nested Git repositories are explicitly rejected during both recording and verification; check each nested repository separately. Without Git, the helper fingerprints local files and reports the missing revision guarantee. Limits cover command execution; filesystem hashing and process cleanup add overhead.

Before/after fingerprint objects require `algorithm: "sha256"`, a 64-character lowercase hex `digest`, a nonnegative integer `files`, and a boolean `git`. The current fingerprint must equal the after fingerprint. Both timestamps require timezone offsets and finish must not precede start. Missing or malformed record fields are rejected as structured errors.


## Delegated scope check

`capture-state --project HOST [--execution-root TASK] --include-files` adds `files`, a sorted array of `[relative_path, kind, value]` entries used to compute its fingerprint (hashes and metadata, no file contents). Save it before the slice in host checks state. `scope-check --project HOST [--execution-root TASK] --before SNAPSHOT --files OWNED.json` reads this snapshot and a nonempty JSON array of owned relative paths; it returns `{ok, changed, outside_scope}`. Exit 0 means all observed changes belong to the explicit list; exit 1 reports outside-scope paths; malformed data, a wrong execution root, changed HEAD or missing file inventory returns a nonzero input error. Existing dirty files that the slice does not touch are preserved in the baseline. No files are reverted or written. Configured checks are excluded only in same-root mode; this is not enforcement of writes or external effects.

## Task checkout lifecycle

All implementation modes use the [task checkout policy](../skills/devcrew-task/references/checkout-policy.md). `task-checkout create --project PROJECT --task-id ID` defaults to the current directory. It registers an existing working branch at its current HEAD without switching; on the primary branch it automatically creates `codex/tasks/ID` at the current HEAD, choosing an unused numeric suffix on collision. It preserves staged, unstaged, untracked and ignored files, including a dirty index, without stashing, resetting, cleaning, or copying edits. Branch selection never requires a user question. An explicit base must match the current HEAD; detached HEAD and invalid lifecycle state fail closed. Commit, merge and push require the user's request. Its durable lifecycle and phase records live in the checkout's Git directory under `devcrew-checkouts/`. Existing task state returns `TASK_CHECKOUT_ALREADY_EXISTS`; use RESOLVE to resume. Delegation, AGENT EXECUTION, autopilot and coordination do not authorize worktrees.

Only an explicit user instruction permits `task-checkout create --worktree-request TEXT` or legacy `task-worktree create --worktree-request TEXT`. TEXT records that actual instruction, never a fabricated approval. An omitted base uses the same primary head; an explicit full `--base-sha SHA` must match that head. The deterministic branch is `codex/tasks/ID`; the repository-scoped worktree path is outside the host checkout. A dirty host is allowed only in this explicitly authorized mode and is never copied or modified. Existing linked checkouts return `TASK_WORKTREE_ALREADY_EXISTS`; invalid bases and branch/path collisions fail closed. There is no removal operation. Linked-mode lifecycle records remain in the sibling namespace and include the explicit request. Both modes bind repository, task, path, branch and expected HEAD to DevCrew provenance. Compatibility fields `TASK_WORKTREE_ROOT` and manifest `worktree` describe either assigned checkout; the execution root equals PROJECT_ROOT by default.

`task-checkout resolve --project PROJECT --task-id ID` (also available as `task-worktree resolve`) verifies the durable record and the registered path, branch, expected HEAD and shared repository before returning a JSON receipt. Legacy linked records without a saved request require the actual user authorization via `resolve --worktree-request TEXT` before use. It reports `CLEAN` or `DIRTY` based on tracked and untracked status and never modifies a present checkout. A missing checkout returns `UNSAFE_MISSING_WORKTREE` unless the record retains an explicit user worktree request and proves a clean snapshot, no writer dispatch since that snapshot, matching repository and branch HEAD, absent target, safe parent, and no attachment elsewhere; in that case it recreates the same linked worktree by attaching the existing task branch and returns `TASK_WORKTREE_RECREATED` only after validating that attachment and resolving it again. If Git retains the exact expected stale registration, RESOLVE removes only that path with plain `git worktree remove`, verifies unrelated registrations are unchanged, and then recreates it. A locked or mismatched expected registration fails closed without removal; global pruning is never used. Dispatch requires a fresh CREATE/RESOLVE/RECREATED receipt, verifies it against the durable record and current Git state, then records that a writer was dispatched. Without trusted writer-completion evidence, dispatch prevents missing-checkout recovery. A `DIRTY` receipt preserves all task changes and is not authorization to overwrite them. For AGENT EXECUTION, the host stores a versioned phase manifest in host orchestration state, linked to this task worktree identity and original base SHA. The planner declares semantic `SINGLE_PHASE` or `MULTI_PHASE` boundaries; even a single capability has a formal phase. `phase init --planner-output PATH --spawn-receipt-id ID` validates and creates the manifest only when absent; it cannot replace an existing manifest. Init requires a persisted routed planner receipt matching task ID and original base SHA. The manifest stores receipt ID, route ID, agent ID, model, effort and spawn mode; resume revalidates the receipt against the host dispatch registry. Owned scope paths must be normalized repository-relative paths, with aliases rejected. Before executor dispatch, the coordinator resolves a fresh receipt, validates and loads the manifest, and constructs the assignment exclusively from `CURRENT_PHASE`. The executor receives that phase's goal, main scenario, bounded scope, acceptance, verification and constraints, never later-phase write assignments. New plans preserve the agreed trigger/input, key steps and observable result as phase `main_scenario` (nonempty text when present). Schema-1 manifests that omit it remain valid without being rewritten; their `MAIN_SCENARIO` dispatch value is null and review must disclose the missing scenario evidence. Every phase of one task uses the same assigned checkout and saved mode. Missing/mismatched manifests fail closed; DIRECT does not require one. `READY_FOR_CLOSE` stops before review and is not `ACCEPTED`. Entering it revalidates current HEAD/fingerprint, unchanged execution state, approval time and complete coverage of the accepted bindings. It requires one or more repeatable host `--evidence-record` paths to run-check records and `--spawn-receipt-id ID`. The executor receipt must match task, phase, base SHA and writer cycle. Initial work uses `WRITER_CYCLE=0`; a repair uses current `active_local_fix.cycle_id` and `REJECTED_CANDIDATE_SHA`. The manifest stores that receipt identity and candidate-commit rechecks it. The CLI verifies each evidence record against the host project, task ID and task execution root, and checks the recorded log digest; it stores normalized host-relative references in the manifest.

Durable checkout receipt writes compare the original revision and complete JSON snapshot under a persistent sibling `.lock` file before atomic replacement. Every successful write increments the durable `receipt_revision`, including refreshes that preserve business fields, so ABA changes cannot authorize an old snapshot. Schema-1 receipts without this optional nonnegative integer are read as revision 0; read-only resolution leaves them unchanged and their first write adds revision 1. Creation expects an absent receipt; a stale revision or changed snapshot fails with `TASK_WORKTREE_STATE_CONFLICT` and requires a fresh resolve before retrying. Nonce generation/validation and the public receipt response shape remain unchanged. The candidate head transition reads the current receipt, advances the Git ref with compare-and-swap, validates Git state and saves the receipt under the same short lock. Verification commands and agent execution run outside it. When both locks are needed, the manifest lock precedes the receipt lock; neither receipt resolution nor dispatch acquires the manifest lock while holding the receipt lock. Receipt writes require supported local file locks and report `TASK_WORKTREE_LOCK_UNSUPPORTED` when unavailable; read-only resolution remains supported. Do not unlink receipt lock files.

Phase manifest updates use a persistent sibling `.lock` file and an exclusive OS lock to serialize revision comparison and atomic replacement. Each successful write increments the nonnegative `manifest_revision`; schema-1 manifests without it are read as revision 0 and acquire a revision on their first update. A stale snapshot fails with `PHASE_MANIFEST_CONFLICT`: reload the manifest and re-evaluate the operation before retrying. Init shares the same lock and still refuses an existing manifest. The lock covers only shared-state commits and their coupled Git/receipt mutations; agent work and verification commands run outside it. Do not unlink the lock file. Phase updates on filesystems without supported file locks fail with `PHASE_MANIFEST_LOCK_UNSUPPORTED`; read-only manifest loading remains available.

Every phase in `phase init` planner output must include `verification_bindings`: one object per ordered `verification` item, with integer `index`, exact `verification` text and nonempty string-list `argv`. Optional positive finite `timeout` defaults to 300 seconds; optional positive finite `idle_timeout` defaults to null. These are commands selected from project instructions and the accepted plan, never inferred from executor evidence. The host freezes normalized bindings, approval time and environment digest in the existing manifest. Changes after approval fail with `VERIFICATION_PLAN_CHANGED`; the existing explicit `replan-complete` operation replaces the current contract and preserves the rejected candidate's original bindings in history. Legacy manifests without bindings cannot certify closure/candidates. The internal pre-execution status transition also freezes bindings for preexisting planned manifests before starting work.

`phase pre-candidate-reapprove --project HOST --task-id ID --phase-id P1 --planner-output FILE --spawn-receipt-id ID` is the narrow recovery operation for the first current phase before any candidate or review history exists. It requires the unchanged original task branch and HEAD, a fresh routed planner receipt for the same task and base, a valid replacement contract, and an eligible `PLANNED`, `EXECUTING` or `VERIFYING` phase. It preserves task identity and the complete semantic contract by exact equality, including domain evidence keys that are distinct from host lifecycle evidence; preserves phase order/dependencies and all later phase contracts; pins the replacement scope, verification bindings, current environment digest and approval time; archives the prior contract and dispatch/evidence history; clears active evidence and executor routing; and advances `contract_generation`. Schema-1 manifests without that field are generation zero. The new planner receipt is the sole generation-crossing authorizer; executor, reviewer, and later planner routes must bind the current generation. Stale issued routes and saved role receipts fail validation. Reapproval history is checked against the exact saved planner receipt and consumed issuance, including task/base, generation, routed identity, and complete saved worktree binding. Historical checks read the local stores directly and never promote an archived receipt to current role authority. A missing, duplicated, corrupt, or mismatched receipt or issuance fails closed; archive self-digests alone do not establish authority. Read-only worktree resolution runs Git queries with optional locks disabled so status inspection cannot refresh and rewrite the index stat cache. The operation uses manifest then receipt then dispatch locking and atomically replaces only the manifest; it preserves Git HEAD, the raw index bytes and staged, unstaged and untracked files. It cannot repair an interrupted candidate, accept or rewrite review history, or reapprove wave contracts.

Candidate evidence retains the frozen verification contract; its digest is bound into the immutable commit by the `DevCrew-Verification` trailer. Review, resume and acceptance revalidate complete command coverage against that contract as well as the immutable Git identity. The full set of required checks is every ordered verification binding selected from project rules and the accepted plan through H4, for any stack (for example Go, PHPUnit, Jest or Python unittest). Candidate creation executes all bindings; acceptance requires fresh successful run-check records covering all of them on the exact candidate SHA and tree, supplied through repeatable `--evidence-record` arguments. The gate compares exact argv, timeouts, environment and approval time and verifies log integrity; it does not classify commands or parse framework-specific output. The inherited environment is compared conservatively except for the exact `CODEX_TOOL_CALL_ID` key, so changes to any other environment variable require restoring the agreed environment or explicit replanning. Host manifests, approvals and evidence are trusted local state; this is not a cryptographic attestation against a process able to rewrite all host files. Semantic adequacy of project-rule-derived commands, including full-suite coverage and a nonempty test run, remains the planner/reviewer's responsibility. Previously accepted phases whose single acceptance record does not cover their complete approved bindings fail closed when revalidated; automatic migration or recertification of those historical phases is not provided.

Each planned phase also carries a printable `commit_subject` in the form `type(TASK_ID): action`. After executor work reaches `READY_FOR_CLOSE`, `phase candidate-commit --project HOST --task-id ID --verification-bindings FILE` revalidates that phase's close-boundary evidence, requires the ordered binding file to equal the bindings already frozen in the accepted manifest, checks all staged, unstaged, untracked and deleted paths against `owned_scope`, and stages only those explicit paths. It computes the index tree before running required commands through task-root `run-check` records. Every receipt must match task ID, execution root, argv, phase base HEAD, unchanged fingerprints and candidate tree. The command creates one commit whose sole parent is the immutable phase base, advances the task branch with compare-and-swap, updates durable lifecycle state, and records commit SHA, tree and verification references as `COMMITTED`.

Candidate creation holds the manifest lock and then the receipt lock for its short publication step. It atomically saves a minimal `candidate_transition` containing phase ID, base and tree **before `commit-tree`**, then adds the exact returned SHA in a second durable revision **before `update-ref`**. These markers detect interruption; they are not replay instructions. Git still advances through compare-and-swap against the immutable base. The lifecycle receipt advances once, and the final atomic manifest write records `COMMITTED` and removes the current operation's marker. Required checks run outside these locks. Every manifest write retains revision CAS and every receipt write compares its full snapshot and revision, including ABA protection; atomic JSON replacement includes file and directory fsync.

Candidates freeze the ordered references and byte digests of their successful check records. The aggregate digest is bound into the candidate's `DevCrew-Evidence` trailer alongside `DevCrew-Verification`. Existing evidence/log integrity, complete approved-command coverage, H4/C1 parameters and local-fix/replan checks remain mandatory. Evidence is checked before Git advancement, after it, and before the final manifest write. Historical committed candidates retain their existing evidence validation behavior.

There is no candidate replay, recovery nonce, proof reconstruction or completion acknowledgement. Any pending marker, including malformed markers, and any retained legacy `recovery_transition` (even one previously acknowledged) stop related mutations with `CANDIDATE_RECONCILIATION_REQUIRED`. Phase commands, writer dispatch, checkout/receipt updates, manifest saves and task-bound `run-check --task-id` fail before their side effects. `run-check` also verifies an existing lifecycle checkout and agreement between receipt and the current phase head before launching a command or writing evidence. A mismatched Git/receipt/manifest stops safely. Existing explicit local-fix and systemic-replan transitions keep their established behavior; candidate interruption cannot enter either recovery path.

Preserve the task checkout, lifecycle receipt, phase manifest and host evidence/logs for manual reconciliation. Diagnostics identify the state paths, phase, base and saved SHA when available. Inspect actual JSON files and `git rev-parse HEAD`; when a SHA was saved, inspect exactly that object with `git cat-file -t SAVED_SHA` and `git show -s --format='%H %P %T %B' SAVED_SHA`. Compare branch/HEAD, receipt `expected_head`, phase base/candidate, staged tree and evidence. An interruption before the SHA save may leave an unidentified object; no SHA is inferred from history. Neither `phase resume` nor repeated `candidate-commit` changes or clears pending/legacy state or creates another candidate. Read-only inspection remains available and never upgrades revisions. There is no automated reconciliation, reset, proof deletion or migration of old recovery state.

A failed final replace/fsync may leave either pending state or a visible consistent `COMMITTED` snapshot. The caller receives an error in either case. If the final snapshot is visible and consistent, normal validation can inspect it, but `candidate-commit` cannot create another commit from `COMMITTED`. This protocol does not make Git and two JSON files one transaction, guarantee hardware durability beyond fsync, or protect against uncooperative external writers. Preserve actual files after uncertain I/O; do not retry candidate creation or manually discard recovery state to force progress.

`phase review-start --phase-id ID` validates the exact candidate commit, sole parent, tree, branch/HEAD, clean task worktree, and verification evidence. It sets `REVIEWING` and returns a reviewer contract bound to `TASK_ID`, `PHASE_ID`, `BASE_SHA` and `TARGET_SHA=CANDIDATE_SHA`, with `GOAL` and `MAIN_SCENARIO` from the same accepted phase contract as the executor, plus phase acceptance, verification, owned scope, and context. Reviewer issuance validates these fields against the persisted manifest and rejects substituted goals or scenarios. Reviewer checks all acceptance criteria, the main scenario, critical edge cases and whether tests demonstrate the behavior, distinguishing observations from assumptions. For ordinary phase acceptance, the reviewer examines the immutable Git snapshot `BASE_SHA..TARGET_SHA`. For the final schema-v2 task review, the fresh reviewer also reads every immutable original-intent source, independently derives task outcomes (including unmapped omissions), and inspects the exact final candidate tree, relevant accepted predecessor work, and existing host evidence bound to that tree; the phase diff alone is insufficient for task-level certification. `phase review-report --report HOST_REPORT.json` accepts exact identity fields plus `VERDICT` (`PASS` or `FAIL`) and `FINDINGS`; the authoritative final schema-v2 PASS also requires its exact `INTENT_REVIEW` extension described below. Reports must be host-local. PASS requires an empty blocking findings array. Ordinary and phase review reports also accept optional `ADVISORIES`, a list of objects with a nonempty `SUMMARY` and a nonempty `EVIDENCE` array of references. Advisories are preserved for the reviewed candidate and do not affect acceptance or recovery routing. Reports without this field remain valid. Each FAIL finding preserves `CLASSIFICATION`, `SUMMARY`, and nonempty `EVIDENCE`. `LOCAL_FIX` halts as `LOCAL_FIX_REQUIRED`, `SYSTEMIC_REPLAN` as `REPLAN_REQUIRED`, and `OWNER_DECISION_REQUIRED` as `BLOCKED`; no repair is automatic. The manifest stores the normalized host-relative report reference and byte digest. Resume and acceptance reread the file and fail closed if it is missing, changed, or no longer matches the candidate identity/verdict/findings. Candidate commit, worktree, branch and evidence are revalidated when recording the report and again at acceptance. `phase accept --phase-id ID` requires the saved PASS and revalidates the immutable target and evidence before setting `ACCEPTED` and `ACCEPTED_SHA`. `phase advance --phase-id NEXT` explicitly makes the next sequential phase current after all declared dependencies and the immediately preceding phase are accepted; its phase base is the immediately preceding accepted SHA. This preserves linear branch history even when a phase depends on an earlier phase. Before any later-phase resume, dispatch, writer routing, or advance, DevCrew rechecks the full accepted predecessor chain: each commit object, sole parent, tree and trailers, candidate evidence, and the host report bytes, digest, identity and PASS verdict. The task branch must still point at the expected accepted boundary and be clean before a later-phase writer is dispatched. `phase resume` returns `PHASE_COMMITTED_AWAITING_REVIEW`, `PHASE_REVIEW_REQUIRED`, `PHASE_REVIEW_ACCEPTANCE_READY`, or `PHASE_ACCEPTED` as appropriate, never dispatching an executor during review. Capacity rejection leaves the candidate at the same `REVIEWING` target for resume. An interrupted or inconsistent candidate fails closed as `PHASE_COMMIT_INCONSISTENT`.

Acceptance also requires the persisted formal `review-start` marker and a
matching routed reviewer spawn receipt. The receipt binds `ROUTE_ID`, resolved
model/effort, spawn mode, actual `AGENT_ID`, task/phase, and exact
`BASE_SHA`/`TARGET_SHA`; authoritative reviewers use `FORK_TURNS=none`. The
host report stores that receipt ID and agent ID. Direct, unrouted, inherited,
or stale-target reports remain advisory and cannot accept or advance a phase.

For `LOCAL_FIX`, resume requests the same phase's executor with only the saved findings, phase base, rejected candidate SHA, owned scope, acceptance, and verification. `phase local-fix-start` preserves the rejected candidate and report in cycle history, then resets the durable task checkout to the unchanged phase base. The fix returns through fresh successful full-suite verification and candidate creation; a separate reviewer receives the replacement candidate's exact new SHA. Old verification and review remain bound to the rejected SHA and cannot accept its replacement. Each further `LOCAL_FIX` starts another explicit cycle. `SYSTEMIC_REPLAN` and `OWNER_DECISION_REQUIRED` remain stop states.

Independent executor work exposes `executor_lifecycle: NEW_CONTEXT_REQUIRED` in
phase resume results, regardless of context telemetry. The executor
receipt store requires `FORK_TURNS=none` and rejects an actual agent identity
already recorded for a different phase in the same task, at both receipt
recording and verification. A same-phase repair can retain its executor only
through a linked continuation receipt recorded after observed
`FOLLOWUP_SUCCESS`; it must match the origin receipt, route and payload, current
writer cycle, rejected candidate SHA, phase, agent and active manifest repair
state. This receipt preserves history but does not authorize another phase.
Receipt helpers enforce recorded authority; they cannot intercept arbitrary
host followup calls or prove host observations that were not reported.


## Original intent and task completion

### Ordinary current-chat lifecycle

For a sufficient user message that the host can currently read, the front door
may use `ordinary` rather than the formal phase lifecycle. The host supplies a
bounded structured extraction containing requirement IDs/statements,
acceptance, session/turn identity and a SHA-256 digest of the message. Prompt
bytes are never persisted. This is host-trusted provenance, not a claim that a
coordinator's extraction independently proves the source. It also supplies an
exact `mcp__codex_app__read_thread` descriptor: host thread ID, turn ID,
`turnLimit`, `includeOutputs`, and `maxOutputCharsPerItem`, plus each separately
authorized `mcp__codex_app__read_page_reference` attachment. The fresh routed
reviewer must execute those host reads, verify full message bytes against the
digest, and return independently derived outcomes and disclosed omissions. A
summary/truncated result, inaccessible attachment, missing thread or unavailable
host tool is `ORDINARY_SOURCE_CONTEXT_REQUIRED`; a boolean source-read claim is
not accepted. The CLI records this semantic report but cannot mechanically
prove the host read.

`ordinary candidate` freezes the original base SHA, current Git HEAD,
worktree fingerprint, and verified unchanged check-record digests. It does
not stage, commit, invoke `commit-tree`, or mutate refs. The host records
observed fresh planner, executor and reviewer spawns against their canonical
model/capability-routed payloads. Each receipt binds the exact payload digest,
role, agent identity, `fork_turns="none"`, and a lifecycle binding containing
the source and requirement-matrix digests, base SHA, candidate identity and
state. An observed spawn is rejected when that issued binding is stale or the
payload changes. Every ordinary issuance also freezes the configured-memory
engineering-requirements registry/envelope digest; spawn recording, review,
acceptance and resume rebuild that canonical envelope and fail closed on a
missing, malformed or changed registry. The reviewer identity must differ from the executor. The
persisted matrix has its own digest, so removal or alteration is a concrete
context gap rather than a mutable substitute for the user request. A planner
receipt alone is insufficient: its requirement-complete technical plan
(steps, owned scope and verification) is persisted and accepted by exact digest
before executor dispatch; executor/reviewer payloads bind that same plan.
`ordinary accept` requires a routed independent PASS, coverage of every
original requirement ID, currently revalidated evidence and the same
fingerprint. Any change rejects the stale review. Resume revalidates candidate
evidence and the saved report digest, so deletion/replacement cannot keep an
accepted status. Resume returns a specific missing-context result if the
bounded requirements no longer exist. The ordinary path refuses
to start when this checkout contains a failed formal manifest; formal
schema-v1/v2 records retain their source-bind, committed-candidate and history
semantics.

The ordinary record also retains observed risk plus distinct decision,
implementation and review complexities and routes each role through
`devcrew_cost`; risk alone never upgrades decision complexity and the executor
remains subject to its policy ceiling. A recovery is an explicit `ordinary
continue` record, not a reset. It seals the failed or stale cycle in a chained,
digest-verified archive, including its plan, candidate, receipts, evidence and
review. The new cycle rejects every archived artifact. `LOCAL_FIX` and
`SYSTEMIC_REPLAN` need evidence, bounded next action, expected result and cost
rationale; the latter starts a fresh planner route. A stale candidate is
classified as local, systemic or foreign; foreign changes cannot be accepted or
silently reverted. An observed same-executor `FOLLOWUP_SUCCESS` has its own
payload-bound continuation receipt and is never represented as a new spawn.
Repeated failures require a different candidate/evidence signature before a
new continuation; this guard does not fabricate an owner decision.
Records created before routing assessments remain readable; `ordinary
attach-routing` records an explicit, append-only assessment attachment rather
than inferring a low-risk default or replacing legacy history.

An accepted plan's author is resolved by the exact `SPAWN_RECEIPT_ID` in its
report, including after archival; the latest planner receipt is never a proxy.
Repeated `LOCAL_FIX` cycles retain the original plan origin. Resume and
recovery validate the plan digest and decreasing archived ancestry. A
recoverable 0.18.1 intermediate-origin chain is followed back to the original
receipt only when every archived plan has the same digest; ambiguous or
contradictory history fails closed. Before `ordinary continue` archives a
cycle, it revalidates candidate identity, accepted-plan binding, check logs
against the captured candidate state, and any review's digest, coverage,
candidate/cycle binding, source read and reviewer independence. For stale
candidates, evidence is checked against the saved state rather than the edited
working tree. Legacy 0.18 records without a file baseline cannot be classified
as stale; no baseline is inferred. A legacy FAIL without `CLASSIFICATION` may
use the continuation's explicit action only with the exact cycle-zero legacy
shape (no report or wrapper cycle field and the pre-cycle candidate/evidence
shape). Its archived report and receipts remain unchanged. Legacy stale
recovery without a captured file baseline is rejected with a specific error.

If an independent FAIL reports source omissions, continuation must be
`SYSTEMIC_REPLAN`. The next planner receives an effective requirement matrix:
the immutable original rows plus stable generated IDs for exact omission
statements, with provenance to the archived cycle, review digest and omission
index. Repeated identical omission text is deduplicated. The source, original
matrix, acceptance statements and host descriptor are never rewritten. The
effective matrix and provenance go to planner, executor and reviewer, and the
plan and final review must cover every effective ID. PASS with omissions and
local retention of an incomplete plan are rejected. A previously saved
`LOCAL_FIX` report with omissions can be continued explicitly as
`SYSTEMIC_REPLAN`; its report and original classification stay immutable.
`OWNER_DECISION_REQUIRED` remains blocked. This restores a source
obligation discovered by independent review; it does not alter the host's
semantic trust boundary or authorize a business requirement change.

For schema-v2 AGENT EXECUTION tasks, bind a verified source **before planner
issuance**, using `phase source-bind --project HOST --task-id ID --source-spec FILE`.
Planner issuance fails with `INTENT_SOURCE_REQUIRED` before model selection or
route persistence when no verified source exists. Binding persists a private
`planning-source.json` selection beside the immutable source snapshots; the
planner payload pins the selected record as `INTENT_SOURCE`. Postflight rereads
the pinned source, not a later selection. Schema-v2 init, resume and replans require that exact pinned source record in
`original_intent.sources`; a confirmed contract cannot downgrade to schema v1.
Binding an unavailable source does not replace an already selected verified
source. A source binding is not plan approval or task acceptance.

`mode: file` retains exact attachment/ticket/document/owner-answer bytes with
an absolute path, expected SHA-256 and origin. It must not be used to relabel a
chat transcript or a coordinator draft as a user-provided document. `mode:
unavailable` records a limitation but cannot authorize a planner or complete
Intent Review. `mode: capture` now fails with `INTENT_FULL_MESSAGE_FORBIDDEN`;
new full prompt snapshots are disabled. Existing legacy snapshots remain
readable, without rewriting their provenance or granting new certification.

If an ordinary current-chat task has already been created but its host reader
cannot access the actual attachment, use the same task ID and original base for
a source-gap transition. First prepare and resolve its existing task checkout
with `task-checkout create --project HOST --task-id ID --base-sha ORDINARY_BASE`
followed by `task-checkout resolve`; the receipt must still identify the
ordinary base and same checkout. Then call
`phase source-bind --ordinary-source-gap FILE`, where the JSON contains exactly
`source_spec`, `authorization`, and `host_observation`. `source_spec` is the
existing `mode: file`, `source_type: attachment` source-binding object.
`authorization` records the prior human authorization reference and its scope.
`host_observation` records the `read_thread` thread/turn and ordinary message
digest, unsupported source format/reference, source-gap result, and empty
active-agent/followup lists. It also records a terminal status for every agent
ID in current and archived spawn/followup receipts; archived issued dispatches
are matched using their persisted lowercase `route_id` and uppercase receipt
`ROUTE_ID`; an archived issuance without a matching receipt fails closed.
Accepted terminal statuses are completed, failed,
cancelled or stopped. A legacy ordinary state without its original checkout
identity cannot be handed off automatically; restore/reconcile the original
checkout first.
The original branch must still match, except a task-checkout receipt that
records `created_from_branch` equal to the captured primary branch and the
identical original base. A branch-name prefix alone is not proof. New receipts
persist this optional provenance only when task-checkout actually branches
from primary; older receipts without it remain readable, but cannot authorize
the primary-branch exception. Ordinary init and formal manifest registration
share a stable task lock under the absolute Git common directory and recheck
ownership/source under that lock, preventing duplicate same-ID ownership across
host and linked checkouts.
The source bytes are checked by the existing binder and the ordinary lifecycle
is sealed under its per-task lock only when task/base/source/history and
quiescent-writer observations match. The sealed record contains digests and
bounded metadata, not message or transcript bytes. A linked private record
binds the same attachment snapshot for formal planner preflight and phase init.
Existing malformed/different handoff links are rejected before source
selection or sealing. If a failure occurs after sealing but before writing the
link, a retry can recover from the verified private snapshot using the exact
requested provenance, digest, size and authorization, even if the original
local attachment is no longer present. Identical retries are idempotent;
conflicts, changed history, an outstanding
route, a live/uncertain writer or missing exact source fail closed. After
sealing, ordinary dispatch and mutations are stale. Formal planning still
needs fresh planner, assessment, candidate and independent-review gates; the
source transfer confers no task acceptance. The handoff itself does not
change tracked files, Git HEAD, index or branch.

The trusted `UserPromptSubmit` hook stores **only** SHA-256, event identity,
project, timestamps, and an optional explicit contract confirmation. A prompt
digest is not a readable/verified source. Receipts use a separate v2 queue,
limited to 64 entries, 8 MiB total and 24 hours, with private directory/file
permissions (0700/0600). The hook never reads transcripts or saves prompt text.
It initializes the absolute `PLUGIN_DATA` path supplied by the runtime and
its missing parents; unset, relative, symlinked or unwritable locations
fail with diagnostics. No checkout/cache/temp-directory fallback is inferred.
`doctor` reports `intent_environment` separately: a shell without this hook-only
environment does not establish what a previously running hook received.

`mode: contract` is a distinct `source_type: intent_contract`, never an extracted
original prompt. It binds a structured JSON file with exactly `task_id`, `goal`,
`requirements`, `acceptance`, and `non_goals`. The latter three are arrays of
nonblank statements; requirements and acceptance must be nonempty. Show the
complete contract and exact file digest to the user first. The user must submit
this exact confirmation as a new message (without quotes or extra text):

```text
Confirm DevCrew Intent Contract TASK_ID SHA256
```

The hook records the task ID and contract digest only when the entire human
message matches that syntax, optionally followed by one LF or CRLF line ending.
It hashes the original bytes without trimming spaces or other text. The binder
can also recognize a legacy v2 receipt with a null confirmation field when its
whole-message digest proves this exact task and contract confirmation, with
only the same optional line ending. It preserves that receipt unchanged; the
same check applies to bound source rereads. Bind with:

```json
{"mode":"contract","path":"/absolute/intent-contract.json","expected_sha256":"<exact-file-sha256>","plugin_data":"<runtime-supplied-path>","confirmation_source_id":"<hook-event-source-id>"}
```

The binder checks schema, bytes, fresh hook receipt, task ID and project before
writing any snapshot. A coordinator boolean, generic yes, wrong task/digest,
expired receipt or unavailable hook cannot stand in for that confirmation. A
bound snapshot carries the confirmation metadata so restart/resume remains
verifiable after queue expiry or deletion of the external draft. Every reread
checks source identity, confirmation digest, schema, permissions and immutable
bytes. This follows the existing trusted-host/local-filesystem boundary, not a
cryptographic attestation against a malicious local writer.

A supported UI/API may make the human message available for an in-memory
comparison, but there is no assumed Python-to-desktop tool bridge. See
[bootstrap recovery](bootstrap-recovery.md) for interface evidence, environment
repair, and the bounded maintenance procedure. A confirmed contract defines
its own explicitly approved scope; it does not certify that it recovered all
obligations of an unavailable earlier message. Final independent Intent Review
still derives outcomes from the entire bound contract and verifies omissions,
evidence and confirmed amendments under the existing gates.

Executor and final-reviewer payloads retain the complete source records,
original and effective requirement matrices, decisions and evidence references.
Only the coordinator's planning-assessment prose/history is represented there
by its status and SHA-256 of the complete canonical state. Full assessment
bytes remain in the unchanged manifest and in coordinator context; validation,
source preflight and final Intent Review still require that full state. The
role-view digest cannot replace a planning assessment or certify acceptance.
Previously recorded spawn receipts remain immutable; a prepared, unconsumed
payload using the older inline view must be issued afresh before dispatch.

The coordinator must read the bound sources and submit its planning assessment
separately from planner JSON with `phase init --intent-assessment FILE`. The
same option is available on the existing `pre-candidate-reapprove` and
`replan-complete` operations; it accepts a bounded JSON object and does not
derive or repair the plan digest. The assessment records source reads or
limitations, stable requirement IDs, exact source fragments, obligation/fact/
technical-decision distinctions, phase and acceptance references, decision
identities, and existing evidence references. A complete matrix is not proof
that the source was fully understood; there is no mandatory extra planning
reviewer. Preserve obligations during technical replans. Only a verified
explicit decision that identifies affected requirements authorizes a business
change. DIRECT retains its lightweight procedure, and diagnosis-first work
may identify facts and later testable criteria without inventing a product
choice or requiring a full upfront specification.

The assessment file is an object with separate `assessment` and `decisions`
members; the host pins the digest from the exact current plan after its source
reads. For example, the top-level structure is:

```json
{"assessment":{"task_id":"TASK_ID","plan_sha256":"<current-intent-plan-digest>","previous_plan_sha256":null,"mode":"IMPLEMENTATION","result":"COMPLETE","source_reads":[{"source_id":"<source-id>","sha256":"<source-sha256>","findings":"<what the host read in the exact source>"}],"unavailable_sources":[],"source_outcomes":[{"source_fragments":[{"source_id":"<source-id>","start_byte":0,"end_byte":42,"sha256":"<fragment-sha256>"}],"expected_outcome":"<expected result>","requirement_ids":["R1"],"disposition":"COVERED","rationale":"<how the proposed phase addresses it>"}],"obligation_checks":[{"requirement_id":"R1","change":"PRESERVED","preserved_outcome":"<obligation>","safety_rationale":"<why it is retained>","acceptance_rationale":"<where it is tested>","decision_id":null}],"technical_change_rationale":"<evidence-backed explanation>"},"decisions":[]}
```

`INTENT_REVIEW` is nested in the existing final review report alongside its
normal task/phase/base/target identity, routed reviewer receipt, `VERDICT`, and
`FINDINGS`. Its fields are exact; this compact shape illustrates the binding
and reference rules (replace placeholders with actual snapshots and records):

```json
{"INTENT_REVIEW":{"plan_sha256":"<current-intent-plan-digest>","result":"COMPLETE","source_reads":[{"source_id":"<source-id>","sha256":"<source-sha256>","findings":"<independent reviewer source findings>"}],"unavailable_sources":[],"decision_ids":[],"source_outcomes":[{"source_fragments":[{"source_id":"<source-id>","start_byte":0,"end_byte":42,"sha256":"<fragment-sha256>"}],"expected_outcome":"<independently derived outcome>","requirement_ids":["R1"],"disposition":"COVERED","rationale":"<candidate and test evidence> ","evidence_records":[".codex/checks/<candidate-bound-record>.json"]}],"obligation_checks":[{"requirement_id":"R1","result":"VERIFIED","rationale":"<evidence-backed outcome check>","evidence_records":[".codex/checks/<candidate-bound-record>.json"]}],"evidence_sha256":{".codex/checks/<candidate-bound-record>.json":"<sha256-of-exact-record-bytes>"}}}
```

Every referenced evidence path must already be an allowed normalized task or
host evidence reference, and every referenced file digest is checked against
its exact bytes. A final `PASS` requires complete source accounting and
outcomes; empty arrays do not prove the task had no obligations.

For the final schema-v2 phase, the fresh routed reviewer independently reads
all exact source snapshots and confirmed decisions, derives expected outcomes
even when a source obligation is absent from the matrix, and inspects the exact
final candidate tree, relevant accepted predecessor work, and existing host
evidence. The existing report carries `INTENT_REVIEW` with `plan_sha256`,
`result`, `source_reads`, `unavailable_sources`, `decision_ids`,
`source_outcomes`, `obligation_checks`, and `evidence_sha256`. Outcomes identify
source fragments and are `COVERED`, `OMITTED`, or `UNCONFIRMED`; every active
explicit obligation has a `VERIFIED` or `UNCONFIRMED` check with evidence.
Evidence references must already exist and be bound to the final candidate
tree. A reviewer reports `COMPLETE` only when every source, outcome, obligation,
and evidence record supports the result; otherwise the normal review returns a
substantive FAIL. Runtime validates report identity, completeness and
freshness, but does not infer semantic completeness from a matrix or boolean.

The existing `phase accept` persists the task-acceptance reference only after
all required phases are accepted and the final routed review and evidence are
revalidated under the manifest lock. Resume reports `task_status: TASK_DONE`
only after revalidating the exact accepted report, source/decision identities,
candidate and evidence. `PHASE_ACCEPTED` alone remains incomplete; pending v2
intent and schema-v1 (`UNVERIFIED_LEGACY`) cannot be certified. An accepted
final phase with a later-discovered omission remains blocked by the existing
`PHASE_NOT_REVIEWABLE` boundary; do not invent an automatic reopen or recovery
operation.

## Progress, inactivity and shared resources

`run-check` emits a JSON `check_started` event to stderr with `pid`, `log` and `record` paths. Stdout remains the final record path followed by its summary. Optional positive finite `--idle-timeout` stops a command that produces no new log bytes for that interval; the default imposes only the wall timeout. Timeout records add `timeout_kind: wall|idle`. Ctrl+C produces an `interrupted` record and stops the owned process tree. POSIX also handles SIGTERM. A hard Windows process termination closes the wrapper’s Job Object and stops its children, but cannot produce a final record. `flaky-hunt` treats interruption as an environment error and stops.

`run-check --resource NAME --resource-dir DIR` requires both options and acquires a nonblocking local file lock before running. Busy resources return an error without starting the command. All participants must use the same dedicated local directory/name. Lock files remain permanently; do not unlink them. On POSIX the child inherits the locked descriptor. On Windows the child holds a duplicate of the share-deny-write resource handle, and the wrapper owns a kill-on-close Job Object for the entire command tree. Killing the wrapper terminates the job; a competing writer cannot take the resource while the direct child still holds its handle. Protected commands must not daemonize or close that descriptor intentionally. These locks coordinate cooperating processes, not remote hosts or unrelated clients.

`resource-status --resource-dir DIR --name NAME` is read-only, returns `{resource, busy, owner}`, and does not create a missing directory or file. `owner` is null when free; busy metadata may briefly be unavailable during acquisition. Metadata includes wrapper PID, host, project and start time; kernel lock ownership, not stale metadata, determines busy state. Existing non-DevCrew files and symlinks are not overwritten.

## Engineering requirements

The configured memory directory may contain one `engineering-requirements.json` registry. `requirements-record --project HOST --expected-registry-revision N --record JSON` accepts only an explicit structured durable assessment; it does not classify arbitrary text or execute saved commands. New canonical entries carry trigger/action/check text. Existing rules use a normalized repository-relative source path, exact fragment and fragment/file SHA-256; their text is not copied. Revisions use compare-and-swap, preserve compact prior digests and reasons, and replay is no-write. `requirements-context --project HOST --paths PATH... --role ROLE --task-class CLASS` applies explicit selectors; it reports selected entry/byte counts and rejects stale sources or size overflow. A missing registry is a no-write empty context.

Final dispatch issuance derives an `ENGINEERING_REQUIREMENTS` envelope after the current phase or review contract, and validation recomputes it from the current registry before route consumption. Planner routes with a nonempty registry require an explicit `requirements_scope`; executor, reviewer, replacement, and repair scopes come from the persisted current phase. A changed registry is loaded on the next dispatch and stale issued context fails closed. This delivery does not grant write authority or guarantee model compliance.

When the host cannot relay an issued formal executor package inline,
`executor-delivery --project HOST --task-id ID --route-id ROUTE` registers
file delivery for that unconsumed route. It revalidates current phase authority
and preserves the exact package in the private dispatch registry. Use the
returned `spawn` unchanged for a fresh executor context. Its message directs
the executor to verify the full package SHA-256 and read all text before
editing. The host reports the actual `spawn` and `spawn_payload_sha256` to
`capacity-report`. The resulting receipt binds both package and delivered
arguments. An inline report for a file-delivery route, an altered package, and
file delivery for a same-agent follow-up are rejected. File delivery requires
executor read access to the host dispatch registry. A spawn receipt records
observed dispatch, while full reading is an executor obligation.

When the host cannot relay an issued formal reviewer package inline,
`reviewer-delivery --project HOST --task-id ID --route-id ROUTE` registers file
delivery for that unconsumed reviewer route. It revalidates the current formal
review authority, including phase, base, exact candidate and generation, and
returns a fresh independent reviewer spawn with the same route, model, effort
and `fork_turns=none`. The reviewer verifies the complete package digest and
reads every segment, including `ORIGINAL_INTENT`, the candidate review
contract, acceptance and evidence, before reviewing. The host reports the
actual `spawn` and `spawn_payload_sha256` through the ordinary capacity
postflight. Its receipt binds both the original issued package and delivered
spawn arguments; altered packages, digests, spawns, roles, consumed routes and
same-agent reuse fail closed. Reviewer delivery requires read access to the
host dispatch registry and does not replace independent review or final Intent
Review.

`requirements-diagnose --project HOST --incident INCIDENT.json` reads the existing issued route and optional spawn receipt, then compares the delivered requirement IDs/revisions with current declared applicability and supplied check evidence. It is read-only. It reports observed delivery/check facts; semantic causes stay undetermined unless separately evidenced. Registry input is capped at 512 KiB, complete serialized envelopes (including role, task class, and scope metadata) at 32 KiB, and selected source files at 512 KiB. Overflow reports the measured UTF-8 size and limit, then stops issuance without truncating requirements. Ordinary planner dispatch declares `--requirements-scope FILE` with a bounded `{"paths":[...]}` object; the declaration is pinned for its cycle. Executor and reviewer requirements use only the accepted plan's `OWNED_SCOPE`, and each issued binding retains its scope and authority for stale checks. Legacy empty-registry tasks remain compatible; a consumed legacy dispatch with a nonempty registry and no trusted bounded scope is blocked with recovery guidance to preserve the task history and start a new ordinary task from the original message with an explicit scope. See [engineering requirements](../skills/devcrew-task/references/engineering-requirements.md) for coordinator, DIRECT, and reviewer obligations.

## Memory and estimates (0.5.0)

`memory-audit --project PATH [--max-files 50] [--max-bytes 512000]` reads the configured memory directory without modifying it. JSON contains sorted file paths, bytes and SHA-256, exact duplicate groups, skipped symlinks/special files and threshold warnings. Missing memory returns an empty inventory. Symlinked directory ancestors and invalid thresholds fail with exit 2. Warnings retain exit 0 and do not authorize deletion. Use a quiescent trusted directory; inventory is not an atomic filesystem snapshot.

`estimate-summary --records LEDGER.jsonl` validates measured observations using the [calibration contract](../skills/devcrew-session/references/estimate-calibration.md). JSON reports groups with counts, totals and total actual/estimate ratio, plus exclusions for changed scope/model. It rejects malformed rows, unsupported units, nonfinite/invalid numbers and repeated observation identities with exit 2. Empty input produces empty groups. Both commands are read-only, require no initialization, and add no external dependencies.

## Agent capacity postflight

DevCrew prepares spawn payloads; the host runtime actually calls `spawn_agent`
and must report its observed result because the Python helper cannot catch a
runtime exception. `capacity-report --project HOST --report REPORT.json`
accepts only explicit `SPAWN_SUCCESS` or `SPAWN_FAILED` reports. Only an
explicit failed call with `RUNTIME_ERROR` exactly `agent thread limit reached`
normalizes to `AGENT_CAPACITY_BLOCKED`; ambiguous outcomes and other failures
retain their classification. The host stores the active blocker atomically in
its configured sessions directory using a safe task-scoped filename. Its
receipt records role, task and roots, stage, resolved model/effort, actual
attempt number, exact error and resumability. The task root is null before the
planner creates it. Successful planner/executor/reviewer spawn reports clear
the active blocker and retain the normal success result.

The report includes `REQUESTED_ROLE` (`planner`, `executor`, or `reviewer`),
`TASK_ID`, both roots, `CURRENT_STAGE`, `ATTEMPT_NUMBER`, `HOST_INITIATED: true`,
and `PREPARED` with the exact `route` and `dispatch` returned by preflight.
Every runnable AGENT EXECUTION payload builder requires the host project, task
ID, and role-specific identity and durably issues the final route and canonical
payload before returning. Planner identity includes the original base SHA;
executor identity includes phase, base and writer cycle (plus rejected
candidate SHA for repair); reviewer identity includes exact phase, base and
candidate target. Reviewer builders also require the bounded formal
`review-start` contract and verification evidence references, and include the
contract, evidence and exact phase/base/target in the payload before issuance.
Decision/planner and reviewer replacement handoffs use the same identity
requirement. Reviewer builders return the complete prepared object so
postflight can report its route, dispatch and issued payload digest. Only the
private executor pipeline can create an unissued intermediate, and it issues
that payload after the final message mutation. A public builder without the
required identity fails before producing a runnable spawn payload.
Each prepared route includes a unique `ROUTE_ID`. A successful report also
includes actual `AGENT_ID`, `FORK_TURNS`, `SPAWN_MODEL`,
`SPAWN_REASONING_EFFORT`, and `SPAWN_MODE`; `capacity-report` persists the
spawn receipt matching route, model/effort, spawn mode, task and role. Planner
reports include original `BASE_SHA`. Executor reports include `PHASE_ID`,
`BASE_SHA`, `WRITER_CYCLE`, and `REJECTED_CANDIDATE_SHA` for repair cycles;
`TARGET_SHA` is not required before a candidate exists. Reviewer reports
include exact `PHASE_ID`, `BASE_SHA`, `TARGET_SHA`, and `FORK_TURNS=none`.
Direct or unreceipted spawns are routing deviations and cannot satisfy DevCrew
role gates. A prepared route is not applied until the successful spawn receipt
exists.
Attempt 1 is the first actual call. After a capacity block, a manually
initiated call is reported with the next attempt number. For a failure, include
the exact observed `RUNTIME_ERROR`; do not synthesize one for an ambiguous
outcome. `capacity-resume` returns a stable `BLOCKER_ID`; include it in the
follow-up report, which must match the saved task, role, stage, roots and exact
prepared route/dispatch. A noncapacity failure ends and clears that blocker.

`capacity-resume --project HOST --task-id ID --host-initiated` returns one
resume payload for only the blocked role and stage, preserving its prepared
route/dispatch and selecting the next attempt number. The coordinator
reconciles completed role results from existing task/session records before
using it, then makes at most one host spawn call and reports that outcome.
There is no automatic retry, role fallback, rerouting, cross-task agent reuse,
or capacity-driven task-worktree operation. Interrupting an agent does not
prove that a runtime slot has been released.

Every candidate/final commit requires a fresh successful full test suite for the current tree, including LOCAL_FIX replacements. Project rules and the accepted plan define that suite through the existing ordered verification bindings; candidate creation and acceptance require complete coverage of those bindings. Targeted checks may be used during development and are not classified by DevCrew.

## Evidence-backed correction and review reconsideration

Repeated FAIL alone is not an owner stop. `phase resume` returns
`PHASE_CONTINUATION_REQUIRED` after a corrected technical failure, with
`coordinator_required: true`, `owner_decision_required: false` and writer/planner
requirements false. The coordinator assesses actual progress and proportionate
cost. Existing history and all evidence/acceptance gates remain mandatory.

`phase local-fix-start` and `phase replan-complete` accept `--continuation FILE`
for autonomous continuation. The JSON has exact `task_id`, `phase_id`, `target_sha`,
`report_sha256`, nonempty `reason`, `next_action`, `expected_result`,
`cost_rationale`, and a nonempty `evidence_records` array. These are successful
host run-check records bound to the unchanged failed candidate SHA and tree.
The helper pins record digests in `evidence_sha256` and archives the assessment
with its cycle. Recovery and historical validation recheck those records. Missing,
stale, tampered or repeated evidence fails closed. Repeating the same tree,
commands and log results cannot become progress through timestamps or new prose.
The coordinator evaluates semantic truth and cost; the helper cannot prove them.

`phase review-reconsider --phase-id ID --continuation FILE` archives a mistaken
failed review and its candidate ref, preserving HEAD/tree, and returns the same
candidate to COMMITTED. Run formal review-start and a new routed independent
reviewer; no executor, automatic PASS or acceptance is authorized by the assessment.
A disproven owner-classified finding may be reconsidered through the same evidence
path; genuine owner decisions remain blocked for the actual owner.

`--owner-decision TEXT` remains for actual owner answers resolving requirements,
authorization or material added cost. It is not required for a proven technical
error. Do not fabricate an owner answer. Follow the shared
[loop guard](../skills/devcrew-task/references/loop-guard.md); stop no-progress
loops without automatically inventing a question for the user.

## Local QA sessions

The `qa` CLI group records local API observations separately from command
execution receipts. `qa run-api --project ROOT --scenario FILE` accepts a strict
version 1 profile and a bounded list of GET probes against literal loopback HTTP
addresses. Every scenario must include at least one expected 2xx response and
one expected non-2xx response. Each probe has a total deadline covering connect,
response headers, and body, plus a bounded response size; declared body lengths
must be complete. The adapter uses no proxy, authentication, query string, or
redirect following, and records only status
codes and expected-field match results. Unexpected observations yield `FAIL`;
connection failures yield `BLOCKED`. Only complete, integrity-checked coverage
can yield `PASS`.

Profile and scenario snapshots live below configured `tasks/qa/`; immutable
run results and evidence live below configured `checks/qa/`. The project index
is mutable discovery metadata; updates are locked and merged against its latest
contents. Each result and evidence document is separately digested, and saved
documents stay within their reader limits. The Markdown report is checked
against the verified result during listing and reporting. `qa list` validates
indexed runs and `qa report --run-id ID` validates
the selected run, then returns the JSON data and derived Markdown without
network access. Unknown schemas, changed snapshots, changed evidence, foreign
project roots, and incomplete coverage are rejected.

`qa web-start --scenario FILE` opens an agent-driven web attempt on a web
scenario version 1. The scenario names a literal loopback base URL, the saved
`environment_run_id` of a managed QA environment that must be `READY`, two to
four viewports including a desktop width of at least 1024 and a mobile-web width
of at most 500, and one to twelve bounded scenarios. Each scenario declares
human-readable steps and the check IDs that must be evidenced. An `e2e` scenario
also requires a readback profile: one loopback GET path with a single `{id}`
placeholder, a successful expected status, and expected fields that may contain
`{{RUN_TOKEN}}` and `{{VIEWPORT_ID}}`.
`web-start` writes immutable snapshots below configured `tasks/qa/`, creates a
run-specific label token, and prints the run ID, token, digest, viewports, and
scenario definitions. The agent drives an available host browser tool; the CLI
does not embed a browser engine or claim to see the page.

`qa web-record --run-id ID --evidence FILE` accepts the agent's bounded evidence:
per scenario and viewport, observations, expected/actual check values, and
before/after PNG paths. The CLI recomputes every check match, rejects missing
scenario or viewport coverage, requires both screenshots, verifies the PNG
signature and exact dimensions against the declared viewport, copies the images
into immutable evidence, and digests them. For an `e2e` scenario it performs a
fresh GET to the same loopback backend by the recorded object ID and compares the
run token and declared fields; only status codes and field match booleans are
stored, never the raw body. A record that is missing or mismatched is `FAIL` even
when the interface claimed success, which exposes optimistic UI without
persistence. An unreachable backend is `BLOCKED`.

Overall `PASS` requires full scenario-by-viewport coverage, all checks and e2e
readbacks passing, valid screenshots, and confirmed environment cleanup:
`web-record` reads the live backend, then stops the bound managed environment
itself and requires the saved state to reach `STOPPED` without a cleanup error.
Stopping the environment earlier would make the mandatory fresh readback
unreachable. Missing browser, viewport, or backend availability is recorded as
an explicit `BLOCKED` attempt, never as a pass. Web results and evidence live
below configured `checks/qa-web/runs/`; `qa web-list` and `qa web-report`
re-verify snapshots, digests, screenshots, status derivation, cleanup, and the
Markdown projection.

`qa web-find` is read-only discovery over the project's saved web runs. It
accepts one selector: `--run-id`, `--scenario`, or `--defect`; with no selector
it returns the newest run. Each candidate is fully verified before it is
returned, so a damaged screenshot, result, evidence document, or report is an
error rather than stale discovery data. Multiple matches set `ambiguous: true`
and return every candidate. Defect identities are derived from failed checks or
readbacks as `SCENARIO_ID:CHECK_ID` or `SCENARIO_ID:readback`. Finding, listing,
or opening a report performs file reads only and never starts a browser, sends
an API request, creates an environment, or writes a lock, index, or directory;
these operations also work on a read-only checkout. A run is indexed only after
its result, evidence, and report are published by atomic rename, so a reader
sees a complete record or an explicit "not fully published" error.

`qa web-replay-start --source-run-id ID --environment-run-id ENV
--base-url URL (--scenario ID | --defect ID)` verifies the source result,
evidence, snapshots, screenshots, and project identity, then opens a new attempt
bound to a new managed environment. A saved `READY` status is not accepted on
its own: the environment must belong to the current project, differ from the
source run's environment, have every service still matching its saved process
identity, and record a readiness address equal to `--base-url`. A dead process,
a stale record, or an address from another environment is rejected before any
new run is created, and `--isolated` does not bypass those checks. Every attempt,
including a plain `web-start`, is bound to its environment where the run
directory is created, so a second start of either kind against the same live
environment is rejected even though the first run is not indexed yet. It copies
the selected scenario
definition and viewports but rebinds the profile to the new environment and base
URL; the readback profile is retained only for an `e2e` selection. A fresh run
ID, token, scenario snapshot, and scenario digest are allocated. The new
state/evidence/result records `replay` provenance: source run ID, source result
and scenario digests, source status, the scenario/defect selection, and whether
the attempt was isolated. Historical status is never copied into the new
result. A completed source is required for a defect selection; an incomplete
source is refused unless `--isolated` is supplied, which records an isolated
attempt instead of pretending the previous effect is known. Existing source
reports, evidence, screenshots, and snapshots are not modified. `web-record` and
`web-report` verify the replay provenance and require fresh observations,
screenshots, and backend reads; a source `PASS` cannot make the new run pass.

`qa environment-start --profile FILE` accepts a version 1 profile containing
named resource claims, inherited environment-variable names, ordered argv
preparation commands, optional argv data-cleanup commands, an optional
project-relative `data_dir`, and dependent service argv commands with loopback
HTTP readiness checks. Commands run without a shell. Start records the run
intent before preparation or service launch, then claims the declared resources
and every readiness loopback address in the shared index, and snapshots status
under configured `checks/qa-environments/`; each service record keeps its
readiness address and loopback URL. The port claim prevents a second DevCrew run
from starting on the same address; it is not visible to a foreign process. The
declared `data_dir` must stay inside the project and outside the configured
checks root. It is created before preparation with an ownership marker naming
the run and project, and it is removed only while that marker still matches.

Commands receive only the variable names listed in `environment`, with values
read from the invoking CLI environment, plus the documented system names `HOME`,
`LANG`, `LC_ALL`, `LC_CTYPE`, `PATH`, `TMPDIR`, and `TZ`. The rest of the CLI
environment is not inherited. Variable values are never written to state,
reports, or command arguments.

Start and stop serialize one environment's transitions with a per-run state lock
and take the project resource-index lock only for short claim reads and writes.
A stop that meets an in-flight start waits for it within a bounded interval or
returns an explicit conflict without changing any state, so claims are never
released while that start can still launch a service. `environment-status
--run-id ID` reports saved state, whether each recorded process still matches
its saved identity, the preparation and cleanup outcomes, the data record, and
which resource and port claims the run still holds. Saved identity includes
process start time and process group, not only a PID and executable path.

Before READY, the expected status must come from the recorded loopback address
requested directly, without an HTTP proxy and without following redirects, and
the original response status is compared, including an expected 4xx or 5xx.
Readiness also requires the listening socket on that port to belong to the
verified process group of this run, the same boundary stop uses to terminate it.
A listener in another process group of the same session is foreign, because stop
would leave it running. The listener is attributed before
the request is sent and attributed again after the response arrives, so a
foreign server that answers and then releases the port cannot have its response
accepted once the owned service binds the address. Listener attribution uses
`/proc` on Linux or `lsof` elsewhere; when neither can tie the listener to the
owned process, start refuses explicitly instead of treating a live PID, a free
port, or an HTTP response as ownership. A changed or disappeared identity never
records READY. A service already recorded as ready is re-checked while later
services start, and immediately before the environment is published as ready it
must still be the verified live process and answer a fresh readiness request;
otherwise start fails instead of reporting `READY`. After those probes the live
identity of every ready service is checked again in reverse order, so a service
that exits while a later one is probed is still detected.

Preparation and declared data cleanup use the same ownership record as services:
written intent, confirmed process identity, and exit status. A command is only
complete when its own process group is empty. If its leader exits while a
descendant remains, the group is terminated when it can still be tied to the
confirmed leader; otherwise the outcome stays `EFFECT_UNKNOWN`, the claim is
held, and nothing is reported as complete. An unreadable current identity is
never signalled, even when the PID is still visible. If the orchestrator dies
between the side effect and the result write, the outcome also stays
`EFFECT_UNKNOWN`; the claim is held and a later stop does not repeat that
command. Declared cleanup runs only after confirmed data ownership, preparation,
or a service launch could have produced a side effect; a start that is refused
earlier runs no profile cleanup, and a command that failed before its process was
spawned owns nothing to clean. The profile's cleanup commands run in their
declared order, each keeping its index for recovery. Before every new profile cleanup command
starts, the recorded data path is revalidated and must still be the directory
carrying this run's marker; a replaced directory or symlinked parent stops the
commands from running at all. The check is repeated on each stop and for each
step, including after an earlier refusal left the data record
`EFFECT_UNKNOWN`. A failed or unresolved declared cleanup is reconciled on every
stop, so a later successful data removal cannot turn it into `COMPLETE`. A failed
declared cleanup keeps its exit status and reason. Prepared file data
is removed only after an atomic rename of its marked directory, and a directory
that cannot be confirmed as owned is never deleted. The pending-removal stage and
the renamed path are recorded and retried on every stop, so a missing original
path is not reported as removed while the renamed directory still holds the data.

Stop terminates the owned process group so that service children end with their
parent, then verifies that the group is empty. Unverifiable or stale identities
are left untouched. A saved start intent without a confirmed identity stays
`EFFECT_UNKNOWN`: stop reports `CLEANUP_FAILED` with `cleanup: INCOMPLETE`,
keeps the claims, and repeated stop neither relaunches the service nor declares
it stopped. If a leader exits before its identity was confirmed, its recorded
group is not signalled; the run stays unresolved until the group is proven empty
or safely cleaned. Descendants that leave the owned process group cannot be
confirmed and remain outside this boundary. A readiness port already in use is a
conflict, never proof that DevCrew owns its listener.

The data record separates creation intent from confirmed ownership: the path is
recorded before any directory is created, but ownership is confirmed only after
the marker is written. A stop that finds an intent without confirmed ownership
neither deletes the directory nor runs the profile's cleanup commands.

`STOPPED` and `cleanup: COMPLETE` are published only after owned processes,
declared cleanup, and marked data are confirmed released and every claim of the
run is gone. Stop revalidates the recorded data path against the current project
before touching it: the directory must still be inside the project, outside the
checks root, and free of symlinked components; otherwise nothing is removed and
the cleanup stays incomplete. A repeated stop revalidates the index even for a saved `STOPPED`
record, so a crash between the state write and the claim release is either
finished safely or reported as unfinished cleanup. Unsupported schema, run ID
mismatch, foreign project root, or missing ownership fields are refused before
the first signal or write; a record without later optional preparation, data, or
port fields remains readable while its confirmed process identity still matches.
A service record marked `READY` or `CLEANUP_FAILED` must carry its PID and the
complete recorded identity (start time, executable, process group, and session),
each with the type the recorder writes; a missing, empty, or wrongly typed field
is refused before any state write, data removal, or signal. A saved identity on
an interrupted start is validated by the same rule. When the leader has exited
and the process group cannot be inspected, stop retains its claims and leaves
the group untouched.
Claims are coordinated under a local file lock, and a confirmed cleanup
releases them for the next start.
