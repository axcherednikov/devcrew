# Brief retrospective

Use a retrospective for a confirmed error in a delivered brief. A new product decision or an implementer departing from a clear requirement has a different cause.

1. Record the task, the original claim, what happened, and the consequences. Verify the code or the execution result. A secondhand report alone is insufficient.
2. Classify the miss. **A:** an existing check was skipped. **B:** a check ran but missed the error. **C:** no check covered this class of error. **D:** the interview or the structure of the brief caused the problem.
3. Correct the current brief and run the original case against it again. Record the confirmed lesson in the target project's configured memory directory.
4. For a single inexpensive type C miss, use the [log template](misses-log.md) to add a row to the project's `brief-misses.md` in that directory. A second similar miss, or a risk of data loss, justifies a new project check. A single isolated case does not establish a general rule.
5. Record improvements proposed for the shared plugin separately. Identify the exact file, rule, before-and-after example, and verification result. Change the plugin source only in an explicitly requested maintenance task. Leave the installed cache untouched.

If you cannot rerun the case, disclose that limit. A walkthrough does not establish an execution result. Periodically remove obsolete or disproved lessons from project memory, retaining the reasons for the change.
