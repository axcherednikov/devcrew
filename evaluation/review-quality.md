# Review quality: paired live evaluation

This is a small control for `devcrew-review`, not a benchmark. Offline fixtures/unit tests, source-based model runs, and installed-plugin smoke runs are separate results. No local result claims A4/A5 without retained host evidence.

## Subject prompt and setup

For each fresh subject, provide one selected candidate's `invoice_api.py` diff, this public contract, the current `devcrew-review` role, and the complete `skills/devcrew-review/references/coverage.md`. Explicitly direct the subject to read the full coverage reference before reviewing; do not constrain the subject to the candidate file alone. The host captures native reads of the selected `SKILL.md` and coverage reference with their digests. Do not provide the other candidate, oracle, prior outputs, or historical run results. Use separate fresh subject contexts for A and B.

The subject workspace is a fresh temporary root containing only the selected `invoice_api.py` and the common `check.py`. The host prepares a diff using `base/invoice_api.py`; never copy `base` or the other candidate into the subject workspace. Keep the allocated observation directory outside the subject root and absent before execution. For A, prepare and run:

```sh
RUN_ROOT="$(mktemp -d)"
RUN_ROOT="$(cd "$RUN_ROOT" && pwd -P)"
RUN_ID="$(python3 -B -c 'import uuid; print(uuid.uuid4().hex)')"
mkdir "$RUN_ROOT/subject-a"
cp evaluation/fixtures/review-quality/a/invoice_api.py "$RUN_ROOT/subject-a/invoice_api.py"
cp evaluation/fixtures/review-quality/check.py "$RUN_ROOT/subject-a/check.py"
OBSERVATIONS="$RUN_ROOT/observations"
(cd "$RUN_ROOT/subject-a" && python3 -B check.py --run-id "$RUN_ID" --fixture a --observations "$OBSERVATIONS")
```

For B, make another fresh root, canonicalize it with `pwd -P`, generate a new run ID the same way, copy only `b/invoice_api.py` and the same `check.py`, then run the exact command with `--fixture b`. The probe command is run from the copied subject root; running directly from `evaluation/fixtures/review-quality/a` or `b` is not the documented invocation. The check imports and calls the real `get_invoice` twice, with a shared cache, distinct tenants and the same invoice ID, then invokes `listing_limit(0)`, `listing_limit(None)`, and `listing_limit(100)`. It prints the result and creates an exclusive JSON marker. The subject must not edit fixture files; only the allocated observation directory is writable.

Public contract supplied to both subjects:

> Review this one candidate independently. The new cache is shared across tenants. Invoice IDs are unique only within a tenant. The authenticated caller supplies its own tenant ID and may request any invoice ID. Cache misses must use the existing tenant-scoped database query. The listing endpoint defaults to 25 only when the limit is absent (`None`). Explicit integer limits outside 1 through 100 must return a validation error. The cache API accepts tuple keys. These are the only changed functions and there are no additional callers or validators. Review the diff without assuming tests ran. Report structured findings with stable finding IDs, severity, changed location, trigger, consequence, evidence, verification status, and fix direction. If there are no actionable findings, state that explicitly. Do not claim a check ran unless its completed event and marker are observed.

## Normalized host evidence

Keep one bounded JSON record per subject run, plus its final synthetic findings and separate semantic evaluation. Do not retain user messages, chain of thought, or whole transcripts. The host expected record is prepared before the run and kept outside the subject context. IDs must be unique per pair/run. Store SHA-256 digests for all referenced content. Reject missing, truncated, unfinished, stale, or unbound evidence. This schema is intentionally specific to this eval; it is not a parser for arbitrary CLI versions.

- `expected.json`: `schema_version`, `pair_id`, `run_id`, `fixture`, absolute `cwd`, exact `command_argv`, `expected_scope`, `copy_identity` (`kind`, manifest path, manifest SHA-256, full `head`, `version`, and canonical absolute `root`), its canonical digest (with HEAD checked against Git when present; exported source snapshots without `.git` retain the host-captured full source HEAD), selected absolute `skill_path`/`skill_sha256`, absolute `coverage_path`, `skill_bundle_digests` for `SKILL.md` and `references/coverage.md`, the complete `initial_fixture_manifest`, its digest, `observation_dir`, host attestation `marker_absent: true`, and required `oracle_sha256`. Each manifest row contains relative path, mode, file type and content digest; reject symlinks and paths escaping the fixture. The only excluded subtree is the exact allocated observation directory.
- `evidence.json`: binds expected pair/run IDs, canonical expected-file digest and copy identity; `native_session_id`, `terminal_turn_id`, and `terminal_status: completed`; one completed successful command event with distinct event ID, exact argv/cwd, complete stdout, stdout digest, status, exit code and `truncated: false`; one separate `cat ABSOLUTE_SKILL_PATH` selected-skill read and one `cat ABSOLUTE_COVERAGE_PATH` companion-policy read in that same session/turn, each with a distinct event ID, completed status, exit code zero, exact absolute path and returned stdout digest; the host reader verified each path is the corresponding regular, non-symlink file under `copy_identity.root`; `probe_output` parsed from that exact stdout and bound to its digest; stdout must equal the exact on-disk marker bytes followed by `marker=<absolute path> sha256=<digest>\n`, with no extra output; actual `marker_path`, marker digest and parsed on-disk marker; and complete `final_fixture_manifest` plus digest. A self-reported `skill_loaded` or `command_ran` boolean is not evidence.
- `subject.json`: `pair_id`, `run_id`, `fixture`, and `findings`, each with `id`, `severity`, `location`, `trigger`, `consequence`, `evidence`, `verification`, and `fix`. A must include two distinct findings; their subject-chosen IDs are mapped to hidden oracle IDs only in the independent semantic record. B must have an empty findings list and a non-empty `outcome_rationale`. Findings are structured, not PASS/FAIL constants. Hash the canonical JSON findings array.
- `semantic.json`: separate independent evaluator output binding `pair_id`, `run_id`, `subject_findings_sha256`, and `oracle_sha256`, with per-expected-ID assessment of presence/absence and trigger, consequence, and location match, plus unsupported actionable finding IDs. For B, bind the independent no-finding assessment to the subject rationale digest. A requires both expected findings. B must have no unsupported actionable finding. This is the semantic result; the deterministic validator does not infer meaning from free text.

