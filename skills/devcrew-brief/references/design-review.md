# Design review of a task brief with a strong model

This is a step in `devcrew-brief`. Run it **after** the applicable first-pass checks (fantasy API, short-brief purity, task consistency and provenance) and before step 7, the checklist. It addresses interactions that mechanical checks miss, including races, contradictions between principles and implementation, inconsistent budgets, and unexamined failure scenarios.

## Purpose

The first-pass checks verify facts: an API exists, the short brief stays readable, and scope agrees across files. A brief can pass all of them yet contain a design error that takes days to correct:

- **The plan contradicts its own principles.** A principle in `architecture.md` requires two concurrent writers to share a limit. Yet `plan.md` gives each worker a private counter. Each file reads coherently on its own and their scopes match.
- **Races across instances.** A Redis lock has a ten-second TTL. A protected external call with backoff can take over thirty seconds. After expiry a second process enters. The slow first process eventually writes stale data with a newer timestamp, overwriting the newer result.
- **Budget arithmetic.** A limit of 80 concurrent operations per pod looks safe against an external limit of 100. Two pods can issue 160 operations.
- **Failure when another process holds the lock.** A process sees another process's lock and exits. A concurrent worker read the old document version before the save completed. The reader sees the old content for the entire refresh interval. All referenced APIs exist and scope is consistent, but nobody walked through the interaction.
- **Operational gaps.** An external call has no timeout, an end-to-end test relies on `sleep(1000)`, or a Map of rate limiters grows without cleanup.
- **Dependency compatibility.** A library exists, but the selected version requires a newer compiler than the oldest version the project supports. Checking the package name alone misses the failed build.

Finding these errors requires **reasoning about how the parts interact**.

## When to run

Run after the applicable first-pass checks succeed and discrepancies are corrected. Reviewing a plan with unverified APIs distracts from design.

**Decide whether independent analysis is warranted** based on unresolved risks. The following signals warrant design attention without automatically requiring a subagent:

- Concurrency: locks, semaphores, rate limits, idempotency, and queues with retries
- Distribution: multiple pods or instances, sharding, and external API quotas
- Money, balances and counters where duplicate application or losing a write would cause an incident
- Timing: TTLs, staleness windows, backoff, and timestamps used to resolve conflicts
- New or upgraded dependencies that can affect supported builds, packaging or execution
- External integrations with retry policies
- Changes to shared types, base classes or inherited signatures with existing consumers

**You may skip this check** for a routine change with none of these risks or shared-contract changes.

## When delegation is justified, use a reviewer qualified for strong reasoning

Use the model configured for this check. The work requires reasoning about timelines, budgets and contradictions between implementation and principles. The project model setting takes precedence over the default.

Use a bounded Codex role with the named brief and configured role model; record the actual role and result.

Supply **all** files containing architecture decisions or implementation sequencing when the task needs them. Both may be sections of a single self-contained file. Provide paths to key source files when the reviewer needs to verify a project dependency, a supported toolchain version, a build flag or the number of concurrent workers.

## Review questions

Here, `architecture.md` and `plan.md` refer to the relevant files or sections in the actual package. Include only existing files in the prompt.

Challenge the design itself. Faithful implementation cannot repair a false premise in the plan. Check decisions against requirement sources and existing project behavior.

1. **Principles, implementation and project conventions.** Compare every numbered basic principle in `architecture.md` with the steps in `plan.md`. A violation blocks the design. Also check the plan against project layering rules in AGENTS.md, code standards and devcrew-review, including conventions omitted from the architecture section. One common failure injects repositories into the application layer despite a convention requiring domain services or ports. The code in plan.md makes repository injection the default and relegates the facade to a caveat. Comparing the plan only with architecture.md misses this. Violating a project convention is also a blocker.
2. **Concurrency.** For every lock, semaphore or idempotency mechanism, identify what it protects. Consider simultaneous callers, expiry halfway through an operation, and a crash before release. Inspect every read-modify-write sequence for lost updates and stale writes.
3. **Budget arithmetic.** Where the design relies on a limit, calculate its worst case. Examples include peak allocation against a memory budget, total backoff against lock expiry and per-worker throughput against a shared quota. Reconcile the numbers stated in the brief.
4. **Failure injection.** For each algorithm step, ask what happens on failure or retry and what the user sees. Examine the steps around side effects: an event may already be published when a transaction rolls back, or a transaction may commit before publication fails.
5. **Resource lifecycle.** Check external call timeouts, cleanup ownership for long-lived maps and caches, and graceful shutdown with operations in flight.
6. **New dependency compatibility.** Verify the selected version against the project's supported toolchains, runtime versions, platforms and build format. Check transitive or peer constraints and licensing when relevant.
7. **Time-dependent tests.** Real sleeps and TTLs can make tests flaky. Use an injectable clock or poll for an observable condition.
8. **Acceptance, steps and estimate.** Each criterion needs an implementation step in the plan and a corresponding item in the estimate. Flag an orphan such as a metrics criterion with no implementation step.
9. **Existing behavioral contracts.** For a changed base class, shared type or inherited signature, find consumers and tests that depend on exception inheritance, response shape, error type or error text. Name the specific tests and scenarios affected. Agreement between new documents alone cannot establish compatibility.

