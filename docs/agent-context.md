# Agent context and usage

DevCrew accepts **observations**, not estimates of an agent's context. The
orchestrator evaluates each observation with `context-evaluate` before starting
a substantial step and after an atomic step. When a runtime supplies no usage
snapshot, token and context fields stay `null`; work follows ordinary task
rules. Cumulative token usage is not a context size or Codex allowance.

## Runtime capability boundary

The current desktop `spawn_agent` tool starts a distinct context and supports
`fork_turns: "none"`, role selection, and model/effort options. Its exposed
result/control schema does not provide per-agent token usage, current context
size, or a model context limit. Agent identity is available from the spawn
result. Model/effort are known when explicitly selected or from an authoritative
role configuration; DevCrew must not infer the actual model if an override may
be ignored. The desktop `get_usage_limits` tool provides account-level rate
limit windows when present, including used percentage and reset timestamp, but
is not a context measurement or a general Python/CLI API. A missing session
window is unavailable, even when the weekly window exists.
The local `codex exec` CLI exposes JSONL events, but no documented way was
confirmed here to subscribe DevCrew to a desktop `spawn_agent` session's live
context count. [Plugin hooks](https://developers.openai.com/plugins/build/plugins)
exist but are trust-gated and do not establish a token-usage callback in this
runtime.

The separate [Agents API observability contract](https://developers.openai.com/api/docs/guides/agents-api/observability)
has best-effort turn `usage` and `subagent_id`. Its token counts can be null or
revised. `cached_tokens` are part of input tokens and `reasoning_tokens` are
part of output tokens. These APIs apply to Agents API sessions, not to desktop
`spawn_agent` calls. [Responses compaction](https://developers.openai.com/api/docs/guides/compaction)
has a threshold, but that API feature is not a DevCrew desktop lifecycle hook.
DevCrew does not scrape UI or read undocumented local session storage.

## Configuration

An optional `agent_context` object belongs in the project's existing
`.codex/devcrew.json` alongside `schema_version` and `paths`:

```json
{
  "agent_context": {
    "enabled": true,
    "telemetry": true,
    "automatic_handoff": true,
    "soft_context_percent": 45,
    "hard_context_percent": 60,
    "soft_context_tokens": null,
    "hard_context_tokens": null,
    "weekly_soft_remaining_percent": null,
    "weekly_hard_remaining_percent": null
  }
}
```

The example shows the optional part only; retain the existing `schema_version`
and `paths`. Percentage thresholds apply only when both
`current_context_tokens` and `context_limit` are supplied by a trusted runtime
source. Absolute thresholds apply when configured and a current context count
is available, even if the limit is unknown. No absolute thresholds are
invented. When both forms apply, the more severe signal wins. Weekly guards
are separate from context handoff and remain inactive without a supplied
allowance observation. `telemetry` controls collection/reporting intent;
`automatic_handoff` controls whether the orchestrator executes a recommended
replacement. A hard signal always stops further substantive work if replacement
is unavailable.

## Observation and commands

Provide a JSON file with `agent_id`, `role`, `task_id`, `model`,
`reasoning_effort`, `started_at`, `finished_at`, `handoff_count`,
`current_context_tokens`, `context_limit`, and `usage` when known. Unknown
values should be `null` or omitted. `usage` accepts the Agents API token shape
(`input_tokens`, nested cached details, `output_tokens`, nested reasoning
details, `total_tokens`) or flattened equivalents. Never count cached input or
reasoning twice. Optionally supply `allowance` with
`session_remaining_percent`, `weekly_remaining_percent`, and `reset_at` when a
stable runtime tool returns them; do not derive them from tokens.
The desktop `get_usage_limits` response can also be placed in `allowance`:
the helper recognizes 300-minute session and 10080-minute weekly windows by
their reported duration, computes `100 - usedPercent`, and preserves absent
windows as null. This is an optional supplied observation; the CLI cannot call
the desktop tool itself.

```text
python3 scripts/devcrew.py context-evaluate --project PROJECT --agent AGENT.json
python3 scripts/devcrew.py context-handoff --agent AGENT.json --contract CONTRACT.json --state STATE.json --replacement-id ID
python3 scripts/devcrew.py context-report --project PROJECT --task-id T04 --agents AGENTS.json
python3 scripts/devcrew.py context-report --project PROJECT --agents PHASE-AGENTS.json
```

`context-evaluate` returns `CONTINUE`, `HANDOFF_RECOMMENDED`, or
`HANDOFF_REQUIRED` plus the measurement source. A recommendation means finish
the current safe atomic step, stop before the next substantial step, and send a
compact handoff. A hard signal requires the same handoff immediately after
safely finishing the current indivisible operation. If the first observation
already exceeds hard, do not begin a new step. The helper does not spawn an
agent or create a commit; the orchestrator performs the actual replacement.

The phase lifecycle reports executor authority separately from telemetry.
`phase resume` exposes `NEW_CONTEXT_REQUIRED` for
independent executor work even when context measurements are unknown or
disabled. An active same-phase repair reports
`LOCAL_FIX_CONTINUATION_ALLOWED`. If the required new context cannot be
started, the existing required-handoff failure is
`AGENT_HANDOFF_REQUIRED_BUT_UNAVAILABLE`; the prior executor and its route do
not become fallback authority.

`context-handoff` emits a compact fixed set of state fields (TASK_ID, GOAL,
DONE, REMAINING, FILES_CHANGED, TESTS, FAILURES, DECISIONS, NEXT_ACTION) and a tool-compatible
`spawn_agent` request (`task_name`, `agent_type`, `fork_turns`, `message`,
`model`, `reasoning_effort`). The
message contains the original task ID, contract and compact state. Add only
needed file references when calling the tool. Never forward the old conversation or reasoning
history. A handoff keeps the same task cycle and commit boundary. Reviewers
always start in a fresh context. If a required replacement cannot be spawned,
stop with `AGENT_HANDOFF_REQUIRED_BUT_UNAVAILABLE`.

## Routing replacement and reviewer contexts

Supply a current `runtime` snapshot with the model availability, capabilities,
efforts and role override controls documented in [cost policy](cost-policy.md).
For `context-handoff`, put it in AGENT.json, alongside optional `routing` inputs
(e.g. `{"risk":"high","implementation_complexity":"NORMAL"}`), a previously
selected `route`, and the exact `role_contract` text when a generic fallback is
needed. The Python API also accepts `runtime`, `selection` and `role_contract`
keyword arguments. Existing observed `model` fields describe the old agent;
they never choose the replacement's model. Missing runtime evidence blocks
payload creation instead of inheriting a default.

`prepare_handoff` returns `spawn`, `route` and `dispatch` metadata alongside the
compact handoff. `reviewer_spawn(..., runtime=..., risk=...,
review_complexity=..., role_contract=...)` returns a tool-compatible spawn
payload with its route record in the message. `routed_spawn` builds a bounded
initial-role payload using the same policy validation and override checks.
All builders include explicit model/effort and fresh context. Matching typed
roles are preferred; unavailable or conflicting pins require a generic role
with the exact caller-supplied role contract, or `AGENT_RUNTIME_REQUIRED`.

Retain the record before the coordinator submits the payload to the real tool.
`ROUTE_APPLIED=false` describes preparation, not a failed spawn. Use
`devcrew_cost.confirm_route` with runtime's effective model/effort after launch;
missing evidence stays UNCONFIRMED and differing values produce MISMATCH.
Never assert that an explicit payload alone proves the runtime applied it.
This helper does not call the desktop spawn tool or migrate role TOMLs.

Task and phase reports aggregate **recorded** tokens. Null usage remains
unknown; these counts are not a subscription billing or allowance estimate.
The phase report sorts tasks/agents by recorded tokens and lists handoffs and
model distribution. Threshold events should be kept in agent observations.
When project configuration sets `telemetry: false`, `context-report` returns
`TELEMETRY_DISABLED` without reading agent observations.