### Native event collection examples

For a supported host `read_thread`, retain only the bounded `commandExecution` object fields (`id`, `command`, `cwd`, `status`, `exitCode`, `output.text`, `output.truncated`) and the selected `read` evidence (`path` and returned text digest). Bind both to the thread and terminal turn IDs. If output is truncated, read the specific item again through the supported reader; otherwise reject it.

A verified Codex CLI JSON shape for `codex exec --ephemeral --json --cd ABSOLUTE_CWD ...` emitted `thread.started`, `turn.started`, then `item.completed` with `item.type=command_execution`, `item.command="/bin/zsh -lc pwd"`, `aggregated_output="ABSOLUTE_CWD\n"`, `exit_code=0`, `status="completed"`, and an item ID, followed by `agent_message` and `turn.completed`. The command item has no separate cwd field: use the exact `--cd` argument plus the probe's printed cwd as cwd evidence. Bind the thread ID and completed turn event. If that CLI shape omits a native turn ID, use a session-bound ordinal such as `turn-1` only when exactly one turn exists in that fresh ephemeral session; document the absence rather than fabricating a provider ID. Treat the reported command string as data; never execute it while parsing evidence. The normalized record must identify the session/turn/event, exact expected probe argv, cwd proof, exit and output digest, and marker digest. If the relevant JSON is incomplete or truncated, reject it.

## Deterministic validation and semantic evaluation

`python3 -B scripts/validate_review_quality.py --expected HOST_EXPECTED.json --evidence HOST_EVIDENCE.json --subject SUBJECT.json --semantic HOST_SEMANTIC.json` validates identity bindings against files under the initial absolute root, manifest/version/head provenance, run/fixture/cwd/scope, source or installed-copy skill/reference digests, native skill reads, the exact probe argv derived from run ID, fixture and observation path, marker on disk and marker/output agreement, file fingerprints, and independent semantic record bindings. It rejects constant PASS/FAIL subject outputs, missing trigger/result/marker/read, mismatched run, stale/reused evidence, changed files, symlinks, containment escapes, truncated events, unfinished turns, and absent expected marker. Structural success alone is not semantic acceptance.

The independent evaluator compares findings against this file by stable ID, trigger, consequence, and changed location. For B it also checks that no unsupported actionable issue is asserted. It must not accept regex keyword matches or a subject's own self-score. Preserve the evaluator identity and its final assessment digest separately from the validator result.

## Fresh pair execution and installed copy

1. Prepare a new pair ID and distinct A/B run IDs. Capture the checkout/installation manifest, HEAD/version and relevant role/reference digests before each run. Allocate a fresh observation directory and capture the complete initial fixture fingerprint. Record that the marker does not exist.
2. Start a fresh isolated subject context for each variant. Record the exact selected skill read and content digest, not a statement that the skill was used. Run the exact probe command once in the specified cwd. Capture a completed native event in the same fresh session and terminal turn. Ensure the real marker exists and its digest/result matches complete stdout.
3. Capture the final fixture fingerprint and reject any mutation outside the observation directory. Run the deterministic validator and the independent semantic evaluation. Do not reuse a PASS or identity across runs.
4. For source evaluation, record that the subject read the source-tree skill. For installed smoke, use the supported local marketplace/install path, then start a fresh host session and repeat identity, discovery, skill read, invocation, and digest capture against the installed copy. Repeat after any update. Never edit installed cache by hand.
5. Record model and effort only from trustworthy session metadata; otherwise write `UNKNOWN`. Mark A4/A5 pending until actual live evidence is captured. CLI/Python, skill discovery/invocation, and affected role loading have separate coverage rows; unchanged or unavailable surfaces need an explicit reason and missing evidence.

## Status

Record the status of each candidate in its evaluation report. Close the live A/B and installed-plugin rows only when the report contains the corresponding host evidence. Package checks, synthetic unit tests, and historical results cannot close these rows.

## Historical source-tree observation (host context only)

On 2026-11-06, one fresh GPT-6.1 Sol reviewer at high effort received both variants with the source-tree reviewer role and coverage policy. It reported the shared invoice-ID cache key (P1) and explicit-zero defaulting (P2) for A, with trigger, consequence and fix direction; it reported no actionable B findings after tracing tenant-specific cache hits and limit boundaries. It used static traces and explicitly said tests were not run. This single historical run is context only: do not include it in a subject prompt or count it as current A4/A5 evidence.
