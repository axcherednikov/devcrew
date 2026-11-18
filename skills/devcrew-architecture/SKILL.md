---
name: devcrew-architecture
description: "Assess the architecture of an existing repository or subsystem using project evidence. For diff review, bug diagnosis, or new feature design, use the corresponding workflow."
---

# DevCrew — architecture assessment

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to bound the audit and its evidence; architecture analysis remains read-only.

Apply the mandatory [loop guard](../devcrew-task/references/loop-guard.md): on `LOOP_DETECTED`, stop dependent automatic work, preserve evidence and resolve the technical blocker internally when possible. Ask the user only for a concrete owner decision, missing data/access or material work/cost authorization.

Use when the requested outcome is an architectural assessment of an existing system: architecture audit, modularity or testability assessment, architectural friction, dependency direction, module or bounded-context boundaries, or high-leverage refactoring opportunities. This is a focused, read-only investigation. It does not change production code, implement recommendations, review a diff, diagnose a failure, or define a new feature.

After resolving the target project and before substantive assessment, apply the shared [project sync preflight](../devcrew-setup/references/project-sync-preflight.md). Do not initialize an uninitialized project implicitly.

## Route by primary outcome

- Concrete code/diff review → [devcrew-review](../devcrew-review/SKILL.md).
- Evidence-backed cause of a specific failure, without an immediate fix → [devcrew-diagnose](../devcrew-diagnose/SKILL.md).
- Requested implementation or fix → [devcrew-task](../devcrew-task/SKILL.md); ordinary diagnosis needed to deliver a fix stays there.
- New feature requirements or design with material open decisions → [devcrew-brief](../devcrew-brief/SKILL.md).
- A user asks what an existing product is and how people use it → [devcrew-product-onboard](../devcrew-product-onboard/SKILL.md); architecture assessment may selectively read its product context when business boundaries affect the assessment.
- Existing-system architectural assessment → this skill.

An architecture audit is never a mandatory stage of a task, bug fix, or review. If scope is omitted, use available evidence (for example repeated edit hotspots, dependency crossings, or a representative complex call path) to select one likely high-friction area; do not read the whole repository by default. Ask only if repository evidence cannot establish a useful scope or an unresolved product/architecture decision blocks a sound conclusion.

## Evidence first

Read only the relevant project instructions, module structure, interfaces/contracts, representative caller-to-implementation paths, tests, configuration, ADRs and domain documentation. Inspect dependency direction and, when useful, recent history or repeated edit hotspots. Follow a behavior through its actual producer, consumers and tests. Separate observed facts from inference; cite repository-relative file paths and symbols, and identify the specific call path or dependency involved. A file count, long class, interface, service layer, or missing pattern is not evidence of friction by itself.

Look for demonstrated costs such as change propagation across unrelated modules, leaked implementation details, duplicated rules, split transaction/error/concurrency/resource ownership, tests that must reach through public seams to internal details, or pass-through layers without their own responsibility. For a suspected shallow module, apply the deletion test: if removing it, what concrete complexity moves to callers? Little or no caller cost is evidence of indirection; complexity spreading across callers is evidence of useful locality. This is analysis only; never delete a layer as part of this skill.

Recommend a new seam or abstraction only when observed variation, a real dependency change, or an already visible boundary supports it and the direction reduces demonstrated coupling. Do not recommend repositories, interfaces, factories, or adapters merely as a pattern.

## ADRs and candidate quality

Check relevant accepted ADRs before recommending a direction. If evidence conflicts with an ADR, report the ADR, observed conflict, new evidence, and whether reopening the decision merits investigation. Do not declare the ADR obsolete or change it; ADR decisions belong to the applicable owner workflow.

Every candidate must include:

- **ID / title**
- **Evidence:** files, symbols/call paths, tests, dependencies, and repeated changes if verified.
- **Current friction:** the concrete maintenance, testing, or change cost observed.
- **Proposed direction:** an architectural direction, not an implementation recipe.
- **Expected leverage:** what becomes more local, simpler, or easier to test.
- **Compatibility / migration risk**
- **Reversibility**
- **Confidence:** `strong`, `worth exploring`, or `speculative`.

Do not label an evidence-free candidate `strong`. Mark speculative ideas as speculative and do not present them as established defects or recommendations. An abstraction justified only by hypothetical future variation is not a recommendation. If an ADR conflicts, include that explicitly in the candidate's evidence and risk.

Prioritize using demonstrated frequency, maintenance cost, blast radius, expected leverage, reversibility, migration risk, and evidence confidence. Use P1/P2/P3 or high/medium/low leverage only when useful; do not invent numeric scores.

## Output

Keep the result compact unless the user asks for a full report:

```text
ARCHITECTURE STATUS
TOP FINDINGS
- [candidate fields above]
TOP RECOMMENDATION
NEXT INVESTIGATION
NOT RECOMMENDED / LOW VALUE
```

Omit empty sections or say none. Use a diagram only when it materially clarifies a dependency or call flow. Make evidence limits explicit; lack of evidence is not proof that a pattern is absent.

## Handoff to implementation

This skill never implements a candidate. If the user chooses one, return this handoff to the orchestrator:

```text
ARCHITECTURE_CANDIDATE:
GOAL:
EVIDENCE:
PROPOSED_DIRECTION:
ACCEPTANCE_INTENT:
RISKS:
RELEVANT_FILES:
OPEN_DECISIONS:
```

Route clear requirements to `devcrew-task`. Route substantial unresolved product or architecture requirements to `devcrew-brief`. Route a request to prove the cause of a specific failure to `devcrew-diagnose`. Preserve the candidate's evidence and uncertainty; do not turn a speculative direction into acceptance criteria.

## Cost and context

Follow the shared [cost policy](../../docs/cost-policy.md), [engineering policy](../devcrew-task/references/engineering-policy.md), and [agent context policy](../../docs/agent-context.md); do not duplicate them here. Default to bounded/low for a local scan and bounded/medium for a multi-module assessment. Escalate to a stronger model only for a concrete evidence-based reason under the shared policy. Prefer one focused assessment context plus deterministic repository evidence; do not fan out architecture agents without a specific independent need. Keep the scope and returned evidence compact.
