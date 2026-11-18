"""Deterministic cost decisions for the DevCrew autopilot workflow."""

from __future__ import annotations

import json
from collections import deque
from pathlib import Path


MODES = ("economy", "balanced", "quality")
RISKS = ("low", "medium", "high")
IMPLEMENTATION_REASONS = (
    "complex_algorithm",
    "concurrency_implementation",
    "heavy_legacy_refactor",
    "complex_data_transformation",
    "complex_implementation",
    "repeated_failure",
)
ESCALATION_REASONS = IMPLEMENTATION_REASONS + (
    "architectural_complexity",
    "security_risk",
    "destructive_change",
    "ambiguous_correctness",
    "systemic_replan",
)
ROLE_ALIASES = {
    "decision": "planner",
    "decision-agent": "planner",
    "executor-agent": "executor",
    "reviewer-agent": "reviewer",
    "orchestrator": "coordinator",
}
DIMENSIONS = {
    "coordination_complexity": ("SIMPLE", "COMPLEX"),
    "decision_complexity": ("ROUTINE", "NORMAL", "HARD", "DEEP"),
    "implementation_complexity": ("MECHANICAL", "NORMAL", "COMPLEX"),
    "review_complexity": ("ROUTINE", "NORMAL", "COMPLEX"),
}


def policy(overrides=None):
    overrides = overrides or {}
    if not isinstance(overrides, dict) or set(overrides) - {"cost_mode"}:
        raise ValueError("cost policy accepts only cost_mode")
    mode = overrides.get("cost_mode", "economy")
    if mode not in MODES:
        raise ValueError("cost_mode must be economy, balanced, or quality")
    return {"cost_mode": mode}


def runtime_models(runtime):
    """Validate a caller-supplied availability/capability observation."""
    if runtime is None:
        return []
    if not isinstance(runtime, dict) or not isinstance(runtime.get("models"), list):
        raise ValueError("runtime must contain a models list")
    seen = set()
    for item in runtime["models"]:
        if not isinstance(item, dict):
            raise ValueError("runtime model must be an object")
        model = item.get("id")
        capabilities, efforts = item.get("capabilities"), item.get("efforts")
        if not isinstance(model, str) or not model.strip() or model in seen:
            raise ValueError("runtime model ids must be nonempty and unique")
        if (
            not isinstance(capabilities, list)
            or not capabilities
            or any(value not in ("bounded", "strong", "deep") for value in capabilities)
        ):
            raise ValueError("model capabilities must contain bounded, strong or deep")
        if (
            not isinstance(efforts, list)
            or not efforts
            or any(
                value not in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
                for value in efforts
            )
        ):
            raise ValueError("model efforts must be recognized reasoning levels")
        if type(item.get("priority", 0)) is not int:
            raise ValueError("model priority must be an integer")
        seen.add(model)
    return runtime["models"]


def resolve_model(model_class, effort, runtime, *, allow_fallback=True):
    models = runtime_models(runtime)
    # Higher runtime priority represents observed suitability. The current
    # host inventory order breaks ties between equally qualified candidates.
    tiers = {
        "bounded": ("bounded", "strong", "deep"),
        "strong": ("strong", "deep"),
        "deep": ("deep", "strong"),
    }[model_class]
    allowed_tiers = tiers if allow_fallback else (model_class,)
    for capability in allowed_tiers:
        candidates = [
            item
            for item in models
            if capability in item["capabilities"] and effort in item["efforts"]
        ]
        if candidates:
            chosen = max(candidates, key=lambda item: item.get("priority", 0))
            return chosen["id"], "RESOLVED" if capability == model_class else "FALLBACK"
    return None, "RUNTIME_REQUIRED" if runtime is None else "BLOCKED"


