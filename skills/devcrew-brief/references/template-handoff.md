# template-handoff.md

Use this prompt to hand an approved brief to the implementation agent, or as a cover note for a human implementer. It completes Step 8, preparing the implementation prompt. Fill it out once the brief passes the Step 6.5 checks and the Step 7 checklist.

The brief gives the implementer verified facts and boundaries. The implementer returns a report using `template-implementation-report.md`. That report supplies the observations needed by `retro.md`.

---

Implement <TASK-NNN / slug> according to the brief.

## Reading order

1. `<path/task-NNN-short.md>` — why the task exists (2 minutes).
2. `<path/task-NNN.md>` — what to build, the rationale, scope, and acceptance criteria.
3. `<path/architecture.md>` — modules, principles, and file layout.
4. `<path/plan.md>` — working plan with steps and code sketches.

<For one- or two-file briefs, include only the applicable documents.>

Also read the relevant entries from “What to read before starting” in architecture.md:

- `<path>` — <reason>
- `<...>`

## Blockers

⚠️ <Restate the prerequisites and their current status. For example, “TASK-IDb is present in the current revision; work can begin,” or “TASK-ID has not landed; wait until …”. If there are none, write “No blockers. Work can begin.”>

## Verified facts

The interfaces, data formats and behaviors cited in the brief were **checked against the codebase** at commit `<hash>` on branch `<branch>` on <date>. If the code has changed since verification, record the discrepancy in the report and implement against the current code within the agreed contract.

## Departures from the brief

The brief defines the **contract and scope**. Follow project conventions when choosing names, locations, and implementation style.

- **Implementer decisions.** Choose class and method names where the brief gives examples (“or equivalent”), the structure of private methods, and the implementation order recommended in architecture.md. Add one line to the report for each departure from a recommendation.
- **Return to the brief author before proceeding.** Ask before expanding scope (e.g. adding bulk operations), implementing excluded work, dropping an acceptance criterion, changing the agreed contract (for example, output format, public signature or interaction), or adding dependencies beyond those specified.
- **Unanticipated in-scope improvements.** If you find a better approach that respects the brief but was not explicitly considered, implement it and record the deviation and rationale. The brief author uses this in the retrospective.

## Definition of done

1. Explicitly check each acceptance criterion in `<task-NNN.md>`.
2. Run the checks from plan.md: `<lint / build / test / e2e commands>`. Confirm the results.
3. Complete `template-implementation-report.md` and save it as `<task directory>/report.md`. The report is required to hand over completed work and improve future briefs.

---

## Guidance for the author

- **Reading order.** Give agents and human implementers a route through the brief: the short version provides context, the main document defines the task, and the plan guides implementation.
- **Verified facts.** Identify the commit and branch checked in Step 6.5. The implementer needs a baseline for recognizing when the brief has become stale.
- **Blockers.** State their status **at handoff**. “Depends on TASK-IDb” alone makes the implementer rediscover whether work can start.
- **Departures.** Spell out the decisions the implementer can make. Otherwise they may copy example names verbatim (anti-pattern 15) or change scope without authorization. Tailor these boundaries to the task. When the API contract is central, explicitly identify the parts that require approval to change.

## Common mistakes

- Repeating the entire brief in the handoff. Duplicate descriptions drift as soon as a source document changes; the task consistency check looks for this class of error.
- Handing over prerequisites without a current status or verified facts without their baseline.
- Dropping the report to save time. The retrospective and estimate calibration both depend on that evidence.