## Prompt template

```
Review this task brief before implementation. Look for design errors that will surface during implementation or in production. API existence and cross-file scope have already been checked. Focus on interactions between components.

Brief files:
- <path/task-NNN-short.md>
- <path/task-NNN.md>
- <path/architecture.md>
- <path/plan.md>

Project context, as needed:
- <project manifest, lockfile or build configuration> — declared dependencies and supported toolchain
- <runtime, deployment or resource configuration> — limits the design relies on
- <other relevant files>

Review the areas that apply to this task:

1. Principles, implementation and conventions: compare every principle in architecture.md with every step in plan.md. Compare the plan with project layering conventions in AGENTS.md or code standards, including rules omitted from architecture.md. For example, an application layer may inject only domain services and ports. A violation of either a principle or a convention is a blocker. Look for defaults in plan.md that violate the rules despite a compliant alternative in a caveat.
2. Concurrency: for each lock, semaphore or idempotency mechanism, consider simultaneous entry, expiry mid-operation and process death. Check for lost updates and stale writes in read-modify-write operations.
3. Budget arithmetic: when the design depends on a resource or timing limit, show the calculation using the brief’s figures. For example, compare peak memory use with the supported limit, or combine retries and timeouts to calculate the longest wait.
4. Failure injection: at each algorithm step, walk through failure and retry. Describe what the user sees. Pay particular attention to event publication and external writes around transaction boundaries.
5. Resource lifecycle: check external call timeouts, cleanup of long-lived structures and graceful shutdown.
6. New or upgraded dependencies: check version, toolchain, platform and packaging compatibility against the actual build configuration and supported environments.
7. Testability: identify real sleeps or time that cannot be injected.
8. Acceptance, plan and estimate: each criterion needs an implementation step and an estimate item.
9. Existing behavior: check shared types, base classes and signature changes against current consumers and tests. Give specific breakages and check whether the brief requires each one.

Findings:

| # | Review area | Finding | Scenario and trigger | Severity | Recommendation |
|---|-------------|---------|----------------------|----------|----------------|

Severity:
- blocker — a design decision to resolve before writing code, such as a violated principle, a race that corrupts data, or an inconsistent budget
- should-fix — a local correction to make in the brief, such as a timeout, resource leak, or acceptance criterion with no step
- nit — an improvement to wording or a potentially flaky test

Rules:
- Give recommendations for design findings. Suggest at least one concrete fix per blocker.
- Provide a timeline for every race, for example “T0: process A reads … T1: the lock expires …”.
- When reporting a budget issue, show the calculation using the figures from the brief. Identify any missing input and how to verify it.
- Report individual findings and options. The brief author owns the design decision.
- For an area with no findings, give one line: “Area N: clear.”

Final verdict: ✓ Ready / ⚠ N findings (B blockers).
```

## After the subagent returns

- **Resolve each blocker before delivery.** Update the design in every affected file and rerun task-consistency-check. If the choice needs product or team input, flag it prominently in a ⚠️ block as a decision required before work starts.
- **Put each should-fix in the brief**, for example as a plan step, test case or estimate item. If you deliberately accept the risk, document it under “Out of scope / Known limitations” with the rationale.
- **Revise the estimate if the review changes the work.** Added metrics or a distributed rate limiter take time. Check any claim that the estimate remains unchanged after two blockers.
- **Verify the reviewer's reasoning.** A reviewer might suggest a distributed lock for an inherently single-threaded task. For every blocker, ask whether the scenario is reachable in this deployment. If a documented guarantee restricts the project to one pod, reject an unsupported multi-pod finding and record the assumption in architecture.md: “Designed for one instance. Revisit X before scaling out.”
- After correcting blockers that changed scope or numbers, **rerun task-consistency-check**. Review-driven edits can introduce new disagreements between files.

## Pitfalls

- **Wait for applicable first-pass checks.** Unverified APIs undermine design reasoning.
- **Require strong reasoning for this check.** Races and budgets require a model capable of complex analysis.
- **Avoid repeating fact checks.** API existence, technical detail in the short brief, and scope consistency were checked earlier.
- **Make acceptance of a limitation explicit.** Document its rationale and the condition for revisiting it in the brief.
- **Always review tasks involving money or balances**, even when they look straightforward. Applying a change twice can turn a simple brief into a costly incident.
