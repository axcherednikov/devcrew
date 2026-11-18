# Source resolution

Before deciding the workflow, identify whether the request depends on an external or local source: tracker/task ID, PR/MR, commit, branch, diff/working tree, task/spec file, repository path, attachment, or plain request. A reference alone never determines intent: for example, “DEMO-105 status” is read-only while “implement DEMO-105” requests implementation.

For ordinary versus formal task intake, establish the actual source format, full-source availability through the supported reader, and existing authorization before dispatch. Ordinary current-message intake is valid only when the host can read the exact complete message. Its attachment reader accepts Page references; descriptors, summaries and local paths do not prove readable bytes. If the source is a local attachment or an unsupported ordinary format, use the existing formal `phase source-bind --source-spec FILE` flow when authorized. If an ordinary task already exists and its reader cannot access the actual source, use the explicit same-task `phase source-bind --ordinary-source-gap FILE` handoff; preserve its history and stop ordinary mutations before formal planning. If access remains unavailable, state the missing reader/access and smallest recovery action without requesting the same failed resubmission.

## Resolve only what the outcome needs

- For external references, inspect the current runtime's available MCP/connectors/integrations by their tool names and descriptions. Use the provider-neutral capability that matches the reference; do not hardcode a tracker or infer task contents from an identifier.
- For an implementation tracker task, collect the relevant title, description, acceptance criteria, material comments/attachments, linked tasks that affect scope, and status/metadata only as needed. Do not retrieve unrelated history or all attachments by default.
- For PR/MR and commit review, resolve the exact target and baseline required by `devcrew-review`. For local branches/diffs/files, inspect exact paths/state only after determining the requested outcome.
- For a repository path, establish that it is the intended workspace before reading its project instructions. For an attached document, read only the attachment relevant to the request.
- After resolving the source and selecting an owner, that workflow performs its own targeted project discovery. Do not front-load a repository-wide scan in the router.

## Failure behavior

- `SOURCE_UNAVAILABLE`: reference recognized but no suitable connector/access or source cannot be retrieved. State which source/capability is unavailable; ask for the smallest useful supplied context only if the user can provide it.
- `WORKSPACE_AMBIGUOUS`: external task cannot be tied to one repository/workspace. Ask which workspace; do not choose by guess.
- `INSUFFICIENT_EVIDENCE`: outcome cannot be classified after available evidence; ask one question that distinguishes the plausible outcomes.
- `OWNER_DECISION_REQUIRED`: materially different product outcomes remain after repository/source evidence. Use `devcrew-brief` when requirements need structuring, otherwise ask the single dependency-frontier question.
- `RUNTIME_CAPABILITY_MISSING`: host cannot activate/continue the selected owner workflow or perform required internal dispatch. Name the missing capability and give a compact handoff. Do not claim success or instruct a manual relaunch when the current context can continue safely.

Do not treat unavailable tools as evidence about the source. Do not ask the user to repeat information already present in the request or resolved source.
