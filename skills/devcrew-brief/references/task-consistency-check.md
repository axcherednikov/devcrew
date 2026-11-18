# Task brief consistency check

Run this part of `devcrew-brief` before the step 7 checklist. For a two- or four-file brief, compare the documents that exist (`<slug>-short.md`, `<slug>.md`, `architecture.md`, `plan.md`). Each must describe the same task. For a one-file brief, check any scenarios repeated across sections.

## Why check consistency?

The API fact-check catches invented APIs; the short-brief check catches implementation details in a business summary. Neither detects a brief that promises one thing in the summary and implements something else in the plan. The implementer needs to know whether a feature belongs in this task or in a follow-up.

Examples of inconsistencies:

- Four files for TASK-ID described three scopes: “view only,” “view and rotate,” and “view, rotate, and revoke.” Revocation moved in and out of scope depending on the file.
- The architecture for a file viewer included editing, which belonged to a separate task. The short brief covered only reading.
- The short brief promised two commands. The plan listed three, one of which belonged to the next task.
- Only the main file identified a prerequisite (`⚠️ Depends on TASK-IDb`). Someone following the architecture or plan alone would miss the blocker.
- The short brief estimated 5–6 hours. After an edit, the full brief estimated 7–9.
- The main brief explicitly excluded bulk operations. The plan still included an implementation step for them.
- A sync tool promised to limit **all** transfers. Acceptance covered downloads only, so uploads bypassed the limit. Trace each part of a broad goal to a separate observable check.
- In a single task document, the flow and unit tests promised that exporting an existing file would leave it unchanged. The end-to-end test and acceptance instead expected an overwrite. The implementer silently picked the latter. Comparing files alone would have missed this contradiction.

These mismatches often appear when an edit changes scope in one file and the other files retain the old boundary.

## When to run the check

Check once the files are written and before the step 7 checklist. The main agent checks the affected repeated claims. Delegate only if a separate reading addresses a concrete risk in a complex package.

For a self-contained `<slug>.md`, skip cross-file checks 1–10. Check 12 applies when the same scenario appears in several sections. Recheck affected claims whenever scope changes, such as moving a feature to a follow-up, merging subtasks, or shifting a boundary.

## Delegation

If delegating, use a bounded research agent. Comparing claims along specific dimensions is structured work, but it requires more than a string search. Tell the agent exactly what to compare to obtain a specific result.

Use a bounded Codex role with the named brief and configured role model; record the actual role and result.

## What to compare

1. **Included scope.** Do all files agree on what the task includes? Investigate a feature that the main file promises and the short file omits.
2. **Excluded scope.** Does the plan implement something that another file excludes? Look for steps implementing a feature marked “deferred to TASK-XXX.”
3. **Prerequisites.** Do blockers appear in each appropriate ⚠️ block, including the architecture and plan?
4. **Public operations.** Do the documents agree on the operations in scope, such as a library call, command, UI action or endpoint? Verify any promised count, inputs and outputs.
5. **Estimates.** Do the ranges in the short and full briefs agree? Does the component breakdown support the total?
6. **Affected components and data.** Do the documents agree on changes to components, formats or schemas the brief actually names?
7. **Acceptance and tests.** Does the plan cover each acceptance criterion with a test?
8. **Ownership.** Does any item belong to another task or epic, such as editing in a read-only file viewer task? An item present only in one file is a reason to investigate.
9. **Numbers.** Compare repeated limits, counts, timings and outcomes using the same units. For example, a limit of one retry allows two total attempts.
10. **Acceptance, steps, and estimates.** Can you identify both an implementation step in the plan and its contribution to the estimate for each criterion? Flag orphan criteria such as metrics promised in acceptance with no step or estimate for them.
11. **Goal and acceptance.** For each promise under Goal or In scope, find an acceptance item or an explicit statement that verification is outside this task. Pay attention to quantifiers: “all calls to X go through Y” and “every operation is logged” describe a class of cases. Checking only one method can quietly narrow the resulting behavior. Review this separately from the general prompt using the focused procedure below, usually in the main agent.
12. **Sections within a file.** For every repeated scenario, compare the flow, applicable tests and acceptance pairwise. Check the observable result at the actual interface: a returned value, exit status, UI state, file contents, or protocol response. Flag conflicting expectations such as overwriting versus preserving an existing output. Checks 1–10 compare files and cannot detect a discrepancy confined to one file.

## General prompt template

