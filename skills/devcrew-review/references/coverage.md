# Review coverage and findings

Choose checks from the changed behavior and risks, not a mandatory roster of all roles. A small cohesive change can be reviewed inline by the parent with zero subagents, using its diff and targeted test evidence. A review lens is a question to cover, not a required agent. A substantial change benefits from independent bounded correctness/standards and requirements/spec passes. Keep each actual report and explicitly name exclusions; an unavailable environment is unverified, not passed.

## Required reasoning

- Challenge the design as well as the implementation. A design-prescribed bug remains a bug. Follow substantive requirements back to their sources.
- Where affected, cover consumers, migrations, boundary values and failure behavior. For integrations distinguish observed behavior from documentation and assumptions inside the finding itself.
- For money, authorization, concurrency or idempotency, inspect at least one negative scenario that would expose the relevant invariant failing. Once an end-to-end path works, test these high-risk scenarios early rather than waiting until final review.
- After rebase or integration, inspect overlapping files, schema and interfaces semantically; absence of Git conflicts is not proof of compatibility.

## Targeted mutation checks

Use when a green test might not distinguish correct from broken behavior, especially a critical guard or a regression fix. Select a criterion, name the change that would violate it, apply that mutation in an exclusively owned scratch checkout, show the actual diff, and run the targeted test. Restore only the mutation, verify restoration, and rerun the test successfully. Never reset a dirty shared checkout. A surviving mutation means investigate the oracle or redundant protection; do not automatically label the whole suite useless. Record criterion, mutation, command, observed failure and restored result. Mutation testing is risk-based, not required for every field or mechanical edit.

The reviewing agents remain read-only. The parent schedules mutation/test execution separately with write ownership and passes its evidence into review. Missing permission to run a needed experiment is a limitation, not approval.

## Finding resolution and later changes

Each actionable finding must be caused by the reviewed change and identify severity, a reachable trigger/path, consequence, precise changed file/line, supporting code or project evidence, verification status and a direction for correction. Speculation is not a bug: label unverified concerns and assumptions separately; label pre-existing issues separately rather than counting them as regressions. Omit style preferences without practical impact. Reject a finding only with a concrete counterexample, inspected code path or observed test, not because an author disagrees. An accepted residual risk must name scope and justification; it cannot turn a confirmed P0/P1 into `rejected`. Such a finding keeps the gate closed.

Fixes after review are new behavioral changes: rerun their acceptance checks and review the delta before refreshing the snapshot. Before handing off or publishing, compare the actual final state to the approved report; commit/rebase changes HEAD even when content stays equal. Keep findings and coverage per round so a later clean report does not silently discard unresolved issues.

## Finish the first pass

- Inspect the full declared diff and trace the affected requirements, callers and failure paths. A confirmed blocker does not end the pass. Check other changed paths that share its cause or invariant, group symptoms of the same cause, and return the complete set found in that scope. Stop expanding when the affected paths are covered or a material evidence gap is explicit.
- For each finding, explain in plain language what breaks, the trigger, the affected behavior, the supporting file and line or observed check, why the impact warrants its priority, and the smallest fix direction. A traced reachable path can establish a defect without running a test. State the actual verification status. Report uncertain but material risks separately with the missing evidence. Drop style preferences with no practical consequence.
- Use P0 for critical exposure or irreversible harm with immediate or widespread impact, P1 for serious failure of an important workflow or safety invariant under a realistic trigger, P2 for a concrete but bounded regression, and P3 for a minor practical impact. Explain the priority from reachability, impact and recovery. Decide separately whether it blocks acceptance, based on the affected requirement or invariant. Neither disagreement nor lack of a runtime reproduction weakens a finding already established by evidence.
- In ordinary and formal phase machine reports, put blocking findings in `FINDINGS` and evidence-backed optional improvements in `ADVISORIES`. Each advisory has `SUMMARY` and a nonempty `EVIDENCE` array of references. `PASS` allows advisories and requires empty `FINDINGS`. `FAIL` still requires at least one blocking finding and the existing recovery classification. Keep material coverage limitations visible and apply the lifecycle's acceptance rules.
- On re-review, check the corrections, affected behavior and earlier unresolved findings. Report any newly established issue and what evidence or changed behavior exposed it.

## Evidence for behavior and negative claims

For each important assertion, identify the possible defect, input condition, real execution trigger and observable violation before choosing a check. A negative conclusion requires a positive witness that the relevant branch/state was reached for the same run and object. Distinguish command acceptance from reached state: an invoked command without a completed result does not prove its effect. Empty, stale, foreign, missing or truncated observations cannot establish absence of a defect. Fixtures establish starting conditions; the reviewed production path must create the observed result.

For complex paths, cover a small matrix of material outcomes, including mixed success/failure, stale identity and missing observations where applicable. Do not require a full Cartesian product or treat every untested combination as a proven bug. Preserve findings established by static traces; reproduction is useful evidence but is not required for every finding. Use the existing targeted mutation checks when risk justifies proving that a test distinguishes correct behavior from a plausible regression. Mutation checks remain risk-based, not a blanket obligation.
