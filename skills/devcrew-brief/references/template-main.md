# template-main.md

Use the main brief to explain the task to the implementer. Describe the technical contract in enough detail to explain what must change and why. The developer chooses names, placement, and style within the project's coding standards.

Typical length: 150–300 lines. Large tasks may need more.

---

<Title> (<back | front | fullstack | infra>)

<⚠️ Depends on … — list specific prerequisites, including service, method, or task names.>

<⚠️ Coordinate with … — include when another team or concurrent task is affected.>

<⚠️ Relevant context, e.g. an empty database makes a data migration unnecessary, or deployment must respect peak load windows.>

## Current behavior and history

<For a change to existing behavior, describe how it works today and what problem it causes. Omit this section for a new system without relevant history.>

## Goal

<Describe the resulting integration contract, system behavior, or observable outcome in 2–5 sentences.>

## Sources of requirements and decisions

Assign an ID to each material requirement. Use the labels `[customer]`, `[from code]`, and `[assumption]` as defined in the [provenance check](provenance-check.md). Replace this note with a table mapping each ID to the requirement or decision, its source, and the supporting evidence. For a large brief, reference a shared `decisions.md` instead of duplicating the log.

## Decisions and questions

### CONFIRMED DECISIONS

- <Decisions the user confirmed or facts established in the repository.>

### OPEN MATERIAL QUESTIONS

- <Questions whose answers could materially change the task. Identify any dependencies that determine their order.>

### IMPLEMENTATION ASSUMPTIONS

- <Justified internal technical choices within the confirmed boundaries. Give the facts, the engineering conclusion, and its rationale. Record unverified hypotheses separately with the evidence they still need. They cannot replace requirements or authorize dependent implementation. Material unresolved requirements and risks belong under OPEN MATERIAL QUESTIONS.>

### ADR STATUS

- <none | recommended | proposed | accepted/existing. Link the existing or accepted ADR when applicable.>

A recommendation becomes a requirement only after confirmation. Attribute internal technical choices to their actual source. Give the rationale for a new ADR and follow the project's approval process and the [ADR criteria and format](adr.md).

### GRILL RESULTS (if the optional critique was used)

- Confirmed decisions: <...>
- Decisions changed after the critique: <... | none>
- Open questions: <... | none>
- Remaining material risks: <... | none>

## Scope of work

### <Affected capability or component>

<Describe the existing contract and the observable change. Repeat for other affected parts. Cite the relevant project source or decision.>

## Tests

- <Normal use through the actual public interface: starting state, action and observable result>
- <Relevant failure or boundary condition and its expected effect>
- <Compatibility or recovery case, if the change affects existing consumers or saved data>
- <Project-declared checks and any justified manual verification>

## Design rationale

<For each significant architectural choice, state the trade-off, the alternatives considered, and why they were rejected.>

**<Decision 1>.** <Rationale, alternatives, and trade-off.>

**<Decision 2>.** <Rationale, alternatives, and trade-off.>

## Acceptance

- <Observable criterion 1 and what the reviewer should see>
- <Criterion 2>
- <Criterion 3>
- <Passing tests are the final item. Include criteria for the behavior itself.>

## Out of scope

- <Item 1 and its destination: another task with an ID or slug, deferred work, or a decision to revisit when a use case emerges>
- <Item 2>

## Estimate

<X–Y hours, broken down by component>

- <Component 1> — <N hours>
- <Component 2> — <M hours>
- <Tests> — <P hours>

---

## Guidance for the author

### Prerequisite, coordination, and context notices

- **Prerequisites.** Name the dependency precisely. For example, “Requires `Y.foo(...)` from task Z, already merged into dev.”
- **Coordination.** If another team or concurrent task consumes a contract that will change, identify who needs to agree on it.
- **Context.** Include relevant legacy behavior, lack of data, load patterns, or production constraints.

### Organizing the scope of work

Group changes around how the target project works. A UI change might cover an interaction and the state behind it; a library change might cover a public operation and compatibility with its callers. Include only components and boundaries touched by the task.

### Algorithms

Use a numbered sequence or pseudocode when ordering matters. For example, a converter validates input, writes a complete temporary result and replaces the output only after success. State the observable result if validation or writing fails.

### Tests

Identify the cases that distinguish correct behavior from failure, including edge cases. List negative scenarios separately. Generic entries such as “unit test of the service” or “end-to-end test of the endpoint” give the implementer too little information.

### Design rationale

Even a choice among only two or three alternatives needs an explanation. Use this form:

> **<Decision>.** <The choice made.> <Alternatives considered and reasons for rejecting them.> <The main benefit and what this choice costs.>

The trade-off should still make sense to a reader three months later. Recording it helps them decide whether new evidence warrants revisiting it.

### Acceptance

Write criteria someone can verify. Examples:

- “The migration applies cleanly to development and test databases.”
- “The first X request returns the expected value in field Y.”
- “Repeating request Z is a no-op and creates no duplicate.”
- “Existing end-to-end tests still pass.”

### Out of scope

Give a destination for each exclusion:

- “<Work>” — <separate task / next release / beyond the MVP / open a task when the requirement arises>.

### Estimate

Give a component breakdown so the estimate can be discussed and calibrated against actual effort.

## Common mistakes

- Specifying the full implementation, including exact class names, method signatures, and lines of code.
- Treating suggested file or directory names as mandatory.
- Listing an exclusion without saying when or where it will be addressed.
- Giving an estimate without a breakdown.
- Omitting the rationale for an architectural choice.
- Using “everything works” as an acceptance criterion.
