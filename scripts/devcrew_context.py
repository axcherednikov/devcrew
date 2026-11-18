"""Best-effort agent telemetry and context handoff decisions.

Inputs come from an explicitly supplied runtime observation. This module never
estimates context size from cumulative token usage or subscription allowance.
"""

from __future__ import annotations

import json
import math
import os
import pathlib
import re
import subprocess
import sys
import tomllib
import uuid

import devcrew_capacity
import devcrew_cost
import devcrew_dispatch
import devcrew_fs as fs
import devcrew_phase
import devcrew_requirements
import devcrew_worktree

DEFAULTS = {
    "enabled": True,
    "telemetry": True,
    "automatic_handoff": True,
    "soft_context_percent": 45,
    "hard_context_percent": 60,
    "soft_context_tokens": None,
    "hard_context_tokens": None,
    "weekly_soft_remaining_percent": None,
    "weekly_hard_remaining_percent": None,
}
USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "total_recorded_tokens",
)
HANDOFF_FIELDS = (
    "TASK_ID",
    "GOAL",
    "DONE",
    "REMAINING",
    "FILES_CHANGED",
    "TESTS",
    "FAILURES",
    "DECISIONS",
    "NEXT_ACTION",
)
CONTRACT_FIELDS = (
    "TASK_ID",
    "GOAL",
    "MAIN_SCENARIO",
    "TYPE",
    "IN_SCOPE",
    "OUT_OF_SCOPE",
    "ACCEPTANCE",
    "DEPENDENCIES",
    "RELEVANT_FILES",
    "RELEVANT_ADR_BRIEF_SECTIONS",
    "VERIFICATION_STRATEGY",
    "OWNED_PATHS",
    "BASELINE_EVIDENCE_PATHS",
    "PROJECT_ROOT",
    "TASK_WORKTREE_ROOT",
    "TASK_WORKTREE_RECEIPT",
    "BASE_SHA",
    "PHASE_ID",
    "TARGET_SHA",
    "REVIEW_CONTRACT",
    "VERIFICATION_EVIDENCE",
    "OWNED_SCOPE",
    "VERIFICATION",
)
REVIEW_CONTRACT_FIELDS = {
    "TASK_ID",
    "PHASE_ID",
    "BASE_SHA",
    "TARGET_SHA",
    "GOAL",
    "MAIN_SCENARIO",
    "ACCEPTANCE",
    "VERIFICATION",
    "OWNED_SCOPE",
    "KNOWN_CONTRACTS",
    "DO_NOT_CHANGE",
    "INSTRUCTION",
}


def _number(value, name, *, maximum=None, positive=False):
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number or null")
    if value < (1 if positive else 0) or (maximum is not None and value > maximum):
        raise ValueError(f"{name} is outside its allowed range")


def policy(overrides=None):
    overrides = overrides or {}
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULTS):
        raise ValueError("unknown agent_context configuration")
    result = DEFAULTS | overrides
    for name in ("enabled", "telemetry", "automatic_handoff"):
        if type(result[name]) is not bool:
            raise ValueError(f"{name} must be boolean")
    for name in (
        "soft_context_percent",
        "hard_context_percent",
        "weekly_soft_remaining_percent",
        "weekly_hard_remaining_percent",
    ):
        _number(result[name], name, maximum=100)
    for name in ("soft_context_tokens", "hard_context_tokens"):
        _number(result[name], name, positive=True)
    for soft, hard in (
        ("soft_context_percent", "hard_context_percent"),
        ("soft_context_tokens", "hard_context_tokens"),
        ("weekly_hard_remaining_percent", "weekly_soft_remaining_percent"),
    ):
        if result[soft] is not None and result[hard] is not None and result[soft] >= result[hard]:
            raise ValueError(f"{soft} must be below {hard}")
    return result


def normalize_usage(observation):
    observation = observation or {}
    if not isinstance(observation, dict):
        raise ValueError("usage must be an object or null")
    result = {field: observation.get(field) for field in USAGE_FIELDS}
    details = observation.get("input_tokens_details") or {}
    output_details = observation.get("output_tokens_details") or {}
    if not isinstance(details, dict) or not isinstance(output_details, dict):
        raise ValueError("usage token details must be objects")
    if result["cached_input_tokens"] is None:
        result["cached_input_tokens"] = details.get("cached_tokens")
    if result["reasoning_tokens"] is None:
        result["reasoning_tokens"] = output_details.get("reasoning_tokens")
    if result["total_recorded_tokens"] is None:
        result["total_recorded_tokens"] = observation.get("total_tokens")
    for field, value in result.items():
        _number(value, field)
    if (
        result["total_recorded_tokens"] is None
        and result["input_tokens"] is not None
        and result["output_tokens"] is not None
    ):
        result["total_recorded_tokens"] = result["input_tokens"] + result["output_tokens"]
    return result


def evaluate(agent, overrides=None):
    settings = policy(overrides)
    if not isinstance(agent, dict):
        raise ValueError("agent observation must be an object")
    current, limit = agent.get("current_context_tokens"), agent.get("context_limit")
    _number(current, "current_context_tokens")
    _number(limit, "context_limit", positive=True)
    percent = current * 100 / limit if current is not None and limit is not None else None
    signal = "CONTINUE"
    source = None
    if settings["enabled"] and current is not None:
        if percent is not None:
            source = "context_percent"
            if (
                settings["hard_context_percent"] is not None
                and percent >= settings["hard_context_percent"]
            ):
                signal = "HANDOFF_REQUIRED"
            elif (
                settings["soft_context_percent"] is not None
                and percent >= settings["soft_context_percent"]
            ):
                signal = "HANDOFF_RECOMMENDED"
        if (
            settings["hard_context_tokens"] is not None
            and current >= settings["hard_context_tokens"]
        ):
            signal, source = "HANDOFF_REQUIRED", "context_tokens"
        elif (
            signal == "CONTINUE"
            and settings["soft_context_tokens"] is not None
            and current >= settings["soft_context_tokens"]
        ):
            signal, source = "HANDOFF_RECOMMENDED", "context_tokens"
    return {
        "signal": signal,
        "current_context_tokens": current,
        "context_limit": limit,
        "context_usage_percent": percent,
        "threshold_source": source,
    }


def normalize_allowance(observation):
    if observation is None:
        return {
            "status": "CODEX_ALLOWANCE_TELEMETRY_NOT_AVAILABLE",
            "session_remaining_percent": None,
            "weekly_remaining_percent": None,
            "reset_at": None,
        }
    if not isinstance(observation, dict):
        raise ValueError("allowance must be an object or null")
    result = {
        name: observation.get(name)
        for name in ("session_remaining_percent", "weekly_remaining_percent", "reset_at")
    }
    for name in ("session_remaining_percent", "weekly_remaining_percent"):
        _number(result[name], name, maximum=100)
    return {
        "status": "AVAILABLE"
        if any(value is not None for value in result.values())
        else "CODEX_ALLOWANCE_TELEMETRY_NOT_AVAILABLE",
        **result,
    }


def codex_limits_allowance(payload):
    """Adapt the exposed desktop get_usage_limits response, if supplied."""
    if not isinstance(payload, dict):
        return normalize_allowance(None)
    buckets = payload.get("rateLimitsByLimitId") or {}
    limits = buckets.get("codex") if isinstance(buckets, dict) else None
    if not isinstance(limits, dict):
        limits = payload.get("rateLimits") or {}
    windows = [limits.get(name) for name in ("primary", "secondary")]
    found = {}
    for window in windows:
        if not isinstance(window, dict):
            continue
        duration = window.get("windowDurationMins")
        kind = {300: "session", 10080: "weekly"}.get(duration)
        used = window.get("usedPercent")
        if kind is None or used is None:
            continue
        _number(used, "usedPercent", maximum=100)
        found[kind] = {"remaining": 100 - used, "reset": window.get("resetsAt")}
    return normalize_allowance(
        {
            "session_remaining_percent": found.get("session", {}).get("remaining"),
            "weekly_remaining_percent": found.get("weekly", {}).get("remaining"),
            "reset_at": {kind: item["reset"] for kind, item in found.items()} or None,
        }
    )


def allowance_guardrail(allowance, overrides=None):
    settings = policy(overrides)
    remaining = normalize_allowance(allowance)["weekly_remaining_percent"]
    if remaining is None:
        return "UNAVAILABLE"
    hard = settings["weekly_hard_remaining_percent"]
    soft = settings["weekly_soft_remaining_percent"]
    if hard is not None and remaining <= hard:
        return "HARD_LIMIT"
    if soft is not None and remaining <= soft:
        return "SOFT_LIMIT"
    return "CONTINUE"


