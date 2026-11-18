# template-architecture.md

Use this architecture document for the implementer when preparing a four-file brief. Describe the modules involved, file layout, design principles, and exclusions.

Typical length: 150–400 lines. A simple feature generally takes 150–200 lines, a feature with migrations and refactoring 250–350, and a large cross-team redesign up to 500. Use more space when clarity requires it.

---

# Architecture for <slug or task name> — file layout

Describe the target location, responsibility, and reason for each file. Implement the agreed structure. Alternatives already considered and rejected should not be reintroduced without justification.

<⚠️ Depends on … — restate the prerequisites.>

<⚠️ Relevant context>

## Where the logic lives (read first)

<In 2–5 paragraphs, explain which modules or layers are affected and why. Give the implementer an overview before they open individual files.>

## Design principles (read before implementation)

1. **<Principle 1>.** <Explain why it applies.>
2. **<Principle 2>.** <…>
3. **<Principle 3>.** <…>
4. **<…>**

Use these principles to:

- Record nonobvious decisions that are easy to violate, e.g. “Reuse the existing X interface.”
- Explain trade-offs the implementer might overlook.
- Identify the project patterns on which the design relies.

## File layout

<List files with NEW, UPDATE, or DELETE markers. Explain each file's purpose and placement.>

### <Group 1, e.g. viewer or command entry point>

`<path/to/file>` — NEW. <Contents, reason for placement, and relationships.>

`<path/to/file>` — UPDATE. <What changes and why.>

### <Group 2, e.g. shared library or external integration>

`<path/to/file>` — NEW. <…>

### <Tests>

`<path/to/test/file>` — NEW / UPDATE. <Scenarios covered.>

## Module dependencies

```
<module A> (extended by this task)
    ├──> <module B> (dependency) — capability used
    ├──> <module C> — capability used
    └──> <…>
```

<Explain any nontrivial dependency direction. For example, “Module A is independent of module X because …”.>

## Exclusions and task-specific pitfalls

<Name approaches that would be tempting in this particular task and explain the problem each would introduce.>

- Avoid <specific approach> because <reason>. Although it appears simpler, it would <increase complexity, violate a contract, etc.>.
- Avoid <another specific approach>.
- Remove <stub X> or record an explicit TODO linked to a future task.

## Suggested implementation order

1. <Step 1>
2. <Step 2>
3. <…>
4. Run tests: `<commands>`

The implementer may choose a better sequence when they have a reason. Record the change and rationale in the implementation report.

## What to read before starting

- `<path/to/document/or/code>` — why it matters.
- `<...>`

---

## Guidance for the author

### Where the logic lives

Write for someone new to this area, even if they are an experienced developer. In 2–5 paragraphs describe the domains and their interactions. The file list comes later.

### Design principles

State task-specific rules an implementer can apply, for example:

- “Store X is the sole point of contact between Y and Z.”
- “Service A has no dependency on an entity from domain B.”
- “`branchId` is now nullable. Only X manages it; sync cannot update it.”
- “Extend the existing Y interface rather than adding Z.”

Number the principles so the team can refer back to them, e.g. “see principle 3.”

### File layout

Group files by responsibility. Mark each change:

- **NEW** — create the file.
- **UPDATE** — modify an existing file.
- **DELETE** — remove the file, when applicable.

For each file describe:

- **Contents:** its primary responsibilities.
- **Location:** why it belongs in this module instead of a neighboring one.
- **Relationships:** what it depends on and who uses it.

### Module dependencies

Use a text diagram to show which modules depend on which others. This is particularly helpful for dependency inversion involving ports and adapters, DI tokens, or similar patterns.

### Exclusions

Use concrete examples that explain why a plausible approach would be wrong for this task:

- “Remove deprecated `oldMethod` completely. Keeping it as a fallback would preserve the behavior being retired.”
- “Extend the existing `IExisting` interface instead of creating another interface.”
- “Changing the signature of `oldMethod` would break its other callers. Add a separate method for the new contract.”

### Implementation order

Recommend an order when one step depends on another, e.g. introduce a compatible reader → update the writer → verify existing files.

### What to read before starting

Identify 5–10 relevant files. Include neighboring services, examples of the project's style, and existing tests that demonstrate the pattern.

## Common mistakes

- Putting detailed implementation instructions, such as exact class names or signatures, here. Keep those in main.md and plan.md.
- Stating generic principles instead of rules specific to this change.
- Listing file paths without explaining their placement.
- Showing that A depends on B without saying what A needs from B.
