# Local QA sessions — evaluation record

This directory holds evidence and instructions for evaluating the local QA
workflow. Keep three layers separate; passing one does not establish another.

## 1. Source checks

Run focused unit tests and package validation against the checkout:

```sh
python3 -B scripts/run_tests.py --jobs 1 test_qa_environment test_qa_web test_qa_replay test_router_contract
python3 -B scripts/check_package.py
```

These checks establish source behavior such as fresh replay IDs, unchanged
source digests, damaged-evidence rejection, backend `BLOCKED`, read-only
discovery without writes, a stop that waits for an in-flight start, replay
rejection of a stale, foreign, or reused environment, and refusal of a second
attempt against an environment already bound to an unfinished run, in both the
`replay → start` and `start → replay` orders. They are not live browser or
installed-plugin evidence.

## 2. Routing contract and live routing evaluation

Contract checks verify that the public `devcrew` router sends local QA,
run discovery, report reading, and replay to `devcrew-qa`, while QA handoff stays
with `devcrew-handoff`, diff review stays with `devcrew-review`, and bug fixes
stay with `devcrew-task`.

Use `tests/routing-evals.md` with a live model for the behavioral cases:
`router-local-qa`, `router-qa-find-read`, and `router-qa-handoff-distinct`.
Record the model, prompt, observed route, and whether report reading stayed
read-only. A static test walkthrough is not a model routing evaluation.

## 3. Installed plugin in a new chat

This layer requires permission to update/enable the plugin and a genuinely new
chat with no prior conversation. It is not covered by source checks.

The live record should include:

- installed version and source tree identity;
- the source run ID and its report/screenshot digests before replay;
- the new replay run ID, selection, screenshots, and backend read;
- the same source digests after replay, proving they are unchanged;
- viewport, browser tool, backend identity, verified defect, and cleanup status;
- an explicit statement that a historical `PASS` was not reused.

If installation or a fresh chat is unavailable, mark this layer
`NOT_VERIFIED` rather than inferring it from source tests.