def _issue_spawn(project_root, task_id, identity, prepared):
    if project_root is None or task_id is None or identity is None:
        raise ValueError(
            "DISPATCH_ROUTE_REQUIRED: final spawn requires host project and exact task identity"
        )
    intent = prepared.get("issuance_intent")
    if not isinstance(intent, dict):
        raise ValueError("DISPATCH_ROUTE_REQUIRED: final spawn requires bounded issuance intent")
    issued = devcrew_dispatch.issue_route(
        project_root,
        task_id=task_id,
        role=intent["role"],
        identity=identity,
        intent=intent,
        store_path=devcrew_capacity.dispatch_receipt_path(
            project_root, task_id, create=intent["role"] != "reviewer"
        ),
    )
    prepared["route"], prepared["dispatch"], prepared["spawn"] = (
        issued["PREPARED"]["route"],
        issued["PREPARED"]["dispatch"],
        issued["SPAWN_PAYLOAD"],
    )
    prepared["issued_route"] = {
        "ROUTE_ID": issued["ROUTE_ID"],
        "SPAWN_PAYLOAD_SHA256": issued["SPAWN_PAYLOAD_SHA256"],
    }
    return prepared


def _requirements_envelope(project_root, role, scope, task_class, task_id):
    """Read the current configured registry; preserve the empty-registry fast path."""
    project = pathlib.Path(project_root)
    try:
        from devcrew import _state_paths

        memory = _state_paths(project)["memory"]
        registry, _size = devcrew_requirements.load(project, memory)
        if not registry["requirements"]:
            return None
        if (
            not isinstance(scope.get("paths"), list)
            or not scope["paths"]
            or not isinstance(task_class, str)
            or not task_class
        ):
            raise ValueError(
                "ENGINEERING_REQUIREMENTS_SCOPE_REQUIRED: current task scope is required for a non-empty registry"
            )
        source_root = project
        if role in {"executor", "reviewer"}:
            binding, _receipt = _dispatch_worktree_binding(project, task_id)
            source_root = pathlib.Path(binding["task_worktree_root"])
        envelope = devcrew_requirements.context(
            project, memory, scope["paths"], role, task_class, source_root=source_root
        )
        envelope.update({"role": role, "task_class": task_class, "scope_paths": scope["paths"]})
        serialized, measured = devcrew_requirements.checked_serialized_context(envelope)
        envelope["context_bytes"] = measured
        return envelope
    except (OSError, ValueError) as exc:
        if "ENGINEERING_REQUIREMENTS" in str(exc):
            raise
        raise ValueError(f"ENGINEERING_REQUIREMENTS_INVALID: {exc}") from exc


def _validate_requirements_payload(project, task_id, message, role, expected_paths=None):
    match = re.search(r"(?:^|\n)ENGINEERING_REQUIREMENTS: (\{[^\n]*\})", message)
    from devcrew import _state_paths

    memory = _state_paths(pathlib.Path(project))["memory"]
    registry, _size = devcrew_requirements.load(project, memory)
    if not registry["requirements"] and not match:
        return
    if not match:
        raise ValueError(
            "ENGINEERING_REQUIREMENTS_REQUIRED: applicable registry was omitted from issued payload"
        )
    supplied = json.loads(match.group(1))
    if supplied.get("role") != role or not isinstance(supplied.get("task_class"), str):
        raise ValueError("ENGINEERING_REQUIREMENTS_MISMATCH: payload role/task class is invalid")
    route_match = re.search(r"(?:^|\n)MODEL_ROUTE: (\{[^\n]*\})", message)
    route = json.loads(route_match.group(1)) if route_match else {}
    if supplied["task_class"] != route.get("TASK_CLASS"):
        raise ValueError(
            "ENGINEERING_REQUIREMENTS_MISMATCH: envelope task class differs from authoritative route"
        )
    paths = supplied.get("scope_paths")
    if expected_paths is not None and paths != expected_paths:
        raise ValueError(
            "ENGINEERING_REQUIREMENTS_MISMATCH: payload scope differs from persisted phase scope"
        )
    expected = _requirements_envelope(
        project, role, {"paths": paths}, supplied["task_class"], task_id
    )
    if supplied != expected:
        raise ValueError(
            "ENGINEERING_REQUIREMENTS_STALE: current applicable requirements differ from issued payload"
        )


def _derive_dispatch_issuance(project_root, task_id, role, identity, intent):
    """Canonical issuance derivation boundary used by the durable receipt writer."""
    required = {"role", "task_name", "message", "runtime", "route_options", "role_contract"}
    optional = {
        "review_contract",
        "verification_evidence",
        "requirements_scope",
        "caller_selection",
    }
    if set(intent) - (required | optional) or required - set(intent):
        raise ValueError(
            "DISPATCH_ROUTE_REQUIRED: issuance intent fields are incomplete or unsupported"
        )
    if "caller_selection" in intent and (
        role != "reviewer" or not isinstance(intent["caller_selection"], dict)
    ):
        raise ValueError(
            "DISPATCH_ROUTE_REQUIRED: caller selection is only supported for reviewer issuance"
        )
    if (
        intent["role"] != role
        or not isinstance(intent["task_name"], str)
        or not intent["task_name"]
    ):
        raise ValueError("DISPATCH_ROUTE_REQUIRED: intent role or task name is invalid")
    if not isinstance(intent["message"], str) or not intent["message"]:
        raise ValueError("DISPATCH_ROUTE_REQUIRED: intent message is required")
    if re.search(r"(?:^|\n)ENGINEERING_REQUIREMENTS:", intent["message"]):
        raise ValueError(
            "DISPATCH_SERVICE_MARKER_DUPLICATE: requirements envelope is reserved for canonical issuance"
        )
    if intent["role_contract"] is not None and not isinstance(intent["role_contract"], str):
        raise ValueError("DISPATCH_ROUTE_REQUIRED: role contract must be text or null")
    route_options = intent["route_options"]
    if not isinstance(route_options, dict):
        raise ValueError("DISPATCH_ROUTE_REQUIRED: route intent must be an object")
    message = intent["message"]
    review_contract = review_evidence = None
    if role == "reviewer":
        review_contract, review_evidence = _authoritative_review_contract(
            project_root,
            task_id,
            identity,
            intent.get("review_contract"),
            intent.get("verification_evidence"),
        )
    if role == "planner":
        if re.search(r"(?:^|\n)INTENT_SOURCE:", message):
            raise ValueError(
                "DISPATCH_SERVICE_MARKER_DUPLICATE: INTENT_SOURCE is reserved for preflight"
            )
        receipt = devcrew_worktree.resolve(pathlib.Path(project_root), task_id, read_only=True)
        source = devcrew_phase.intent_source_preflight(pathlib.Path(project_root), receipt)
        message += "\nINTENT_SOURCE: " + json.dumps(source, ensure_ascii=False, sort_keys=True)
    selection = _spawn_selection(
        role, intent["runtime"], intent.get("caller_selection"), route_options
    )
    if role == "executor":
        message = _authoritative_executor_message(project_root, task_id, identity)
        if intent["message"] != message:
            raise ValueError(
                "PHASE_EXECUTION_CONTRACT_MISMATCH: executor intent differs from current host phase"
            )
    elif role == "reviewer":
        message += "\n" + _reviewer_contract(
            task_id,
            identity["PHASE_ID"],
            identity["BASE_SHA"],
            identity["TARGET_SHA"],
            review_contract,
            review_evidence,
        )
    elif "review_contract" in intent or "verification_evidence" in intent:
        raise ValueError(
            "REVIEW_CONTRACT_INVALID: reviewer contract/evidence supplied for non-reviewer route"
        )
    scope = intent.get("requirements_scope") if role == "planner" else None
    if role in {"executor", "reviewer"}:
        contract_line = re.search(
            r"(?:^|\n)PHASE_(?:EXECUTION|REVIEW)_CONTRACT: (\{[^\n]*\})", message
        )
        contract = json.loads(contract_line.group(1)) if contract_line else {}
        scope = {
            "paths": contract.get("OWNED_SCOPE", []),
            "task_class": selection.get("TASK_CLASS"),
        }
    if scope is not None:
        if not isinstance(scope, dict) or set(scope) != {"paths", "task_class"}:
            raise ValueError(
                "ENGINEERING_REQUIREMENTS_SCOPE_INVALID: explicit path/task-class scope required"
            )
        envelope = _requirements_envelope(
            project_root, role, scope, selection.get("TASK_CLASS"), task_id
        )
        if envelope is not None:
            serialized, _measured = devcrew_requirements.checked_serialized_context(envelope)
            message += "\nENGINEERING_REQUIREMENTS: " + serialized
    return _build_routed_spawn(
        intent["task_name"],
        message,
        selection,
        intent["runtime"],
        role_contract=intent["role_contract"],
    )


