# Canonical routing policy

This is the cross-skill routing source of truth. Specialized skills keep their direct trigger descriptions and own execution details; this matrix decides which workflow owns a request made through `$devcrew`.

For substantive requests, route by outcome first, then the owning workflow applies the shared [task readiness rule](../../devcrew-task/references/task-readiness.md). Readiness determines what evidence, questions and checks the task needs; it does not create another workflow or approval gate.

Classify the requested outcome as one of `IMPLEMENT`, `DIAGNOSE`, `REVIEW`, `HANDOFF`, `QA`, `ARCHITECTURE`, `BRIEF`, `PRODUCT_ONBOARD`, `SESSION_CONTROL`, or `READ_ONLY_ANALYSIS`. Record only observable decision facts; do not infer intent from a source identifier or isolated verb when the requested outcome is clear from the whole request.

## Decision order

1. Respect an explicit specialized skill override, subject to safety and runtime capability.
2. If the host still has a sufficient current human message for an ordinary
   implementation, select the ordinary current-chat lifecycle before asking for
   a formal source binding. Persist only its bounded matrix and digests; do not
   downgrade an existing formal task or use this exception for a referenced
   external source that must be read.
3. Resolve a referenced external source before dispatch when the request
   depends on it.
4. Route by the requested outcome, using this precedence:
   - Explicit commit-message drafting/checking or an authorized commit stage → use `devcrew-commit-message` as described below; a mixed implementation request keeps its implementation owner until that stage.
   - Existing change is the review target → `devcrew-review`; if audience-specific handoff is also explicitly requested, run review first and then handoff from confirmed review evidence.
   - Run local QA, find or read saved QA evidence, or replay a QA scenario/defect → `QA` → `devcrew-qa`. Finding or opening a saved report is read-only and must not start a run, browser, API request, or environment. Preparing delivery text for QA remains `HANDOFF`.
   - Audience-specific delivery text or summary for completed/evidenced work → `HANDOFF` → `devcrew-handoff`. Review/check/inspect an MR by itself remains `REVIEW`.
   - Investigate a materially unknown cause of an application failure or malfunction (including performance or concurrency problems), with no requested change → `devcrew-diagnose`.
   - Assessment of existing architecture only → `devcrew-architecture`.
   - Durable understanding of an existing product, its users and flows → `devcrew-product-onboard`.
   - New feature/design/plan has unresolved material product or owner decisions → `devcrew-brief`.
   - A requested fix belongs to `devcrew-task` unless coordinated delivery evidence warrants `devcrew-autopilot`; diagnosis needed to deliver a fix stays inside the implementation owner.
   - Session continuation/persistence → `devcrew-session`; requested project initialization/configuration → `devcrew-setup`; coordination of multiple existing Codex tasks → `devcrew-coordinate`; explicit skill/routing maintenance → `devcrew-skill-doctor`. `devcrew-small-talk` remains an explicit style override, not a default workflow.
   - Otherwise answer a simple read-only question directly with bounded evidence, including code or concept explanations.

Use these intent values in routing evidence: `IMPLEMENT`, `DIAGNOSE`, `REVIEW`, `HANDOFF`, `QA`, `ARCHITECTURE`, `BRIEF`, `PRODUCT_ONBOARD`, `SESSION_CONTROL`, and `READ_ONLY_ANALYSIS` (short label: `READ_ONLY`). Handoff audience selection is `QA`, `CLIENT`, `CUSTOMER`, or `ALL`; selection does not create separate workflows.

Grill/stress-test requests use the existing Grill mode of `devcrew-brief`; they do not create a new route. “Design + implement” is implementation unless a material decision remains unresolved. A user asking to assess the design of an existing system gets architecture assessment; a user shaping a new solution gets brief.

## Commit-message stage

An explicit request to draft/check a commit message is `READ_ONLY_ANALYSIS` using [devcrew-commit-message](../../devcrew-commit-message/SKILL.md), without a new agent or project setup. A request to make a commit uses the same skill for its message; the caller retains the authorized Git actions and existing verification requirements. For “fix, commit, push and create MR”, keep implementation with its owning workflow and apply the skill only at the later commit stage. Carry the user's earlier authorization and bounded candidate diff through an agent handoff; do not ask for the same authorization again. An agent's unsolicited commit request, a dirty index, or ordinary implementation completion is not authorization. Requests to implement or discuss the skill remain implementation or read-only discussion, not a commit request. This stage adds no new lifecycle or intent value and does not replace the owning workflow.

## Implementation topology

Choose `devcrew-task` for a bounded change with one primary implementation flow and no meaningful dependency graph. Choose `devcrew-autopilot` only when evidence shows coordinated delivery: dependent tasks, multiple independently gated layers, migration plus compatibility/rollout, an implementation epic, or sequential READY work. Prompt length and risk alone do not justify autopilot. High risk changes review depth and verification under the existing policy; it does not automatically change the owner workflow.

Use coarse scope only: `SMALL` is a bounded change with one main flow; `MEDIUM` spans related components but remains one deliverable; `LARGE`/epic has dependent deliverables or separate migration, compatibility, rollout, or verification gates. Judge from boundaries and dependency topology, not prompt size or lines changed. High-risk signals include money/payment, auth/security, tenant isolation, destructive migrations, irreversible data changes, concurrency, external side effects, and production-critical state machines. Risk changes verification/review depth, not task-versus-autopilot selection by itself.

## Decision facts

For debugging or tests, preserve a concise routing decision with only these facts: `source`, `source_status`, `intent`, `scope` (`SMALL|MEDIUM|LARGE`), `uncertainty` (`LOW|MEDIUM|HIGH`), `risk` (`LOW|MEDIUM|HIGH`), `workflow`, and a one-line `reason`. `LOW` uncertainty means requirements are evidenced; `MEDIUM` means technical unknowns can be resolved from project evidence; `HIGH` means a material owner/product decision remains. Technical unknowns alone are not a reason to ask the user. Use low reasoning effort for ordinary routing. No model or high effort is mandatory. Do not store or emit chain-of-thought. This is lightweight evidence, not a user-facing report requirement.

Technical uncertainty is normally resolved by targeted repository evidence after ownership is selected. Ask one focused question only when evidence cannot settle a material product/owner decision or a required source/workspace is unavailable or ambiguous. Use the failure labels and behavior in [source resolution](source-resolution.md).

For `IMPLEMENT → HANDOFF`, complete the requested implementation and verification first; create handoff only from confirmed results. For `REVIEW → HANDOFF`, use confirmed review evidence; any blocking finding yields `HANDOFF_BLOCKED`, never a successful completion claim. An ordinary implementation or review does not trigger handoff automatically.

Follow the shared [cost policy](../../../docs/cost-policy.md): use a bounded model at low effort for ordinary classification; use medium only for genuine routing ambiguity. No model or high effort is mandatory for routing. Do not spend delegated-agent budget to classify a routine request. Model escalation and risk-specific review belong to the selected owner workflow.

Changes to this canonical policy require evidence from repeated routing errors or a contract gap. One mistake is a candidate for investigation, not authority to change policy; use the existing `POLICY_CANDIDATE` mechanism in DevCrew retro guidance.
