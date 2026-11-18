# Engineering execution and verification policy

This is the shared policy for direct execution, delegated slices, coordination/autopilot and review. Apply it after establishing the task's acceptance criteria from the user and repository. It does not require extra agents, task cards or a separate review skill.

For the single shared readiness rule—including proportional investigation, task boundaries, uncertainty classes, decomposition and source-format routing—follow [task readiness](task-readiness.md). The selected owning workflow performs the research; this policy supplies implementation constraints and does not add an intake gate.

## Stop recursive work

Apply the mandatory [loop guard](loop-guard.md) before each corrective pass or
repeated probe. `LOOP_DETECTED` stops dependent automatic work without evidence of progress.
Resolve technical errors internally; ask the user only for a concrete owner choice,
missing data/access or material work/cost authorization. Repeated FAIL alone is
not a loop; automatic replan or model escalation cannot erase history.

## Classify before execution

### Language and version profile

Before a code or language-specific configuration change, identify only the
affected language/runtime profile: primary and affected languages, supported
version/range, runtime/framework, relevant library versions, and evidence paths.
Use relevant manifests, lock files, toolchain/version files, CI matrix, container
images, compiler/runtime config, and project instructions as evidence. Do not
inventory unrelated parts of a polyglot repository. If a range is supported,
target its minimum unless the actual build matrix proves a
narrower target; avoid syntax, APIs, framework features, or compiler options
newer than confirmed support. Conflicting authoritative evidence that changes
the implementation is a blocker, not a reason to guess or update dependencies.
Omit the profile when it does not affect a pure documentation or operational
task.

Before putting a concrete path, command, version, or service/resource name in
executor-facing instructions, verify it from repository evidence. Preserve
the command as defined by the project. If it cannot be verified, mark it
explicitly for runtime verification instead of inventing it.

Choose the task type and verification strategy before editing or dispatching. Include them in the existing brief/report or coordination inventory; a separate artifact is unnecessary. For mixed tasks, classify each meaningful phase; a spike label must not hide production implementation.

## Understand first; choose the smallest complete solution

Separate confirmed facts (with source/observation), engineering conclusions
(reasoning from those facts), and unverified hypotheses (what remains to check)
in the existing plan and user report. A plausible mechanism is not a verified
fact. An engineering recommendation is not a confirmed user requirement.

When requirements and confirmed facts suffice, research feasible options,
choose an evidence-backed solution and explain why it fits the constraints.
Do not delegate an internal implementation choice to the user merely because
several implementations are possible. The assigned planner chooses material
technical strategy; the executor resolves details within the accepted plan.

For restoration, confirmed original behavior is the compatibility baseline
unless the requirements explicitly say otherwise. Investigate how to preserve
it safely; do not offer an incompatible simplification without a concrete
reason, or implement that behavior change without owner agreement. For example, RAM-only locks
do not restore confirmed persistence across reboot. That fact determines the
required behavior, but does not prove the original storage mechanism or that
a proposed persistence method is safe.

When facts are insufficient, continue available targeted research rather than
asking the user to choose prematurely; do not fill evidence gaps with invented
facts or turn hypotheses into requirements or test expectations. If accessible
research cannot resolve a material gap, state the evidence limit and ask for
the missing information or owner decision before dependent implementation.

When a choice involves material unresolved requirements or a material unresolved risk
to data preservation, security, or significant irreversible consequences,
stop dependent work, explain the concrete risk and ask a specific question
with a recommended option and its rationale. An established safe pattern
within confirmed constraints is an engineering choice; touching storage or
security alone does not require asking the user to design it. A recommendation
awaiting owner agreement stays unconfirmed; silence is not agreement.
This guidance does not override safety, scope, role boundaries or the loop
guard; use existing technical-replan and owner-decision routes.

In DIRECT, the host performs the bounded discovery below. In AGENT EXECUTION,
the assigned research/planning role performs substantive flow and precedent
discovery and owns material technical solution selection; the executor inspects
implementation details within the accepted plan. The coordinator checks the
plan and evidence references for sufficiency but must not repeat the task's
substantive investigation or decide its technical approach.

