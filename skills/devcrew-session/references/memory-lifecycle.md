# Memory lifecycle

Read `.codex/devcrew.json` for the memory location. Use `python3 <plugin>/scripts/devcrew.py memory-audit --project <project>` at a relevant session boundary when memory is growing or conflicting. It inventories files, exact-content duplicates, skipped symlinks/special files and optional size/count warnings. It never deletes, merges or reads outside symlinked memory paths. Run on a quiescent trusted directory; this is not an atomic snapshot against concurrent replacement. Thresholds are attention prompts, not correctness limits.

Read only relevant lessons and their sources. Classify each candidate as current, superseded with evidence, duplicate, or unresolved. Age alone does not make a lesson stale. Prefer a small index pointing to detailed sources. Preserve decision history when replacing guidance; fix references when consolidating. Do not automatically delete files from an inventory result.

For each failure distinguish a missing rule from an existing rule that was ignored. Strengthen the application/check of an existing rule before adding another copy. Capture the concrete trigger, evidence, bounded lesson, applicability, owner and verification. Prefer a deterministic check for a repeatable invariant. Do not promote a one-off workaround into a universal rule. Parent owns the final memory index update; workers return proposals. Reconcile conflicting lessons with current code and user decisions, never silently select the newest text.

See [estimate calibration](estimate-calibration.md) for measured planning feedback.

Explicit user engineering requirements have a separate one-observation intake path and are not subject to the retro recurrence threshold. Use the canonical configured `engineering-requirements.json` registry and source references described in [engineering requirements](../../devcrew-task/references/engineering-requirements.md); do not copy rules already owned by project instructions or tools.
