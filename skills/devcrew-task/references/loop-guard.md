# Loop guard: evidence, progress and bounded cost

This is mandatory for every DevCrew workflow, DIRECT and delegated work alike,
including implementation, updates, diagnosis, review, planning and skill repair.
It takes precedence over automatic local-fix, replan, escalation and retry rules. Preserve history
across agents, models, files, phases and context compaction for the same unresolved work.

## Autonomous correction

A repeated FAIL alone is not a loop and does not require an owner answer. Correct
confirmed technical errors autonomously within the accepted scope and constraints.
The coordinator checks contested findings against requirements, code and test evidence;
a model's opinion is not proof. If the reviewer is wrong, preserve its report and
reconsider it using contrary evidence on the same candidate; do not change correct
code to satisfy a mistaken review. Fresh independent review is still required for
acceptance. Do not weaken tests, acceptance, safety or scope to obtain PASS.

Before another correction or costly investigation, state what was learned, the
bounded next action, its expected observable result and verification, and why its
cost is proportionate. New findings do not grant unlimited work. Reuse verified
context and unchanged evidence; repeat a check only for a concrete question or
changed relevant state. Keep the existing allowance/budget guardrails; unavailable
usage measurements are unknown, not invented. Stop when acceptance and required
checks pass; do not start speculative improvements or another audit.

## Triggers and bounded correction

Return STATUS: LOOP_DETECTED and stop dependent automatic work when actions repeat
without discriminating evidence, changes oscillate (A → B → A), hypotheses are
merely renamed, or models keep disputing the same facts without a resolving check.
A recurring symptom may justify another bounded step only when evidence actually
narrows its cause or supports a different testable approach. Changing agents,
models, finding labels or prose does not reset history or establish progress.

Expected test-first RED, planned reproduction samples, new independent findings,
and purposeful checks supplying new evidence are not loop triggers by themselves.
A boolean claim of new evidence is insufficient. The coordinator owns semantic
assessment; the helper verifies identity, recorded checks and preserved history,
but cannot prove that a model's reasoning or cost justification is true.

## Stop, question, and resumption

Preserve changes, failed reports, attempts and check records on stop. A delegated
agent reports the blocker to the coordinator and stops; it does not start a debate
or dispatch another agent. The coordinator may resolve a technical disagreement
from bounded existing evidence without asking the user.

A technical blocker is not automatically an owner question. Report it accurately
when no justified next step exists. Ask the user only for a concrete unresolved
requirement/product choice, missing owner-supplied data/access, authorization or
agreement to a material expansion of work/cost. Never invent a question or approval.
Wait for the answer before dependent work when that owner decision is necessary.
An actual answer resolves only its named boundary; it does not erase history or
authorize unlimited attempts. Silence and elapsed time are not approval.

After a corrected phase fails again, `phase resume` returns
`PHASE_CONTINUATION_REQUIRED` for coordinator assessment, not an automatic owner
stop. Before `phase local-fix-start` or `phase replan-complete`, supply
`--continuation FILE`: exact task, phase, candidate SHA and failed report digest,
reason, real evidence_records, next_action, expected_result and cost_rationale.
The helper verifies and archives the proof with the failed cycle; recovery and
later resume revalidate it. An actual owner answer may use `--owner-decision TEXT`
when a real owner boundary exists. Never fabricate one for a technical correction.

For a mistaken reviewer, `phase review-reconsider --continuation FILE` preserves
the failed report and candidate, and returns to formal independent review of the
same SHA. It cannot accept a FAIL or bypass PASS, receipt or verification gates.
Repeating the same tree and check results does not authorize another attempt.
