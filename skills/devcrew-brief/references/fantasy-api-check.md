# Verify interfaces referenced by the brief

Use this check after drafting and before handoff when the brief relies on an existing interface, tool, dependency, data format, or behavior. New functionality must be clearly identified as proposed. A proven precedent can still have a different contract from the one this task needs.

## Gather claims

Read every file the implementer will receive. List each claim about an existing symbol, option, configuration key, path, field, event, supported version, or behavior. Include conditional claims such as “use the cancellation option if it exists” and placeholders that conceal an unverified value. For numeric claims, record the search scope, command and actual output.

For each claim, locate the declaration **and a real consumer**. Start from the repository's entry points, manifests, lockfiles, build configuration, tests and published contracts. Follow generated bindings, re-exports and adapters to their source. Search by identifier across the relevant tree before concluding that a conventionally named directory should contain it.

## Check behavior as well as spelling

- Verify arguments, defaults, return type, error behavior, ownership and lifetime. A method called `save` might enqueue work rather than guarantee a completed write.
- Trace the writer as well as the reader of a value. An optional field can exist yet never be populated on this path.
- For a command, verify that the installed or supported version accepts the option. For a library, check compatibility with the target's configured runtime, build and packaging. A registry listing alone cannot establish this.
- For integrations, distinguish documented behavior, fixture behavior and a response actually observed under authorized conditions. State unavailable checks.
- For a guarantee such as validation or deduplication, trace the implementation to the point that enforces it and identify a distinguishing input.

## Handoff for a bounded check

If independent verification is warranted, give the checker the specific files, precise claims and evidence scope. Use the project's role and routing policy. A self-check must be identified as such.

```text
Verify the existing interfaces and behaviors named in these brief files: <paths>.
Claims: <numbered list>. For each, return confirmed, mismatch, missing, or
unverified, followed by the observed contract, source location and any
executed check. Distinguish proposed behavior from existing behavior.
```

For a mismatch, update every affected artifact and check the amended claim. For a missing interface, choose an evidenced alternative, explicitly scope new implementation, or raise the unresolved dependency. Remove unsupported counts, options and guessed values before handing the brief to an implementer.