Minimal Diff is a solution-selection discipline, not a reason to shorten
discovery or confirmed scope. Before choosing an implementation, trace the
affected behavioral flow, inspect likely change sites, find analogous project
precedents, understand affected layers/contracts, and separate confirmed
requirements from speculative improvements. If the flow is unclear, continue
targeted discovery; a short edit at the wrong architectural seam is not minimal.

Then move down this ladder and stop at the first option that fully satisfies
the confirmed task:

1. Remove only speculative framework, future-proofing, or unrequested
   extension points; never reduce confirmed scope.
2. Reuse an existing project helper, component, service, migration pattern,
   convention, or utility.
3. Use the language standard library instead of a wrapper without its own
   contract.
4. Use the framework, platform, or database primitive that enforces the
   behavior at the correct seam.
5. Use an already-installed **direct** dependency accepted in that layer. A
   new dependency is a separate explicit decision; never rely on an incidental
   transitive package or add one silently during implementation.
6. Use one local operation when it stays clear and respects module boundaries.
7. Otherwise implement the smallest complete local solution.

Do not add an abstraction merely for cleanliness, a possible second
implementation, or pattern conformity. Require evidence of a real boundary:
a public/external contract, actually varying implementation, dependency
boundary, documented invariant, or repeated repository precedent. One
hypothetical future implementation is not evidence. Prefer a correct root seam
over duplicated caller patches, while preserving established module
boundaries; file count alone is not a goal.

Minimal Diff must never defer or remove trust-boundary, user-input, webhook or
external-API validation; necessary data-loss prevention; security,
authentication, authorization or tenant isolation; data integrity; required
accessibility; explicitly requested behavior; approved acceptance criteria;
or documented project invariants. Use the ladder to implement these obligations
fully. If one appears unnecessary, return it to the decision owner as an open
question; do not mark a reduced-scope task done.

An optional `minimal-diff: <observable ceiling> — <verifiable trigger and
action>` marker is valid only for an intentionally simple, reversible choice
outside those protected obligations, with a known observable ceiling, a
checkable trigger, and a concrete next action. A vague future optimization is
invalid, and a marker does not create a backlog item automatically.

For nontrivial logic, use the existing test-first policy below: add the
smallest behavioral test for the changed semantics, observe RED then GREEN
where TDD applies, and use the existing runner. Do not create another TDD
workflow or introduce a test framework for one signal. Verification policy
remains the source of truth.

| Type | Default verification strategy |
|---|---|
| IMPLEMENTATION | Test-first for changed observable application behavior that can reasonably be tested automatically. |
| SPIKE | First ask: “Can this check become a useful permanent regression/integration test?” Prefer a reusable automated capability test when reasonable. |
| EVALUATION | Reproducible benchmark/evidence: inputs, method, environment, measurements and limitations. |
| DOCUMENTATION | Check consistency, references and relevant document/package validation. Includes ADRs and research conclusions. |
| MIGRATION/OPERATIONS | Verify the plan or operation with appropriate dry-runs, pre/postconditions, data checks and recovery evidence. Classify behavior-changing implementation within it as IMPLEMENTATION. |

Do not mechanically require TDD for documentation, pure research, benchmarks/evaluations, ADRs, migration plans without implementation, experiments whose outcome is still unknown, or changes that cannot adequately be expressed as an automated test. State why test-first is inapplicable and explain the chosen verification strategy. Unavailable infrastructure is a verification limitation, not a successful check or proof that the behavior is untestable.

## Implementation: test-first

Use `RED → minimal implementation → GREEN → refactor → targeted verification → broader verification → review` when applicable, including small direct fixes.

