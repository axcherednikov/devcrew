---
name: devcrew-qa
description: Run bounded local QA sessions against isolated local web and HTTP API surfaces, then inspect saved evidence.
---

# DevCrew QA

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Use this workflow when the user asks to run local product QA. Managed lifecycle
commands own only processes they started and can still identify. Never stop a
service merely because it answers a readiness URL. For a bounded run or replay
checklist, read [exploratory run](references/exploratory-run.md).

## Managed local environment

An environment profile is version 1 JSON with `id`, `resources`, `environment`,
`prepare`, and `services`, plus optional `cleanup` and `data_dir`. `prepare` and
`cleanup` are ordered lists of argv arrays. Each service has an `id`, argv
`command`, `depends_on` service IDs, and a `readiness` object with a literal
loopback `url`, expected HTTP `status`, and bounded `timeout_seconds`.
Executables must be absolute paths. `environment` contains only names of
inherited variables; put no secret values in the profile. Commands receive those
names plus only the documented system variables (`HOME`, `LANG`, `LC_ALL`,
`LC_CTYPE`, `PATH`, `TMPDIR`, `TZ`), never the whole CLI environment. `data_dir`
is a project-relative directory outside the checks root for test data; start
creates it with an ownership marker and stop removes it only while that marker
matches. Commands are executed directly without a shell. Keep profiles and
fixtures local, use synthetic data, and do not point them at remote or
production services.

```sh
python3 scripts/devcrew.py qa environment-start --project . --profile /path/to/environment.json
python3 scripts/devcrew.py qa environment-status --project . --run-id RUN_ID
python3 scripts/devcrew.py qa environment-stop --project . --run-id RUN_ID
```

Start prints a run ID only after all services pass readiness. Readiness requests
the recorded loopback address directly, without a proxy or redirect, compares
the original status (including an expected 4xx/5xx), and additionally requires
the listener to belong to the verified process group of this run, the same
boundary stop terminates; a listener in another group of the same session is
foreign and blocks readiness. The
listener is attributed both before the request and after the response, so an
answer from a foreign server that releases the port is not accepted once the
owned service binds it. If the platform cannot tie the listener to the owned
process, start refuses explicitly. A service already recorded as ready is
re-checked while later services start and, with a fresh readiness request,
immediately before `READY` is published; after those probes every ready service
is checked again in reverse order, so one that exits while a later service is
probed is still caught. If it exited or stopped answering, start fails. On
failure, inspect the reported run ID with `environment-status`;
cleanup failures remain visible and keep resource claims. Stop verifies process
start time and process group beyond PID before signaling, terminates the owned
process group so service children end too, and then verifies the group is empty.
A stale or unverifiable process is left alone and reported as cleanup failure,
never as stopped.

Preparation and declared data cleanup record intent, confirmed identity, and
result like services, and a result only stays settled while the command's
process group is empty. A leader that exits while a descendant remains is
terminated when the group can still be tied to the confirmed leader; otherwise
the outcome becomes `EFFECT_UNKNOWN`. An interrupted start that left an unknown
outcome keeps its claims: stop reports `CLEANUP_FAILED` with `cleanup:
INCOMPLETE` and `EFFECT_UNKNOWN`, and repeated stop neither repeats the command
nor declares it stopped, so resolve it manually before starting that profile
again. If a leader exits before its identity is confirmed, its group is not
signalled while surviving members exist; an unreadable current identity is never
signalled either. Declared cleanup runs only after confirmed data ownership,
preparation, or a service launch could have produced a side effect, so a start
refused earlier leaves those commands unstarted, and a command that failed
before its process was spawned owns nothing to clean. The profile's cleanup
commands run in their declared order. Immediately before every new
cleanup command starts, on each stop, the data path is revalidated and must
still carry this run's marker; a replaced directory or symlinked parent stops
them, even after an earlier refusal left the data record `EFFECT_UNKNOWN`. A
cleanup step that already started is checked on every stop, so a saved failure
or `EFFECT_UNKNOWN` keeps
its claims held even after the data directory is gone. Resource and readiness-port conflicts fail before
service launch; the port claim keeps a second DevCrew run away but is not visible
to a foreign process. Startup and cleanup state is retained under configured
`checks/qa-environments/` so an interrupted operation is not silently replayed.
Start records the data-directory intent before creating it and confirms
ownership only after writing the marker; a stop that finds an unconfirmed intent
neither deletes the directory nor runs cleanup commands. A confirmed cleanup,
including marked data removal, releases every claim. Stop
revalidates the recorded data path against the current project before removal
and leaves it in place when it is outside the project, under the checks root, or
behind a symlinked component. After the rename, the pending-removal stage and
path stay recorded until the directory is gone, so a repeated stop finishes the
removal instead of reporting it complete. A service record marked `READY` or
`CLEANUP_FAILED` must carry its PID and full identity with the recorded types; a
missing, empty, or wrongly typed field is refused before any state change or
signal.

## Local API session

Create a version 1 JSON scenario with one literal loopback HTTP profile and
bounded GET probes. The profile accepts `base_url` and optional
`timeout_seconds` (0.1–10 seconds). Each probe has a unique lowercase `id`, a
path without query parameters, an `expected_status`, and `expect_json` scalar
fields to compare. Do not put credentials, tokens, or personal data in the
scenario.

Run and inspect sessions with:

```sh
python3 scripts/devcrew.py qa run-api --project . --scenario /path/to/scenario.json
python3 scripts/devcrew.py qa list --project .
python3 scripts/devcrew.py qa report --project . --run-id RUN_ID
```