def route(
    role,
    risk="low",
    *,
    cost_mode="economy",
    escalation_reason=None,
    retry_stage=0,
    coordination_complexity=None,
    decision_complexity=None,
    implementation_complexity=None,
    review_complexity=None,
    task_class=None,
    runtime=None,
):
    """Select capability independently for each job, then resolve availability.

    Legacy model/effort/reason aliases are retained. Without runtime evidence
    model is null: this is a policy recommendation, never a spawn receipt.
    """
    policy({"cost_mode": cost_mode})
    role = ROLE_ALIASES.get(role, role)
    if not isinstance(risk, str) or risk.lower() not in RISKS:
        raise ValueError("invalid risk")
    risk = risk.lower()
    if role not in ("executor", "reviewer", "coordinator", "planner", "research"):
        raise ValueError("invalid role")
    if escalation_reason is not None and escalation_reason not in ESCALATION_REASONS:
        raise ValueError("invalid escalation reason")
    if type(retry_stage) is not int or retry_stage not in (0, 1, 2, 3):
        raise ValueError("retry_stage must be 0..3")
    values = dict(
        coordination_complexity=coordination_complexity,
        decision_complexity=decision_complexity,
        implementation_complexity=implementation_complexity,
        review_complexity=review_complexity,
    )
    for name, value in values.items():
        if value is not None:
            if not isinstance(value, str) or value.upper() not in DIMENSIONS[name]:
                raise ValueError(f"invalid {name}")
            values[name] = value.upper()
    if task_class is not None and (not isinstance(task_class, str) or not task_class.strip()):
        raise ValueError("task_class must be nonempty")
    replan = (
        task_class in ("SYSTEMIC_REPLAN", "TECHNICAL_REPLAN", "REPLAN_REQUEST")
        or escalation_reason == "systemic_replan"
    )
    if replan:
        role = "planner"
        if values["decision_complexity"] != "DEEP":
            values["decision_complexity"] = "HARD"
    dimensions = {name.upper(): None for name in DIMENSIONS}
    if role == "coordinator":
        value = values["coordination_complexity"] or "SIMPLE"
        dimensions["COORDINATION_COMPLEXITY"] = value
        model_class, effort = "strong", "medium" if value == "COMPLEX" else "low"
        reason = "complex_coordination" if value == "COMPLEX" else "simple_coordination"
    elif role == "planner":
        value = values["decision_complexity"] or "NORMAL"
        dimensions["DECISION_COMPLEXITY"] = value
        model_class = "deep" if value == "DEEP" else "strong"
        effort = {"ROUTINE": "low", "NORMAL": "medium", "HARD": "high", "DEEP": "high"}[value]
        reason = {
            "ROUTINE": "routine_decision",
            "NORMAL": "normal_decision",
            "HARD": "architectural_complexity",
            "DEEP": "deep_system_design",
        }[value]
        if replan:
            reason = "systemic_replan"
    elif role == "executor":
        value = values["implementation_complexity"] or "NORMAL"
        dimensions["IMPLEMENTATION_COMPLEXITY"] = value
        # AGENT EXECUTION has a strict executor ceiling. Complexity, risk,
        # escalation reasons, and retries route back through planning rather
        # than selecting a stronger implementation model.
        model_class, effort = "bounded", "low" if value == "MECHANICAL" else "medium"
        reason = "mechanical_implementation" if value == "MECHANICAL" else "normal_implementation"
    elif role == "reviewer":
        value = values["review_complexity"] or "NORMAL"
        dimensions["REVIEW_COMPLEXITY"] = value
        model_class = "strong"
        effort = {"ROUTINE": "low", "NORMAL": "medium", "COMPLEX": "high"}[value]
        reason = {
            "ROUTINE": "routine_review",
            "NORMAL": "normal_review",
            "COMPLEX": "complex_review",
        }[value]
        if risk == "high":
            effort, reason = "high", "high_risk_review"
        elif retry_stage == 3:
            if escalation_reason != "repeated_failure":
                raise ValueError("review escalation requires a reason: repeated_failure")
            effort, reason = "high", "repeated_failure"
    else:
        value = values["decision_complexity"] or "ROUTINE"
        dimensions["DECISION_COMPLEXITY"] = value
        model_class = "deep" if value == "DEEP" else "strong" if value == "HARD" else "bounded"
        effort = "low" if value == "ROUTINE" else "medium" if value == "NORMAL" else "high"
        reason = "factual_research" if value in ("ROUTINE", "NORMAL") else "complex_inference"
    model, status = resolve_model(
        model_class, effort, runtime, allow_fallback=role not in ("executor", "reviewer")
    )
    return {
        "ROLE": role,
        "TASK_CLASS": task_class or role.upper(),
        **dimensions,
        "RISK": risk.upper(),
        "MODEL_CLASS": model_class,
        "RESOLVED_MODEL": model,
        "REASONING_EFFORT": effort,
        "SELECTION_REASON": reason,
        "RESOLUTION_STATUS": status,
        "ROUTE_APPLIED": False,
        "EFFECTIVE_MODEL": None,
        "EFFECTIVE_REASONING_EFFORT": None,
        "model": model,
        "effort": effort,
        "reason": reason,
    }


def confirm_route(selection, observation=None):
    """Only an effective runtime observation can attest a prepared route."""
    observation = observation or {}
    if not isinstance(observation, dict):
        raise ValueError("runtime confirmation must be an object")
    model, effort = observation.get("model"), observation.get("reasoning_effort")
    matched = bool(
        selection.get("RESOLVED_MODEL")
        and model == selection["RESOLVED_MODEL"]
        and effort == selection["REASONING_EFFORT"]
    )
    return {
        **selection,
        "ROUTE_APPLIED": matched,
        "EFFECTIVE_MODEL": model,
        "EFFECTIVE_REASONING_EFFORT": effort,
        "CONFIRMATION_STATUS": "CONFIRMED"
        if matched
        else "MISMATCH"
        if model or effort
        else "UNCONFIRMED",
    }


