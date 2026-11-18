#!/usr/bin/env python3
"""Offline virtual-time comparison of coordinator waiting schedules.

All counts emitted here are simulation results, not historical measurements.
"""

EVENTS = (
    (17, "task-1", "agent-a", "executor", "implementation", "progress", "dispatch-a"),
    (91, "task-1", "agent-a", "executor", "implementation", "steering", "dispatch-a"),
    (147, "task-1", "agent-a", "executor", "implementation", "success", "dispatch-a"),
    (230, "task-1", "agent-b", "executor", "verification", "failure", "dispatch-b"),
    (311, "task-1", "agent-c", "reviewer", "review", "blocker", "dispatch-c"),
    (314, "task-1", "agent-c", "reviewer", "review", "blocker", "dispatch-c"),  # duplicate
)


def simulate(wait_seconds, events=EVENTS, start_time=0, saved_terminal=None):
    """Deliver all mailbox events and count empty timeout wakeups."""
    now = start_time
    index = 0
    empty_wakeups = 0
    delivered = []
    terminal = dict(saved_terminal or {})
    duplicate_terminals = 0
    while index < len(events):
        deadline = now + wait_seconds
        event_time = events[index][0]
        if event_time > deadline:
            now = deadline
            empty_wakeups += 1
            continue
        now = event_time
        while index < len(events) and events[index][0] == now:
            event = events[index]
            delivered.append(event)
            if event[5] in {"success", "failure", "blocker"}:
                if event[6] in terminal:
                    duplicate_terminals += 1
                else:
                    terminal[event[6]] = event[5]
            index += 1
    return {
        "empty_wakeups": empty_wakeups,
        "delivered_events": len(delivered),
        "unique_terminal_results": terminal,
        "duplicate_terminal_deliveries": duplicate_terminals,
        "identities": {(e[1], e[2], e[3], e[4], e[6]) for e in delivered},
    }


def main():
    schedules = {
        "native_60s": simulate(60),
        "reference_30s": simulate(30),
        "reference_short_sleep_5s_status": simulate(5),
    }
    boundary = simulate(60, events=(
        (60, "task-boundary", "agent-boundary", "executor", "implementation", "success", "dispatch-boundary"),
    ))
    resumed = simulate(60, events=EVENTS[3:], start_time=200,
                       saved_terminal={"dispatch-a": "success"})
    native = schedules["native_60s"]
    assert native["empty_wakeups"] < schedules["reference_30s"]["empty_wakeups"]
    assert native["empty_wakeups"] < schedules["reference_short_sleep_5s_status"]["empty_wakeups"]
    assert all(result["delivered_events"] == len(EVENTS) for result in schedules.values())
    assert all(len(result["unique_terminal_results"]) == 3 for result in schedules.values())
    assert boundary["empty_wakeups"] == 0 and boundary["delivered_events"] == 1
    assert len(resumed["unique_terminal_results"]) == 3
    assert resumed["duplicate_terminal_deliveries"] == 1
    for name, result in schedules.items():
        print(f"SIMULATION {name}: empty_coordinator_wakeups={result['empty_wakeups']} "
              f"delivered_events={result['delivered_events']} "
              f"unique_terminal_results={len(result['unique_terminal_results'])} "
              f"duplicate_terminal_deliveries={result['duplicate_terminal_deliveries']} "
              f"distinct_identities={len(result['identities'])}")
    print("SIMULATION boundary_exactly_60s: delivered=1 empty_wakeups=0")
    print("SIMULATION resumed_with_saved_result: unique_terminal_results=3 duplicate_terminal_deliveries=1")
    print("Token telemetry: UNKNOWN (no trustworthy runtime measurement supplied).")
    print("All counts above are virtual-time simulation results, not historical or billing measurements.")


if __name__ == "__main__":
    main()
