# Check the short brief for implementation details

Apply this check to the short brief intended for a product owner, issue tracker or other nontechnical audience. Technical details belong in the accompanying implementation brief when they do not explain an outcome the audience needs. Skip this check for a technical-only package.

Compare the short brief with the project's vocabulary. In “use a file watcher with a 250 ms debounce,” the reader usually needs to know that a changed document refreshes automatically. If a specific refresh time is a confirmed requirement, keep that measurable expectation. For a developer-facing library, an option name may itself be the public feature. Determine who uses it before flagging it.

## What to inspect

- Internal symbols, types, application paths, storage keys and dependency names.
- Protocol and scheduling details, error codes, locking primitives or resource limits that do not describe a user-visible contract.
- Code examples or commands intended for the implementer.
- Terms the audience actually uses. Accept them when supported by product copy, documentation or the project glossary. A project-specific meaning takes precedence over a generic dictionary.

For each occurrence record the line, excerpt, reason, and proposed wording. Preserve prerequisites, exclusions, observable failures and accepted numerical criteria. Move useful implementation evidence into the technical brief.

## Checker prompt

```text
Read <short brief> as <intended audience>. Use the project's glossary if available.
Identify details this audience cannot act on. For each, give the line, excerpt,
reason and a plain-language replacement that preserves the requirement.
Keep public interface terms when this audience uses them.
Return either a list of findings or a statement that none were found.
```

An independent check, when appropriate, follows the project's role and cost policy. Review the result against the audience before changing terminology. Record a recurring verified term in the local glossary.