1. Before implementing the behavior, add or modify a test expressing an acceptance criterion. Use the repository's established test suite and assert observable outcomes rather than copying the production algorithm.
2. Run the test against the pre-implementation behavior. Inspect the failure: it must expose the missing/incorrect behavior, not merely a broken fixture, unavailable service or unrelated setup error. If it already passes, investigate the criterion and coverage before implementation; do not claim RED.
3. Make the smallest implementation that satisfies the criterion and rerun the test to observe GREEN. Never weaken valid existing tests or alter expectations to accommodate incorrect behavior.
4. Refactor only where needed, preserving GREEN. Run targeted checks on the final change, then broader checks justified by affected contracts, integration paths, risk or project requirements. If no broader run is warranted or a check is blocked, explain that scope/limit; do not automatically run every suite.
5. For DIRECT, the host reviews the final diff and acceptance evidence inline. For `devcrew-task` AGENT EXECUTION, a separate read-only reviewer-agent reviews after deterministic checks; coordinator acceptance and diff/scope inspection do not replace that independent review. Other workflows retain their own review rules.

No separate RED commit is required. In the execution report, identify the added/changed test, the observed initial failure and its reason, and the implementation that made it GREEN, with actual commands/results. Historical RED evidence is expected to fail and predates the final tree: retain it as history, never use it as a successful final gate record. Existing execution-evidence and freshness rules still govern final checks. Do not reconstruct or claim an unobserved test-first sequence after implementation.

## Spike boundaries and durable capability tests

When a stable infrastructure capability can be checked repeatedly, keep its check in the normal regression/integration/functional suite with reusable fixtures, setup/cleanup and explicit environment requirements. For example, `tests/Integration/Qdrant/HybridSearchCapabilitiesTest.php` is preferable to `evaluation/t03/check-qdrant.php` for BM25, Query API, dense+sparse fusion, tenant isolation, structured filters, restart compatibility or an SDK/API contract. Follow the actual repository layout; these paths are examples. The test must remain useful after the spike ends.

Unknown experimental outcomes do not require an invented failing expectation. Establish the capability, record the observed evidence, and preserve a useful repeatable test when reasonable. Explain why a disposable harness is needed if a permanent test would otherwise be useful.

A spike may use a minimal adapter/harness needed to probe a capability. It must not silently implement the next production task. If that code is effectively a future production component, record the conclusion and leave production implementation to its assigned task; do not automatically move the probe into the application.

Use `evaluation/` primarily for research/measurement artifacts: benchmark datasets, labelled corpora, metrics, captured responses, measurements, experiment reports, environment fingerprints and genuinely disposable exploratory scripts. Do not place production implementation there or duplicate checks that belong in permanent integration/functional tests. Tests and evaluation should exercise real interfaces and use independent expected outcomes, not duplicate production logic.

## Review and acceptance

Never use `git add -f` or `git add --force` to stage ignored files or to stage
the whole working tree. Stage only explicit task-owned paths/hunks. Project-local
`.codex/` contents are reinstallable runtime state and must remain untracked,
even when an owned scope would otherwise include them.

For implementation, check that a test covers the new behavior and the actual acceptance criterion, that valid assertions were not weakened to fit the implementation, and that applicable test-first work has evidence of the initial meaningful failure and subsequent GREEN. A final passing test alone does not establish test-first history; report unavailable history accurately.

Check tests/evaluation for duplicated production logic and for an unexplained disposable harness where a useful integration test was feasible. For spikes, also check lasting test value, reproducibility and the boundary with future production work.

Absence of TDD alone is not FAIL when the executor has reasonably explained why test-first is inapplicable and supplied an appropriate verification strategy. Assess the explanation against the task; distinguish an evidence gap from a demonstrated behavior defect. Do not invent RED evidence or convert a justified exception into a mandatory gate failure.

Treat Minimal Diff as a risk question, never a file-count blocker. Check for
unsupported layers, abstractions or dependencies; an available project
primitive; unrequested future functionality; duplicated caller patches where
a correct root seam exists; skipped protected obligations; and speculative
flexibility. Pure excess may be a non-blocking recommendation. Scope drift,
broken invariants, protected-obligation violations, failed acceptance, and
verification failures remain blocking.
