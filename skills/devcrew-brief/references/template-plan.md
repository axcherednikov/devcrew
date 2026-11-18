# Implementation plan for <task>

## Outcome and boundaries

<Reference the approved brief. Describe the observable change, what stays out of scope, and the prerequisites that block a step.>

## Steps

Repeat the following block for each independently verifiable step. Order steps by their dependencies. Use the target project's vocabulary and normal entry points.

### <Outcome of the step>

- **Starting point:** <existing entry point, component or artifact, and a source that confirms its behavior>
- **Change:** <what the implementer changes, who consumes it and which existing contract must be preserved>
- **Dependencies:** <prior steps or external decisions, if any>
- **Verification:** <representative input or action, expected result, and the project's command or manual procedure>

Use a sketch in the project's language or pseudocode only when order, concurrency, recovery or a boundary is hard to explain in prose. Show what happens on interruption or retry if the operation has side effects.

## Scenarios and checks

| Scenario | Starting state and action | Observable result | How to check |
| --- | --- | --- | --- |
| <Normal use> | <Input and action> | <Expected output or change> | <Project check> |
| <Relevant edge case or failure> | <Trigger> | <Expected behavior, including preserved state> | <Targeted check> |
| <Existing behavior at risk> | <Existing input or workflow> | <Compatibility expectation> | <Regression check> |

For example, a CLI might verify output and exit status, a library its public return value, a user interface its state after an interaction, and a service its response. Choose the actual interface. Name fixtures and real dependencies only where the scenario needs them.

## Completion

- <Acceptance criterion and the step or check that demonstrates it>
- <Known limitation, pending decision or evidence still to collect>
- <Estimate by component, with its basis and uncertainty>