def _authoritative_executor_message(project_root, task_id, identity):
    """Build the exact executor contract from the current persisted phase input."""
    project = pathlib.Path(project_root)
    _binding, receipt = _dispatch_worktree_binding(project, task_id)
    manifest = devcrew_phase.load_for_resume(
        devcrew_phase.manifest_path(project, task_id), receipt, project=project
    )
    task_root = pathlib.Path(receipt["task_worktree_root"])
    verifier = getattr(sys.modules.get("__main__"), "_verified_record", None)
    if verifier is None:
        from devcrew import _verified_record as verifier
    phase_input = devcrew_phase.executor_input(manifest, task_root, project, verifier)
    phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_input["phase_id"])
    active = phase.get("active_local_fix")
    expected = {
        "TASK_ID": task_id,
        "PHASE_ID": phase_input["phase_id"],
        "BASE_SHA": phase_input["base_sha"],
        "WRITER_CYCLE": active["cycle_id"] if active else 0,
        "REJECTED_CANDIDATE_SHA": phase_input.get("rejected_candidate_sha"),
    }
    if identity != expected:
        raise ValueError(
            "PHASE_EXECUTOR_ROUTE_STALE: route must match current phase and writer cycle"
        )
    if "rejected_candidate_sha" in phase_input:
        contract = {
            "TASK_ID": task_id,
            "PHASE_ID": phase_input["phase_id"],
            "BASE_SHA": phase_input["base_sha"],
            "REJECTED_CANDIDATE_SHA": phase_input["rejected_candidate_sha"],
            "FINDINGS": phase_input["findings"],
            "OWNED_SCOPE": phase_input["owned_scope"],
            "ACCEPTANCE": phase_input["acceptance"],
            "VERIFICATION": phase_input["verification"],
            "INSTRUCTION": phase_input["instruction"] + " Do not commit.",
        }
        if "original_intent" in phase_input:
            contract["ORIGINAL_INTENT"] = phase_input["original_intent"]
        message = "PHASE_EXECUTION_CONTRACT: " + json.dumps(
            contract, ensure_ascii=False, sort_keys=True
        )
        message += "\nEXECUTION_LOCATION: " + json.dumps(
            {"PROJECT_ROOT": str(project), "TASK_WORKTREE_ROOT": str(task_root)},
            ensure_ascii=False,
            sort_keys=True,
        )
        return message
    contract = {
        "TASK_ID": task_id,
        "PHASE_ID": phase_input["phase_id"],
        "CURRENT_PHASE": phase_input["phase_id"],
        "TASK_WORKTREE_ROOT": phase_input["task_worktree_root"],
        "GOAL": phase_input["goal"],
        "MAIN_SCENARIO": phase_input["main_scenario"],
        "OWNED_SCOPE": phase_input["owned_scope"],
        "ACCEPTANCE": phase_input["acceptance"],
        "VERIFICATION": phase_input["verification"],
        "KNOWN_CONTRACTS": phase_input["known_contracts"],
        "DO_NOT_CHANGE": phase_input["do_not_change"],
        "INSTRUCTION": phase_input["instruction"] + " Do not commit.",
        "PROJECT_ROOT": str(project),
        "BASE_SHA": phase_input["base_sha"],
        "TASK_WORKTREE_RECEIPT": receipt,
    }
    if "original_intent" in phase_input:
        contract["ORIGINAL_INTENT"] = phase_input["original_intent"]
    return "PHASE_EXECUTION_CONTRACT: " + json.dumps(contract, ensure_ascii=False, sort_keys=True)


def _route_options(selection):
    """Reduce a prior policy result back to route intent for boundary re-derivation."""
    names = (
        "COORDINATION_COMPLEXITY",
        "DECISION_COMPLEXITY",
        "IMPLEMENTATION_COMPLEXITY",
        "REVIEW_COMPLEXITY",
    )
    result = {name.lower(): selection.get(name) for name in names}
    result.update(task_class=selection.get("TASK_CLASS"), risk=selection.get("RISK", "low"))
    reason = selection.get("SELECTION_REASON")
    if reason in devcrew_cost.ESCALATION_REASONS:
        result["escalation_reason"] = reason
    if reason == "repeated_failure":
        result["retry_stage"] = 3
    return result


def _validate_reviewer_issuance(
    project_root, task_id, identity, review_contract, verification_evidence
):
    """Require the host-persisted formal review start before issuing a reviewer route."""
    project = pathlib.Path(project_root)
    if not project.is_absolute() or not isinstance(task_id, str):
        raise ValueError(
            "PHASE_REVIEW_START_REQUIRED: reviewer route requires a persisted host review start"
        )
    try:
        _binding, receipt = _dispatch_worktree_binding(project, task_id)
        path = devcrew_phase.manifest_path(project, task_id)
        manifest = devcrew_phase.load_for_resume(path, receipt, project=project)
        phase_id = identity["PHASE_ID"]
        phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_id)
        candidate = phase.get("candidate_commit")
        expected_identity = (
            {
                "TASK_ID": task_id,
                "PHASE_ID": phase_id,
                "BASE_SHA": candidate.get("base"),
                "TARGET_SHA": candidate.get("sha"),
            }
            if isinstance(candidate, dict)
            else {}
        )
        expected = devcrew_phase.review_target(manifest, phase_id)
        if (
            manifest["task_id"] != task_id
            or manifest.get("current_phase_id") != phase_id
            or phase.get("status") != "REVIEWING"
            or phase.get("review_start") != expected_identity
            or identity != expected_identity
            or review_contract != expected
        ):
            raise ValueError(
                "PHASE_REVIEW_START_REQUIRED: reviewer route requires matching persisted review-start identity"
            )
        task_root = pathlib.Path(receipt["task_worktree_root"])
        verifier = getattr(sys.modules.get("__main__"), "_verified_record", None)
        if verifier is None:
            from devcrew import _verified_record as verifier
        devcrew_phase._verify_candidate_commit(manifest, phase, task_root)
        devcrew_phase._verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        persisted = candidate.get("evidence")
        if verification_evidence != persisted:
            raise ValueError(
                "PHASE_REVIEW_EVIDENCE_MISMATCH: reviewer evidence must match persisted candidate evidence"
            )
        return expected, persisted
    except ValueError as exc:
        if "PHASE_REVIEW" in str(exc):
            raise
        raise ValueError(f"PHASE_REVIEW_START_REQUIRED: {exc}") from exc


def _authoritative_review_contract(
    project_root, task_id, identity, caller_contract, caller_evidence
):
    """Load the current formal review target and cross-check caller observations."""
    # The persisted target and candidate evidence are reloaded at issuance time.
    derived, evidence = _validate_reviewer_issuance(
        project_root, task_id, identity, caller_contract, caller_evidence
    )
    return derived, evidence


def _dispatch_worktree_binding(project_root, task_id, expected_binding=None):
    """Resolve only an existing safe task checkout and return stable identity."""
    project = pathlib.Path(project_root)
    metadata = devcrew_worktree._repository_details(project)
    root, _common, repository_id, task_root, branch, state_path = devcrew_worktree.location(
        project, task_id, _repository_metadata=metadata
    )
    for candidate in (task_root, state_path):
        if devcrew_worktree._has_symlink_component(candidate):
            raise ValueError(
                "TASK_WORKTREE_PATH_COLLISION: registered task path or state contains a symlink"
            )
    if not task_root.is_dir() or not state_path.is_file():
        raise ValueError(
            "TASK_WORKTREE_UNSAFE_MISSING: registered task worktree must already exist"
        )
    receipt = devcrew_worktree.resolve(
        project, task_id, read_only=True, _repository_metadata=metadata
    )
    if receipt.get("state") != "RESOLVED":
        raise ValueError("TASK_WORKTREE_UNSAFE_MISSING: task worktree is not currently resolved")
    binding = {
        "repository_id": repository_id,
        "task_id": task_id,
        "task_worktree_root": str(task_root),
        "branch": branch,
        "expected_head": receipt["expected_head"],
    }
    manifest_path = devcrew_phase.manifest_path(project, task_id, _repository_metadata=metadata)
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            generation = manifest.get("contract_generation", 0)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"TASK_WORKTREE_BINDING_STALE: phase manifest is unreadable: {exc}"
            ) from None
        if type(generation) is not int or generation < 0:
            raise ValueError("TASK_WORKTREE_BINDING_STALE: contract generation is invalid")
        devcrew_phase._verify_pre_candidate_history(project, manifest)
        if generation:
            binding["contract_generation"] = generation
    if expected_binding is not None and expected_binding != binding:
        raise ValueError("TASK_WORKTREE_BINDING_STALE: issued route no longer matches its worktree")
    return binding, receipt


