# Bounded workflow handoff

When handing off pending collaboration-agent work, preserve its task/agent identity, role, stage, original dispatch receipt, expected result, any result already received, and next action. The receiving coordinator follows the shared [agent waiting procedure](../../devcrew-task/references/agent-waiting.md) and reconciles results before replacement or further dispatch.

Pass only the context the selected owner needs to begin. Use this compact structure, omitting unknown or irrelevant fields:

```text
ORIGINAL_INTENT:
RESPONSE_LANGUAGE:
DELIVERABLE_LANGUAGE: (when different)
SOURCE_REFERENCE:
SOURCE_STATUS:
CONFIRMED_SOURCE_SUMMARY:
WORKSPACE:
INTENT:
SCOPE / RISK / UNCERTAINTY:
ACCEPTANCE_CRITERIA:
UNRESOLVED_OWNER_DECISIONS:
RELEVANT_EVIDENCE_REFERENCES:
```

Pass the conversation language and any separate deliverable language to the receiving agent. Preserve both preferences across fresh contexts. Include confirmed source facts, relevant acceptance criteria, and paths/URLs/identifiers that can be reopened. Do not pass the full transcript, unrelated comments, complete tool logs, whole-repository context, or full product memory. The owner workflow continues targeted discovery and applies its own policies.

Dispatch exactly one owning workflow. Once dispatched, that workflow owns the request. It may surface a concrete evidence-backed decision or reclassify only when new evidence changes the requested outcome. A targeted escalation is not a return to this router for another classification pass. Never create `router → owner → router → owner` loops.

Small implementation stays on the ordinary `devcrew → devcrew-task` path; routing alone never creates an agent team. Prefer a bounded model at low effort for routing when model selection is available; no model or high effort is mandatory. Do not require a separate router agent. `devcrew-autopilot` applies only its existing runtime gate, topology rules, model/cost policy and executor/reviewer lifecycle. A lightweight routing decision record may contain the decision facts described in `routing.md`; never include chain-of-thought. The owner workflow applies its execution and cost policy after handoff.

For `HANDOFF`, pass source identifiers, confirmed requirements, linked implementation/review/verification evidence, requested audiences and unresolved limitations only. Keep the evidence bounded; the handoff owner resolves provider-neutral sources and verifies implementation evidence before writing audience text. No external comment is posted automatically.
