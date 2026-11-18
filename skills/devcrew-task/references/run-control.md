# Test progress and shared resources

Select actual commands from the repository's manifests, scripts and existing tests. Check runtime, required environment variable presence and fixture ownership first; do not print secrets or inherit another project's commands. Batch expensive integration runs after targeted checks pass. Do not overlap heavy runs against the same database, port, container or constrained runner.

`run-check` emits a `check_started` event to stderr with child PID, log path and eventual record path. Use the native process/session wait tool and inspect the log when progress is uncertain. Distinguish a live quiet process, a failed environment and a hung process using evidence. An unchanging log alone does not prove a hang. Do not repeatedly restart without diagnosis or kill a process owned by another task.

The wall timeout stays bounded by `--timeout`. For a command known to emit periodic output, opt in to `--idle-timeout SECONDS`; reaching it records `status: timeout`, `timeout_kind: idle` and terminates the owned process group on POSIX or the owned Job Object on Windows. Silent successful tools should not receive an idle limit. Ctrl+C, and SIGTERM on POSIX, produce an `interrupted` result and stop the owned tree. Terminating the Windows wrapper forcibly stops its job without a final record. Do not report timeout/interruption as a failing assertion or successful test.

For cooperating local runs use the same explicit resource directory and name across worktrees:

```text
python3 <plugin-root>/scripts/devcrew.py resource-status --resource-dir SHARED_DIR --name database
python3 <plugin-root>/scripts/devcrew.py run-check --project PATH --name integration --resource database --resource-dir SHARED_DIR --timeout SECONDS -- COMMAND ARG...
```

Choose a dedicated shared directory outside fingerprinted source, or in configured checks storage shared by all participants. `run-check` fails before executing if the resource is busy; queue the task and retry after the holder finishes. Do not delete lock files: stable inodes prevent two holders. POSIX advisory locks are released by descriptor closure, not by trusting a PID string. On POSIX the direct child inherits the locked descriptor. On Windows the child holds a duplicate of the share-deny-write file handle. The wrapper owns a kill-on-close Job Object so its unexpected exit also stops the command tree. Protected commands must not daemonize work or deliberately close inherited lock descriptors. All participants must cooperate; this does not control unrelated clients or distributed hosts.

The status command does not create files and reports active ownership from the kernel. Historical lock metadata is not a live holder. Busy status is not a failure of the tests. For conflicting external resources that cannot use this mechanism, the parent owns a documented queue and verifies release before dispatching the next run.
