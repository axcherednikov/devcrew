# template-implementation-report.md

The implementer, whether a person or an agent, fills out this report and saves it as `report.md` in the task directory. It provides **structured input for `retro.md`**. Sections 2–3 identify gaps in the brief that the author can classify. Section 4 provides data for calibrating estimates.

Typical length: 15–40 lines. Keep empty sections and explicitly say “None.”

---

# Report for <TASK-NNN / slug>

Branch or PR: `<...>`. Checks: `<lint/build/test/e2e>` — passed / <what failed and why>.

## 1. Acceptance

- [x] <Criterion 1 from the brief — met>
- [x] <Criterion 2>
- [ ] <Criterion N — unmet because …>

## 2. Departures from the brief

<Record each departure from a recommendation, what you did instead, and why. Include unanticipated in-scope decisions from the handoff. If there were none, write “None. Implemented as specified.”>

- <The brief recommended X> → <implemented Y> because <reason>.
- <...>

## 3. Errors and gaps in the brief

<This section is the primary input for the retrospective. Include incorrect, missing, or ambiguous details, even when you resolved them in five minutes.>

- **Incorrect:** <The brief said X; the actual behavior was Y. Record time lost.>
- **Missing:** <What you needed to know and where you found the answer: code, someone you asked, or your own implementation decision.>
- **Ambiguous:** <The passage with two possible meanings and which interpretation was correct.>

<If you found no issues, write “No findings.” This is also evidence for evaluating the checks.>

## 4. Effort in hours: estimate and actual

| Component | Estimate | Actual |
|-----------|----------|--------|
| <Component from the brief's breakdown> | <N hours> | <M hours> |
| ... | | |
| **Total** | <X–Y hours> | <Z hours> |

<In one line, explain the largest difference and its cause.>

## 5. Findings outside the task

<Record technical debt, bugs in neighboring code, or ideas discovered outside scope. Leave implementation to follow-up work and identify where the finding belongs.>

- <Finding> → <suggested action: open a new issue / add to TASK-NNN / take no further action>.

---

## Guidance for the implementer

- **Section 3.** Be candid. Five minutes spent searching for an undocumented enum value is worth reporting. The author needs that information to improve the process.
- **Section 4.** Use the same components as the estimate. If the original breakdown cannot describe the actual work, record that as a gap in section 3 as well.
- **Section 5.** A departure in section 2 is an in-scope decision. Implementing an out-of-scope finding would cross the boundaries established in the handoff.

## How the brief author uses the report

1. Apply `retro.md` to sections 2–3. Classify each item (A/B/C/D), make corrections through the corresponding process, and record isolated category C cases in `misses-log.md`.
2. Add section 4 to the calibration log when one exists. Until then, the report is sufficient; do not accumulate estimates in a separate section of `misses-log.md`.
3. Turn actionable findings from section 5 into new tasks or add them to existing ones.
4. Treat “No findings” in section 3 as evidence that the checks served their purpose for this task.
