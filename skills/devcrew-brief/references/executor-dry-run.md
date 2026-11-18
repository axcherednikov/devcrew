# Executor dry run: reading a brief without the interview context

Use this `devcrew-brief` check **last**, after fantasy-api, short-purity, task-consistency and design-review, after correcting their findings, and before step 7, the checklist. Its question is **can someone who missed the interview work from this brief?**

## Purpose

The other checks compare the brief with the code or check its internal consistency. The author, who remembers the interview, may unconsciously supply information that never made it onto the page. A fresh reader needs enough information in the files to work independently.

Typical gaps:

- The interview settled on round-half-to-even, but only the chat records that decision.
- The author knows the webhook follows receipt closure. With no ordering in the brief, the implementer might assume the reverse.
- The author assumes it is obvious that the feature applies only to the admin realm.
- Two items in “What we will do” appear contradictory without the discussion that led to them.

A reader with a **fresh context** can independently test what the text conveys.

## When to run

Review the final text, after all other findings have been addressed. A dry run on an intermediate draft evaluates a package the implementer will never see.

Use an independent reader only for a complex or ambiguous handoff where losing context presents a concrete risk. The main agent can self-check a straightforward brief. Package size alone does not warrant a dry run.

## How to run with a fresh subagent

**Give the subagent only the brief files** and the source access an actual implementer would have. Exclude chat history, interview summaries and hints such as “remember that we discussed X.” Background from the interview defeats the fresh-reader check.

Use a strong reasoning model by default because comprehension requires reasoning. For a small single-file brief (less than three hours, one component), a bounded research agent is sufficient.

Use a read-only product-agent or a generic read-only reader with this brief; never executor-agent or test-runner. Do not implement or execute tests. Record the actual role and result.

## Prompt template

```
You are the developer receiving this task. You did not participate in the discussion. The brief and the codebase are your only sources. Assess whether you could work from them without implementing the task.

Complete executor package (read in the actual order and include applicable contracts and research):
- <path/task-NNN-short.md>
- <path/task-NNN.md>
- <path/architecture.md>
- <path/plan.md>

Consult the source code as an implementer would.

Answer three questions:

1. **First step.** What would you do first? Name the file you would create, the migration you would write or the source you would read. If you cannot identify the first step, report that finding and explain why.

2. **Questions.** What would you have to ask the brief author before or during implementation? Include minor questions. For each one, identify the point at which you would need the answer.

3. **Ambiguities.** Which passages admit more than one interpretation? Give both readings and the one you would choose by default.

Format:

## First step
<concrete description, or “I cannot determine this because …”>

## Questions
| # | Question | Where I would get stuck | Impact (blocks start / arises during work / cosmetic) |
|---|----------|-------------------------|-------------------------------------------------------|

## Ambiguities
| # | Passage | Reading A | Reading B | My default choice |
|---|---------|-----------|-----------|-------------------|

Rules:
- Assess clarity and completeness. Leave the accepted design decisions in place and omit architecture suggestions.
- List every case where you would have to guess the author's meaning.
- Omit questions that you answered by reading the source. A real implementer can find those answers too. Include questions that remain open after checking the code.
```

## Triage the response

Classify each question and ambiguity and apply its corresponding fix:

1. **The files contain the answer, but the reader missed it.** Improve the structure. For example, the order of steps may be in architecture.md when the implementer looks in plan.md. Move or repeat the answer where it is needed.
2. **Only the interview contains the answer.** This is the knowledge gap the dry run is designed to expose. Write the decision in the appropriate file. Put a business decision in the main brief under “Rationale” or “Context,” and a technical invariant in the architecture or plan.
3. **No source answers the question.** The interview missed a decision. Resolve it with the user or flag it as “Product decision required before work starts.” See anti-pattern 6 on unresolved business questions.

If the reader would start with the wrong action, the implementation order in plan.md is probably unclear or at odds with the document structure. Correct the “Implementation order” section.

**Ready for handoff:** Triage leaves no recurring questions that block the start. Questions that arise during work are either answered in the files or deliberately left to the implementer because the code demonstrably answers them.

**Evaluate both empty and noisy reports.** An empty report on a large task may mean the reader guessed. Check the proposed first step. A confident, correct step supports the result. If the step is vague, rerun with a more explicit instruction to surface guesses. When the files already answer a question, improve discoverability as in case 1 rather than expanding the brief unnecessarily.

## Pitfalls

- **Keep interview context out of the assignment.** Even a summary provided “for reference” undermines the independent perspective that the author cannot recreate through self-review.
- **Wait for earlier checks.** Invented APIs or inconsistent files generate questions that cheaper checks could have resolved.
- **Stay within the reader's role.** Disregard suggestions such as “use a queue instead.” The rationale section records the decisions already taken.
- **Triage before adding prose.** Case 1 needs a structural change, case 3 needs a decision, and only case 2 needs previously missing text. A brief that explains everything indiscriminately becomes hard to read and generates more questions.
- **The author's sense that a task is obvious can mask a real handoff risk.** When that risk warrants a dry run, apparent familiarity is a poor reason to skip it. This check needs little preparation because the prompt does not require a list of claims or review areas.

## Rehearse the complete package

If the implementer receives multiple documents, provide every one in its actual reading order, including the contract, design and plan. The reviewer works read-only and answers “Could I complete the next step without a missing decision?”

- Identify the exact stopping point and ambiguous passages, with two possible readings for each.
- Check dependencies in sequence. The input to a stage must exist by the time that stage begins.
- For claims such as “all consumers,” “unused everywhere” or “safe to delete,” compare the listed consumers with real references. An internally consistent list can still be incomplete.
- Recalculate values that will be copied into code or fixtures. Include the commands used.
- Find acceptance criteria without observable checks and required fixtures that are missing.
- For a claim that a change breaks existing behavior, name a specific file, test or consumer and the scenario affected.

The rehearsal evaluates whether the instructions are executable. `fantasy-api-check` verifies the behavior of functions the plan relies on. `provenance-check` verifies requirement sources. `design-review` evaluates whether the design itself is sound. Each needs its own evidence.
