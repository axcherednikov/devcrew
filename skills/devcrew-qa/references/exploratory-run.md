# Exploratory run and replay checklist

Use this reference for one bounded web QA attempt. It records what the agent
must observe; the CLI owns IDs, digests, state, and evidence verification.

## Before the run

- State the product scope and the user flow in one or two sentences.
- Include a successful path, an applicable negative or edge probe, and the
  desktop and mobile-web viewports.
- Confirm the managed environment is isolated, synthetic, and `READY`.
- Confirm the browser tool can set exact viewport sizes and capture images.
- Keep the run token; it is the unique label for this attempt.

## During the run

- Set the exact viewport before each scenario, not only once per session.
- Capture a before image and an after image for every scenario and viewport.
- Actually inspect the images for clipped text, overlap, missing feedback,
  empty/loading/error states, and inaccessible controls.
- Read the visible success or error result and the object ID. Do not treat a
  success message as persistence proof.
- Record expected versus actual values; do not adjust the expectation after
  seeing the result.

## Replay

- Find the source with `qa web-find` and open it with `qa web-report`; this is
  read-only.
- Select one scenario or one derived defect such as `bad-name:error-message`
  or `create-item:readback`.
- Start a new managed environment and pass its run ID and base URL to
  `qa web-replay-start`. It must not be the source run's environment, its saved
  services must still match their process identity, the base URL must be one of
  its recorded readiness addresses, and no unfinished attempt may already own
  it; a stale `READY` record or a bound environment is rejected.
- Use `--isolated` only when the source run is incomplete and its side effects
  are unknown. Do not repeat a changing action blindly.
- Re-run the actions with the new token, capture new screenshots, and record a
  new backend read. The source report stays untouched.
- Report the source and new run IDs, the selection, cleanup, and any remaining
  blocker. A source `PASS` is historical; only the new evidence can support a
  new `PASS`.
