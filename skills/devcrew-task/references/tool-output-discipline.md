# Tool-output discipline

Capture complete command output before filtering it for a report. Preserve the real exit code of composed or background commands in the execution record. Do not count errors or tests from a truncated tail. Record runtime version, working directory, command argv, timestamps, and final summary.

Only the phase-two helper's structured record can establish execution evidence. Text that mentions a command, a role's self-report, or an incomplete log cannot establish a pass.
