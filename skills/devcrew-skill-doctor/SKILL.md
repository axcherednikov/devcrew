---
name: devcrew-skill-doctor
description: "Audit or minimally repair skills, routing, and supporting infrastructure on explicit request. Use other workflows for application review, architecture, bug diagnosis, or project setup."
---

# DevCrew — Skill Doctor

Reply in the user’s language. If the user writes in Russian, reply in Russian unless they request another language. Honor a separately requested language for deliverables. Keep code identifiers, commands, and machine-readable fields unchanged.

Apply the shared [task readiness rule](../devcrew-task/references/task-readiness.md) to bound the requested skills or routing work and identify checks before edits.

Apply the mandatory [loop guard](../devcrew-task/references/loop-guard.md): on `LOOP_DETECTED`, stop dependent automatic work, preserve evidence and resolve the technical blocker internally when possible. Ask the user only for a concrete owner decision, missing data/access or material work/cost authorization.

Use this skill only when the user explicitly asks to audit, troubleshoot, optimize, repair or migrate agent skills, their routing, metadata, references or supporting helpers, or when maintenance of DevCrew itself explicitly requires a skill-level audit. Its subject is agent instructions and infrastructure, not application code or project state.

## Boundary and scope

- Ordinary code/diff review → [devcrew-review](../devcrew-review/SKILL.md).
- Existing application architecture → [devcrew-architecture](../devcrew-architecture/SKILL.md).
- Application failure investigation → [devcrew-diagnose](../devcrew-diagnose/SKILL.md).
- Feature implementation or unrelated repository work → [devcrew-task](../devcrew-task/SKILL.md).
- Project setup and project-local DevCrew state → [devcrew-setup](../devcrew-setup/SKILL.md).
- Do not become a required stage of normal task or autopilot execution.

Establish the requested scope before reading files. Distinguish source skills, installed plugin caches, system skills, personal skills, and vendor/imported skills by their actual paths and provenance. Same skill names in different installations are distinct files. For DevCrew, edit only its source repository. Never edit an installed plugin cache as source; plugin installation/update is a separate supported operation after commit. Inspect a cache only when explicitly in scope, and label it as installed state. Do not run user projects or perform a global live audit unless requested.

## Audit and repair mode

- “Audit”, “check”, “what is wrong”, or a request for recommendations means **read-only audit**. Do not edit files.
- “Fix”, “optimize”, “clean up”, or “migrate” authorizes the smallest necessary local repairs in the requested source scope. Do not ask for permission again. Preserve unrelated changes; do not commit or update installed caches unless separately requested.

Inspect the entrypoint, metadata (`agents/openai.yaml`), only relevant references, executable helpers and callers, shared policies, package structure, and routing tests. For DevCrew, also inspect autopilot routing and the relevant overlaps among brief, task, diagnose, architecture, review, coordinate, session, setup, and autopilot. Stop when the evidence is sufficient.

Check for:

1. Descriptions attracting another skill's primary outcome; overlapping primary outcomes; routing contradicting descriptions.
2. Contradictions between `SKILL.md`, metadata, references, helper behavior and callers.
3. Repeated shared policies that have a clear owner; stale paths/commands, missing dependencies, broken helpers, or helpers that report PASS without verifying the intended result.
4. Tests that omit realistic positive or negative routing boundaries, package structure defects, and skills made mandatory without need.
5. Model/cost routing that conflicts with the shared policy or escalates without a concrete reason.
6. Responsibilities that combine unrelated outcomes.

For DevCrew, follow [the shared engineering policy](../devcrew-task/references/engineering-policy.md) and [cost policy](../../docs/cost-policy.md); do not copy their general rules here. Prefer a bounded model: routine static audit uses bounded/low, a broad multi-skill audit may use bounded/medium, and a strong model requires a specific difficult ambiguity. Do not start live/model evaluations automatically. Prefer static inspection, deterministic tests, package validation, and routing contract tests.

## Evidence and findings

Classify each observation as **PROVEN DEFECT**, **STATIC FINDING**, **UNTESTED HYPOTHESIS**, or **PREFERENCE**. A proven defect needs direct evidence of a violated contract or a reproducible failure mechanism. A static finding is a concrete contradiction or invalid structure established from files, even if no runtime failure was executed. A plausible but unverified runtime concern is a hypothesis. Style or preference is not a defect.

Every actionable finding names its owning file, evidence, a concrete failure scenario, severity, and the smallest justified repair. Do not promote absence of evidence into proof. If the current instruction already requires the desired behavior and only a model incident ignored it, classify it as a model behavior incident; do not add a duplicate rule. Determine whether the owner is the instruction, routing, helper, or model behavior before editing. Do not compensate for a broken helper with more prose.

## Minimal repair and verification

Replace the faulty rule at its owning surface; do not stack warnings. Change the fewest files, preserve stable names/interfaces, and move conditional detail to a reference only when it makes the entrypoint meaningfully smaller. If helper arguments change, update callers, tests, and examples together. Save recoverable originals outside active skill roots when doing a repair, without overwriting user data.

After repair, reread the final diff and linked references/metadata; check the affected positive and negative routing scenarios, relevant unit/contract tests, package validation and `git diff --check`. Use the repository's actually working test discovery command; plain `python3 -m unittest` may discover no tests. Do not claim model evaluation unless it ran. Never edit plugin cache as part of source repair.

## Output

Audit:

```text
FINDINGS
- [severity] [classification] owning file — evidence; failure scenario; proposed minimal repair
NO-CHANGE FINDINGS
LIMITATIONS
```

Repair additionally reports `CHANGED`, `VERIFICATION`, and `REMAINING RISKS`. Include checks only when actually run. Do not require HTML, scorecards or report artifacts unless useful or requested.