def _validate_dispatch_authority(
    project_root,
    task_id,
    role,
    identity,
    spawn_payload,
    expected_binding=None,
    *,
    already_issued=False,
):
    """Recheck host-owned role authority at the durable issuance boundary."""
    project = pathlib.Path(project_root)
    if not project.is_absolute():
        raise ValueError("DISPATCH_ROUTE_REQUIRED: host project must be absolute")
    try:
        if already_issued and not isinstance(expected_binding, dict):
            raise ValueError(
                "DISPATCH_ROUTE_REQUIRED: issued route has no immutable worktree binding"
            )
        pinned_planner_authorizer = False
        if role == "planner" and isinstance(expected_binding, dict):
            binding, receipt = _dispatch_worktree_binding(project, task_id)
            if expected_binding != binding:
                current_generation = binding.get("contract_generation", 0)
                try:
                    manifest = json.loads(
                        devcrew_phase.manifest_path(project, task_id).read_text(encoding="utf-8")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    manifest = {}
                pinned = manifest.get("planner_dispatch") or {}
                match = re.search(
                    r"(?:^|\n)MODEL_ROUTE: (\{[^\n]*\})", spawn_payload.get("message", "")
                )
                route = json.loads(match.group(1)) if match else {}
                pinned_route_id = pinned.get("route_id")
                if pinned_route_id is None and pinned.get("spawn_receipt_id"):
                    try:
                        store = devcrew_dispatch._read(
                            devcrew_capacity.dispatch_receipt_path(project, task_id)
                        )
                        prior = next(
                            (
                                item
                                for item in store["receipts"]
                                if item.get("SPAWN_RECEIPT_ID") == pinned["spawn_receipt_id"]
                            ),
                            None,
                        )
                        pinned_route_id = prior.get("ROUTE_ID") if prior else None
                    except (OSError, ValueError, StopIteration):
                        pinned_route_id = None
                stable_expected = {
                    key: value
                    for key, value in expected_binding.items()
                    if key not in {"contract_generation", "expected_head"}
                }
                stable_current = {
                    key: value
                    for key, value in binding.items()
                    if key not in {"contract_generation", "expected_head"}
                }
                authorizer = (
                    pinned_route_id == route.get("ROUTE_ID")
                    and pinned.get("spawn_receipt_id")
                    and manifest.get("worktree", {}).get("base_sha") == identity.get("BASE_SHA")
                    and stable_expected == stable_current
                    and expected_binding.get("contract_generation", 0)
                    == max(0, current_generation - 1)
                    and (
                        current_generation == 0
                        or (
                            pinned.get("reapproval_authorizer") is True
                            and pinned.get("authorization_generation") == current_generation - 1
                        )
                    )
                )
                if not authorizer:
                    raise ValueError(
                        "TASK_WORKTREE_BINDING_STALE: issued planner route is from an old contract generation"
                    )
                pinned_planner_authorizer = True
        else:
            binding, receipt = _dispatch_worktree_binding(project, task_id, expected_binding)
        if role == "planner":
            original_base = None
            if pinned_planner_authorizer:
                original_base = manifest.get("worktree", {}).get("base_sha")
            if identity.get("BASE_SHA") != (original_base or binding["expected_head"]):
                raise ValueError(
                    "PLANNER_BASE_MISMATCH: planner must use the registered original base"
                )
            # New routes carry the exact preflight source. Reread it on
            # postflight/resume without depending on the mutable selection.
            matches = re.findall(r"(?:^|\n)INTENT_SOURCE: (\{[^\n]*\})", spawn_payload["message"])
            if len(matches) > 1:
                raise ValueError("INTENT_SOURCE_REQUIRED: ambiguous planner source")
            if matches:
                devcrew_phase.intent_source_preflight(
                    project, receipt, source=json.loads(matches[0])
                )
            _validate_requirements_payload(project, task_id, spawn_payload["message"], role)
            return binding
        manifest = devcrew_phase.load_for_resume(
            devcrew_phase.manifest_path(project, task_id), receipt, project=project
        )
        phase = next(
            item for item in manifest["phases"] if item["phase_id"] == identity.get("PHASE_ID")
        )
        if role == "executor":
            task_root = pathlib.Path(receipt["task_worktree_root"])
            verifier = getattr(sys.modules.get("__main__"), "_verified_record", None)
            if verifier is None:
                from devcrew import _verified_record as verifier
            if phase.get("status") == "READY_FOR_CLOSE":
                close_manifest = json.loads(json.dumps(manifest))
                next(
                    item
                    for item in close_manifest["phases"]
                    if item["phase_id"] == phase["phase_id"]
                )["status"] = "VERIFYING"
                phase_input = devcrew_phase.executor_input(
                    close_manifest, task_root, project, verifier
                )
            else:
                phase_input = devcrew_phase.executor_input(manifest, task_root, project, verifier)
            active = phase.get("active_local_fix")
            expected = {
                "TASK_ID": task_id,
                "PHASE_ID": phase_input["phase_id"],
                "BASE_SHA": phase_input["base_sha"],
                "WRITER_CYCLE": active["cycle_id"] if active else 0,
                "REJECTED_CANDIDATE_SHA": phase_input.get("rejected_candidate_sha"),
            }
            if identity != expected:
                raise ValueError(
                    "PHASE_EXECUTOR_ROUTE_STALE: route must match current phase and writer cycle"
                )
            match = re.search(
                r"(?:^|\n)PHASE_EXECUTION_CONTRACT: (\{[^\n]*\})", spawn_payload["message"]
            )
            if not match:
                raise ValueError(
                    "PHASE_EXECUTION_CONTRACT_REQUIRED: executor payload must carry the host phase contract"
                )
            supplied = json.loads(match.group(1))
            if "rejected_candidate_sha" in phase_input:
                expected_contract = {
                    "TASK_ID": phase_input["task_id"],
                    "PHASE_ID": phase_input["phase_id"],
                    "BASE_SHA": identity["BASE_SHA"],
                    "REJECTED_CANDIDATE_SHA": phase_input["rejected_candidate_sha"],
                    "FINDINGS": phase_input["findings"],
                    "OWNED_SCOPE": phase_input["owned_scope"],
                    "ACCEPTANCE": phase_input["acceptance"],
                    "VERIFICATION": phase_input["verification"],
                    "INSTRUCTION": phase_input["instruction"] + " Do not commit.",
                }
                if "original_intent" in phase_input:
                    expected_contract["ORIGINAL_INTENT"] = phase_input["original_intent"]
            else:
                expected_contract = {
                    "TASK_ID": phase_input["task_id"],
                    "PHASE_ID": phase_input["phase_id"],
                    "CURRENT_PHASE": phase_input["phase_id"],
                    "TASK_WORKTREE_ROOT": phase_input["task_worktree_root"],
                    "GOAL": phase_input["goal"],
                    "MAIN_SCENARIO": phase_input["main_scenario"],
                    "OWNED_SCOPE": phase_input["owned_scope"],
                    "ACCEPTANCE": phase_input["acceptance"],
                    "VERIFICATION": phase_input["verification"],
                    "KNOWN_CONTRACTS": phase_input["known_contracts"],
                    "DO_NOT_CHANGE": phase_input["do_not_change"],
                    "INSTRUCTION": phase_input["instruction"] + " Do not commit.",
                    "PROJECT_ROOT": str(project),
                    "BASE_SHA": identity["BASE_SHA"],
                    "TASK_WORKTREE_RECEIPT": receipt,
                }
            if "rejected_candidate_sha" not in phase_input:
                bound_receipt = supplied.get("TASK_WORKTREE_RECEIPT")
                stable_fields = (
                    "owner",
                    "state",
                    "task_id",
                    "repository_id",
                    "task_worktree_root",
                    "branch",
                    "expected_head",
                    "head",
                )
                if not isinstance(bound_receipt, dict) or any(
                    bound_receipt.get(key) != receipt.get(key) for key in stable_fields
                ):
                    raise ValueError(
                        "TASK_WORKTREE_BINDING_STALE: phase payload has a mismatched worktree receipt"
                    )
                expected_contract["TASK_WORKTREE_RECEIPT"] = bound_receipt
            if "original_intent" in phase_input:
                expected_contract["ORIGINAL_INTENT"] = phase_input["original_intent"]
            if supplied != expected_contract:
                raise ValueError(
                    "PHASE_EXECUTION_CONTRACT_MISMATCH: payload must match host current phase"
                )
            _validate_requirements_payload(
                project, task_id, spawn_payload["message"], role, phase_input["owned_scope"]
            )
            return binding
        if role == "reviewer":
            contract_match = re.search(
                r"(?:^|\n)PHASE_REVIEW_CONTRACT: (\{[^\n]*\})", spawn_payload["message"]
            )
            evidence_match = re.search(
                r"(?:^|\n)VERIFICATION_EVIDENCE: (\[[^\n]*\])", spawn_payload["message"]
            )
            if not contract_match or not evidence_match:
                raise ValueError(
                    "PHASE_REVIEW_CONTRACT_REQUIRED: final reviewer payload must include contract and evidence"
                )
            contract = json.loads(contract_match.group(1))
            evidence = json.loads(evidence_match.group(1))
            _validate_reviewer_issuance(project, task_id, identity, contract, evidence)
            _validate_requirements_payload(
                project, task_id, spawn_payload["message"], role, contract.get("OWNED_SCOPE", [])
            )
            return binding
        raise ValueError("DISPATCH_ROUTE_REQUIRED: unsupported dispatch role")
    except (KeyError, StopIteration, OSError, ValueError) as exc:
        if "DISPATCH_ROUTE_REQUIRED" in str(exc) or "PHASE_" in str(exc) or "PLANNER_" in str(exc):
            raise
        raise ValueError(
            f"DISPATCH_ROUTE_REQUIRED: role authority could not be verified: {exc}"
        ) from exc
    except Exception as exc:
        raise ValueError(f"PHASE_REVIEW_START_REQUIRED: {exc}") from exc


def _build_routed_spawn(task_name, message, selection, runtime, *, role_contract=None):
    """Build an unissued payload for an internal final-mutation pipeline only.

    The capability snapshot must describe actual loaded roles, rather than
    role-file existence. A generic fallback needs the exact supplied contract.
    """
    if not isinstance(selection, dict) or not selection.get("RESOLVED_MODEL"):
        raise ValueError("AGENT_RUNTIME_REQUIRED: unresolved route")
    if not isinstance(runtime, dict) or runtime.get("explicit_model_effort") is not True:
        raise ValueError("AGENT_RUNTIME_REQUIRED: explicit model/effort controls unavailable")
    selection = _spawn_selection(selection.get("ROLE"), runtime, selection, {})
    selection["ROUTE_ID"] = uuid.uuid4().hex
    model, effort = selection["RESOLVED_MODEL"], selection.get("REASONING_EFFORT")
    observed = next(
        (item for item in devcrew_cost.runtime_models(runtime) if item["id"] == model), None
    )
    if observed is None or effort not in observed["efforts"]:
        raise ValueError("AGENT_RUNTIME_REQUIRED: route unavailable in current snapshot")
    role = selection.get("ROLE")
    typed = {
        "executor": "executor-agent",
        "reviewer": "reviewer-agent",
        "planner": "decision-agent",
    }.get(role)
    roles = runtime.get("roles", {})
    if not isinstance(roles, dict):
        raise ValueError("runtime roles must be an object")
    settings = roles.get(typed, {}) if typed else {}
    if not isinstance(settings, dict):
        raise ValueError("runtime role settings must be an object")
    # Select once before the caller invokes runtime. The generic tool's
    # model/effort fields do not prove this specific typed role can override.
    available = bool(typed and settings.get("available") is True)
    matches = settings.get("model") == model and settings.get("reasoning_effort") == effort
    if available and matches:
        mode, reason = "typed", "typed_model_effort_match"
    elif available and settings.get("supports_overrides") is True:
        mode, reason = "typed", "typed_supported_override"
    else:
        mode = "generic"
        if not available:
            reason = "unavailable_role"
        elif settings.get("model") is not None and settings["model"] != model:
            reason = "pinned_model_mismatch"
        elif (
            settings.get("reasoning_effort") is not None and settings["reasoning_effort"] != effort
        ):
            reason = "pinned_effort_mismatch"
        else:
            reason = "unconfirmed_role_override"
        if runtime.get("generic_available") is not True:
            raise ValueError(
                "AGENT_RUNTIME_REQUIRED: compatible typed role and generic fallback unavailable"
            )
        if role != "reviewer" and (not isinstance(role_contract, str) or not role_contract.strip()):
            raise ValueError(
                "AGENT_RUNTIME_REQUIRED: generic fallback requires exact role contract"
            )
    agent_type = typed if mode == "typed" else "default"
    dispatch = {
        "SPAWN_MODE": mode,
        "SPAWN_REASON": reason,
        "ROLE_FALLBACK": "generic" if mode == "generic" else None,
        "ROUTE_APPLIED": False,
    }
    loop_guard = (
        "LOOP_GUARD: Correct evidenced technical errors autonomously within accepted scope. "
        "Repeated FAIL alone is not a loop. Before another correction/probe, state what was learned, "
        "the bounded next action, expected result/check and proportionate cost. Resolve reviewer "
        "disputes against requirements, code and tests; model opinion alone is not proof. Stop with "
        "STATUS: LOOP_DETECTED for repeated actions without discriminating evidence, oscillation "
        "or unchanged disputes. Return the technical blocker to the coordinator, which resolves it "
        "internally when possible; ask the user only for a concrete owner choice, missing data/access "
        "or material work/cost authorization. Preserve history across handoffs and agents/models. "
        "A boolean assertion or rewording is not progress. Expected test-first RED and planned "
        "bounded reproduction alone are not recurrence. Follow skills/devcrew-task/references/loop-guard.md."
    )
    message = loop_guard + "\n" + message
    if role == "reviewer":
        package = pathlib.Path(__file__).resolve().parents[1]
        role_path = package / "skills/devcrew-setup/assets/agents/reviewer-agent.toml"
        policy_path = package / "skills/devcrew-review/references/coverage.md"
        reviewer_instructions = tomllib.loads(role_path.read_text(encoding="utf-8"))[
            "developer_instructions"
        ]
        message = (
            reviewer_instructions + "\n" + policy_path.read_text(encoding="utf-8") + "\n" + message
        )
        if role_contract == reviewer_instructions:
            role_contract = None
    # Preserve caller-provided role text byte-for-byte, including on fallback.
    if role_contract:
        message = role_contract + "\n" + message
    message += "\nMODEL_ROUTE: " + json.dumps(selection, ensure_ascii=False, sort_keys=True)
    message += "\nDISPATCH_SELECTION: " + json.dumps(dispatch, sort_keys=True)
    spawn = {
        "task_name": task_name,
        "agent_type": agent_type,
        "fork_turns": "none",
        "message": message,
        "model": model,
        "reasoning_effort": effort,
    }
    return {"spawn": spawn, "route": selection, "dispatch": dispatch}


def routed_spawn(
    task_name,
    message,
    selection,
    runtime,
    *,
    role_contract=None,
    project_root=None,
    task_id=None,
    identity=None,
    review_contract=None,
    verification_evidence=None,
    requirements_scope=None,
):
    """Build and durably issue a runnable DevCrew dispatch payload."""
    if (
        not isinstance(project_root, (str, pathlib.Path))
        or not isinstance(task_id, str)
        or not task_id
        or not isinstance(identity, dict)
    ):
        raise ValueError(
            "DISPATCH_ROUTE_REQUIRED: final routed spawn requires host project, task ID and role identity"
        )
    original_message = message
    if isinstance(selection, dict) and selection.get("ROLE") == "reviewer":
        review_context = _reviewer_contract(
            task_id,
            identity.get("PHASE_ID"),
            identity.get("BASE_SHA"),
            identity.get("TARGET_SHA"),
            review_contract,
            verification_evidence,
        )
        message += "\n" + review_context
    elif isinstance(selection, dict) and selection.get("ROLE") == "executor":
        raise ValueError("TASK_WORKTREE_REQUIRED: executor routes must use routed_task_spawn")
    elif review_contract is not None or verification_evidence is not None:
        raise ValueError(
            "REVIEW_CONTRACT_INVALID: reviewer contract/evidence supplied for a non-reviewer route"
        )
    prepared = (
        {}
        if isinstance(selection, dict) and selection.get("ROLE") == "reviewer"
        else _build_routed_spawn(
            task_name, message, selection, runtime, role_contract=role_contract
        )
    )
    prepared["issuance_intent"] = {
        "role": selection["ROLE"],
        "task_name": task_name,
        "message": original_message,
        "runtime": runtime,
        "route_options": _route_options(selection),
        "role_contract": role_contract,
        **({"caller_selection": selection} if selection["ROLE"] == "reviewer" else {}),
        **(
            {"review_contract": review_contract, "verification_evidence": verification_evidence}
            if selection["ROLE"] == "reviewer"
            else {}
        ),
        **(
            {"requirements_scope": requirements_scope}
            if selection["ROLE"] == "planner" and requirements_scope is not None
            else {}
        ),
    }
    return _issue_spawn(project_root, task_id, identity, prepared)


def routed_task_spawn(
    task_name,
    message,
    selection,
    runtime,
    *,
    task_id,
    project_root,
    task_worktree_root,
    base_sha,
    owned_scope,
    acceptance,
    verification,
    receipt=None,
    role_contract=None,
):
    """Build the executor payload bound to its registered task checkout."""
    if not isinstance(selection, dict) or selection.get("ROLE") != "executor":
        raise ValueError("task worktree payload requires an executor route")
    fields = {
        "TASK_ID": task_id,
        "PROJECT_ROOT": project_root,
        "TASK_WORKTREE_ROOT": task_worktree_root,
        "BASE_SHA": base_sha,
        "OWNED_SCOPE": owned_scope,
        "ACCEPTANCE": acceptance,
        "VERIFICATION": verification,
    }
    if any(value is None or value == "" for value in fields.values()):
        raise ValueError("AGENT_RUNTIME_REQUIRED: complete task worktree contract required")
    if not isinstance(owned_scope, list) or not owned_scope:
        raise ValueError("OWNED_SCOPE must be a nonempty array")
    if (
        not isinstance(acceptance, list)
        or not acceptance
        or not isinstance(verification, list)
        or not verification
    ):
        raise ValueError("ACCEPTANCE and VERIFICATION must be nonempty arrays")
    project_path = pathlib.Path(project_root)
    worktree_path = pathlib.Path(task_worktree_root)
    if not project_path.is_absolute() or not worktree_path.is_absolute():
        raise ValueError(
            "TASK_WORKTREE_INVALID: TASK_WORKTREE_ROOT and PROJECT_ROOT must be absolute"
        )
    if not isinstance(base_sha, str) or not re.fullmatch(
        r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", base_sha
    ):
        raise ValueError("BASE_SHA must be a full commit object ID")
    if receipt is None:
        raise ValueError("TASK_WORKTREE_INVALID: expected fresh lifecycle receipt")
    # CREATE is a lifecycle step; RESOLVE supplies the dispatch-time snapshot.
    _binding, receipt = _dispatch_worktree_binding(project_path, task_id)
    if (
        pathlib.Path(receipt["task_worktree_root"]).resolve() != worktree_path.resolve()
        or receipt["expected_head"].lower() != base_sha.lower()
    ):
        raise devcrew_phase.PhaseError(
            "WORKTREE_LINKAGE_MISMATCH", "resolved worktree does not match executor contract"
        )
    # Executor assignments are sourced only from the durable host phase manifest.
    # Caller narrative and task-level fields are not write authority.
    manifest_file = devcrew_phase.manifest_path(project_path, task_id)
    manifest = devcrew_phase.load_for_resume(manifest_file, receipt, project_path)
    linked = manifest["worktree"]
    if (
        manifest["task_id"] != task_id
        or pathlib.Path(linked["task_worktree_root"]).resolve() != worktree_path.resolve()
    ):
        raise devcrew_phase.PhaseError(
            "WORKTREE_LINKAGE_MISMATCH", "phase manifest does not match executor worktree contract"
        )
    verifier = getattr(sys.modules.get("__main__"), "_verified_record", None)
    if verifier is None:
        from devcrew import _verified_record as verifier
    phase_input = devcrew_phase.executor_input(manifest, worktree_path, project_path, verifier)
    if phase_input["base_sha"].lower() != base_sha.lower():
        raise devcrew_phase.PhaseError(
            "WORKTREE_LINKAGE_MISMATCH",
            "executor base SHA differs from the accepted current-phase base",
        )
    # Mark writer dispatch only after manifest and phase gates have passed.
    _validate_task_worktree(project_root, task_id, task_worktree_root, base_sha, receipt)
    # The lifecycle validator rotates its nonce when a writer dispatch is recorded.
    receipt = devcrew_worktree.resolve(project_path, task_id, read_only=True)
    if "rejected_candidate_sha" in phase_input:
        phase_contract = {
            "TASK_ID": phase_input["task_id"],
            "PHASE_ID": phase_input["phase_id"],
            "BASE_SHA": base_sha,
            "REJECTED_CANDIDATE_SHA": phase_input["rejected_candidate_sha"],
            "FINDINGS": phase_input["findings"],
            "OWNED_SCOPE": phase_input["owned_scope"],
            "ACCEPTANCE": phase_input["acceptance"],
            "VERIFICATION": phase_input["verification"],
            "INSTRUCTION": phase_input["instruction"] + " Do not commit.",
        }
        if "original_intent" in phase_input:
            phase_contract["ORIGINAL_INTENT"] = phase_input["original_intent"]
    else:
        phase_contract = {
            "TASK_ID": phase_input["task_id"],
            "PHASE_ID": phase_input["phase_id"],
            "CURRENT_PHASE": phase_input["phase_id"],
            "TASK_WORKTREE_ROOT": phase_input["task_worktree_root"],
            "GOAL": phase_input["goal"],
            "MAIN_SCENARIO": phase_input["main_scenario"],
            "OWNED_SCOPE": phase_input["owned_scope"],
            "ACCEPTANCE": phase_input["acceptance"],
            "VERIFICATION": phase_input["verification"],
            "KNOWN_CONTRACTS": phase_input["known_contracts"],
            "DO_NOT_CHANGE": phase_input["do_not_change"],
            "INSTRUCTION": phase_input["instruction"] + " Do not commit.",
            "PROJECT_ROOT": str(project_path),
            "BASE_SHA": base_sha,
            "TASK_WORKTREE_RECEIPT": receipt,
        }
        if "original_intent" in phase_input:
            phase_contract["ORIGINAL_INTENT"] = phase_input["original_intent"]
    contract = "PHASE_EXECUTION_CONTRACT: " + json.dumps(
        phase_contract, ensure_ascii=False, sort_keys=True
    )
    # `message` may contain the planner's whole-task notes; it is deliberately not
    # forwarded to a writer. The manifest owns the current phase assignment.
    spawned = _build_routed_spawn(
        task_name, contract, selection, runtime, role_contract=role_contract
    )
    if "rejected_candidate_sha" in phase_input:
        # Location is dispatch metadata for the receiving executor's cwd selection;
        # keep it outside the bounded repair contract itself.
        spawned["spawn"]["message"] += "\nEXECUTION_LOCATION: " + json.dumps(
            {"PROJECT_ROOT": str(project_path), "TASK_WORKTREE_ROOT": str(worktree_path)},
            ensure_ascii=False,
            sort_keys=True,
        )
        contract += "\nEXECUTION_LOCATION: " + json.dumps(
            {"PROJECT_ROOT": str(project_path), "TASK_WORKTREE_ROOT": str(worktree_path)},
            ensure_ascii=False,
            sort_keys=True,
        )
    active_fix = phase_input.get("rejected_candidate_sha")
    phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_input["phase_id"])
    active = phase.get("active_local_fix")
    identity = {
        "TASK_ID": task_id,
        "PHASE_ID": phase_input["phase_id"],
        "BASE_SHA": phase_input["base_sha"],
        "WRITER_CYCLE": active["cycle_id"] if active else 0,
        "REJECTED_CANDIDATE_SHA": active_fix,
    }
    spawned["issuance_intent"] = {
        "role": "executor",
        "task_name": task_name,
        "message": contract,
        "runtime": runtime,
        "route_options": _route_options(selection),
        "role_contract": role_contract,
    }
    _issue_spawn(project_path, task_id, identity, spawned)
    return spawned