def valid_continuation(value):
    """Check an assessment's shape, not the truth of its technical reasoning."""
    return (
        isinstance(value, dict)
        and all(
            isinstance(value.get(key), str) and value[key].strip()
            for key in ("reason", "next_action", "expected_result", "cost_rationale")
        )
        and isinstance(value.get("evidence_records"), list)
        and bool(value["evidence_records"])
        and all(isinstance(ref, str) and ref.strip() for ref in value["evidence_records"])
    )


def review_action(
    *,
    pass_state=None,
    current_state=None,
    failed_cycles=0,
    new_evidence=True,
    continuation=None,
    loop_detected=False,
    owner_decision_required=False,
):
    if loop_detected or owner_decision_required:
        return "blocked"
    if pass_state is not None and pass_state == current_state:
        return "commit"
    if failed_cycles >= 2:
        return "review" if new_evidence and valid_continuation(continuation) else "blocked"
    return "review"


def reusable_check(record, current_state, command):
    """Caller must first validate record with verify-check; compare command and tree."""
    record_root = record.get("execution_root", record.get("cwd"))
    current_root = current_state.get("execution_root", current_state.get("cwd"))
    return (
        record.get("status") == "passed"
        and record.get("exit_code") == 0
        and (record_root is None or current_root is None or record_root == current_root)
        and record.get("command") == command
        and record.get("git_head_before") == record.get("git_head")
        and record.get("worktree_fingerprint_before") == record.get("worktree_fingerprint_after")
        and record.get("git_head") == current_state.get("git_head")
        and record.get("worktree_fingerprint_after") == current_state.get("worktree_fingerprint")
    )


def check_summary(record_path, *, tail_lines=20):
    record_path = Path(record_path)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    log = record_path.parent / record["log"]
    with log.open(encoding="utf-8", errors="replace") as handle:
        lines = deque((line.rstrip("\n") for line in handle), maxlen=max(1, tail_lines))
    lines = list(lines)
    return {
        "command": record["command"],
        "exit_code": record["exit_code"],
        "status": record["status"],
        "summary": lines[-1] if lines else "",
        "failures": lines[-tail_lines:] if record["status"] != "passed" else [],
        "record": str(record_path),
        "log": str(log),
    }


def task_report(
    task_id,
    *,
    risk,
    executor_routes,
    reviewer_routes,
    handoffs=0,
    review_cycles=0,
    full_suite_runs=0,
    targeted_test_runs=0,
    token_telemetry="unavailable",
):
    if risk not in RISKS or token_telemetry not in ("available", "unavailable"):
        raise ValueError("invalid report risk or telemetry")
    strong = [
        item
        for item in executor_routes + reviewer_routes
        if item.get("MODEL_CLASS") in ("strong", "deep")
        and item.get("reason") in (*IMPLEMENTATION_REASONS, "high_risk_review", "complex_review")
    ]
    return {
        "task": task_id,
        "risk": risk,
        "executor_model": executor_routes[-1]["model"] if executor_routes else None,
        "executor_effort": executor_routes[-1]["effort"] if executor_routes else None,
        "executor_count": len(executor_routes),
        "executor_handoffs": handoffs,
        "reviewer_model": reviewer_routes[-1]["model"] if reviewer_routes else None,
        "reviewer_effort": reviewer_routes[-1]["effort"] if reviewer_routes else None,
        "review_cycles": review_cycles,
        "full_suite_runs": full_suite_runs,
        "targeted_test_runs": targeted_test_runs,
        "strong_model_escalations": len(strong),
        "strong_model_reason": [item["reason"] for item in strong],
        "token_telemetry": token_telemetry,
    }


def main(argv=None):
    """Read-only routing dry-run; snapshots are supplied, never discovered."""
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True, help="runtime capability snapshot JSON")
    parser.add_argument(
        "--request", required=True, help="one route request or a list of requests as JSON"
    )
    args = parser.parse_args(argv)
    try:
        runtime = json.loads(Path(args.runtime).read_text(encoding="utf-8"))
        requests = json.loads(Path(args.request).read_text(encoding="utf-8"))
        multiple = isinstance(requests, list)
        rows = requests if multiple else [requests]
        if not all(isinstance(row, dict) for row in rows):
            raise ValueError("requests must be objects")
        results = [route(runtime=runtime, **row) for row in rows]
    except (OSError, ValueError, TypeError) as error:
        parser.exit(2, f"routing error: {error}\n")
    print(json.dumps(results if multiple else results[0], ensure_ascii=False, indent=2))
    return 0 if all(row["RESOLVED_MODEL"] for row in results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
