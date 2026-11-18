# Optional machine review gate

Use primarily for large/high-risk reviews when machine approval is useful, or when project instructions require it. Delegation alone does not require a gate. First resolve the target and base; the gate validates a local checkout snapshot, not hosting metadata or an arbitrary commit range. The checkout must represent the reviewed target; do not certify another working tree.

Every expected lens must report status, findings and verification state for the same snapshot. Lenses are coverage responsibilities, not mandatory agents.

Before inspecting the review target, run `capture-state` for the target checkout and retain its JSON object unchanged as the report's `state`. In task-worktree use, pass the host as `--project` and task checkout as `--execution-root`; the state reflects the task tree while the report remains in host checks state. Every expected lens must cover that same state. Finish planned task-document edits first and keep the machine report under host checks state. Do not run concurrent writers during capture or review.

The local review gate accepts only a complete exact lens set with no confirmed or unverified P0/P1 findings, an explicit proposed approval, and a snapshot matching the current project, HEAD and fingerprint. Missing, malformed, failed, stale, or unresolved evidence fails closed. Use:

```text
python3 <plugin-root>/scripts/devcrew.py capture-state --project HOST [--execution-root TASK]
python3 <plugin-root>/scripts/devcrew.py review-gate --project HOST [--execution-root TASK] --report PATH
```

For wave ownership and execution evidence, use `$devcrew-task` and the [runtime contract](../../../docs/runtime-contract.md) in this plugin. Do not publish, merge, or send external messages as part of review.

If the tree changes, review the delta with the affected lenses, preserve which earlier findings still apply, and record that coverage before adopting a new snapshot. A new snapshot alone cannot refresh an old review. The gate verifies local consistency, not whether an agent actually examined every line or chose sufficient lenses.
