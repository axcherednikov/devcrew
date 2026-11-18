# Memory-map synchronization

Project memory maps live in the repository's configured memory location. Add only durable, project-relevant invariants supported by code, tests, or accepted decisions. Keep task status, one-off observations, and unresolved hypotheses in task cards.

Before changing a map, show the proposed trigger, source files, invariant, and reason to the parent. Remove duplicate or contradicted entries rather than accumulating rules. Package-level skill changes require an explicit maintenance task.

Explicit durable user requirements are assessed after one observation and stored in the canonical `engineering-requirements.json` registry. If an applicable instruction/tool already owns the rule, add only a bounded source reference; never copy its wording into a second durable body.
