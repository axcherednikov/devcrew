# Common mistakes in task briefs

Use these examples to identify errors in a draft, understand the effect on the reader, and choose a correction.

## 1. Technical details in short.md

State the user-visible change in the short summary. Move field names and parsing details to the technical brief. For example, “readers can reopen the last document” explains the feature without prescribing the settings file format.

## 2. Duplicating an existing interface

Search the existing subsystem for a suitable interface or extension point before specifying a new one. Link the declaration and an actual caller; a similar name alone proves little.

## 3. Defensive task splitting

Keep the smallest complete change together. A command-line option without the action it triggers cannot demonstrate acceptance. Split work when the earlier result is useful or independently verifiable.

## 4. Exclusions without a destination

Give each exclusion a destination: a linked task, a later decision when demand exists, or explicit removal from scope. “Bulk edits later” leaves the owner and criteria unclear.

## 5. Unexplained estimates

Break the estimate down by the actual work, including verification and unknowns. For a file importer this might be format detection, error recovery and compatibility fixtures. Use the target project’s evidence when available.

## 6. Open business questions left unresolved

An implementer cannot infer what should happen to an unsaved document on close. Check project precedent and ask the owner if the desired outcome remains unclear. Only the dependent work needs to wait.

## 7. Vague dependencies

“Needs a parser” is too vague. Identify the actual input format, the existing reader and its verified return or error contract. If the reader has yet to be written, link the task that provides it.

## 8. Burying a blocking dependency

Place a missing prerequisite above the goal and identify the affected steps. For example, name the format version the reader must accept before turning on the new writer.

## 9. Stubs without a plan for the real implementation

A fixture can verify a consumer but cannot establish that another component implements the protocol. State who will provide the real component and what check proves the connection works.

## 10. Premature generalization

A new extension interface should solve an identified need. If the project uses one format, implement that format directly. If two confirmed formats must share the same consumer, define the common behavior from actual examples of both.

## 11. Test bullets without task scenarios

Specify the initial state, action and expected outcome. For an export command, check a valid input, a malformed input and a destination that cannot be written. Verify exit status, diagnostics and whether partial output was left behind.

## 12. “Everything works” as acceptance

Passing the test suite is evidence. Add an observable acceptance criterion, for example that an older reader opens a new file and preserves fields it understands.

## 13. Missing rationale for a design choice

Explain why an architectural choice matters here. For example, incremental parsing can bound memory use on large files, at the cost of carrying state between chunks. Link the requirement that justifies that trade-off.

## 14. Leaking domain boundaries

Use existing public interfaces between components. Editing the internal storage of another component can bypass validation or lifecycle assumptions. Trace the supported operation and its side effects.

## 15. Prescribing incidental implementation details

Specify the outcome and confirmed constraints. Prescribing a private helper name, directory or representation narrows valid implementations unless compatibility or repository convention requires it.

## 16. Making a noncompliant design the default

Validate proposed plans against existing project constraints before offering them as the default. If published artifacts must have deterministic contents, a writer that inserts the current time violates the contract even if it simplifies implementation.

## 17. Specifying a mechanism in place of a property

State the property that must hold across failure. For a file export, users should see a complete previous or new result after interruption. Atomic replacement is one possible mechanism when the target filesystem supports it; verify the observable property under failure.

## Before handing off the brief

Check scope, verified dependencies, open decisions, acceptance evidence and the destination of exclusions. Keep examples clearly illustrative and use the actual project vocabulary in the brief.