```text
Check whether the documents in this task package agree. They must describe the same scope. Compare the claims and report discrepancies without architectural recommendations.

Files:
- <path/task-NNN-short.md>
- <path/task-NNN.md>
- <path/architecture.md>
- <path/plan.md>

For each of the following, determine whether the files agree. Locate both sides of any discrepancy.

1. Included scope: do the files include the same work?
2. Excluded scope: is anything excluded in one file still being implemented in another?
3. Prerequisites (⚠️): do blockers appear in every applicable prerequisite block?
4. Public operations: do the documents agree on the commands, interactions, calls or other operations they mention? Verify names and counts where promised.
5. Estimates: do the short and full briefs agree on the range in hours?
6. Affected components, formats and schemas: are the declared changes consistent wherever the documents mention them?
7. Acceptance and tests: does each criterion have a corresponding test?
8. Ownership: is an item in one document actually part of another task or epic?
9. Numerical parameters: do retries, TTLs, limits, timeouts, and batch sizes agree across files?
10. Acceptance, steps, and estimates: does every criterion have an implementation step in plan.md and a share of the estimate?
12. Within <path/task-NNN.md>, list each scenario repeated across flow, applicable tests or acceptance. Compare the observable behavior, including any output, error, state change or limit the scenario promises. Flag discrepancies and quote both lines with line numbers. Do this in addition to the cross-file comparisons. Check 11, Goal and acceptance, uses its own prompt.

Include ONLY discrepancies in the table. Give each discrepancy one row. Do not repeat a single fact under several types or add rows for matching claims.

| # | Check | File or section A says | File or section B says | Type | Blocker? |
|---|-------|------------------------|------------------------|------|----------|

Types: scope-mismatch, excluded item reintroduced, missing dependency, estimate mismatch, operation count, unrelated scope, acceptance without a test, numerical parameter mismatch, acceptance without a step or estimate, goal without acceptance coverage, sections of the same file disagree, other.

Mark “yes” for a blocker when the discrepancy changes the task boundary or behavior. Examples include operation counts, estimate mismatches, missing dependencies, reintroduced exclusions, unrelated scope, conflicting inclusions and exclusions, inconsistent retries/TTLs/limits, criteria without an implementation step, goal promises narrowed or lost in acceptance, and different observable results for the same scenario in different sections. Use “low” for advisory issues such as differences in wording or a minor omission in a step description.

Conclude with one verdict:
- ✓ Consistent — no discrepancies.
- ⚠ N discrepancies (H blocking) — at least one row.

Do not qualify a “Consistent” verdict with exceptions. Any discrepancy gets ⚠. Blockers prevent delivery; a report containing only low-severity items is advisory and permits delivery.

Rules:
- Compare what the files say. Do not infer the author's intent or propose fixes.
- Compare the disposition of each item across the whole document. The same exclusion can be expressed in different sections or words. If both files agree that the item is deferred, permanently excluded, or owned elsewhere, the omission of a specific follow-up filename (e.g. task-124b.md) from the short brief is not itself a discrepancy.
- Allow implementation choices. The architecture or plan may let the implementer pick service X or direct repositories, or treat class names as examples, even when the main file is more prescriptive. A private service or the internal method that fills a field is usually an implementation detail. Flag a change to the task boundaries, or a promised step, public operation, or field missing entirely from another document that should cover it.
- Use “excluded item reintroduced” when the plan or architecture implements a feature assigned to a separate task. This is a blocker.
- Reserve “unrelated scope” for work belonging to another epic, such as editing in a read-only file viewer task. Differing levels of detail about excluded work do not meet this condition.
- Allow content specific to a template, such as an ASCII diagram in the architecture. The short brief can be shorter than the main file.
```

## Check 11: focused comparison of goal and acceptance

A broader pass can miss this discrepancy in a complex brief. Perform the focused check yourself unless an independent reading addresses a specific remaining risk. Use this prompt if you delegate:

```text
Compare the goal and acceptance criteria of this task on this dimension only.

Files:
- <path/task-NNN.md> (primary; Goal and Acceptance sections)
- <path/plan.md> (supporting)

1. List each distinct promise from Goal on a separate line. Preserve quantifiers, including “all,” “each,” “single,” “any,” “always,” and “completely.” If every call to X must go through Y, the promise applies to each such call.
2. For each promise, locate its acceptance criteria. Compare their scope. Do they cover every case in the quantified promise, or only a specific method, instance, or scenario?
3. Report only coverage gaps:

| # | Goal promise (quote) | What acceptance covers | Gap | Blocker? |
|---|----------------------|------------------------|-----|----------|

Classify the gap: no acceptance criterion; narrowed from a class to a single case; partial coverage with the rest unmarked.
Mark “yes” when acceptance reduces a quantified promise such as “all,” “one shared,” or “every” to a single case. Implementers may reasonably follow acceptance and silently narrow behavior. Use “low” for minor omissions.

Exclude promises explicitly marked “not verified in this task” or moved to Out of scope.

Verdict: ✓ Full coverage / ⚠ N promises with gaps (H blocking).
```

Resolve each blocking quantifier mismatch. Expand acceptance to the full promise, narrow the goal to what is actually verified, or explicitly state “not verified in this task; see <task>.” Any of these outcomes makes the boundary visible.

## Resolve findings

- **Scope mismatch.** Establish the correct boundary, asking the user if it remains unclear, and synchronize every affected file. Resolve it before handing over to the implementer.
- **Missing dependency.** Add the ⚠️ block to each applicable file in the package, including the main brief, architecture, and plan.
- **Different estimates.** Reconcile them to one range and check the component breakdown against it.
- **Unrelated scope.** Remove work belonging to another epic. If it genuinely belongs here, carry it through all the files and add it to the ⚠️ coordination block.
- **Acceptance without a test.** Add a case to `plan.md`, or remove the criterion if it belongs outside this task.
- **Sections disagree.** Select one response. If it requires a business decision, ask the owner, for example whether repeating an operation should succeed or report a conflict. Update every section that mentions it. A correct acceptance section alone leaves implementers reading contradictory flows and tests.
- **Check a clean report after major scope edits.** When the checker reports “✓ Consistent,” check items 1, 2, and 8 yourself if the scope has just changed substantially. Different wording can conceal a mismatch.

The package is ready when a reader can open any of its documents and infer the same boundaries. Unresolved scope mismatches across four files still block delivery, regardless of other passing checks.

## Common mistakes

- Checking multiple files when the brief consists of one. Check 12 still applies to repeated scenarios within that file.
- Asking for a vague consistency check. Specify the comparisons so that the report can name a discrepancy.
- Selecting a stronger reasoning model for this structured comparison when the checker has the explicit comparison criteria.
- Treating the short-brief check as a substitute. It checks a business-facing file for implementation details; this check compares scope and behavior across files and sections.
- Skipping the check after changing scope. Updating boundaries in one file at a time is a common source of drift.
