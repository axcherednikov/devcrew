# Agent waiting schedule evaluation

Run the offline standard-library harness from the repository root:

```sh
python3 evaluation/agent_waiting.py
```

The harness compares 60-second native mailbox waits with explicit 30-second and hypothetical 5-second sleep/status schedules on the same virtual event timeline. The timeline includes progress, steering, multiple agents and roles, success, failure, blocker delivery, and a duplicate terminal delivery. Each schedule must deliver every event and reconcile the three unique terminal results by dispatch identity. Additional assertions cover an event arriving exactly at the 60-second timeout boundary and a resumed run that carries a saved terminal result across continuation and ignores its duplicate delivery.

The output labels all counts as simulation. Fewer empty wakeups in this virtual timeline demonstrate the schedule property, not historical coordinator behavior, token savings, or billing savings. The evaluation introduces no delay, agent, scheduler, process, or runtime dependency. Actual useful-run timestamps and observed waits belong to the coordinator's existing wait activity. Token telemetry is `UNKNOWN` unless the runtime supplies a trustworthy measurement.
