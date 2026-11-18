# Handoff audiences

Claims in every section must be supported by the bounded evidence map. Keep unavailable facts as `UNKNOWN`; `NOT_APPLICABLE` means evidence confirms that the audience has no relevant change.

## Private summary

`Private summary` is for the DevCrew user in the Codex window, never Tracker/MR/customer copy. Include what changed (2–5 concise bullets), significant technical/behavior detail, primary checks actually run, blockers/risks/follow-up (or `None`), and MR/commit/branch when known. Class/service/test names are allowed. Keep it to a 15–30 second read; do not dump files, logs, assertions or tooling output.

## QA

Describe user/system behavior, not implementation structure. Provide 1–3 sentences on the behavior, then actionable scenarios with **Precondition**, **Action**, **Expected result**. Prioritize the changed path and evidenced failure/state-transition/regression cases. Add **Critical cases** only for critical evidenced edge cases. Mention unchanged behavior only when it bounds regression scope. State verification limitations. Avoid internal class names and build-tool results.

## Frontend / client

Include only externally observable contract facts: endpoint, request/response fields, enum/status, validation/errors, ordering/pagination, auth, side effects, events/webhooks, and compatibility. Give concrete client actions, or explicitly say no frontend/mobile changes are required if evidence confirms no public contract change. State compatibility only if verified. Do not invent client work.

## Customer / PM

State 1–3 business outcomes and briefly explain the resulting user/business flow. Include only meaningful limitations, rollout or compatibility notes. Omit source code names, architecture, test tools/counts, and internal implementation details.
