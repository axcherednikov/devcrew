# Task readiness

This is the shared readiness rule for substantive DevCrew work. Apply it after
the owning workflow has established enough context to understand the request.
The front door routes to this rule; each direct substantive skill links here.
The owning workflow does the research and records the result in its existing
plan, task state or concise response. Do not create a second readiness workflow,
mandatory document, lifecycle stage, or approval gate.

## Establish readiness in proportion to the task

After targeted research, establish:

- the requested outcome;
- scope and behavior that must be preserved;
- observable checks or acceptance criteria;
- material unresolved decisions and their dependencies.

For a simple, clear task, do the work and report the result. Do not add an
interview, brief, plan document, agent, or approval step just to repeat the
request. For a nontrivial task, state the outcome, scope and checks briefly,
then continue without waiting for ceremonial confirmation. When independent
outcomes need sequencing, preserve the full requested scope and identify their
dependencies and readiness conditions.

## Resolve uncertainty at its source

- Establish accessible facts from the repository or other permitted sources.
- Make bounded technical choices within the confirmed requirements.
- Ask one concrete question when a material owner decision is genuinely
  unresolved; explain its effect and recommendation when evidence supports one.
- Use `devcrew-brief` when related unresolved decisions prevent defining the
  outcome or scope. A brief organizes questions; it does not answer them.

Do not demand absolute certainty. Block only work that depends on the open
decision, and continue useful independent work. Silence is not approval.
Diagnosis, review, research and planning remain read-only unless implementation
was separately requested. A readiness check does not grant new authority.

Keep checks and conclusions tied to confirmed requirements. In the final
report, distinguish verified criteria from unverified ones and state why a
check was not run. Perform checks inside the task's authority and budget.
Report an independent adjacent finding at the end with its observation,
evidence and impact, and label facts separately from hypotheses. A real
prerequisite is a dependency, not an adjacent finding. Preserve the shared
[loop guard](loop-guard.md) and existing execution and review gates.

## Resolve the original source before dispatch

Before ordinary or formal dispatch, establish the actual source format, whether
the complete source is independently readable, and what route was authorized.
Use ordinary current-message access only when the host can read the exact full
message with the supported reader. An attachment reference or descriptor is
not proof of its contents. The ordinary attachment reader accepts only its
supported Page references. For a local attachment or unsupported ordinary
format, use the existing formal `phase source-bind --source-spec FILE` path to
bind the exact verified file when that route was authorized. If the exact local
file can be read and bound but formal binding is not yet authorized, ask one
concrete question whether to authorize binding that exact file. If neither
route can read the exact bytes, report the missing reader/access and the
smallest action that restores access; do not ask for the same failed
resubmission. Never reconstruct the source from a summary or fabricate a read
receipt.

For a verified source gap discovered after an ordinary task exists, use the
existing same-task checkout receipt, prepared and resolved before the handoff
if needed, then use the explicit same-task handoff contract implemented by `phase source-bind
--ordinary-source-gap FILE`. The handoff must preserve ordinary history and
source identity, bind the same task/base/checkout and exact attachment, and
seal ordinary work before formal planning proceeds. A stale ordinary route is
not authority after sealing. Formal planning must independently verify the
binding and source; every existing planner, assessment, candidate and review
gate still applies. Repeating a handoff is safe only for the same validated
binding. Conflicts, tampering, unreadable bytes or an active/uncertain writer
fail closed. Do not transfer full transcripts or change checkout files, Git
state or the index as part of the handoff.