def _validate_task_worktree(project_root, task_id, task_worktree_root, base_sha, receipt):
    """Verify the executor root is the registered task worktree for this project."""
    invalid = "TASK_WORKTREE_INVALID: expected registered linked task worktree"
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id)
        or fs.unsafe_parts([task_id])
    ):
        raise ValueError(invalid)
    project = pathlib.Path(project_root)
    supplied_worktree = pathlib.Path(task_worktree_root)
    if not project.is_absolute() or not supplied_worktree.is_absolute():
        raise ValueError(invalid)

    def git(directory, *args):
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        )
        if result.returncode:
            raise ValueError(invalid)
        return result.stdout.strip()

    try:
        repository = pathlib.Path(git(project, "rev-parse", "--show-toplevel")).resolve()

        def has_symlink_component(path):
            current = pathlib.Path(path.anchor)
            for part in path.parts[1:]:
                current /= part
                if fs.is_link(current):
                    return True
            return False

        registered_path = pathlib.Path(os.path.abspath(supplied_worktree))
        if not registered_path.is_dir() or has_symlink_component(registered_path):
            raise ValueError(invalid)
        _, _, _, expected, branch, _ = devcrew_worktree.location(repository, task_id)
        if has_symlink_component(expected) or registered_path != expected:
            raise ValueError(invalid)
        if subprocess.run(
            ["git", "-C", str(repository), "check-ref-format", "--branch", branch],
            capture_output=True,
        ).returncode:
            raise ValueError(invalid)
        entries = git(repository, "worktree", "list", "--porcelain").split("\n\n")
        registered = []
        for block in entries:
            fields = {}
            for line in block.splitlines():
                key, _, value = line.partition(" ")
                fields[key] = value
            if (
                fields.get("worktree")
                and pathlib.Path(os.path.abspath(fields["worktree"])) == registered_path
            ):
                registered.append(fields)
        if len(registered) != 1 or registered[0].get("branch") != f"refs/heads/{branch}":
            raise ValueError(invalid)
        checkout = pathlib.Path(
            os.path.abspath(git(registered_path, "rev-parse", "--show-toplevel"))
        )
        common_repo = pathlib.Path(
            git(repository, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        common_worktree = pathlib.Path(
            git(registered_path, "rev-parse", "--path-format=absolute", "--git-common-dir")
        ).resolve()
        if checkout != registered_path or common_repo != common_worktree:
            raise ValueError(invalid)
        resolved_base = git(repository, "rev-parse", "--verify", f"{base_sha}^{{commit}}")
        head = git(registered_path, "rev-parse", "HEAD")
        if resolved_base.lower() != base_sha.lower() or head.lower() != base_sha.lower():
            raise ValueError(invalid)
        devcrew_worktree.validate_dispatch(repository, task_id, registered_path, base_sha, receipt)
    except (OSError, ValueError, RuntimeError):
        raise ValueError(invalid) from None


def _spawn_selection(role, runtime, selection, options):
    if selection is None:
        selection = devcrew_cost.route(role, runtime=runtime, **options)
    expected_role = devcrew_cost.ROLE_ALIASES.get(role, role)
    if selection.get("ROLE") != expected_role:
        raise ValueError(
            "route role does not match spawn role; dispatch replan to planner separately"
        )
    if not selection.get("RESOLVED_MODEL"):
        raise ValueError(
            "AGENT_RUNTIME_REQUIRED: " + selection.get("RESOLUTION_STATUS", "unresolved route")
        )
    # Confirm this selection against policy and the current snapshot. Do not
    # accept old observed model/effort fields as a replacement policy decision.
    requested = {
        name.lower(): selection.get(name)
        for name in (
            "COORDINATION_COMPLEXITY",
            "DECISION_COMPLEXITY",
            "IMPLEMENTATION_COMPLEXITY",
            "REVIEW_COMPLEXITY",
        )
    }
    reason = selection.get("SELECTION_REASON")
    requested.update(task_class=selection.get("TASK_CLASS"), risk=selection.get("RISK", "low"))
    if reason in devcrew_cost.ESCALATION_REASONS:
        requested["escalation_reason"] = reason
    if reason == "repeated_failure":
        requested["retry_stage"] = 3
    current = devcrew_cost.route(role, runtime=runtime, **requested)
    if any(
        current.get(key) != selection.get(key)
        for key in ("ROLE", "MODEL_CLASS", "RESOLVED_MODEL", "REASONING_EFFORT", "SELECTION_REASON")
    ):
        raise ValueError("route is stale or conflicts with policy; resolve again")
    return current


def _reviewer_contract(
    task_id, phase_id, base_sha, target_sha, review_contract, verification_evidence
):
    optional = {"ORIGINAL_INTENT"}
    if not isinstance(review_contract, dict) or set(review_contract) not in (
        REVIEW_CONTRACT_FIELDS,
        REVIEW_CONTRACT_FIELDS | optional,
    ):
        raise ValueError("REVIEW_CONTRACT_REQUIRED: supply the exact formal review-start contract")
    expected = {
        "TASK_ID": task_id,
        "PHASE_ID": phase_id,
        "BASE_SHA": base_sha,
        "TARGET_SHA": target_sha,
    }
    if any(review_contract.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "REVIEW_CONTRACT_STALE: formal review contract identity differs from reviewer route"
        )
    if not isinstance(review_contract.get("GOAL"), str) or not review_contract["GOAL"].strip():
        raise ValueError("REVIEW_CONTRACT_REQUIRED: GOAL must be nonempty text")
    scenario = review_contract["MAIN_SCENARIO"]
    if scenario is not None and (not isinstance(scenario, str) or not scenario.strip()):
        raise ValueError(
            "REVIEW_CONTRACT_REQUIRED: MAIN_SCENARIO must be nonempty text or null for legacy plans"
        )
    for field in ("ACCEPTANCE", "VERIFICATION", "OWNED_SCOPE", "KNOWN_CONTRACTS", "DO_NOT_CHANGE"):
        values = review_contract.get(field)
        if not isinstance(values, list) or any(
            not isinstance(value, str) or not value.strip() for value in values
        ):
            raise ValueError(
                f"REVIEW_CONTRACT_REQUIRED: {field} must be an array of nonempty strings"
            )
    if (
        not isinstance(review_contract.get("INSTRUCTION"), str)
        or not review_contract["INSTRUCTION"].strip()
    ):
        raise ValueError("REVIEW_CONTRACT_REQUIRED: formal review instruction is required")
    if "ORIGINAL_INTENT" in review_contract:
        intent = review_contract["ORIGINAL_INTENT"]
        if (
            not isinstance(intent, dict)
            or set(intent)
            != {
                "plan_sha256",
                "phase_id",
                "sources",
                "requirements",
                "effective_requirements",
                "decisions",
                "planning_assessment",
                "evidence_records",
            }
            or intent.get("phase_id") != phase_id
            or not re.fullmatch(r"[0-9a-f]{64}", str(intent.get("plan_sha256")))
            or not isinstance(intent.get("sources"), list)
            or not intent["sources"]
            or not isinstance(intent.get("requirements"), list)
            or not isinstance(intent.get("effective_requirements"), dict)
            or not isinstance(intent.get("decisions"), list)
            or not isinstance(intent.get("planning_assessment"), dict)
            or not isinstance(intent.get("evidence_records"), list)
        ):
            raise ValueError("REVIEW_CONTRACT_REQUIRED: original intent binding is malformed")
    if (
        not isinstance(verification_evidence, list)
        or not verification_evidence
        or len(verification_evidence) > 64
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2048
            for item in verification_evidence
        )
    ):
        raise ValueError(
            "REVIEW_EVIDENCE_REQUIRED: include 1 to 64 bounded verification evidence references"
        )
    return (
        "PHASE_REVIEW_CONTRACT: "
        + json.dumps(review_contract, ensure_ascii=False, sort_keys=True)
        + "\nVERIFICATION_EVIDENCE: "
        + json.dumps(verification_evidence, ensure_ascii=False, sort_keys=True)
    )


def prepare_handoff(
    agent, contract, state, replacement_id, *, runtime=None, selection=None, role_contract=None
):
    """Build a bounded replacement request; caller performs the actual spawn."""
    if not all(isinstance(value, dict) for value in (agent, contract, state)):
        raise ValueError("agent, contract and state must be objects")
    aliases = {field.replace("_", " "): field for field in CONTRACT_FIELDS}
    aliases.update(
        {
            "RELEVANT ADR/BRIEF SECTIONS": "RELEVANT_ADR_BRIEF_SECTIONS",
            "BASELINE/EVIDENCE PATHS": "BASELINE_EVIDENCE_PATHS",
        }
    )
    contract = {aliases.get(key, key): value for key, value in contract.items()}
    task_id = agent.get("task_id")
    if not task_id or not contract.get("GOAL") or not replacement_id:
        raise ValueError("task_id, GOAL and replacement_id are required")
    if set(contract) - set(CONTRACT_FIELDS) or (contract.get("TASK_ID") not in (None, task_id)):
        raise ValueError("contract must contain only bounded fields for the same task")
    contract = {field: contract[field] for field in CONTRACT_FIELDS if field in contract}
    contract["TASK_ID"] = task_id
    handoff = {field: state.get(field) for field in HANDOFF_FIELDS}
    handoff.update(TASK_ID=task_id, GOAL=contract["GOAL"])
    count = agent.get("handoff_count", 0)
    _number(count, "handoff_count")
    task_name = re.sub(r"[^a-z0-9_]", "_", replacement_id.lower()).strip("_")
    if not task_name or not re.fullmatch(r"[a-z][a-z0-9_]*", task_name):
        raise ValueError("replacement_id cannot form a valid task_name")
    runtime = runtime if runtime is not None else agent.get("runtime")
    selection = selection if selection is not None else agent.get("route")
    role_contract = role_contract if role_contract is not None else agent.get("role_contract")
    role = agent.get("role")
    selection = _spawn_selection(role, runtime, selection, agent.get("routing", {}))
    instruction = (
        "Continue the same task in a fresh context. Use only this compact contract and handoff. "
        "Do not commit. " + ("Review read-only. " if selection["ROLE"] == "reviewer" else "")
    )
    message_contract = contract
    if selection["ROLE"] == "reviewer":
        review_fields = {
            "PHASE_ID",
            "BASE_SHA",
            "TARGET_SHA",
            "ACCEPTANCE",
            "OWNED_SCOPE",
            "VERIFICATION",
            "KNOWN_CONTRACTS",
            "DO_NOT_CHANGE",
            "INSTRUCTION",
            "REVIEW_CONTRACT",
            "VERIFICATION_EVIDENCE",
        }
        message_contract = {
            key: value for key, value in contract.items() if key not in review_fields
        }
    message = (
        instruction
        + "\n"
        + json.dumps({"contract": message_contract, "handoff": handoff}, ensure_ascii=False)
    )
    if selection["ROLE"] == "executor":
        required = (
            "PROJECT_ROOT",
            "TASK_WORKTREE_ROOT",
            "BASE_SHA",
            "OWNED_SCOPE",
            "ACCEPTANCE",
            "VERIFICATION",
        )
        if any(field not in contract for field in required):
            raise ValueError(
                "AGENT_RUNTIME_REQUIRED: executor replacement requires complete task worktree contract"
            )
        # Every replacement gets its own fresh lifecycle snapshot. Caller state
        # may carry a consumed receipt from the previous writer dispatch.
        try:
            receipt = devcrew_worktree.resolve(contract["PROJECT_ROOT"], task_id)
        except (OSError, ValueError):
            receipt = None
        prepared = routed_task_spawn(
            task_name,
            message,
            selection,
            runtime,
            task_id=task_id,
            project_root=contract["PROJECT_ROOT"],
            task_worktree_root=contract["TASK_WORKTREE_ROOT"],
            base_sha=contract["BASE_SHA"],
            owned_scope=contract["OWNED_SCOPE"],
            acceptance=contract["ACCEPTANCE"],
            verification=contract["VERIFICATION"],
            receipt=receipt,
            role_contract=role_contract,
        )
    elif selection["ROLE"] == "planner":
        required = ("PROJECT_ROOT", "BASE_SHA")
        if any(
            not isinstance(contract.get(field), str) or not contract[field] for field in required
        ):
            raise ValueError(
                "DISPATCH_ROUTE_REQUIRED: planner replacement requires PROJECT_ROOT and original BASE_SHA"
            )
        identity = {"TASK_ID": task_id, "BASE_SHA": contract["BASE_SHA"]}
        prepared = routed_spawn(
            task_name,
            message,
            selection,
            runtime,
            role_contract=role_contract,
            project_root=contract["PROJECT_ROOT"],
            task_id=task_id,
            identity=identity,
            requirements_scope=(
                {"paths": contract["OWNED_SCOPE"], "task_class": selection.get("TASK_CLASS")}
                if isinstance(contract.get("OWNED_SCOPE"), list)
                else None
            ),
        )
    elif selection["ROLE"] == "reviewer":
        required = ("PROJECT_ROOT", "PHASE_ID", "BASE_SHA", "TARGET_SHA")
        if any(
            not isinstance(contract.get(field), str) or not contract[field] for field in required
        ):
            raise ValueError(
                "DISPATCH_ROUTE_REQUIRED: reviewer replacement requires project, phase, base and target"
            )
        identity = {
            "TASK_ID": task_id,
            "PHASE_ID": contract["PHASE_ID"],
            "BASE_SHA": contract["BASE_SHA"],
            "TARGET_SHA": contract["TARGET_SHA"],
        }
        prepared = routed_spawn(
            task_name,
            message,
            selection,
            runtime,
            role_contract=role_contract,
            project_root=contract["PROJECT_ROOT"],
            task_id=task_id,
            identity=identity,
            review_contract=contract["REVIEW_CONTRACT"],
            verification_evidence=contract["VERIFICATION_EVIDENCE"],
        )
    else:
        raise ValueError(
            "DISPATCH_ROUTE_REQUIRED: replacement role must be planner, executor or reviewer"
        )
    return {"handoff": handoff, "handoff_count": count + 1, **prepared}


def reviewer_spawn(
    task_id,
    review_number=1,
    *,
    runtime=None,
    selection=None,
    role_contract=None,
    risk="low",
    review_complexity="NORMAL",
    project_root=None,
    phase_id=None,
    base_sha=None,
    target_sha=None,
    review_contract=None,
    verification_evidence=None,
):
    if not task_id:
        raise ValueError("task_id required")
    if type(review_number) is not int or review_number < 1:
        raise ValueError("review_number must be a positive integer")
    if not isinstance(project_root, (str, pathlib.Path)) or not all(
        isinstance(value, str) and value for value in (phase_id, base_sha, target_sha)
    ):
        raise ValueError(
            "DISPATCH_ROUTE_REQUIRED: reviewer dispatch requires project, phase, base and target"
        )
    name = re.sub(r"[^a-z0-9_]", "_", task_id.lower()).strip("_")
    selection = _spawn_selection(
        "reviewer", runtime, selection, {"risk": risk, "review_complexity": review_complexity}
    )
    message = (
        f"Review task {task_id} read-only in a fresh context. Request only the bounded contract, "
        "diff, and verification evidence. Do not edit or commit."
    )
    identity = {
        "TASK_ID": task_id,
        "PHASE_ID": phase_id,
        "BASE_SHA": base_sha,
        "TARGET_SHA": target_sha,
    }
    return routed_spawn(
        f"reviewer_{name}_{review_number}",
        message,
        selection,
        runtime,
        role_contract=role_contract,
        project_root=project_root,
        task_id=task_id,
        identity=identity,
        review_contract=review_contract,
        verification_evidence=verification_evidence,
    )


def spawn_failure(signal):
    return (
        "AGENT_HANDOFF_REQUIRED_BUT_UNAVAILABLE"
        if signal in {"HANDOFF_REQUIRED", "NEW_CONTEXT_REQUIRED"}
        else "HANDOFF_UNAVAILABLE"
    )


def _recorded_tokens(agent):
    value = agent.get("total_recorded_tokens")
    if value is None and isinstance(agent.get("usage"), dict):
        value = agent["usage"].get("total_recorded_tokens")
    return value


def task_report(task_id, agents):
    if not task_id or not isinstance(agents, list):
        raise ValueError("task_id and agent list required")
    selected = [agent for agent in agents if agent.get("task_id") == task_id]
    groups = {
        role: [agent for agent in selected if agent.get("role") == role]
        for role in ("orchestrator", "executor", "reviewer")
    }
    known = [_recorded_tokens(agent) for agent in selected if _recorded_tokens(agent) is not None]
    contexts = [agent for agent in selected if agent.get("current_context_tokens") is not None]
    peak = max(contexts, key=lambda agent: agent["current_context_tokens"], default=None)
    return {
        "task_id": task_id,
        "orchestrator": {
            "model": groups["orchestrator"][0].get("model") if groups["orchestrator"] else None,
            "turns": sum(agent["turns"] for agent in groups["orchestrator"])
            if groups["orchestrator"]
            and all(agent.get("turns") is not None for agent in groups["orchestrator"])
            else None,
            "recorded_tokens": sum(_recorded_tokens(agent) for agent in groups["orchestrator"])
            if all(_recorded_tokens(agent) is not None for agent in groups["orchestrator"])
            and groups["orchestrator"]
            else None,
        },
        "executors": {
            "count": len(groups["executor"]),
            "handoffs": max(
                (agent.get("handoff_count", 0) for agent in groups["executor"]), default=0
            ),
            "recorded_tokens": sum(_recorded_tokens(agent) for agent in groups["executor"])
            if all(_recorded_tokens(agent) is not None for agent in groups["executor"])
            and groups["executor"]
            else None,
        },
        "reviewers": {
            "count": len(groups["reviewer"]),
            "recorded_tokens": sum(_recorded_tokens(agent) for agent in groups["reviewer"])
            if all(_recorded_tokens(agent) is not None for agent in groups["reviewer"])
            and groups["reviewer"]
            else None,
        },
        "total_recorded_tokens": sum(known) if known else None,
        "peak_agent_context": peak["current_context_tokens"] if peak else None,
        "peak_agent_role": peak.get("role") if peak else None,
        "unknown_usage": sorted(
            {agent.get("role", "unknown") for agent in selected if _recorded_tokens(agent) is None}
        ),
    }


def phase_report(agents):
    if not isinstance(agents, list):
        raise ValueError("agent list required")
    tasks = sorted({agent["task_id"] for agent in agents if agent.get("task_id")})
    reports = [task_report(task_id, agents) for task_id in tasks]
    return {
        "tasks": reports,
        "most_expensive_tasks": sorted(
            (
                {"task_id": row["task_id"], "recorded_tokens": row["total_recorded_tokens"]}
                for row in reports
                if row["total_recorded_tokens"] is not None
            ),
            key=lambda row: (-row["recorded_tokens"], row["task_id"]),
        ),
        "most_expensive_agents": sorted(
            (
                {
                    "agent_id": agent.get("agent_id"),
                    "task_id": agent.get("task_id"),
                    "recorded_tokens": _recorded_tokens(agent),
                }
                for agent in agents
                if _recorded_tokens(agent) is not None
            ),
            key=lambda row: (-row["recorded_tokens"], row["agent_id"] or ""),
        ),
        "sessions_with_handoff": [
            agent.get("agent_id") for agent in agents if agent.get("handoff_count", 0)
        ],
        "threshold_agents": {
            signal: [agent.get("agent_id") for agent in agents if agent.get("signal") == signal]
            for signal in ("HANDOFF_RECOMMENDED", "HANDOFF_REQUIRED")
        },
        "models": {
            model: sum(agent.get("model") == model for agent in agents)
            for model in sorted({agent["model"] for agent in agents if agent.get("model")})
        },
        "unknown_usage_agents": [
            agent.get("agent_id") for agent in agents if _recorded_tokens(agent) is None
        ],
    }
