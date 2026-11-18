---
name: devcrew-product-onboard
description: "Explore an existing connected project from the user perspective and build product context from evidence. Run on explicit request or workflow recommendation, not automatically after setup or implementation."
---

# DevCrew product onboard

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to bound the requested product investigation and its observable evidence.

Use only when the user explicitly asks to understand an existing product, its users and flows, or when a workflow explicitly recommends refreshing stale product context. This is a read-only product discovery followed by a bounded write to the project's configured memory. It is not setup, feature design, architecture assessment, diagnosis, implementation, or review. Never run automatically after setup or for every task. Do not use this for greenfield projects without meaningful implementation or product materials: route product formation through `devcrew-brief` and owner decisions instead.

Read applicable `AGENTS.md`, `.codex/devcrew.json` if present, the configured context and memory index, and only relevant existing memory. Follow [DevCrew project memory lifecycle](../devcrew-session/references/memory-lifecycle.md), [cost policy](../../docs/cost-policy.md), and [agent context policy](../../docs/agent-context.md). Reuse configured memory paths; do not introduce a parallel memory system, overwrite README/AGENTS/dev memory, initialize the project, or modify production files. Preserve unrelated worktree changes. Do not stage or commit unless separately requested.

## Scope the research

First establish that the project is an existing, initialized DevCrew target and has meaningful code or product materials. Inventory its actual routes/controllers/pages/API surfaces, domain models, authorization, analytics/flags/plans, and clearly identified product documents. Establish which lenses have substrate before choosing readers. For a small scope, inspect inline or use the minimum sufficient read-only researcher(s); do not create a fixed agent roster. For a broad monorepo scan or substantial fan-out, state the lenses, expected number of researchers, and scope, then wait for approval before that material cost. Ordinary bounded read-only inspection needs no approval ceremony.

Default researchers to a bounded model at low/medium effort, bounded to specific lenses and paths, read-only, and returning evidence only. Stronger model/effort is allowed only for a concrete reason under shared cost policy. No model or high effort is mandatory because this is onboarding. Researcher reports are proposals: the coordinator checks conflicting evidence directly before writing.

Use only applicable lenses:

1. **Product map and user flows:** user-facing surfaces, sections and actions, registration/onboarding, primary value flow, checkout/payment when present, and administrative flows that are part of the product. Return a concise map and critical end-to-end paths.
2. **Actors, permissions and visibility:** user-relevant actors, roles, permissions, tenant boundaries, visibility and ownership. This is not a security audit.
3. **Domain glossary:** business-facing entities, relationships, meaningful statuses/transitions and terms. Do not dump database schemas.
4. **Product signals:** implemented analytics events, tracking, feature flags, plans, quotas, usage limits and experiments, only when a substrate exists. If analytics is absent, record a white spot; never invent metrics.
5. **Product materials:** explicitly available product docs, research, interviews, ticket exports or metric exports. Keep source type clear and distinguish owner statements, hypotheses and evidence; do not assume a conventional folder exists.

Omit unsupported lenses and say why. Do not run a full security audit or architecture audit under this skill.

## Evidence contract

Every durable observed product claim carries a precise source: repository-relative file and line/range, route/schema/event/config identifier, or project document and section. Use these labels:

- **CONFIRMED** — directly established by code or an authoritative project document.
- **INFERRED** — a strong conclusion from cited evidence that is not directly stated; preserve the distinction and rationale.
- **UNKNOWN** — evidence is insufficient or conflicting; record the open question and sources checked where useful.

Do not turn inference, absence of evidence, researcher consensus, or owner preference into a confirmed fact. Product decisions belong to their existing owner mechanism (brief/accepted decision/ADR), not product memory. A fact such as a tenant boundary may be discovered; a desired ranking or display policy cannot be inferred from it. Conflicting researcher reports must be independently checked by the coordinator. An absent analytics substrate is a confirmed observation only when the bounded inventory supports that conclusion; its meaning or future success metric remains unknown.

## Product memory and refresh

Write one concise `product-context.md` in the memory directory configured by `.codex/devcrew.json` (or the existing project memory convention when no config exists). Do not assume the directory is literally `memory/`; do not write into this DevCrew source package or global/plugin cache. Organize the file under stable sections: `Product map / flows`, `Actors / permissions / visibility`, `Domain glossary`, `Signals / analytics / flags / plans`, and `Unknowns / assumptions`. Sections without substrate say so briefly. Keep each fact next to its status and evidence. Put confirmed owner decisions only as references to their owning brief/ADR/decision source, never as code-derived product facts. Keep product context separate from engineering conventions and avoid copying README, AGENTS, project context or existing memory.

Add or update a single entry in the existing memory index/map following its format, linking to `product-context.md` and explaining its product scope; do not duplicate the product facts in the index. If an existing product context uses another established path, update it in place instead of creating this filename. On refresh, revalidate each old source and claim: update changed evidence, remove claims no longer supported or move unresolved ones to UNKNOWN, and retain owner decisions by reference to their owning record. Never let an inference overwrite an accepted decision. Do not append a second copy on rerun.

For freshness, record evidence paths and line ranges/identifiers next to claims. On later runs compare those sources and refresh the affected sections; missing or moved evidence makes the claim stale until verified. Avoid global hashes, timestamps as truth, or a new versioning system. Keep the memory concise and indexed for selective loading.

## Downstream context loading

Do not make every executor read the whole product context. Consumers first inspect the index, identify product-facing behavior and relevant terms, then load only matching sections and cited source/decision references. For example, payment changes need the payment flow, relevant actors and glossary; search ranking needs search flow, terminology and implemented signals; an internal refactor with no product behavior needs no product context. If the task's affected product invariant is missing and materially blocks planning, ask a targeted question or recommend this skill; do not fill the gap with an assumption.

- `devcrew-brief` uses relevant product facts as evidence while forming requirements and questions; onboarding is not repeated inside brief.
- `devcrew-task` receives only relevant product facts/invariants and owning decision references.
- `devcrew-review` checks a confirmed product invariant only when it overlaps the review scope.
- `devcrew-architecture` loads product context only when business flows or boundaries affect the architectural assessment.
- `devcrew-autopilot` never invokes this skill automatically. If missing product context blocks planning, it may recommend it or return a targeted question.
- `devcrew-session` does not promote observations to product facts; durable product claims require evidence and belong in the configured product context.

## Routing boundaries and result

- “Understand what this product is and how people use it” → this skill.
- “Design a new cart / decide unavailable-product behavior” → `devcrew-brief`, informed by existing product context; decisions remain with the owner.
- “Assess payment architecture” → `devcrew-architecture`.
- “Find why checkout fails” → `devcrew-diagnose`; “fix checkout” → `devcrew-task`.
- “Review this checkout change” → `devcrew-review`.
- “Initialize DevCrew” → `devcrew-setup`; setup does not trigger this skill.

Report memory file/index updated, lenses used and omitted, confirmed/inferred/unknown highlights, evidence limits and white spots, reader count/model route if delegated, and which downstream work can selectively load it. Never claim an unsearched area is absent.