`run-api` sends real HTTP GET requests only to a literal loopback address. It
does not use proxies, follow redirects, send auth, or accept query strings.
Unexpected status or field values produce `FAIL`; connection failures produce
`BLOCKED`. A passing run requires complete probe coverage. Treat only verified
saved evidence as a report; `list` and `report` are read-only and make no HTTP
requests.

Results and digests are stored under the configured `checks/qa/` directory;
immutable profile and scenario snapshots are stored under configured
`tasks/qa/`. JSON is the data source and Markdown is its readable projection.
The CLI deliberately stores field match booleans and status codes, not raw
response bodies, headers, exception text, or arbitrary returned values.

Do not describe source tests as browser or installed-plugin evidence.

## Local web QA

Web QA is agent-driven: the CLI stores and verifies state and evidence; it does
not embed a browser engine or pretend to see the page. Use the host browser tool
available in the chat, and record an explicit blocked attempt when that tool, a
required mobile viewport, or the backend is unavailable.

1. Start the managed environment and leave it `READY`.
2. Write a version 1 web scenario with `profile`, `viewports`, and `scenarios`.
   The profile has a literal loopback `base_url` and the saved
   `environment_run_id`; an `e2e` scenario also needs `readback` with one
   `/path/{id}` GET, a 2xx `expected_status`, and `expect_json` fields that may
   contain `{{RUN_TOKEN}}` and `{{VIEWPORT_ID}}`. Declare two to four viewports
   including desktop (>=1024 px) and mobile-web (<=500 px), and one to twelve
   scenarios with human steps and check IDs.
3. Open the attempt:

```sh
python3 scripts/devcrew.py qa web-start --project . --scenario /path/to/web.json
```

   Keep the printed run ID, run token, scenario digest, viewports, and scenario
   definitions. The token is the unique label for this attempt.
4. In the browser, for every scenario and viewport, set the exact viewport size,
   take a before screenshot, perform the bounded flow, take an after screenshot,
   and read the visible result and object ID. Actually inspect the images before
   claiming a defect. Use the token as the record label, and note concrete
   observations such as clipped text, overlapping controls, missing error
   feedback, or a success message without a saved record.
5. Leave the environment `READY` and write the evidence manifest, then record it:

```sh
python3 scripts/devcrew.py qa web-record --project . --run-id RUN_ID --evidence /path/to/evidence.json
python3 scripts/devcrew.py qa web-report --project . --run-id RUN_ID
```

   Evidence contains, per scenario and viewport, `observations`, `checks` with
   `expected` and `actual`, both screenshot paths, and for `e2e` the `ui_saved`
   boolean and `object_id`. The CLI recomputes matches, verifies PNG dimensions,
   and performs a fresh backend GET by ID against the run token. Do not substitute
   browser state for that read. Recording reads the live backend first, then stops
   the bound managed environment and reports confirmed cleanup; do not stop the
   environment before recording.

An `e2e` scenario passes only when the UI confirmed the action and the fresh
backend read matches the token and declared fields. A success message with no
saved record is `FAIL`, not `PASS`. Missing screenshots or blocked capabilities
are visible as `BLOCKED`/`FAIL`; overall `PASS` also requires complete coverage
and confirmed environment cleanup. Screenshots, observations, and backend
readbacks are stored under the configured `checks/qa-web/`; reports and lists
re-verify the saved artifacts.

## Find and replay a saved run

Finding a run and opening its report are read-only. They must not start a
browser, send API requests, or create an environment, and they write no lock or
directory, so they work on a read-only checkout.

```sh
python3 scripts/devcrew.py qa web-find --project .
python3 scripts/devcrew.py qa web-find --project . --run-id RUN_ID
python3 scripts/devcrew.py qa web-find --project . --scenario bad-name
python3 scripts/devcrew.py qa web-find --project . --defect bad-name:error-message
python3 scripts/devcrew.py qa web-report --project . --run-id RUN_ID
```

With no selector, `web-find` returns the newest saved run. Multiple matches set
`ambiguous: true`; show them and ask which one is intended instead of guessing.
A defect ID is `SCENARIO_ID:CHECK_ID` for a failed check, or
`SCENARIO_ID:readback` for a failed backend read.

To repeat one scenario or one defect, start a new managed environment and open a
fresh attempt linked to the source:

```sh
python3 scripts/devcrew.py qa web-replay-start --project . \
  --source-run-id SOURCE_ID --environment-run-id NEW_ENV_ID \
  --base-url http://127.0.0.1:PORT --scenario bad-name
python3 scripts/devcrew.py qa web-replay-start --project . \
  --source-run-id SOURCE_ID --environment-run-id NEW_ENV_ID \
  --base-url http://127.0.0.1:PORT --defect bad-name:error-message
```

Replay verifies the source result, evidence, screenshots, and snapshots before
creating a new run ID, token, and scenario snapshot. It also re-checks the new
environment live: it must belong to this project, differ from the source run's
environment, still have every service on its saved process identity, and record
the same address as `--base-url`. A stale `READY` record, a dead process, a
foreign address, the source environment itself, or an environment already bound
to another attempt is rejected before a run is created; `--isolated` does not
bypass that. The binding is written with the new run directory for both a plain
start and a replay, so an unfinished attempt still owns its environment and a
concurrent second attempt of either kind is refused. The source files are never
modified. Re-run the selected scenario at every declared viewport with the new
token, take new screenshots, and record a fresh backend read. A source `PASS`
does not transfer: the new result comes only from the new observations. If the
source run is incomplete, pass `--isolated` only after checking or accepting the
unknown side effects and starting a separate environment with its own data;
otherwise resolve the effect first. Record a blocked attempt when the browser,
viewport, or backend is unavailable.
