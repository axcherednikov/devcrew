---
name: devcrew-diagnose
description: "Investigate the unknown cause of intermittent failures, regressions, performance issues, or concurrency problems. Establish the cause with evidence and hand confirmed fixes to devcrew-task."
---

# DevCrew diagnosis

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to bound the investigation and state observable evidence; diagnosis remains read-only unless a fix was separately requested.

Apply the mandatory [loop guard](../devcrew-task/references/loop-guard.md): on `LOOP_DETECTED`, stop dependent automatic work, preserve evidence and resolve the technical blocker internally when possible. Ask the user only for a concrete owner decision, missing data/access or material work/cost authorization.

Use this skill when the primary outcome is an evidence-backed root cause, not a production fix. It is a separate investigation only when the cause is materially unknown and the investigation must precede implementation. Do not make it a required stage of ordinary bugfix work. If the user asks for a fix and diagnosis is part of completing it, use [devcrew-task](../devcrew-task/SKILL.md), which owns ordinary diagnosis and the test-first → fix → verification workflow. Use [devcrew-review](../devcrew-review/SKILL.md) to review a completed fix.

After resolving the target project and before substantive investigation, apply the shared [project sync preflight](../devcrew-setup/references/project-sync-preflight.md). Do not initialize an uninitialized project implicitly.

## Investigation contract

1. Read applicable project instructions and the minimum relevant code, contracts, tests, configuration, and operational evidence. Follow the shared [engineering policy](../devcrew-task/references/engineering-policy.md), including its preference for reusable regression/integration tests over disposable harnesses when practical. Establish the smallest reproduction and feedback loop before broad exploration.
2. State at most 3–5 active, falsifiable hypotheses. For each, name the observation that would support or rule it out. Update the list as evidence changes; do not keep stale hypotheses active.
3. Label conclusions `PROVEN`, `SUPPORTED`, `POSSIBLE`, or `RULED_OUT`. Suspicious code, temporal correlation, or a plausible mechanism alone is not causal evidence. Call a root cause proven only when a discriminating check connects the cause to the observed failure and competing explanations are adequately addressed.
4. For intermittent failures, record attempts and reproduction rate (failures / total attempts), along with the reproduction conditions. For performance investigations, capture a comparable baseline before changing inputs, environment, or the measured path; report method, environment, sample count and limitations.
5. Prefer deterministic tools and small controlled experiments. Keep each probe tied to a hypothesis and expected information gain. If a second consecutive probe cycle produces no new discriminating evidence, return `LOOP_DETECTED`, stop probing and report the technical blocker to the coordinator. Resolve it internally from bounded evidence when possible; ask the user only for a concrete owner choice, missing data/access or material work/cost authorization, and wait for that answer before dependent work.
6. Do not change production implementation, apply a fix, or silently turn a probe into product code. Test or diagnostic fixtures needed for a reusable reproduction are allowed when they do not alter production behavior; explain and contain any disposable harness.
7. Follow the shared [cost policy](../../docs/cost-policy.md): prefer a bounded model for delegated read-only analysis, deterministic tools first, compact context, and a stronger model only through the documented escalation path. Do not dispatch agents merely because a diagnosis is difficult. For long investigations, use the shared [context handoff policy](../../docs/cost-policy.md) and pass compact state rather than full history.

## Completion and handoff

Return a concise, evidence-linked report:

```text
STATUS: ROOT_CAUSE_PROVEN | ROOT_CAUSE_UNPROVEN | IMPLEMENTATION_REQUIRED
ROOT_CAUSE:
CLASSIFICATION: PROVEN | SUPPORTED | POSSIBLE | RULED_OUT
EVIDENCE:
MINIMIZED_REPRODUCTION:
REPRODUCTION_RATE: failures / attempts | not applicable
BASELINE: measurements and method | not applicable
REGRESSION_TEST_RECOMMENDATION:
FIX_ACCEPTANCE_CRITERIA:
FILES_CHANGED:
CHECKS: command, exit code, concise result
LIMITATIONS:
NEXT_ACTION:
```

Distinguish observations from inferences, include the strongest counter-evidence, and identify what would falsify the conclusion. A non-proven cause is a valid outcome; do not promote a hypothesis to root cause to make the report look complete.

When an implementation is requested or accepted after diagnosis, return `IMPLEMENTATION_REQUIRED` and route the compact report to the normal [devcrew-task](../devcrew-task/SKILL.md) workflow. Include the reproduction/test recommendation and acceptance criteria so the executor can implement test-first where applicable. The diagnosis agent stops at handoff and does not write production code.

For a long investigation, the compact context handoff contains the original question, established reproduction and rate/baseline, active hypotheses and their evidence labels, completed probes, changed files, constraints, unresolved question, and next discriminating probe. Preserve unknown measurements as unknown; do not infer them.
