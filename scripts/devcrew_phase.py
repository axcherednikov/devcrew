"""Durable task phase contracts and bounded phase dispatch."""

from __future__ import annotations

import devcrew_fs as fs
import json
import datetime
import math
import hashlib
import importlib.util
import os
import pathlib
import re
import secrets
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

import devcrew_worktree
import devcrew_dispatch
import devcrew_capacity
import devcrew_cost

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = {1, 2}
INTENT_SOURCE_TYPES = {"user_prompt", "attachment", "ticket", "document", "owner_answer", "intent_contract"}
INTENT_SOURCE_FIELDS = {"source_id", "source_type", "status", "sha256", "size_bytes",
                        "snapshot", "provenance", "captured_at"}
STATUSES = {"PLANNED", "EXECUTING", "VERIFYING", "READY_FOR_CLOSE", "COMMITTED", "REVIEWING",
            "ACCEPTED", "LOCAL_FIX_REQUIRED", "BLOCKED", "REPLAN_REQUIRED"}
MODES = {"SINGLE_PHASE", "MULTI_PHASE"}
REVIEW_FAILURES = {"LOCAL_FIX", "SYSTEMIC_REPLAN", "OWNER_DECISION_REQUIRED"}


class PhaseError(ValueError):
    """Machine-readable phase contract failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _fail(code: str, message: str):
    raise PhaseError(code, message)


def _loop_stop(phase: dict[str, Any]) -> dict[str, Any] | None:
    if phase.get("review_cycles") and phase.get("status") in {"LOCAL_FIX_REQUIRED", "REPLAN_REQUIRED"}:
        return {"status": "PHASE_CONTINUATION_REQUIRED", "phase_id": phase["phase_id"],
                "planner_required": False, "executor_required": False, "reviewer_required": False,
                "owner_decision_required": False, "coordinator_required": True,
                "completed_corrections": len(phase["review_cycles"]),
                "findings": (phase.get("review_report") or {}).get("findings", []),
                "reason": "Assess evidence, the bounded next action and its cost; repeated FAIL alone is not an owner question."}
    return None


def _repair_owner_decision(phase: dict[str, Any], owner_decision: str | None,
                           continuation: dict[str, Any] | None = None) -> str | None:
    if owner_decision is not None and (not isinstance(owner_decision, str) or not owner_decision.strip()):
        _fail("LOOP_DETECTED", "a nonempty actual user decision is required; do not manufacture approval")
    if _loop_stop(phase) and owner_decision is None and continuation is None:
        _fail("CONTINUATION_REQUIRED", "supply an evidence-backed continuation assessment; do not invent an owner question")
    return owner_decision.strip() if owner_decision is not None else None


def _verify_continuation(value, manifest, phase, project, task_root, verifier, *, persisted=False):
    """Bind coordinator reasoning to real checks and the exact failed report/candidate."""
    identity = {"task_id": manifest["task_id"], "phase_id": phase["phase_id"],
                "target_sha": phase["candidate_commit"]["sha"],
                "report_sha256": phase["review_report"]["sha256"]}
    required = set(identity) | {"reason", "next_action", "expected_result", "cost_rationale", "evidence_records"}
    if (not devcrew_cost.valid_continuation(value) or set(value) - (required | {"evidence_sha256"}) or
            not required <= set(value) or any(value.get(key) != item for key, item in identity.items())):
        _fail("INVALID_CONTINUATION", "assessment requires exact task/phase/candidate/report, evidence and a bounded next action")
    refs = verify_evidence_records(value["evidence_records"], project, task_root,
                                   manifest["task_id"], verifier, current=False)
    bindings, proofs = {}, []
    for ref in refs:
        record_path, data, _ = verifier(str(project / ref), current=False,
                                        expected_execution_root=str(task_root), expected_task_id=manifest["task_id"])
        candidate = phase["candidate_commit"]
        if (data.get("git_head_before") != candidate["sha"] or data.get("git_head") != candidate["sha"] or
                data.get("candidate_tree_before") != candidate["tree"] or
                data.get("candidate_tree_after") != candidate["tree"] or
                data.get("worktree_fingerprint_before") != data.get("worktree_fingerprint_after")):
            _fail("STALE_CONTINUATION", "continuation evidence must describe the unchanged failed candidate")
        bindings[ref] = hashlib.sha256(record_path.read_bytes()).hexdigest()
        proofs.append([data.get("command"), data.get("log_sha256")])
    if (persisted or "evidence_sha256" in value) and value.get("evidence_sha256") != bindings:
        _fail("INVALID_CONTINUATION", "saved continuation evidence is missing or changed")
    # Timestamps, model changes and reworded reasons do not make the same proof new.
    proofs.sort(key=lambda item: json.dumps(item, sort_keys=True))
    signature = [phase["candidate_commit"]["tree"], proofs]
    for cycle in phase.get("review_cycles", []):
        previous = cycle.get("continuation")
        if previous and cycle["candidate_commit"]["tree"] == signature[0]:
            old_proofs = []
            for ref in previous["evidence_records"]:
                _, data, _ = verifier(str(project / ref), current=False,
                                      expected_execution_root=str(task_root), expected_task_id=manifest["task_id"])
                old_proofs.append([data.get("command"), data.get("log_sha256")])
            old_proofs.sort(key=lambda item: json.dumps(item, sort_keys=True))
            if old_proofs == proofs:
                _fail("LOOP_DETECTED", "the same candidate tree and check results do not justify another attempt")
    return {**value, "evidence_records": refs, "evidence_sha256": bindings}


def manifest_path(project: pathlib.Path, task_id: str, *, _repository_metadata=None) -> pathlib.Path:
    try:
        *_, receipt_path = devcrew_worktree.location(project, task_id,
                                                       _repository_metadata=_repository_metadata)
    except ValueError as exc:
        _fail("INVALID_TASK_ID", str(exc))
    return receipt_path.with_name(f"{task_id}.phases.json")


def _safe_scope(scope: Any, task_root: pathlib.Path) -> list[str]:
    if not isinstance(scope, list) or not scope:
        _fail("INVALID_OWNED_SCOPE", "owned_scope must be a non-empty list")
    result = []
    seen = set()
    root = task_root.resolve()
    for value in scope:
        if not isinstance(value, str) or not value or "\x00" in value:
            _fail("INVALID_OWNED_SCOPE", "scope paths must be non-empty strings")
        raw = value
        if "\\" in raw:
            _fail("UNSAFE_OWNED_SCOPE", f"scope path is not normalized: {value}")
        path = pathlib.PurePosixPath(raw)
        if (fs.unsafe_parts(path.parts) or path.is_absolute() or re.match(r"^[A-Za-z]:", raw) or
                any(part in ("", ".", "..") for part in path.parts) or
                raw == "*" or any(char in raw for char in "*?[")):
            _fail("UNSAFE_OWNED_SCOPE", f"unsafe repository-relative scope: {value}")
        normalized = path.as_posix()
        if normalized != raw:
            _fail("UNSAFE_OWNED_SCOPE", f"scope path is not normalized: {value}")
        if normalized == ".":
            _fail("UNSAFE_OWNED_SCOPE", "owned_scope must not include the repository root")
        if normalized in seen:
            _fail("DUPLICATE_OWNED_SCOPE", f"duplicate scope path: {normalized}")
        seen.add(normalized)
        candidate = task_root / pathlib.Path(*path.parts)
        try:
            candidate.resolve(strict=False).relative_to(root)
        except ValueError:
            _fail("UNSAFE_OWNED_SCOPE", f"scope escapes task worktree: {value}")
        current = task_root
        for part in path.parts:
            current = current / part
            if fs.is_link(current):
                _fail("UNSAFE_OWNED_SCOPE", f"scope traverses symlink: {value}")
        result.append(normalized)
    return result


def validate_manifest(manifest: Any, receipt: Any, task_root: pathlib.Path | None = None) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        _fail("INVALID_MANIFEST", "manifest must be an object")
    _require_no_candidate_transition(manifest)
    _manifest_revision(manifest)
    _contract_generation(manifest)
    def reject_nonce(value):
        if isinstance(value, dict):
            if "receipt_nonce" in value:
                _fail("EPHEMERAL_RECEIPT_FIELD", "lifecycle receipt nonce must not be stored in phase manifest")
            for child in value.values(): reject_nonce(child)
        elif isinstance(value, list):
            for child in value: reject_nonce(child)
    reject_nonce(manifest)
    if type(manifest.get("schema_version")) is not int or manifest["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
        _fail("UNSUPPORTED_SCHEMA_VERSION", "unsupported phase manifest schema")
    if manifest["schema_version"] == 1 and "original_intent" in manifest:
        _fail("INVALID_ORIGINAL_INTENT", "schema v1 cannot carry an original_intent extension")
    task_id = manifest.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        _fail("INVALID_TASK_ID", "task_id must be non-empty")
    mode = manifest.get("task_mode")
    if not isinstance(mode, str) or mode not in MODES:
        _fail("INVALID_TASK_MODE", "task_mode must be SINGLE_PHASE or MULTI_PHASE")
    semantic = manifest.get("semantic_contract")
    capabilities = semantic.get("capabilities") if isinstance(semantic, dict) else None
    if (not isinstance(capabilities, list) or not capabilities or
            any(not isinstance(item, str) or not item.strip() for item in capabilities)):
        _fail("INVALID_SEMANTIC_CONTRACT", "planner must list semantic capabilities")
    if (mode == "SINGLE_PHASE" and len(capabilities) != 1) or (mode == "MULTI_PHASE" and len(capabilities) < 2):
        _fail("SEMANTIC_CONTRACT_MISMATCH", "task mode must represent planner-declared semantic capabilities")
    link = manifest.get("worktree")
    if not isinstance(link, dict) or not isinstance(receipt, dict):
        _fail("WORKTREE_LINKAGE_MISMATCH", "manifest requires a fresh worktree receipt")
    base = link.get("base_sha")
    phase_values = manifest.get("phases")
    committed = [p.get("candidate_commit") for p in phase_values
                 if isinstance(p, dict) and isinstance(p.get("candidate_commit"), dict)] if isinstance(phase_values, list) else []
    expected_heads = [base, *(item.get("sha") for item in committed if isinstance(item, dict))]
    expected_head_ok = any(receipt.get("expected_head") == expected for expected in expected_heads)
    if (receipt.get("owner") != "DEVCREW" or receipt.get("state") != "RESOLVED" or
            receipt.get("task_id") != task_id or receipt.get("repository_id") != link.get("repository_id") or
            receipt.get("task_worktree_root") != link.get("task_worktree_root") or
            receipt.get("branch") != link.get("branch") or not expected_head_ok or
            not isinstance(base, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base)):
        _fail("WORKTREE_LINKAGE_MISMATCH", "manifest does not match fresh resolved worktree receipt")
    if task_root is None:
        task_root = pathlib.Path(link["task_worktree_root"])
    phases = manifest.get("phases")
    if not isinstance(phases, list) or not phases:
        _fail("INVALID_PHASES", "phases must be a non-empty list")
    if (mode == "SINGLE_PHASE" and len(phases) != 1) or (mode == "MULTI_PHASE" and len(phases) < 2):
        _fail("TASK_MODE_PHASE_COUNT_MISMATCH", "task mode must match planner phase count")
    ids = []
    by_id = {}
    active = []
    for phase in phases:
        if not isinstance(phase, dict):
            _fail("INVALID_PHASE", "each phase must be an object")
        phase_id = phase.get("phase_id")
        if not isinstance(phase_id, str) or not phase_id.strip() or not phase_id.isprintable():
            _fail("INVALID_PHASE_ID", "phase_id must be non-empty")
        if phase_id in by_id:
            _fail("DUPLICATE_PHASE_ID", f"duplicate phase_id: {phase_id}")
        ids.append(phase_id); by_id[phase_id] = phase
        if not isinstance(phase.get("goal"), str) or not phase["goal"].strip():
            _fail("EMPTY_GOAL", f"phase {phase_id} goal must be non-empty")
        if "main_scenario" in phase and (not isinstance(phase["main_scenario"], str) or
                                         not phase["main_scenario"].strip()):
            _fail("INVALID_MAIN_SCENARIO", f"phase {phase_id} main_scenario must be non-empty text")
        if not isinstance(phase.get("acceptance"), list) or not phase["acceptance"] or any(not isinstance(x, str) or not x.strip() for x in phase["acceptance"]):
            _fail("EMPTY_ACCEPTANCE", f"phase {phase_id} acceptance must be non-empty")
        if (not isinstance(phase.get("verification"), list) or not phase["verification"] or
                any(not isinstance(item, str) or not item.strip() for item in phase["verification"])):
            _fail("MISSING_VERIFICATION", f"phase {phase_id} requires verification")
        if "verification_sha256" in phase:
            required_verification(phase)
        if not isinstance(phase.get("commit_boundary"), str) or not phase["commit_boundary"].strip():
            _fail("MISSING_COMMIT_BOUNDARY", f"phase {phase_id} requires commit boundary")
        subject = phase.get("commit_subject")
        if (not isinstance(subject, str) or not subject.isprintable() or "\n" in subject or "\r" in subject or
                not re.fullmatch(rf"type\({re.escape(task_id)}\): \S(?:.*\S)?", subject)):
            _fail("INVALID_COMMIT_SUBJECT", f"phase {phase_id} requires a printable task-bound commit subject")
        status = phase.get("status")
        if not isinstance(status, str) or status not in STATUSES:
            _fail("INVALID_PHASE_STATUS", f"invalid status for {phase_id}")
        if status in {"EXECUTING", "VERIFYING"}:
            active.append(phase_id)
        if status == "READY_FOR_CLOSE":
            evidence = phase.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                _fail("PHASE_EVIDENCE_REQUIRED", "READY_FOR_CLOSE requires verified evidence references")
            for reference in evidence:
                _validate_evidence_ref(reference)
        if status in {"COMMITTED", "REVIEWING", "ACCEPTED"} or phase.get("candidate_commit"):
            candidate = phase.get("candidate_commit")
            phase_base = phase.get("base_sha", link.get("base_sha"))
            if (not isinstance(candidate, dict) or
                    not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(candidate.get("sha", ""))) or
                    not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(candidate.get("tree", ""))) or
                    candidate.get("base") != phase_base or
                    not isinstance(candidate.get("evidence"), list) or not candidate["evidence"]):
                _fail("PHASE_COMMIT_INCONSISTENT", "COMMITTED phase requires candidate SHA, tree, base and evidence")
            for reference in candidate["evidence"]:
                _validate_evidence_ref(reference)
        if status == "ACCEPTED":
            candidate = phase.get("candidate_commit")
            if (not isinstance(candidate, dict) or phase.get("accepted_sha") != candidate.get("sha") or
                    not isinstance(phase.get("review_report"), dict) or
                    phase["review_report"].get("verdict") != "PASS" or
                    phase["review_report"].get("authoritative") is not True):
                _fail("PHASE_ACCEPTANCE_INCONSISTENT", "ACCEPTED phase must bind a passing report to its candidate SHA")
            acceptance_evidence = phase.get("acceptance_evidence")
            if not isinstance(acceptance_evidence, list) or not acceptance_evidence:
                _fail("PHASE_ACCEPTANCE_INCONSISTENT", "ACCEPTED phase requires its approved verification evidence")
            for reference in acceptance_evidence:
                _validate_evidence_ref(reference)
        review = phase.get("review_report")
        review_start = phase.get("review_start")
        if status == "REVIEWING" and not isinstance(review_start, dict):
            _fail("PHASE_REVIEW_START_REQUIRED", "reviewing phase requires a persisted formal review-start")
        if isinstance(review_start, dict) and (status in {"REVIEWING", "ACCEPTED"} or review is not None):
            expected_target = {"TASK_ID": task_id, "PHASE_ID": phase_id,
                               "BASE_SHA": (phase.get("candidate_commit") or {}).get("base"),
                               "TARGET_SHA": (phase.get("candidate_commit") or {}).get("sha")}
            if any(review_start.get(key) != value for key, value in expected_target.items()):
                _fail("PHASE_REVIEW_INCONSISTENT", "formal review-start must match the exact phase candidate")
        if review is not None:
            candidate = phase.get("candidate_commit")
            if (not isinstance(review, dict) or not isinstance(candidate, dict) or
                    review.get("authoritative") is not True or
                    review.get("task_id") != task_id or review.get("phase_id") != phase_id or
                    review.get("base_sha") != candidate.get("base") or
                    review.get("target_sha") != candidate.get("sha") or
                    not isinstance(review.get("verdict"), str) or review.get("verdict") not in {"PASS", "FAIL"} or
                    not isinstance(review.get("findings"), list) or
                    not valid_review_advisories(review.get("advisories", [])) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(review.get("sha256", ""))) or
                    (review.get("verdict") == "PASS" and review.get("findings")) or
                    (review.get("verdict") == "FAIL" and not review.get("findings")) or
                    any(not isinstance(item, dict) or set(item) != {"CLASSIFICATION", "SUMMARY", "EVIDENCE"} or
                        not isinstance(item.get("CLASSIFICATION"), str) or item.get("CLASSIFICATION") not in REVIEW_FAILURES or
                        not isinstance(item.get("SUMMARY"), str) or not item["SUMMARY"].strip() or
                        not isinstance(item.get("EVIDENCE"), list) or not item["EVIDENCE"] or
                        any(not isinstance(ref, str) or not ref.strip() for ref in item["EVIDENCE"])
                        for item in review.get("findings", [])) or
                    not isinstance(review.get("report_ref"), str)):
                _fail("PHASE_REVIEW_INCONSISTENT", "saved review report does not match the immutable candidate")
            if review.get("authoritative") is True and (
                    not isinstance(review.get("spawn_receipt_id"), str) or not review["spawn_receipt_id"] or
                    not isinstance(review.get("agent_id"), str) or not review["agent_id"] or
                    review.get("review_started") is not True):
                _fail("PHASE_REVIEW_INCONSISTENT", "authoritative report requires formal start and routed reviewer receipt")
            if review["verdict"] == "FAIL":
                classes = {item["CLASSIFICATION"] for item in review["findings"]}
                expected_status = ("BLOCKED" if "OWNER_DECISION_REQUIRED" in classes else
                                   "REPLAN_REQUIRED" if "SYSTEMIC_REPLAN" in classes else "LOCAL_FIX_REQUIRED")
                if status != expected_status:
                    _fail("PHASE_REVIEW_INCONSISTENT", "failed review classification does not match halted phase status")
            elif status not in {"REVIEWING", "ACCEPTED"}:
                _fail("PHASE_REVIEW_INCONSISTENT", "passing review report requires REVIEWING or ACCEPTED status")
        cycles = phase.get("review_cycles", [])
        if not isinstance(cycles, list):
            _fail("PHASE_REVIEW_INCONSISTENT", "review_cycles must be an array")
        for cycle_index, cycle in enumerate(cycles, 1):
            old_candidate = cycle.get("candidate_commit") if isinstance(cycle, dict) else None
            old_review = cycle.get("review_report") if isinstance(cycle, dict) else None
            if (not isinstance(cycle, dict) or cycle.get("cycle_id") != cycle_index or
                    cycle.get("backup_ref") != _rejected_candidate_ref(task_id, phase_id, cycle_index) or
                    not isinstance(old_candidate, dict) or old_candidate.get("base") !=
                    (phase.get("base_sha") or base) or
                    not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(old_candidate.get("sha", ""))) or
                    not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(old_candidate.get("tree", ""))) or
                    not isinstance(old_review, dict) or old_review.get("target_sha") != old_candidate.get("sha") or
                    old_review.get("base_sha") != old_candidate.get("base") or
                    old_review.get("phase_id") != phase_id or old_review.get("task_id") != task_id or
                    old_review.get("verdict") != "FAIL" or
                    not re.fullmatch(r"[0-9a-f]{64}", str(old_review.get("sha256", ""))) or
                    not isinstance(old_review.get("findings"), list) or not old_review["findings"]):
                _fail("PHASE_REVIEW_INCONSISTENT", "review cycle history must bind an immutable failed report to its candidate")
            if "owner_decision" in cycle and (not isinstance(cycle["owner_decision"], str) or not cycle["owner_decision"].strip()):
                _fail("PHASE_REVIEW_INCONSISTENT", "archived owner decision must be nonempty text")
            completion = cycle.get("systemic_replan_completion")
            classes = {item.get("CLASSIFICATION") for item in old_review["findings"] if isinstance(item, dict)}
            if "SYSTEMIC_REPLAN" in classes and cycle.get("review_correction") is not True:
                if (not isinstance(completion, dict) or completion.get("task_id") != task_id or
                        completion.get("phase_id") != phase_id or
                        completion.get("failed_candidate_sha") != old_candidate["sha"] or
                        completion.get("report_sha256") != old_review.get("sha256") or
                        completion.get("planner_dispatch", {}).get("base_sha") != old_candidate["sha"]):
                    _fail("PHASE_REVIEW_INCONSISTENT", "systemic replan history lacks matching completion")
            elif completion is not None:
                _fail("PHASE_REVIEW_INCONSISTENT", "systemic replan completion is attached to a non-systemic review")
        local_fix = phase.get("active_local_fix")
        if local_fix is not None:
            if (phase_id != manifest.get("current_phase_id") or
                    status not in {"EXECUTING", "VERIFYING", "READY_FOR_CLOSE", "BLOCKED", "REPLAN_REQUIRED"} or
                    not isinstance(local_fix, dict) or
                    set(local_fix) != {"cycle_id", "rejected_candidate_sha", "findings"} or
                    type(local_fix.get("cycle_id")) is not int or local_fix["cycle_id"] < 1 or
                    local_fix["cycle_id"] > len(cycles)):
                _fail("PHASE_LOCAL_FIX_INCONSISTENT", "active local fix must belong to the current writer phase and an archived cycle")
            archived = cycles[local_fix["cycle_id"] - 1]
            cycle_candidate = archived["candidate_commit"]
            cycle_review = archived["review_report"]
            if (local_fix["rejected_candidate_sha"] != cycle_candidate["sha"] or
                    local_fix["findings"] != cycle_review["findings"] or
                    cycle_candidate["base"] != (phase.get("base_sha") or base) or
                    any(item.get("CLASSIFICATION") != "LOCAL_FIX" for item in local_fix["findings"])):
                _fail("PHASE_LOCAL_FIX_INCONSISTENT", "active local fix must exactly match its archived local-fix review cycle")
        deps = phase.get("depends_on")
        if not isinstance(deps, list) or any(not isinstance(dep, str) for dep in deps):
            _fail("INVALID_DEPENDENCIES", f"phase {phase_id} dependencies must be a list")
        if len(deps) != len(set(deps)):
            _fail("DUPLICATE_DEPENDENCY", f"phase {phase_id} has duplicate dependencies")
        if phase_id in deps:
            _fail("SELF_DEPENDENCY", f"phase {phase_id} depends on itself")
        phase["owned_scope"] = _safe_scope(phase.get("owned_scope"), task_root)
    for phase_id, phase in by_id.items():
        phase_base = phase.get("base_sha")
        if phase_base is not None and (not isinstance(phase_base, str) or
                                       not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", phase_base)):
            _fail("INVALID_PHASE_BASE", f"phase {phase_id} base_sha must be a full Git object ID")
        if phase_id == ids[0] and phase_base is not None and phase_base != base:
            _fail("INVALID_PHASE_BASE", "first phase base must equal the original task base SHA")
        for dep in phase["depends_on"]:
            if dep not in by_id:
                _fail("MISSING_DEPENDENCY", f"phase {phase_id} depends on missing phase {dep}")
    visiting, visited = set(), set()
    def visit(item):
        if item in visiting:
            _fail("DEPENDENCY_CYCLE", "phase dependencies contain a cycle")
        if item in visited:
            return
        visiting.add(item)
        for dep in by_id[item]["depends_on"]:
            visit(dep)
        visiting.remove(item); visited.add(item)
    for item in ids:
        visit(item)
    if manifest["schema_version"] == 2:
        _validate_original_intent(manifest, pathlib.Path(task_root), by_id)
    current = manifest.get("current_phase_id")
    if not isinstance(current, str) or current not in by_id:
        _fail("INVALID_CURRENT_PHASE", "current_phase_id must identify a phase")
    current_index = ids.index(current)
    if any(by_id[phase_id]["status"] != "ACCEPTED" for phase_id in ids[:current_index]):
        _fail("CLOSE_GATE_REQUIRED", "all earlier phases must be ACCEPTED before advancing")
    if any(dep not in ids[:current_index] or by_id[dep]["status"] != "ACCEPTED"
           for dep in by_id[current]["depends_on"]):
        _fail("CLOSE_GATE_REQUIRED", "current phase dependencies must be ACCEPTED")
    if len(active) > 1 or (active and active[0] != current):
        _fail("MULTIPLE_ACTIVE_PHASES", "only current phase may be active")
    if any(ids.index(phase_id) > current_index and phase["status"] != "PLANNED" for phase_id, phase in by_id.items()):
        _fail("NONCURRENT_PHASE_ADVANCED", "later phases must remain PLANNED until explicitly advanced")
    # Before an acceptance gate exists, phase 1 is the sole dispatchable phase.
    return manifest


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _validate_original_intent(manifest: dict[str, Any], task_root: pathlib.Path,
                              phases: dict[str, dict[str, Any]]) -> None:
    intent = manifest.get("original_intent")
    if not isinstance(intent, dict) or set(intent) != {"sources", "requirements", "decisions",
                                                       "planning_assessment", "task_acceptance"}:
        _fail("INVALID_ORIGINAL_INTENT", "schema v2 requires the exact original_intent fields")
    sources = intent["sources"]
    requirements = intent["requirements"]
    if not isinstance(sources, list) or not sources:
        _fail("INVALID_ORIGINAL_INTENT", "original_intent.sources must be a nonempty array")
    if not isinstance(requirements, list):
        _fail("INVALID_ORIGINAL_INTENT", "original_intent.requirements must be an array")
    if not isinstance(intent["decisions"], list):
        _fail("INVALID_ORIGINAL_INTENT", "decisions must be an array")
    for name, value_key in (("planning_assessment", "assessment"), ("task_acceptance", "review")):
        state = intent[name]
        pending = (isinstance(state, dict) and set(state) == {"status", value_key} and
                   state.get("status") == "NOT_ASSESSED" and state.get(value_key) is None)
        if name == "task_acceptance" and not pending:
            accepted = (isinstance(state, dict) and set(state) == {"status", "review"} and
                        state.get("status") == "ACCEPTED" and isinstance(state.get("review"), dict) and
                        set(state["review"]) == {"phase_id", "report_sha256"} and
                        state["review"].get("phase_id") == manifest.get("phases", [{}])[-1].get("phase_id") and
                        isinstance(state["review"].get("report_sha256"), str) and
                        re.fullmatch(r"[0-9a-f]{64}", state["review"]["report_sha256"]))
            if not accepted:
                _fail("INTENT_CERTIFICATION_UNAVAILABLE", "task_acceptance must be pending or bound to a final review")
        if name == "planning_assessment" and not pending:
            if not isinstance(state, dict) or state.get("status") != "ASSESSED":
                _fail("INTENT_CERTIFICATION_UNAVAILABLE", "planner cannot supply a planning assessment")
            if set(state) != {"status", "assessment", "history"} or not isinstance(state.get("history"), list):
                _fail("INVALID_INTENT_ASSESSMENT", "planning assessment must be pending or assessed with history")
    source_bytes: dict[str, bytes | None] = {}
    source_records: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict) or set(source) != INTENT_SOURCE_FIELDS:
            _fail("INVALID_INTENT_SOURCE", "source record has unknown or missing fields")
        source_id, source_type, status = source.get("source_id"), source.get("source_type"), source.get("status")
        if (not isinstance(source_id, str) or not re.fullmatch(r"[0-9a-f]{64}", source_id) or
                source_id in source_records or not isinstance(source_type, str) or source_type not in INTENT_SOURCE_TYPES or
                not isinstance(status, str) or
                status not in {"VERIFIED", "UNVERIFIED"} or not isinstance(source.get("provenance"), dict)):
            _fail("INVALID_INTENT_SOURCE", "source identity, type, status or provenance is invalid")
        provenance = source["provenance"]
        if source_type == "user_prompt" and status == "VERIFIED" and provenance.get("kind") == "user_prompt_submit":
            if (set(provenance) != {"kind", "session_id", "turn_id", "project"} or
                    provenance.get("kind") != "user_prompt_submit" or
                    hashlib.sha256(_canonical_json(provenance)).hexdigest() != source_id):
                _fail("INVALID_INTENT_SOURCE", "captured prompt provenance identity is invalid")
        elif source_type == "intent_contract" and status == "VERIFIED":
            _validate_contract_confirmation(provenance, source.get("sha256"), manifest["task_id"])
            expected_id = hashlib.sha256(_canonical_json({"source_type": source_type, "provenance": provenance,
                                                           "sha256": source.get("sha256")})).hexdigest()
            if expected_id != source_id:
                _fail("INVALID_INTENT_SOURCE", "confirmed contract source identity is invalid")
        elif status == "UNVERIFIED":
            if (set(provenance) != {"kind", "origin", "reason"} or provenance.get("kind") != "unavailable" or
                    not isinstance(provenance.get("origin"), str) or not provenance["origin"].strip() or
                    not isinstance(provenance.get("reason"), str) or not provenance["reason"].strip()):
                _fail("INVALID_INTENT_SOURCE", "unavailable source provenance is invalid")
            expected_id = hashlib.sha256(_canonical_json({"source_type": source_type, "provenance": provenance,
                                                           "sha256": None})).hexdigest()
            if expected_id != source_id:
                _fail("INVALID_INTENT_SOURCE", "unavailable source identity is invalid")
        else:
            if source_type == "user_prompt":
                _fail("INVALID_INTENT_SOURCE", "verified prompt must carry capture provenance")
            if (set(provenance) != {"kind", "path", "origin"} or provenance.get("kind") != "file_snapshot" or
                    not isinstance(provenance.get("path"), str) or not pathlib.Path(provenance["path"]).is_absolute() or
                    not isinstance(provenance.get("origin"), str) or not provenance["origin"].strip()):
                _fail("INVALID_INTENT_SOURCE", "file snapshot provenance is invalid")
            expected_id = hashlib.sha256(_canonical_json({"source_type": source_type, "provenance": provenance,
                                                           "sha256": source.get("sha256")})).hexdigest()
            if expected_id != source_id:
                _fail("INVALID_INTENT_SOURCE", "file snapshot identity is invalid")
        if status == "UNVERIFIED":
            if any(source.get(key) is not None for key in ("sha256", "size_bytes", "snapshot")) or source.get("captured_at") is not None:
                _fail("INVALID_INTENT_SOURCE", "unverified source must not claim bytes or capture provenance")
            source_bytes[source_id] = None
        else:
            digest, size, snapshot = source.get("sha256"), source.get("size_bytes"), source.get("snapshot")
            if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                    type(size) is not int or size < 0 or not isinstance(snapshot, str)):
                _fail("INVALID_INTENT_SOURCE", "verified source byte metadata is malformed")
            if source_type == "user_prompt" and type(source.get("captured_at")) is not int:
                _fail("INVALID_INTENT_SOURCE", "captured prompt requires its integer capture timestamp")
            if source_type != "user_prompt" and source.get("captured_at") is not None:
                _fail("INVALID_INTENT_SOURCE", "file source cannot claim a prompt capture timestamp")
            expected_path = _source_snapshot_path(task_root, manifest["task_id"], source_id)
            if (not fs.has_private_mode(expected_path.parent, 0o700) or
                    any(not fs.safe_writer_permissions(directory) for directory in expected_path.parents[1:3])):
                _fail("UNSAFE_INTENT_SOURCE_PATH", "source snapshot directory permissions are not private")
            if (snapshot != expected_path.relative_to(task_root.resolve()).as_posix() or fs.is_link(expected_path) or
                    not expected_path.is_file() or not fs.has_private_mode(expected_path, 0o600)):
                _fail("INTENT_SOURCE_SNAPSHOT_MISSING", "verified source snapshot is missing or mislocated")
            data = expected_path.read_bytes()
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                _fail("INTENT_SOURCE_SNAPSHOT_CHANGED", "verified source snapshot bytes changed")
            if source_type == "intent_contract":
                _validate_contract_bytes(data, manifest["task_id"])
            source_bytes[source_id] = data
        source_records[source_id] = source
    seen_requirements = set()
    for requirement in requirements:
        fields = {"requirement_id", "kind", "statement", "source_fragments", "acceptance_refs", "evidence_records"}
        if not isinstance(requirement, dict) or set(requirement) != fields:
            _fail("INVALID_INTENT_REQUIREMENT", "requirement has unknown or missing fields")
        rid, kind = requirement.get("requirement_id"), requirement.get("kind")
        if (not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", rid) or rid in seen_requirements or
                not isinstance(kind, str) or kind not in {"explicit_user_obligation", "established_fact", "technical_decision"} or
                not isinstance(requirement.get("statement"), str) or not requirement["statement"].strip()):
            _fail("INVALID_INTENT_REQUIREMENT", "requirement identity, kind or statement is invalid")
        seen_requirements.add(rid)
        fragments = requirement.get("source_fragments")
        if not isinstance(fragments, list):
            _fail("INVALID_INTENT_REQUIREMENT", "source_fragments must be an array")
        if kind == "explicit_user_obligation" and not fragments:
            _fail("INVALID_INTENT_REQUIREMENT", "explicit obligation requires a verified source fragment")
        _validate_intent_fragments(fragments, source_bytes, error_code="INVALID_INTENT_REQUIREMENT")
        refs = requirement.get("acceptance_refs")
        if not isinstance(refs, list):
            _fail("INVALID_INTENT_REQUIREMENT", "acceptance_refs must be an array")
        for reference in refs:
            if not isinstance(reference, dict) or set(reference) != {"phase_id", "acceptance_index", "sha256"}:
                _fail("INVALID_INTENT_REQUIREMENT", "acceptance reference has unknown or missing fields")
            phase_id = reference.get("phase_id")
            phase = phases.get(phase_id) if isinstance(phase_id, str) else None
            index = reference.get("acceptance_index")
            if (phase is None or type(index) is not int or index < 0 or index >= len(phase["acceptance"]) or
                    not isinstance(reference.get("sha256"), str) or
                    hashlib.sha256(phase["acceptance"][index].encode("utf-8")).hexdigest() != reference["sha256"]):
                _fail("INVALID_INTENT_REQUIREMENT", "acceptance reference is stale or unknown")
        evidence = requirement.get("evidence_records")
        if not isinstance(evidence, list):
            _fail("INVALID_INTENT_REQUIREMENT", "evidence_records must be an array")
        normalized = [_validate_evidence_ref(item) for item in evidence]
        if normalized != list(dict.fromkeys(normalized)):
            _fail("INVALID_INTENT_REQUIREMENT", "evidence references must be deduplicated")
    _validate_intent_decisions(intent, source_records, source_bytes, seen_requirements)
    _validate_planning_assessment(manifest, intent, phases, source_records, source_bytes)


def _validate_intent_fragments(fragments, source_bytes, *, error_code):
    if not isinstance(fragments, list):
        _fail(error_code, "source_fragments must be an array")
    for fragment in fragments:
        if not isinstance(fragment, dict) or set(fragment) != {"source_id", "start_byte", "end_byte", "sha256"}:
            _fail(error_code, "source fragment has unknown or missing fields")
        sid, start, end, digest = (fragment.get("source_id"), fragment.get("start_byte"),
                                   fragment.get("end_byte"), fragment.get("sha256"))
        data = source_bytes.get(sid) if isinstance(sid, str) else None
        if data is None:
            _fail(error_code, "source fragment must refer to a verified source")
        if (type(start) is not int or type(end) is not int or start < 0 or start >= end or end > len(data) or
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest) or
                hashlib.sha256(data[start:end]).hexdigest() != digest):
            _fail(error_code, "source fragment byte range or digest is invalid")
        try:
            data[:start].decode("utf-8"); data[start:end].decode("utf-8"); data[:end].decode("utf-8")
        except UnicodeDecodeError:
            _fail(error_code, "source fragment offsets must align to UTF-8 boundaries")


def _intent_definition(row):
    return {key: row[key] for key in ("kind", "statement", "source_fragments")}


def effective_intent_requirements(manifest: dict[str, Any]) -> dict[str, dict[str, Any] | None]:
    """Return each original requirement's current definition after addressed decisions."""
    intent = manifest.get("original_intent", {})
    effective = {row["requirement_id"]: _intent_definition(row)
                 for row in intent.get("requirements", [])}
    for decision in intent.get("decisions", []):
        for change in decision.get("changes", []):
            effective[change["requirement_id"]] = change["after"]
    return effective


def _intent_phase_technical_projection(phase: dict[str, Any]) -> dict[str, Any]:
    item = {"phase_id": phase["phase_id"], "depends_on": phase["depends_on"]}
    item.update({key: phase[key] for key in _REAPPROVAL_FIELDS if key in phase})
    return item


def _intent_technical_projection(manifest: dict[str, Any]) -> dict[str, Any]:
    return {"task_mode": manifest["task_mode"], "semantic_contract": manifest["semantic_contract"],
            "phases": [_intent_phase_technical_projection(phase) for phase in manifest["phases"]]}


def intent_plan_digest(manifest: dict[str, Any]) -> str:
    """Digest only source/business intent and the accepted technical contract."""
    intent = manifest["original_intent"]
    requirement_rows = []
    for row in intent["requirements"]:
        requirement_rows.append({key: row[key] for key in
                                 ("requirement_id", "kind", "statement", "source_fragments", "acceptance_refs")})
    projection = {"task_id": manifest["task_id"],
                  "original_intent": {"sources": intent["sources"], "requirements": requirement_rows,
                                      "decisions": intent["decisions"]}}
    projection.update(_intent_technical_projection(manifest))
    return hashlib.sha256(_canonical_json(projection)).hexdigest()


def _validate_intent_decisions(intent, source_records, source_bytes, requirement_ids):
    seen = set()
    effective = {row["requirement_id"]: _intent_definition(row) for row in intent["requirements"]}
    for decision in intent["decisions"]:
        fields = {"decision_id", "source_fragments", "changes", "human_confirmation"}
        if not isinstance(decision, dict) or set(decision) != fields:
            _fail("INVALID_INTENT_DECISION", "decision has unknown or missing fields")
        decision_id = decision["decision_id"]
        if (not isinstance(decision_id, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", decision_id) or
                decision_id in seen):
            _fail("INVALID_INTENT_DECISION", "decision identity is invalid or duplicated")
        seen.add(decision_id)
        fragments = decision["source_fragments"]
        _validate_intent_fragments(fragments, source_bytes, error_code="INVALID_INTENT_DECISION")
        if not fragments or any(source_records[f["source_id"]]["source_type"] not in {"user_prompt", "owner_answer", "intent_contract"}
                                for f in fragments):
            _fail("INVALID_INTENT_DECISION", "decision must cite verified human-answer source fragments")
        changes = decision["changes"]
        if not isinstance(changes, list) or not changes:
            _fail("INVALID_INTENT_DECISION", "decision changes must be a nonempty array")
        changed_ids = set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {"requirement_id", "before", "after"}:
                _fail("INVALID_INTENT_DECISION", "decision change has unknown or missing fields")
            rid = change["requirement_id"]
            if (not isinstance(rid, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", rid) or
                    rid not in requirement_ids or rid in changed_ids or effective[rid] is None):
                _fail("INVALID_INTENT_DECISION", "decision references an unknown, duplicate or removed requirement")
            changed_ids.add(rid)
            if change["before"] != effective[rid]:
                _fail("INVALID_INTENT_DECISION", "decision before definition is stale")
            after = change["after"]
            if after is not None:
                if (not isinstance(after, dict) or set(after) != {"kind", "statement", "source_fragments"} or
                        not isinstance(after["kind"], str) or
                        after["kind"] not in {"explicit_user_obligation", "established_fact", "technical_decision"} or
                        not isinstance(after["statement"], str) or not after["statement"].strip()):
                    _fail("INVALID_INTENT_DECISION", "decision after definition is malformed")
                if (effective[rid]["kind"] == "explicit_user_obligation" and
                        after["kind"] != "explicit_user_obligation"):
                    _fail("INVALID_INTENT_DECISION", "decision cannot reclassify an explicit obligation")
                _validate_intent_fragments(after["source_fragments"], source_bytes,
                                           error_code="INVALID_INTENT_DECISION")
                if not after["source_fragments"] or any(fragment not in fragments for fragment in after["source_fragments"]):
                    _fail("INVALID_INTENT_DECISION", "amended definition must cite the decision answer")
            if after == change["before"]:
                _fail("INVALID_INTENT_DECISION", "decision must change the effective definition")
            effective[rid] = after
        human = decision["human_confirmation"]
        human_fields = {"actor", "channel", "interpretation", "authority_rationale"}
        if (not isinstance(human, dict) or set(human) != human_fields or
                any(not isinstance(human.get(field), str) or not human[field].strip()
                    for field in human_fields - {"channel"}) or
                not isinstance(human.get("channel"), str) or
                human.get("channel") not in {"codex_user_message", "external_owner_message"}):
            _fail("INVALID_INTENT_DECISION", "human confirmation interpretation is incomplete")
        if any(source_records[fragment["source_id"]]["source_type"] == "owner_answer" for fragment in fragments) and \
                human["channel"] != "external_owner_message":
            _fail("INVALID_INTENT_DECISION", "owner_answer source requires external owner confirmation")


_ASSESSMENT_FIELDS = {"task_id", "plan_sha256", "previous_plan_sha256", "mode", "result", "source_reads",
                      "unavailable_sources", "source_outcomes", "obligation_checks", "technical_change_rationale"}


def _validate_planning_assessment(manifest, intent, phases, source_records, source_bytes):
    state = intent["planning_assessment"]
    if state.get("status") == "NOT_ASSESSED":
        return
    assessment = state.get("assessment")
    if not isinstance(assessment, dict) or set(assessment) != _ASSESSMENT_FIELDS:
        _fail("INVALID_INTENT_ASSESSMENT", "assessment fields are malformed")
    if (assessment.get("task_id") != manifest["task_id"] or
            assessment.get("plan_sha256") != intent_plan_digest(manifest) or
            not re.fullmatch(r"[0-9a-f]{64}", str(assessment.get("plan_sha256"))) or
            assessment.get("previous_plan_sha256") is not None and
            not re.fullmatch(r"[0-9a-f]{64}", str(assessment.get("previous_plan_sha256"))) or
            not isinstance(assessment.get("mode"), str) or
            assessment.get("mode") not in {"IMPLEMENTATION", "DIAGNOSIS"} or
            not isinstance(assessment.get("result"), str) or
            assessment.get("result") not in {"COMPLETE", "INCOMPLETE"} or
            not isinstance(assessment.get("technical_change_rationale"), str) or
            not assessment["technical_change_rationale"].strip()):
        _fail("INVALID_INTENT_ASSESSMENT", "assessment identity, mode, result or rationale is invalid")
    arrays = {field: assessment.get(field) for field in
              ("source_reads", "unavailable_sources", "source_outcomes", "obligation_checks")}
    if any(not isinstance(value, list) for value in arrays.values()):
        _fail("INVALID_INTENT_ASSESSMENT", "assessment collections must be arrays")
    verified = {sid: row for sid, row in source_records.items() if row["status"] == "VERIFIED"}
    unavailable = {sid: row for sid, row in source_records.items() if row["status"] == "UNVERIFIED"}
    read_ids, unavailable_ids = set(), set()
    for row in arrays["source_reads"]:
        if (not isinstance(row, dict) or set(row) != {"source_id", "sha256", "findings"} or
                not isinstance(row.get("source_id"), str) or row.get("source_id") not in verified or
                row.get("sha256") != verified[row["source_id"]]["sha256"] or
                not isinstance(row.get("findings"), str) or not row["findings"].strip() or
                row["source_id"] in read_ids):
            _fail("INVALID_INTENT_ASSESSMENT", "source_reads must identify each verified source with concrete findings")
        read_ids.add(row["source_id"])
    for row in arrays["unavailable_sources"]:
        if (not isinstance(row, dict) or set(row) != {"source_id", "limitation"} or
                not isinstance(row.get("source_id"), str) or row.get("source_id") not in unavailable or
                not isinstance(row.get("limitation"), str) or
                not row["limitation"].strip() or row["source_id"] in unavailable_ids):
            _fail("INVALID_INTENT_ASSESSMENT", "unavailable_sources must state each unverified source limitation")
        unavailable_ids.add(row["source_id"])
    if read_ids != set(verified) or unavailable_ids != set(unavailable):
        _fail("INVALID_INTENT_ASSESSMENT", "assessment must account for every verified and unverified source")
    active = {rid: value for rid, value in effective_intent_requirements(manifest).items() if value is not None}
    mapped = {rid for rid, definition in active.items() if definition["kind"] == "explicit_user_obligation"}
    for rid in mapped:
        original = next(row for row in intent["requirements"] if row["requirement_id"] == rid)
        if not original["acceptance_refs"]:
            _fail("INVALID_INTENT_ASSESSMENT", "each active explicit obligation requires acceptance mapping")
    for row in arrays["source_outcomes"]:
        fields = {"source_fragments", "expected_outcome", "requirement_ids", "disposition", "rationale"}
        if (not isinstance(row, dict) or set(row) != fields or not isinstance(row.get("expected_outcome"), str) or
                not row["expected_outcome"].strip() or not isinstance(row.get("rationale"), str) or
                not row["rationale"].strip() or not isinstance(row.get("requirement_ids"), list) or
                any(not isinstance(rid, str) for rid in row["requirement_ids"]) or
                len(row["requirement_ids"]) != len(set(row["requirement_ids"])) or
                any(rid not in active for rid in row["requirement_ids"]) or
                not isinstance(row.get("disposition"), str) or
                row.get("disposition") not in {"COVERED", "OMITTED", "DIAGNOSIS_PENDING"}):
            _fail("INVALID_INTENT_ASSESSMENT", "source outcome is malformed")
        _validate_intent_fragments(row["source_fragments"], source_bytes,
                                   error_code="INVALID_INTENT_ASSESSMENT")
        if not row["source_fragments"]:
            _fail("INVALID_INTENT_ASSESSMENT", "source outcome requires verified source fragments")
        if row["disposition"] == "COVERED" and not row["requirement_ids"]:
            _fail("INVALID_INTENT_ASSESSMENT", "covered source outcome requires active requirements")
    if assessment["result"] == "COMPLETE" and any(row["disposition"] == "OMITTED" for row in arrays["source_outcomes"]):
        _fail("INVALID_INTENT_ASSESSMENT", "an omitted source outcome cannot receive a complete assessment")
    if any(row["disposition"] == "DIAGNOSIS_PENDING" for row in arrays["source_outcomes"]) and assessment["mode"] != "DIAGNOSIS":
        _fail("INVALID_INTENT_ASSESSMENT", "DIAGNOSIS_PENDING is valid only in diagnosis mode")
    history = state.get("history")
    if not isinstance(history, list):
        _fail("INVALID_INTENT_ASSESSMENT", "assessment history must be an array")
    previous_decisions = set()
    prior_digest = None
    for previous in history:
        if (not isinstance(previous, dict) or set(previous) != _ASSESSMENT_FIELDS or
                previous.get("task_id") != manifest["task_id"] or
                not isinstance(previous.get("plan_sha256"), str) or
                not re.fullmatch(r"[0-9a-f]{64}", previous["plan_sha256"]) or
                previous.get("previous_plan_sha256") != prior_digest or
                not isinstance(previous.get("mode"), str) or
                previous.get("mode") not in {"IMPLEMENTATION", "DIAGNOSIS"} or
                not isinstance(previous.get("result"), str) or
                previous.get("result") not in {"COMPLETE", "INCOMPLETE"} or
                not isinstance(previous.get("technical_change_rationale"), str) or
                not previous["technical_change_rationale"].strip() or
                any(not isinstance(previous.get(field), list) for field in
                    ("source_reads", "unavailable_sources", "source_outcomes", "obligation_checks"))):
            _fail("INVALID_INTENT_ASSESSMENT", "assessment history digest chain is malformed")
        prior_digest = previous["plan_sha256"]
        for previous_check in previous.get("obligation_checks", []):
            if (isinstance(previous_check, dict) and previous_check.get("decision_id") and
                    isinstance(previous_check["decision_id"], str)):
                previous_decisions.add(previous_check["decision_id"])
    checked = set()
    original_explicit = {row["requirement_id"] for row in intent["requirements"]
                         if row["kind"] == "explicit_user_obligation"}
    removed = {rid for rid in original_explicit if effective_intent_requirements(manifest).get(rid) is None}
    previously_removed = set()
    for previous in history:
        if isinstance(previous, dict) and isinstance(previous.get("obligation_checks"), list):
            previously_removed.update(row.get("requirement_id") for row in previous["obligation_checks"]
                                      if isinstance(row, dict) and row.get("change") == "USER_REMOVED")
    removed_to_check = removed - previously_removed
    checkable = mapped | removed_to_check
    decisions_by_id = {row["decision_id"]: row for row in intent["decisions"]}
    for row in arrays["obligation_checks"]:
        fields = {"requirement_id", "change", "preserved_outcome", "safety_rationale", "acceptance_rationale", "decision_id"}
        rid = row.get("requirement_id") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or set(row) != fields or not isinstance(rid, str) or
                rid not in checkable or rid in checked or not isinstance(row.get("change"), str) or
                row.get("change") not in {"PRESERVED", "USER_AMENDED", "USER_REMOVED"} or
                any(not isinstance(row.get(key), str) or not row[key].strip() for key in
                    ("preserved_outcome", "safety_rationale", "acceptance_rationale"))):
            _fail("INVALID_INTENT_ASSESSMENT", "obligation check is malformed")
        decision_id = row["decision_id"]
        if row["change"] == "PRESERVED":
            if decision_id is not None:
                _fail("INVALID_INTENT_ASSESSMENT", "preserved obligations cannot cite an amendment decision")
        else:
            if not isinstance(decision_id, str):
                _fail("INVALID_INTENT_ASSESSMENT", "amended obligation check requires a decision identifier")
            if row["change"] == "USER_AMENDED" and decision_id in previous_decisions:
                _fail("INVALID_INTENT_ASSESSMENT", "an earlier addressed amendment must be preserved in later assessments")
            if (decision_id not in decisions_by_id or not any(change["requirement_id"] == rid
                                                               for change in decisions_by_id[decision_id]["changes"])):
                _fail("INVALID_INTENT_ASSESSMENT", "amended obligation check requires its addressed decision")
            if row["change"] == "USER_REMOVED" and not any(
                    change["requirement_id"] == rid and change["after"] is None
                    for change in decisions_by_id[decision_id]["changes"]):
                _fail("INVALID_INTENT_ASSESSMENT", "removal check must cite the decision that removes the obligation")
            if (row["change"] == "USER_REMOVED") != (rid in removed_to_check):
                _fail("INVALID_INTENT_ASSESSMENT", "removal check does not match the active intent definition")
        checked.add(rid)
    if checked != checkable:
        _fail("INVALID_INTENT_ASSESSMENT", "assessment must check each active explicit obligation")
    if removed:
        for rid in removed:
            removal_is_bound = False
            for reviewed in [*history, assessment]:
                checks = reviewed.get("obligation_checks", []) if isinstance(reviewed, dict) else []
                for removal_check in checks:
                    if (not isinstance(removal_check, dict) or removal_check.get("requirement_id") != rid or
                            removal_check.get("change") != "USER_REMOVED"):
                        continue
                    decision = decisions_by_id.get(removal_check.get("decision_id"))
                    if (decision is not None and any(change["requirement_id"] == rid and change["after"] is None
                                                      for change in decision["changes"])):
                        removal_is_bound = True
            if not removal_is_bound:
                _fail("INVALID_INTENT_ASSESSMENT", "removed obligation requires a USER_REMOVED check bound to its null-after decision")
    if not arrays["source_outcomes"]:
        diagnosis_without_active_obligation = assessment["mode"] == "DIAGNOSIS" and not mapped
        implementation_after_addressed_removals = (
            assessment["mode"] == "IMPLEMENTATION" and bool(original_explicit) and removed == original_explicit)
        if not (diagnosis_without_active_obligation or implementation_after_addressed_removals):
            _fail("INVALID_INTENT_ASSESSMENT", "empty source outcomes require diagnosis without active obligations or implementation after addressed removals")
    if any(not isinstance(previous, dict) or set(previous) != _ASSESSMENT_FIELDS for previous in history):
        _fail("INVALID_INTENT_ASSESSMENT", "assessment history entries are malformed")
    if history and history[-1].get("plan_sha256") != assessment.get("previous_plan_sha256"):
        _fail("INVALID_INTENT_ASSESSMENT", "assessment history does not link to the previous plan digest")
    if not history and assessment.get("previous_plan_sha256") is not None:
        _fail("INVALID_INTENT_ASSESSMENT", "first assessment cannot claim a previous plan digest")
def _source_snapshot_path(task_root: pathlib.Path, task_id: str, source_id: str) -> pathlib.Path:
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id) or fs.unsafe_parts([task_id]):
        _fail("INVALID_TASK_ID", "task id is not safe for a source snapshot path")
    if not re.fullmatch(r"[0-9a-f]{64}", source_id):
        _fail("INVALID_INTENT_SOURCE", "source_id must be a lowercase SHA-256 digest")
    root = task_root.resolve()
    relative = pathlib.PurePosixPath(".codex/tasks") / task_id / "sources" / f"{source_id}.bin"
    candidate = root.joinpath(*relative.parts)
    current = root
    for part in relative.parts:
        current = current / part
        if fs.is_link(current):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "source snapshot path traverses a symlink")
    try:
        candidate.resolve(strict=False).relative_to(root)
    except ValueError:
        _fail("UNSAFE_INTENT_SOURCE_PATH", "source snapshot escapes the task worktree")
    return candidate


def _read_regular_bytes(path: pathlib.Path, max_bytes: int, error_code: str) -> tuple[bytes, os.stat_result]:
    """Read a bounded regular file without following symlinks or blocking on FIFOs."""
    flags = os.O_RDONLY
    try:
        descriptor = fs.open_regular(path, flags)
    except OSError as exc:
        _fail(error_code, f"cannot open saved source file safely: {exc}")
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "saved source path must be a regular file")
        if metadata.st_size > max_bytes:
            _fail(error_code, f"saved source file exceeds the {max_bytes}-byte limit")
        content = bytearray()
        while len(content) <= max_bytes:
            chunk = os.read(descriptor, min(65536, max_bytes + 1 - len(content)))
            if not chunk:
                break
            content.extend(chunk)
        if len(content) > max_bytes:
            _fail(error_code, f"saved source file exceeds the {max_bytes}-byte limit")
        return bytes(content), metadata
    except OSError as exc:
        _fail(error_code, f"cannot read saved source file safely: {exc}")
    finally:
        os.close(descriptor)


def _create_source_snapshot(path: pathlib.Path, data: bytes) -> None:
    if len(data) > 8 * 1024 * 1024:
        _fail("INTENT_SOURCE_TOO_LARGE", "source snapshot exceeds the 8 MiB limit")
    parent = path.parent
    missing = []
    current = parent
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
            fs.private(directory, 0o700)
        except FileExistsError:
            pass
        if fs.is_link(directory) or not directory.is_dir():
            _fail("UNSAFE_INTENT_SOURCE_PATH", "source snapshot ancestor is not a regular directory")
    for directory in [parent, *parent.parents]:
        if fs.is_link(directory) or not directory.is_dir():
            _fail("UNSAFE_INTENT_SOURCE_PATH", "source snapshot ancestor is not a regular directory")
        # Existing task directories are shared state created by init (normally 0755).
        # Keep source bytes behind a private container without changing those parents.
        if (directory == parent and not fs.has_private_mode(directory, 0o700)) or not fs.safe_writer_permissions(directory):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "source snapshot directory permissions are not private")
        if directory == path.parents[2]:
            break
    if path.exists() or fs.is_link(path):
        if fs.is_link(path) or not path.is_file() or path.read_bytes() != data:
            _fail("INTENT_SOURCE_CONFLICT", "existing immutable source snapshot has conflicting bytes")
        if not fs.has_private_mode(path, 0o600):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "existing source snapshot permissions are not private")
        if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(data).digest():
            _fail("INTENT_SOURCE_CONFLICT", "existing immutable source snapshot failed rehash")
        return
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(parent))
    try:
        fs.private(temporary, 0o600, fd=fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        try:
            fs.publish(temporary, path, replace=False)
        except FileExistsError:
            if fs.is_link(path) or not path.is_file() or path.read_bytes() != data:
                _fail("INTENT_SOURCE_CONFLICT", "concurrent immutable source has conflicting bytes")
    finally:
        try: os.unlink(temporary)
        except FileNotFoundError: pass


def _prompt_receipts():
    hook_path = pathlib.Path(__file__).resolve().parents[1] / "hooks" / "user_prompt_submit.py"
    spec = importlib.util.spec_from_file_location("devcrew_prompt_receipts", hook_path)
    if spec is None or spec.loader is None:
        _fail("INTENT_SOURCE_UNAVAILABLE", "prompt receipt hook cannot be loaded")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_contract_bytes(data, task_id):
    try:
        contract = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        _fail("INVALID_INTENT_CONTRACT", str(exc))
    fields = {"task_id", "goal", "requirements", "acceptance", "non_goals"}
    if (not isinstance(contract, dict) or set(contract) != fields or contract.get("task_id") != task_id
            or not isinstance(contract.get("goal"), str) or not contract["goal"].strip()):
        _fail("INVALID_INTENT_CONTRACT", "contract requires task_id, goal, requirements, acceptance and non_goals")
    for field in ("requirements", "acceptance", "non_goals"):
        rows = contract[field]
        if (not isinstance(rows, list) or (field != "non_goals" and not rows)
                or any(not isinstance(row, str) or not row.strip() for row in rows)):
            _fail("INVALID_INTENT_CONTRACT", "contract lists must contain structured outcome/constraint statements")
    return contract


def _validate_contract_confirmation(provenance, digest, task_id, projects=None):
    if (not isinstance(provenance, dict) or set(provenance) != {"kind", "confirmation"}
            or provenance.get("kind") != "confirmed_intent_contract"):
        _fail("INTENT_CONFIRMATION_INVALID", "contract requires separate human confirmation provenance")
    record = provenance["confirmation"]
    module = _prompt_receipts()
    try:
        module._validate_capture_record(record, record.get("source_id"))
    except (ValueError, AttributeError, TypeError) as exc:
        _fail("INTENT_CONFIRMATION_INVALID", str(exc))
    # Recover unclassified legacy receipts only from an exact whole-message
    # digest. Never rewrite the original receipt or infer a generic approval.
    if (record.get("confirmation") not in (None, {"task_id": task_id, "sha256": digest})
            or record["sha256"] not in module.confirmation_digests(task_id, digest)
            or projects is not None and record["provenance"]["project"] not in projects):
        _fail("INTENT_CONFIRMATION_INVALID", "user confirmation does not bind this task, project and exact contract digest")


def intent_source_preflight(project, receipt, *, source=None):
    """Fail before planner issuance; reread immutable source on every recovery."""
    task_root = pathlib.Path(receipt["task_worktree_root"])
    path = _source_snapshot_path(task_root, receipt["task_id"], "0" * 64).with_name("planning-source.json")
    if source is None:
        if fs.is_link(path) or not path.is_file() or path.stat().st_size > 1024 * 1024:
            _fail("INTENT_SOURCE_REQUIRED", "bind a verified source or user-confirmed Intent Contract before planner dispatch")
        try:
            record = json.loads(path.read_bytes())
        except (OSError, ValueError) as exc:
            _fail("INVALID_INTENT_SOURCE", str(exc))
    else:
        record = source
    if not isinstance(record, dict) or record.get("status") != "VERIFIED":
        _fail("INTENT_SOURCE_REQUIRED", "unavailable sources cannot authorize a planner")
    intent = {"sources": [record], "requirements": [], "decisions": [],
              "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
              "task_acceptance": {"status": "NOT_ASSESSED", "review": None}}
    _validate_original_intent({"task_id": receipt["task_id"], "original_intent": intent}, task_root, {})
    if record["source_type"] == "intent_contract":
        _validate_contract_confirmation(record["provenance"], record["sha256"], receipt["task_id"],
                                        {str(project.resolve()), str(task_root.resolve())})
    _verify_ordinary_handoff(project, receipt, record)
    return record


def _ordinary_handoff_path(project: pathlib.Path, task_id: str) -> pathlib.Path:
    return pathlib.Path(project) / ".codex" / "tasks" / task_id / "ordinary-handoff.json"


def _verify_ordinary_handoff(project, receipt, source):
    """Validate a same-task seal and link when an explicit handoff exists."""
    import devcrew_ordinary
    ordinary_path = pathlib.Path(project) / ".codex" / "tasks" / f"{receipt['task_id']}.ordinary.json"
    link_path = _ordinary_handoff_path(pathlib.Path(project), receipt["task_id"])
    if not (ordinary_path.exists() or fs.is_link(ordinary_path) or link_path.exists() or fs.is_link(link_path)):
        return None
    if fs.is_link(ordinary_path) or fs.is_link(link_path) or not ordinary_path.is_file() or not link_path.is_file():
        _fail("ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED", "ordinary task history exists without an intact formal handoff")
    try:
        ordinary = devcrew_ordinary._read(ordinary_path)
        link = json.loads(link_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", str(exc))
    seal = ordinary.get("formal_handoff")
    if (not isinstance(seal, dict) or link != {"schema_version": 1, "task_id": receipt["task_id"], "seal": seal} or
            not isinstance(seal.get("payload"), dict)):
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "ordinary seal and formal handoff link do not match")
    payload = seal["payload"]
    checkout = ordinary.get("checkout_identity")
    if (payload.get("checkout_root") != str(pathlib.Path(receipt["task_worktree_root"]).resolve()) or
            payload.get("branch") != receipt.get("branch") or payload.get("base_sha") != ordinary.get("base_sha") or
            not isinstance(checkout, dict) or checkout.get("base_sha") != ordinary.get("base_sha") or
            checkout.get("root") != payload.get("checkout_root") or
            payload.get("source_id") != source.get("source_id") or payload.get("source_sha256") != source.get("sha256") or
            payload.get("source_size_bytes") != source.get("size_bytes") or
            source.get("source_type") != "attachment"):
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "formal source does not match the sealed task, checkout or attachment")
    return seal


def bind_ordinary_source_gap(project: pathlib.Path, receipt: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    import devcrew_ordinary
    task_id = receipt.get("task_id") if isinstance(receipt, dict) else None
    ordinary_path = pathlib.Path(project) / ".codex" / "tasks" / f"{task_id}.ordinary.json"
    if fs.is_link(ordinary_path) or not ordinary_path.is_file():
        _fail("ORDINARY_SOURCE_GAP_STATE_MISSING", "same-task ordinary history is unavailable")
    with devcrew_ordinary._task_lock(ordinary_path):
        return _bind_ordinary_source_gap_locked(project, receipt, spec)


def _bind_ordinary_source_gap_locked(project: pathlib.Path, receipt: dict[str, Any], spec: dict[str, Any]) -> dict[str, Any]:
    """Bind one exact local attachment and seal an existing ordinary lifecycle."""
    import devcrew_ordinary
    if (not isinstance(receipt, dict) or receipt.get("state") != "RESOLVED" or receipt.get("owner") != "DEVCREW" or
            receipt.get("task_id") is None or pathlib.Path(receipt.get("task_worktree_root", "")).resolve() != project.resolve()):
        _fail("WORKTREE_UNRESOLVED", "ordinary source-gap handoff requires the same resolved checkout")
    if (not isinstance(spec, dict) or set(spec) != {"source_spec", "authorization", "host_observation"} or
            not isinstance(spec.get("source_spec"), dict) or spec["source_spec"].get("mode") != "file" or
            spec["source_spec"].get("source_type") != "attachment"):
        _fail("INVALID_ORDINARY_SOURCE_GAP", "handoff requires one exact file attachment source spec")
    task_id = receipt["task_id"]
    ordinary_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
    if fs.is_link(ordinary_path) or not ordinary_path.is_file():
        _fail("ORDINARY_SOURCE_GAP_STATE_MISSING", "same-task ordinary history is unavailable")
    try:
        ordinary = devcrew_ordinary._read(ordinary_path)
    except (OSError, ValueError) as exc:
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", str(exc))
    original_source = ordinary.get("source", {})
    checkout_identity = ordinary.get("checkout_identity")
    if (not isinstance(checkout_identity, dict) or not isinstance(checkout_identity.get("branch"), str) or
            not checkout_identity.get("branch")):
        _fail("ORDINARY_SOURCE_GAP_CHECKOUT_IDENTITY_UNAVAILABLE",
              "legacy ordinary state lacks the original branch identity; reconcile the task from its original checkout before retrying")
    original_branch = checkout_identity.get("branch")
    branch_matches = original_branch == receipt.get("branch")
    registered_primary_branch = (
        isinstance(receipt.get("branch"), str) and
        receipt["branch"].startswith(f"codex/tasks/{task_id}") and
        receipt.get("created_from_branch") == original_branch and
        receipt.get("expected_head") == ordinary.get("base_sha"))
    if not (branch_matches or registered_primary_branch):
        _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED",
              "the current branch differs from the branch that captured the ordinary request")
    if (ordinary.get("mode") != "ordinary_chat" or ordinary.get("status") in {"ACCEPTED", "REJECTED"}):
        _fail("ORDINARY_SOURCE_GAP_NOT_HANDOFFABLE", "ordinary task is not active for a formal source handoff")
    if (ordinary.get("task_id") != task_id or ordinary.get("base_sha") != receipt.get("expected_head") or
            not isinstance(checkout_identity, dict) or checkout_identity.get("root") != str(project.resolve()) or
            checkout_identity.get("base_sha") != receipt.get("expected_head") or
            pathlib.Path(receipt["task_worktree_root"]).resolve() != project.resolve()):
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "ordinary task ID, base or checkout differs from formal receipt")
    try:
        head = devcrew_worktree._git(project, "rev-parse", "HEAD", read_only=True).stdout.strip().lower()
        branch = devcrew_worktree._git(project, "symbolic-ref", "--short", "HEAD", read_only=True).stdout.strip()
        status = devcrew_worktree._git(project, "status", "--porcelain", "--untracked-files=all",
                                       read_only=True)
    except (OSError, ValueError) as exc:
        _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED", str(exc))
    if head != receipt.get("expected_head") or branch != receipt.get("branch"):
        _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED", "checkout HEAD or branch changed before source handoff")
    if status.returncode or status.stdout:
        _fail("ORDINARY_SOURCE_GAP_FOREIGN_DIFF", "source handoff requires a clean checkout; preserve and reconcile existing changes first")
    source_spec = spec["source_spec"]
    expected_source_sha = source_spec.get("expected_sha256")
    input_path = pathlib.Path(source_spec.get("path", ""))
    authorization = spec["authorization"]
    observation = spec["host_observation"]
    existing_seal = ordinary.get("formal_handoff")
    link_path = _ordinary_handoff_path(project, task_id)
    if fs.is_link(link_path):
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "formal handoff link must not be a symlink")
    if link_path.exists():
        try:
            saved_link = json.loads(link_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", str(exc))
        if not isinstance(existing_seal, dict) or saved_link != {"schema_version": 1, "task_id": task_id,
                                                                  "seal": existing_seal}:
            _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "existing formal handoff link differs from the saved seal")
    history_sha256 = (existing_seal["payload"].get("history_sha256")
                      if isinstance(existing_seal, dict) and isinstance(existing_seal.get("payload"), dict)
                      else hashlib.sha256(_canonical_json(ordinary)).hexdigest())
    ordinary_source = original_source
    if (not isinstance(authorization, dict) or set(authorization) != {"source_id", "scope"} or
            authorization.get("source_id") != ordinary_source.get("source_id") or
            authorization.get("scope") != f"formal source-gap transition for task {task_id}"):
        _fail("ORDINARY_SOURCE_GAP_AUTHORIZATION_REQUIRED", "bind the existing human authorization and scope")
    if (not isinstance(observation, dict) or observation.get("ordinary_state_sha256") != history_sha256 or
            observation.get("attachment_reference") != str(input_path.resolve())):
        _fail("ORDINARY_SOURCE_GAP_OBSERVATION_INVALID", "host observation must bind this exact attachment and ordinary state")
    try:
        devcrew_ordinary.validate_source_gap_observation(ordinary, observation)
    except ValueError as exc:
        _fail("ORDINARY_SOURCE_GAP_OBSERVATION_INVALID", str(exc))
    current_cycle = ordinary.get("cycle_id", 0)
    if any(isinstance(item, dict) and item.get("cycle_id", 0) == current_cycle and not item.get("consumed")
           for item in ordinary.get("issued_dispatches", [])):
        _fail("ORDINARY_SOURCE_GAP_WRITER_ACTIVE", "an issued ordinary route has no observed spawn")
    followup = ordinary.get("followup")
    if isinstance(followup, dict) and followup.get("cycle_id") == current_cycle and followup.get("issued") and not followup.get("used"):
        _fail("ORDINARY_SOURCE_GAP_WRITER_ACTIVE", "an ordinary followup is unresolved")
    provenance = {"kind": "file_snapshot", "path": str(input_path.resolve()), "origin": source_spec.get("origin")}
    expected_source_id = hashlib.sha256(_canonical_json({"source_type": "attachment", "provenance": provenance,
                                                         "sha256": expected_source_sha})).hexdigest()
    selection_path = _source_snapshot_path(pathlib.Path(receipt["task_worktree_root"]), task_id,
                                           "0" * 64).with_name("planning-source.json")
    if fs.is_link(selection_path):
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "source selection must not be a symlink")
    expected_snapshot_path = _source_snapshot_path(pathlib.Path(receipt["task_worktree_root"]), task_id,
                                                   expected_source_id)
    expected_source_record = {"source_id": expected_source_id, "source_type": "attachment",
                              "status": "VERIFIED", "sha256": expected_source_sha,
                              "size_bytes": (existing_seal["payload"].get("source_size_bytes")
                                             if isinstance(existing_seal, dict) else None),
                              "snapshot": expected_snapshot_path.relative_to(
                                  pathlib.Path(receipt["task_worktree_root"]).resolve()).as_posix(),
                              "provenance": provenance, "captured_at": None}
    if selection_path.exists():
        try:
            selection_bytes, _ = _read_regular_bytes(selection_path, 256 * 1024,
                                                                   "ORDINARY_SOURCE_GAP_BINDING_INVALID")
            previous_selection = json.loads(selection_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", str(exc))
        expected_source_record["size_bytes"] = (input_path.stat().st_size if input_path.is_file()
                                                 else expected_source_record["size_bytes"])
        if previous_selection != expected_source_record:
            _fail("ORDINARY_SOURCE_GAP_CONFLICT", "selected source record differs from the requested provenance, digest or size")
        if (not fs.has_private_mode(selection_path, 0o600) or
                not fs.has_private_mode(selection_path.parent, 0o700)):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "selected source metadata is not in private regular storage")
    if (isinstance(existing_seal, dict) and isinstance(existing_seal.get("payload"), dict) and
            existing_seal["payload"].get("source_sha256") != expected_source_sha):
        _fail("ORDINARY_SOURCE_GAP_CONFLICT", "ordinary task is already sealed to different source bytes")
    # Once the exact bytes have been privately snapshotted and sealed, retry can
    # complete the link from those bytes even if the original attachment vanished.
    if (existing_seal is not None and not input_path.is_file()):
        if not selection_path.is_file() or fs.is_link(selection_path):
            _fail("ORDINARY_SOURCE_GAP_SOURCE_INVALID", "saved source selection is unavailable; reattach the exact original source")
        if (existing_seal["payload"].get("source_id") != expected_source_id or
                existing_seal["payload"].get("source_sha256") != expected_source_sha or
                existing_seal["payload"].get("authorization") != authorization or
                existing_seal["payload"].get("host_observation") != observation):
            _fail("ORDINARY_SOURCE_GAP_CONFLICT", "retry provenance, digest, authorization or observation differs from the saved seal")
        saved_source = _source_snapshot_path(pathlib.Path(receipt["task_worktree_root"]), task_id,
                                             existing_seal["payload"]["source_id"])
        try:
            saved_bytes, _ = _read_regular_bytes(saved_source, 8 * 1024 * 1024,
                                                           "ORDINARY_SOURCE_GAP_SOURCE_INVALID")
        except (OSError, PhaseError):
            _fail("ORDINARY_SOURCE_GAP_SOURCE_INVALID", "saved private source bytes are unavailable; reattach the exact original source")
        if (hashlib.sha256(saved_bytes).hexdigest() != expected_source_sha or
                len(saved_bytes) != existing_seal["payload"]["source_size_bytes"]):
            _fail("ORDINARY_SOURCE_GAP_SOURCE_INVALID", "saved private source bytes do not match the requested digest and size")
        if (fs.is_link(saved_source) or not fs.has_private_mode(saved_source, 0o600) or
                expected_source_record.get("size_bytes") != len(saved_bytes) or
                expected_source_record.get("snapshot") != saved_source.relative_to(
                    pathlib.Path(receipt["task_worktree_root"]).resolve()).as_posix()):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "saved private source record or snapshot path is invalid")
        source = expected_source_record
    else:
        # Revalidate the checkout immediately before bind_intent_source can
        # publish the selected-source record or immutable source bytes.
        try:
            pre_receipt = devcrew_worktree.resolve(project, task_id, read_only=True)
            pre_head = devcrew_worktree._git(project, "rev-parse", "HEAD", read_only=True).stdout.strip().lower()
            pre_branch = devcrew_worktree._git(project, "symbolic-ref", "--short", "HEAD", read_only=True).stdout.strip()
            pre_status = devcrew_worktree._git(project, "status", "--porcelain", "--untracked-files=all",
                                               read_only=True)
        except (OSError, ValueError) as exc:
            _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED", str(exc))
        if (pre_receipt.get("state") != "RESOLVED" or pre_receipt.get("receipt_nonce") != receipt.get("receipt_nonce") or
                pre_receipt.get("task_worktree_root") != receipt.get("task_worktree_root") or
                pre_receipt.get("branch") != receipt.get("branch") or
                pre_receipt.get("created_from_branch") != receipt.get("created_from_branch") or
                pre_receipt.get("expected_head") != receipt.get("expected_head") or
                pre_head != receipt.get("expected_head") or pre_branch != receipt.get("branch") or
                pre_status.returncode or pre_status.stdout):
            _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED", "checkout changed before publishing the source selection; preserve state and reconcile")
        source = bind_intent_source(project, receipt, source_spec, _ordinary_handoff=True)
    if source.get("status") != "VERIFIED" or source.get("source_type") != "attachment":
        _fail("ORDINARY_SOURCE_GAP_SOURCE_INVALID", "formal handoff requires a verified exact attachment")
    payload = {"task_id": task_id, "ordinary_source_id": original_source.get("source_id"),
               "ordinary_message_sha256": original_source.get("message_sha256"),
               "cycle_id": ordinary.get("cycle_id"), "base_sha": ordinary.get("base_sha"),
               "checkout_root": str(project.resolve()), "branch": receipt.get("branch"),
               "history_sha256": (existing_seal["payload"]["history_sha256"] if isinstance(existing_seal, dict)
                                  else history_sha256),
               "source_id": source["source_id"], "source_sha256": source["sha256"],
               "source_size_bytes": source["size_bytes"], "authorization": authorization,
               "host_observation": observation}
    try:
        current_receipt = devcrew_worktree.resolve(project, task_id, read_only=True)
        current_head = devcrew_worktree._git(project, "rev-parse", "HEAD", read_only=True).stdout.strip().lower()
        current_branch = devcrew_worktree._git(project, "symbolic-ref", "--short", "HEAD", read_only=True).stdout.strip()
        current_status = devcrew_worktree._git(project, "status", "--porcelain", "--untracked-files=all",
                                               read_only=True)
    except (OSError, ValueError) as exc:
        _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED", str(exc))
    if (current_receipt.get("state") != "RESOLVED" or current_receipt.get("receipt_nonce") != receipt.get("receipt_nonce") or
            current_receipt.get("task_worktree_root") != receipt.get("task_worktree_root") or
            current_receipt.get("branch") != receipt.get("branch") or
            current_receipt.get("created_from_branch") != receipt.get("created_from_branch") or
            current_receipt.get("expected_head") != receipt.get("expected_head") or
            current_head != receipt.get("expected_head") or current_branch != receipt.get("branch") or
            current_status.returncode or current_status.stdout):
        _fail("ORDINARY_SOURCE_GAP_CHECKOUT_CHANGED", "checkout changed while binding the exact source; preserve state and reconcile")
    try:
        seal = devcrew_ordinary.seal_for_formal_handoff(project, task_id, payload)
    except ValueError as exc:
        _fail("ORDINARY_SOURCE_GAP_SEAL_FAILED", str(exc))
    link_path = _ordinary_handoff_path(project, task_id)
    link = {"schema_version": 1, "task_id": task_id, "seal": seal}
    if fs.is_link(link_path):
        _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "formal handoff link must not be a symlink")
    if link_path.exists():
        try:
            if json.loads(link_path.read_text(encoding="utf-8")) != link:
                _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", "existing formal handoff link differs from the saved seal")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("ORDINARY_SOURCE_GAP_BINDING_INVALID", str(exc))
    else:
        _atomic_create_json(link_path, link)
    _verify_ordinary_handoff(project, receipt, source)
    return {"status": "ORDINARY_SOURCE_GAP_SEALED", "source": source, "seal": seal}


def bind_intent_source(project: pathlib.Path, receipt: dict[str, Any], spec: dict[str, Any], *,
                       _ordinary_handoff: bool = False) -> dict[str, Any]:
    """Bind a file or explicitly confirmed contract as an immutable task-local source."""
    if not isinstance(receipt, dict) or receipt.get("state") != "RESOLVED" or receipt.get("owner") != "DEVCREW":
        _fail("WORKTREE_UNRESOLVED", "source binding requires a fresh resolved task receipt")
    task_id = receipt.get("task_id")
    raw_task_root = receipt.get("task_worktree_root")
    if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id) or
            not isinstance(raw_task_root, str)):
        _fail("WORKTREE_UNRESOLVED", "resolved task receipt has invalid task identity")
    ordinary_path = pathlib.Path(project) / ".codex" / "tasks" / f"{task_id}.ordinary.json"
    if ordinary_path.exists() and not _ordinary_handoff:
        _fail("ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED", "an existing ordinary task requires the explicit same-task source-gap handoff")
    task_root = pathlib.Path(raw_task_root)
    if not task_root.is_absolute() or not task_root.is_dir():
        _fail("WORKTREE_UNRESOLVED", "resolved task receipt has no accessible worktree root")
    if not isinstance(spec, dict) or not isinstance(spec.get("mode"), str):
        _fail("INVALID_INTENT_SOURCE_SPEC", "source spec must be an object with a mode")
    mode = spec["mode"]
    captured_at = None
    data = None
    provenance: dict[str, Any]
    if mode == "capture":
        _fail("INTENT_FULL_MESSAGE_FORBIDDEN", "prompt snapshots are disabled; bind a user-confirmed Intent Contract")
    elif mode in {"file", "contract"}:
        fields = ({"mode", "path", "expected_sha256", "plugin_data", "confirmation_source_id"}
                  if mode == "contract" else {"mode", "source_type", "path", "expected_sha256", "origin"})
        if set(spec) != fields:
            _fail("INVALID_INTENT_SOURCE_SPEC", "file source spec has unknown or missing fields")
        source_type = "intent_contract" if mode == "contract" else spec["source_type"]
        raw_path, expected = spec["path"], spec["expected_sha256"]
        origin = "user-confirmed structured contract" if mode == "contract" else spec["origin"]
        if (not isinstance(source_type, str) or source_type not in ({"intent_contract"} if mode == "contract" else {"attachment", "ticket", "document", "owner_answer"}) or
                not isinstance(raw_path, str) or not pathlib.Path(raw_path).is_absolute() or
                not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected) or
                not isinstance(origin, str) or not origin.strip()):
            _fail("INVALID_INTENT_SOURCE_SPEC", "file source spec has invalid field types")
        input_path = pathlib.Path(raw_path)
        chain = [input_path, *input_path.parents]
        system_aliases = {pathlib.Path("/var"): pathlib.Path("/private/var"),
                          pathlib.Path("/tmp"): pathlib.Path("/private/tmp")}
        if any(fs.is_link(item) and system_aliases.get(item) != item.resolve() for item in chain):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "input source traverses a symlink")
        if not input_path.is_file():
            _fail("INTENT_SOURCE_UNAVAILABLE", "input source is not a regular file")
        try:
            source_fd = fs.open_regular(input_path, os.O_RDONLY)
        except OSError as exc:
            _fail("UNSAFE_INTENT_SOURCE_PATH", f"cannot securely open input source: {exc}")
        with os.fdopen(source_fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                _fail("INTENT_SOURCE_UNAVAILABLE", "input source is not a regular file")
            data = stream.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            _fail("INTENT_SOURCE_TOO_LARGE", "source snapshot exceeds the 8 MiB limit")
        actual = hashlib.sha256(data).hexdigest()
        if actual != expected:
            _fail("INTENT_SOURCE_DIGEST_MISMATCH", "input source bytes do not match expected_sha256")
        provenance = {"kind": "file_snapshot", "path": str(input_path.resolve()), "origin": origin}
        if mode == "contract":
            _validate_contract_bytes(data, task_id)
            module = _prompt_receipts()
            try:
                confirmation = module.claim_prompt(spec["plugin_data"], spec["confirmation_source_id"])
            except (OSError, ValueError, TypeError) as exc:
                _fail("INTENT_CONFIRMATION_INVALID", str(exc))
            if confirmation is None:
                _fail("INTENT_CONFIRMATION_UNAVAILABLE", "confirmation receipt is missing or expired; obtain a fresh user confirmation")
            provenance = {"kind": "confirmed_intent_contract", "confirmation": confirmation}
            _validate_contract_confirmation(provenance, expected, task_id, {str(project.resolve()), str(task_root.resolve())})

        source_id = hashlib.sha256(_canonical_json({"source_type": source_type, "provenance": provenance,
                                                    "sha256": actual})).hexdigest()
    elif mode == "unavailable":
        if set(spec) != {"mode", "source_type", "origin", "reason"}:
            _fail("INVALID_INTENT_SOURCE_SPEC", "unavailable source spec has unknown or missing fields")
        source_type, origin, reason = spec["source_type"], spec["origin"], spec["reason"]
        if (not isinstance(source_type, str) or source_type not in INTENT_SOURCE_TYPES or not isinstance(origin, str) or not origin.strip() or
                not isinstance(reason, str) or not reason.strip()):
            _fail("INVALID_INTENT_SOURCE_SPEC", "unavailable source spec has invalid field types")
        provenance = {"kind": "unavailable", "origin": origin, "reason": reason}
        source_id = hashlib.sha256(_canonical_json({"source_type": source_type, "provenance": provenance,
                                                    "sha256": None})).hexdigest()
    else:
        _fail("INVALID_INTENT_SOURCE_SPEC", "unsupported source mode")
    digest = hashlib.sha256(data).hexdigest() if data is not None else None
    snapshot = None
    status = "UNVERIFIED" if data is None else "VERIFIED"
    size = None if data is None else len(data)
    if data is not None:
        snapshot_path = _source_snapshot_path(task_root, task_id, source_id)
        _create_source_snapshot(snapshot_path, data)
        snapshot = snapshot_path.relative_to(task_root.resolve()).as_posix()
    record = {"source_id": source_id, "source_type": source_type, "status": status, "sha256": digest,
              "size_bytes": size, "snapshot": snapshot, "provenance": provenance, "captured_at": captured_at}
    # Persist only selected source metadata for planner preflight/recovery. The
    # immutable bytes and confirmation remain the authority on every reread.
    selection = _source_snapshot_path(task_root, task_id, "0" * 64).with_name("planning-source.json")
    if fs.is_link(selection):
        _fail("UNSAFE_INTENT_SOURCE_PATH", "planner source selection must not be a symlink")
    if not selection.exists():
        _create_source_snapshot(selection, _canonical_json(record))
    elif data is not None:
        if not selection.is_file() or not fs.has_private_mode(selection.parent, 0o700):
            _fail("UNSAFE_INTENT_SOURCE_PATH", "planner source selection is not in private regular storage")
        _replace_json(selection, record)
    return record


def _manifest_revision(manifest: dict[str, Any]) -> int:
    revision = manifest.get("manifest_revision", 0)
    if type(revision) is not int or revision < 0:
        _fail("INVALID_MANIFEST_REVISION", "manifest_revision must be a nonnegative integer")
    return revision


def _contract_generation(manifest: dict[str, Any]) -> int:
    generation = manifest.get("contract_generation", 0)
    history = manifest.get("pre_candidate_reapprovals", [])
    if type(generation) is not int or generation < 0 or not isinstance(history, list) or len(history) != generation:
        _fail("INVALID_PRE_CANDIDATE_HISTORY", "contract generation and reapproval history are inconsistent")
    for index, item in enumerate(history, 1):
        required = {"generation", "prior_generation", "task_id", "phase_id", "base_sha", "previous_contract",
                    "planner_dispatch", "authorizing_spawn_receipt_id", "previous_verification_sha256", "archive_sha256"}
        allowed_shapes = {frozenset(required)}
        if manifest.get("schema_version") == 2:
            required.add("original_intent")
            allowed_shapes = {frozenset(required), frozenset(required | {"technical_projection"})}
        allowed_shapes |= {frozenset(shape | {"reapproval_mode"}) for shape in tuple(allowed_shapes)}
        if (not isinstance(item, dict) or frozenset(item) not in allowed_shapes or item.get("generation") != index or
                item.get("prior_generation") != index - 1 or not isinstance(item.get("task_id"), str) or
                not isinstance(item.get("phase_id"), str) or
                not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(item.get("base_sha", ""))) or
                not isinstance(item.get("previous_contract"), dict) or
                not isinstance(item.get("planner_dispatch"), dict) or
                not isinstance(item.get("authorizing_spawn_receipt_id"), str) or
                not item["authorizing_spawn_receipt_id"]):
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "reapproval history generations must be contiguous")
        if "reapproval_mode" in item and item["reapproval_mode"] != "ENVIRONMENT_ONLY":
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "reapproval mode is invalid")
        if item.get("reapproval_mode") == "ENVIRONMENT_ONLY":
            previous = item["previous_contract"]
            if (previous.get("status") not in {"EXECUTING", "VERIFYING", "READY_FOR_CLOSE"} or
                    not isinstance(previous.get("review_cycles"), list) or not previous["review_cycles"] or
                    not isinstance(previous.get("active_local_fix"), dict) or
                    any(previous.get(key) not in (None, [], {}) for key in
                        ("candidate_commit", "review_report", "review_start", "local_fix_transition",
                         "systemic_replan_transition", "accepted_sha", "acceptance_evidence"))):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "environment archive lacks a valid active local-fix snapshot")
        body = {key: value for key, value in item.items() if key != "archive_sha256"}
        expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode()).hexdigest()
        if item.get("archive_sha256") != expected:
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "reapproval history archive digest is invalid")
    if generation:
        dispatch = manifest.get("planner_dispatch")
        if (not isinstance(dispatch, dict) or dispatch.get("reapproval_authorizer") is not True or
                dispatch.get("authorization_generation") != generation - 1 or
                dispatch.get("spawn_receipt_id") != history[-1]["authorizing_spawn_receipt_id"] or
                dispatch.get("route_id") is None):
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "active planner receipt does not match the latest reapproval")
    return generation


def _verify_pre_candidate_history(project: pathlib.Path, manifest: dict[str, Any]) -> None:
    """Bind reapproval archives to authentic historical planner authority."""
    generation = _contract_generation(manifest)
    if generation == 0:
        return
    phases = manifest.get("phases")
    base = (manifest.get("worktree") or {}).get("base_sha")
    if not isinstance(phases, list) or not phases or not isinstance(base, str):
        _fail("INVALID_PRE_CANDIDATE_HISTORY", "reapproval history must remain bound to the initial phase and original base")

    seen: set[str] = set()
    history = manifest["pre_candidate_reapprovals"]
    worktree = manifest.get("worktree") or {}
    def expected_binding_for(receipt_generation: int) -> dict[str, Any]:
        binding = {"repository_id": worktree.get("repository_id"),
                   "task_id": manifest.get("task_id"),
                   "task_worktree_root": worktree.get("task_worktree_root"),
                   "branch": worktree.get("branch"), "expected_head": base}
        if receipt_generation:
            binding["contract_generation"] = receipt_generation
        return binding
    for index, entry in enumerate(history, 1):
        prior_generation = index - 1
        if (entry.get("task_id") != manifest.get("task_id") or
                entry.get("phase_id") != phases[0].get("phase_id") or entry.get("base_sha") != base):
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "reapproval archive identity differs from the initial task contract")
        previous = entry["previous_contract"]
        if (previous.get("phase_id") != entry["phase_id"] or
                previous.get("base_sha", base) != base or
                not isinstance(previous.get("verification"), list) or
                not isinstance(previous.get("verification_bindings"), list)):
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "archived phase or frozen verification contract is invalid")
        if entry.get("reapproval_mode") == "ENVIRONMENT_ONLY":
            current_phase = phases[0]
            archived_cycles = previous.get("review_cycles")
            current_cycles = current_phase.get("review_cycles")
            if (previous.get("status") not in {"EXECUTING", "VERIFYING", "READY_FOR_CLOSE"} or
                    not isinstance(archived_cycles, list) or not isinstance(current_cycles, list) or
                    len(current_cycles) < len(archived_cycles) or
                    current_cycles[:len(archived_cycles)] != archived_cycles):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "environment archive is not a prefix of preserved review-cycle history")
            archived_fix = previous.get("active_local_fix")
            cycle_id = archived_fix.get("cycle_id") if isinstance(archived_fix, dict) else None
            if (type(cycle_id) is not int or cycle_id < 1 or cycle_id > len(archived_cycles)):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "environment archive has no valid active local-fix cycle")
            fix_cycle = archived_cycles[cycle_id - 1]
            fix_candidate = fix_cycle.get("candidate_commit") if isinstance(fix_cycle, dict) else None
            fix_review = fix_cycle.get("review_report") if isinstance(fix_cycle, dict) else None
            expected_fix = {"cycle_id": cycle_id,
                            "rejected_candidate_sha": fix_candidate.get("sha") if isinstance(fix_candidate, dict) else None,
                            "findings": fix_review.get("findings") if isinstance(fix_review, dict) else None}
            if (archived_fix != expected_fix or not isinstance(fix_review, dict) or
                    not fix_review.get("findings") or
                    any(item.get("CLASSIFICATION") != "LOCAL_FIX" for item in fix_review["findings"]
                        if isinstance(item, dict)) or
                    len([item for item in fix_review["findings"] if isinstance(item, dict)]) !=
                    len(fix_review["findings"])):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "environment archive active repair is not bound to its failed local-fix cycle")

            def contract_projection(phase_contract):
                if not isinstance(phase_contract, dict):
                    return None
                return {key: json.loads(json.dumps(phase_contract[key]))
                        for key in ({"phase_id", "depends_on"} | _REAPPROVAL_FIELDS)
                        if key in phase_contract}

            expected_contract = contract_projection(previous)
            for cycle in current_cycles[len(archived_cycles):]:
                completion = cycle.get("systemic_replan_completion") if isinstance(cycle, dict) else None
                if completion is None:
                    continue
                failed_contract = completion.get("failed_contract")
                replacement_contract = completion.get("replacement_contract")
                replacement_phases = replacement_contract.get("phases") if isinstance(replacement_contract, dict) else None
                replacement_phase = next((item for item in replacement_phases
                                          if isinstance(item, dict) and item.get("phase_id") == entry["phase_id"]), None) \
                    if isinstance(replacement_phases, list) else None
                if (contract_projection(failed_contract) != expected_contract or
                        replacement_phase is None):
                    _fail("INVALID_PRE_CANDIDATE_HISTORY", "authorized systemic history does not continue the archived technical contract")
                expected_contract = contract_projection(replacement_phase)
            if expected_contract != contract_projection(current_phase):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "environment archive technical contract changed without an authorized lifecycle record")
        try:
            normalized = verification_bindings(previous["verification"], previous["verification_bindings"])
            if previous.get("verification_sha256") is not None:
                required_verification(previous)
            elif (previous.get("verification_environment_sha256") is not None or
                  previous.get("verification_approved_at") is not None):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "unapproved archived verification has partial approval metadata")
        except (PhaseError, KeyError, TypeError) as exc:
            _fail("INVALID_PRE_CANDIDATE_HISTORY", f"archived verification contract is invalid: {exc}")
        if ((previous.get("verification_sha256") is not None and
             normalized != previous["verification_bindings"]) or
                previous.get("verification_sha256") != entry.get("previous_verification_sha256")):
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "archived frozen verification contract does not match its saved digest")
        if manifest.get("schema_version") == 2:
            archived_intent = entry.get("original_intent")
            if not isinstance(archived_intent, dict):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "schema v2 archive must retain the complete original_intent")
            historical = json.loads(json.dumps(manifest))
            historical["original_intent"] = archived_intent
            current_projection = _intent_technical_projection(manifest)
            saved_projection = entry.get("technical_projection")
            if saved_projection is None:
                # Old v2 archives may be reconstructed only when their historical
                # digest proves the current later-phase contracts are unchanged.
                historical_projection = json.loads(json.dumps(current_projection))
                historical_projection["phases"][0] = _intent_phase_technical_projection(previous)
            else:
                if (not isinstance(saved_projection, dict) or
                        set(saved_projection) != {"task_mode", "semantic_contract", "phases"} or
                        saved_projection.get("task_mode") != manifest.get("task_mode") or
                        saved_projection.get("semantic_contract") != manifest.get("semantic_contract") or
                        not isinstance(saved_projection.get("phases"), list) or
                        len(saved_projection["phases"]) != len(phases)):
                    _fail("INVALID_PRE_CANDIDATE_HISTORY", "saved technical projection has invalid task or phase shape")
                historical_projection = json.loads(json.dumps(saved_projection))
                allowed_phase_fields = {"phase_id", "depends_on", *_REAPPROVAL_FIELDS}
                for current_phase, archived_phase in zip(phases, historical_projection["phases"]):
                    if (not isinstance(archived_phase, dict) or
                            not {"phase_id", "depends_on"} <= set(archived_phase) or
                            set(archived_phase) - allowed_phase_fields or
                            archived_phase.get("phase_id") != current_phase.get("phase_id") or
                            archived_phase.get("depends_on") != current_phase.get("depends_on")):
                        _fail("INVALID_PRE_CANDIDATE_HISTORY", "saved technical projection phase identity or shape is invalid")
                if historical_projection["phases"][0] != _intent_phase_technical_projection(previous):
                    _fail("INVALID_PRE_CANDIDATE_HISTORY", "saved P1 technical projection differs from previous_contract")
            historical["task_mode"] = historical_projection["task_mode"]
            historical["semantic_contract"] = historical_projection["semantic_contract"]
            historical["phases"] = historical_projection["phases"]
            try:
                _validate_original_intent(historical, pathlib.Path(worktree["task_worktree_root"]),
                                           {item["phase_id"]: item for item in historical["phases"]})
            except (PhaseError, KeyError, TypeError) as exc:
                _fail("INVALID_PRE_CANDIDATE_HISTORY", f"archived intent contract is invalid: {exc}")
            later_intent = history[index].get("original_intent") if index < len(history) else manifest.get("original_intent")
            archived_sources = archived_intent.get("sources")
            later_sources = later_intent.get("sources") if isinstance(later_intent, dict) else None
            archived_requirements = archived_intent.get("requirements")
            later_requirements = later_intent.get("requirements") if isinstance(later_intent, dict) else None
            archived_decisions = archived_intent.get("decisions")
            later_decisions = later_intent.get("decisions") if isinstance(later_intent, dict) else None
            requirements_preserved = (
                isinstance(archived_requirements, list) and isinstance(later_requirements, list) and
                len(later_requirements) >= len(archived_requirements) and
                all(isinstance(before, dict) and isinstance(after, dict) and
                    all(after.get(field) == before.get(field) for field in
                        ("requirement_id", "kind", "statement", "source_fragments")) and
                    isinstance(before.get("evidence_records"), list) and
                    isinstance(after.get("evidence_records"), list) and
                    after["evidence_records"][:len(before["evidence_records"])] == before["evidence_records"]
                    for before, after in zip(archived_requirements, later_requirements)))
            if (not isinstance(later_intent, dict) or
                    not isinstance(archived_sources, list) or not isinstance(later_sources, list) or
                    later_sources[:len(archived_sources)] != archived_sources or
                    not requirements_preserved or
                    not isinstance(archived_decisions, list) or not isinstance(later_decisions, list) or
                    later_decisions[:len(archived_decisions)] != archived_decisions):
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "archived intent identity chain is broken")

        prior_dispatch = entry["planner_dispatch"]
        prior_id = prior_dispatch.get("spawn_receipt_id")
        if not isinstance(prior_id, str) or not prior_id:
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "archived planner authority is missing")
        if index > 1 and prior_id != history[index - 2].get("authorizing_spawn_receipt_id"):
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "historical planner authority chain is broken")
        try:
            devcrew_dispatch.verify_historical_planner(
                project, prior_id, task_id=manifest["task_id"], base_sha=base,
                generation=max(0, prior_generation - 1), planner_dispatch=prior_dispatch,
                expected_worktree_binding=expected_binding_for(max(0, prior_generation - 1)),
                store_path=devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
        except devcrew_dispatch.DispatchError as exc:
            _fail("INVALID_PRE_CANDIDATE_HISTORY", str(exc))
        authorizer_id = entry["authorizing_spawn_receipt_id"]
        if not isinstance(authorizer_id, str) or not authorizer_id or authorizer_id in seen:
            _fail("INVALID_PRE_CANDIDATE_HISTORY", "reapproval authorizer is missing or reused")
        try:
            devcrew_dispatch.verify_historical_planner(
                project, authorizer_id, task_id=manifest["task_id"], base_sha=base,
                generation=prior_generation,
                expected_worktree_binding=expected_binding_for(prior_generation),
                store_path=devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
        except devcrew_dispatch.DispatchError as exc:
            _fail("INVALID_PRE_CANDIDATE_HISTORY", str(exc))
        seen.add(authorizer_id)

    active = manifest.get("planner_dispatch")
    latest = history[-1].get("authorizing_spawn_receipt_id")
    if not isinstance(active, dict) or active.get("spawn_receipt_id") != latest:
        _fail("INVALID_PRE_CANDIDATE_HISTORY", "active planner is not the latest pinned historical authorizer")
    try:
        devcrew_dispatch.verify_historical_planner(
            project, latest, task_id=manifest["task_id"], base_sha=base,
            generation=generation - 1, planner_dispatch=active,
            expected_worktree_binding=expected_binding_for(generation - 1),
            store_path=devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
    except devcrew_dispatch.DispatchError as exc:
        _fail("INVALID_PRE_CANDIDATE_HISTORY", str(exc))


def _require_no_candidate_transition(manifest, *, path=None):
    """Legacy recovery state and pending candidates require manual reconciliation."""
    _manifest_revision(manifest)
    def has_recovery(value):
        if isinstance(value, dict):
            return ("candidate_transition" in value or "recovery_transition" in value or
                    any(has_recovery(child) for child in value.values()))
        return isinstance(value, list) and any(has_recovery(child) for child in value)

    if has_recovery(manifest):
        marker = manifest.get("candidate_transition")
        marker = marker if isinstance(marker, dict) else {}
        candidate = marker.get("candidate_commit") or marker
        sha = candidate.get("sha", "unknown (not saved)") if isinstance(candidate, dict) else "unknown"
        _fail("CANDIDATE_RECONCILIATION_REQUIRED",
              f"unfinished candidate or legacy recovery state; manifest={path or 'loaded snapshot'}, "
              f"phase={marker.get('phase_id', manifest.get('current_phase_id'))}, saved_sha={sha}. "
              "Preserve Git, receipt, manifest and evidence for manual reconciliation. "
              "Compare the task branch/HEAD, receipt expected_head, phase base/candidate and saved SHA "
              "with git rev-parse HEAD and git show -s --format='%H %P %T %B' SAVED_SHA when known. "
              "Do not retry candidate creation, guess a SHA, reset Git, or delete recovery state.")


def _candidate_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode()).hexdigest()


_held_manifest_locks = ContextVar("held_manifest_locks", default=frozenset())


@contextmanager
def _manifest_lock(path: pathlib.Path):
    # Lock a stable sidecar inode: the manifest itself is replaced on every save.
    key = (os.getpid(), str(path.resolve()))
    held = _held_manifest_locks.get()
    if key in held:
        yield
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(fs.open_regular(path.with_name(path.name + ".lock"), os.O_CREAT | os.O_RDWR), "r+b") as lock:
        try:
            fs.lock(lock.fileno())
        except (ImportError, OSError) as exc:
            _fail("PHASE_MANIFEST_LOCK_UNSUPPORTED", f"filesystem lock unavailable: {exc}")
        token = _held_manifest_locks.set(held | {key})
        try:
            yield
        finally:
            _held_manifest_locks.reset(token)
            fs.unlock(lock.fileno())


def _systemic_transitions(manifest):
    return {phase.get("phase_id"): phase.get("systemic_replan_transition")
            for phase in manifest.get("phases", []) if isinstance(phase, dict) and
            phase.get("systemic_replan_transition") is not None}


def _validate_systemic_replacement(old, proposed, authorization):
    """Rebuild the only authorized v2 replacement from the persisted old intent."""
    transitions = _systemic_transitions(old)
    transition = next(iter(transitions.values()), None)
    completion = (transition.get("cycle", {}).get("systemic_replan_completion")
                  if isinstance(transition, dict) else None)
    if not isinstance(completion, dict):
        _fail("INVALID_SYSTEMIC_REPLAN", "replacement handoff lacks completion authority")
    old_intent = completion.get("original_intent")
    stored_auth = completion.get("intent_assessment")
    replacement = completion.get("replacement_contract")
    if (old_intent != old.get("original_intent") or stored_auth != authorization or
            not isinstance(replacement, dict)):
        _fail("INVALID_SYSTEMIC_REPLAN", "replacement handoff differs from stored old intent or host input")
    if proposed.get("original_intent") != replacement.get("original_intent"):
        _fail("INVALID_SYSTEMIC_REPLAN", "installed intent must exactly match the authorized replacement")
    reconstructed = json.loads(json.dumps(replacement))
    # A historical replacement already carries the result of the host merge.
    # Reconstruct from its planner form retained by resetting only the host-owned
    # assessment/decision overlay to the prior accepted state.
    intent = reconstructed.get("original_intent", {})
    prior = old_intent
    if not isinstance(prior, dict):
        _fail("INVALID_SYSTEMIC_REPLAN", "old intent history is unavailable")
    intent["decisions"] = json.loads(json.dumps(prior.get("decisions", [])))
    intent["planning_assessment"] = json.loads(json.dumps(prior.get("planning_assessment")))
    old_projection = json.loads(json.dumps(old))
    old_projection.pop("systemic_replan_transition", None)
    for phase in old_projection.get("phases", []):
        phase.pop("systemic_replan_transition", None)
    _install_intent_assessment(reconstructed, authorization, old=old_projection)
    if reconstructed.get("original_intent") != replacement.get("original_intent"):
        _fail("INVALID_SYSTEMIC_REPLAN", "replacement does not equal the validated host merge")
    new_projection = json.loads(json.dumps(replacement))
    new_projection.pop("systemic_replan_transition", None)
    for phase in new_projection.get("phases", []):
        phase.pop("systemic_replan_transition", None)
    _validate_intent_transition(old_projection, new_projection, authorization=authorization)


def _validate_intent_transition(old: dict[str, Any], proposed: dict[str, Any], *, authorization=None,
                                _systemic_replacement=None, _task_acceptance_report=None) -> None:
    """Protect v2 intent and plan contracts at every generic atomic write."""
    old_v2, new_v2 = old.get("schema_version") == 2, proposed.get("schema_version") == 2
    if old_v2 != new_v2:
        _fail("INTENT_SCHEMA_MIGRATION_UNAVAILABLE", "generic writes cannot migrate phase manifest schema")
    if not old_v2:
        return
    if old.get("systemic_replan_transition") != proposed.get("systemic_replan_transition"):
        _fail("INTENT_AUTHORIZATION_REQUIRED", "systemic transition payload must be phase-owned")
    old_transitions, new_transitions = _systemic_transitions(old), _systemic_transitions(proposed)
    if old_transitions != new_transitions:
        if _systemic_replacement is None:
            _fail("INTENT_AUTHORIZATION_REQUIRED", "systemic replacement transitions require the internal validated handoff")
        changed = {key for key in set(old_transitions) | set(new_transitions)
                   if old_transitions.get(key) != new_transitions.get(key)}
        if len(changed) != 1 or authorization is None:
            _fail("INVALID_SYSTEMIC_REPLAN", "handoff must change exactly one phase transition with host authorization")
        phase_id = next(iter(changed))
        before, after = old_transitions.get(phase_id), new_transitions.get(phase_id)
        expected = before if before is not None else after
        if (not isinstance(expected, dict) or
                expected.get("replacement_contract") != _systemic_replacement):
            _fail("INVALID_SYSTEMIC_REPLAN", "phase transition replacement differs from internal handoff")
        if before is None:
            if old.get("original_intent") != proposed.get("original_intent"):
                _fail("INVALID_SYSTEMIC_REPLAN", "pending transition must preserve the outer old intent")
            completion = expected.get("cycle", {}).get("systemic_replan_completion", {})
            if (completion.get("replacement_contract") != _systemic_replacement or
                    completion.get("intent_assessment") != authorization or
                    completion.get("original_intent") != old.get("original_intent")):
                _fail("INVALID_SYSTEMIC_REPLAN", "pending completion differs from exact replacement")
            _validate_intent_transition(old, _systemic_replacement, authorization=authorization)
        else:
            _validate_systemic_replacement(old, proposed, authorization)
            archived = proposed.get("phases", [])
            phase = next((row for row in archived if row.get("phase_id") == phase_id), {})
            history = phase.get("review_cycles", [])
            if not history or history[-1] != before.get("cycle"):
                _fail("INVALID_SYSTEMIC_REPLAN", "completion must archive the exact pending cycle")
        if old.get("original_intent") == proposed.get("original_intent"):
            # The host input authorizes the replacement, not the still-pending
            # outer manifest.  Its old intent was checked separately above.
            authorization = None
    old_intent, new_intent = old.get("original_intent"), proposed.get("original_intent")
    if not isinstance(old_intent, dict) or not isinstance(new_intent, dict):
        _fail("INTENT_IMMUTABILITY_VIOLATION", "saved v2 manifest must retain original_intent")
    for field in ("sources",):
        previous, current = old_intent.get(field), new_intent.get(field)
        if not isinstance(previous, list) or not isinstance(current, list) or current[:len(previous)] != previous:
            _fail("INTENT_IMMUTABILITY_VIOLATION", f"existing original_intent {field} cannot be rewritten or removed")
    previous_rows, current_rows = old_intent.get("requirements"), new_intent.get("requirements")
    if not isinstance(previous_rows, list) or not isinstance(current_rows, list) or len(current_rows) < len(previous_rows):
        _fail("INTENT_IMMUTABILITY_VIOLATION", "existing requirements cannot be removed")
    for before, after in zip(previous_rows, current_rows):
        if not isinstance(before, dict) or not isinstance(after, dict):
            _fail("INTENT_IMMUTABILITY_VIOLATION", "requirement rows must remain objects")
        for key in ("requirement_id", "kind", "statement", "source_fragments"):
            if before.get(key) != after.get(key):
                _fail("INTENT_IMMUTABILITY_VIOLATION", f"original requirement {key} cannot be rewritten")
        old_refs, new_refs = before.get("evidence_records"), after.get("evidence_records")
        if (not isinstance(old_refs, list) or not isinstance(new_refs, list) or
                new_refs[:len(old_refs)] != old_refs or len(new_refs) != len(set(new_refs))):
            _fail("INTENT_IMMUTABILITY_VIOLATION", "evidence references may only be appended without duplicates")
    old_decisions, new_decisions = old_intent.get("decisions"), new_intent.get("decisions")
    if not isinstance(old_decisions, list) or not isinstance(new_decisions, list) or new_decisions[:len(old_decisions)] != old_decisions:
        _fail("INTENT_IMMUTABILITY_VIOLATION", "existing decisions cannot be rewritten or removed")
    old_acceptance = old_intent.get("task_acceptance")
    new_acceptance = new_intent.get("task_acceptance")
    pending_acceptance = {"status": "NOT_ASSESSED", "review": None}
    if old_acceptance != new_acceptance:
        if (old_acceptance != pending_acceptance or not isinstance(new_acceptance, dict) or
                new_acceptance.get("status") != "ACCEPTED" or
                _task_acceptance_report != new_acceptance.get("review", {}).get("report_sha256")):
            _fail("INTENT_CERTIFICATION_UNAVAILABLE", "only final review acceptance may install task certification")
        final_id = proposed.get("phases", [{}])[-1].get("phase_id")
        final_phase = next((row for row in proposed.get("phases", []) if row.get("phase_id") == final_id), {})
        review = new_acceptance.get("review", {})
        report = final_phase.get("review_report")
        if (new_acceptance.get("review") != {"phase_id": final_id, "report_sha256": _task_acceptance_report} or
                final_phase.get("status") != "ACCEPTED" or not isinstance(report, dict) or
                report.get("sha256") != _task_acceptance_report or report.get("verdict") != "PASS" or
                report.get("authoritative") is not True):
            _fail("INTENT_CERTIFICATION_UNAVAILABLE", "task certification must bind the accepted final phase report")
    elif old_acceptance != pending_acceptance and _task_acceptance_report is not None:
        _fail("INTENT_CERTIFICATION_UNAVAILABLE", "accepted task certification is immutable")
    old_assessment = old_intent.get("planning_assessment")
    new_assessment = new_intent.get("planning_assessment")
    try:
        changed_digest = intent_plan_digest(old) != intent_plan_digest(proposed)
    except (KeyError, TypeError, AttributeError):
        _fail("INVALID_ORIGINAL_INTENT", "v2 transition projection is malformed")
    decisions_added = new_decisions[len(old_decisions):]
    if authorization is not None:
        proposed_assessment = new_assessment.get("assessment") if isinstance(new_assessment, dict) else None
        if (not isinstance(authorization, dict) or set(authorization) != {"assessment", "decisions"} or
                authorization.get("assessment") != proposed_assessment or
                authorization.get("decisions") != decisions_added or not isinstance(authorization.get("decisions"), list)):
            auth_assessment = authorization.get("assessment") if isinstance(authorization, dict) else None
            assessment_diffs = ([key for key in set(auth_assessment or {}) | set(proposed_assessment or {})
                                 if (auth_assessment or {}).get(key) != (proposed_assessment or {}).get(key)]
                                if isinstance(auth_assessment, dict) and isinstance(proposed_assessment, dict) else [])
            _fail("INVALID_INTENT_AUTHORIZATION", "explicit host authorization does not match proposed assessment and decisions "
                  f"(assessment={isinstance(authorization, dict) and authorization.get('assessment') == proposed_assessment}, "
                  f"decisions={isinstance(authorization, dict) and authorization.get('decisions') == decisions_added}, "
                  f"assessment_fields={assessment_diffs}, "
                  f"authorized_plan={authorization.get('assessment', {}).get('plan_sha256') if isinstance(authorization, dict) else None}, "
                  f"proposed_plan={proposed_assessment.get('plan_sha256') if isinstance(proposed_assessment, dict) else None})")
        if (not isinstance(proposed_assessment, dict) or
                proposed_assessment.get("task_id") != proposed.get("task_id") or
                proposed_assessment.get("plan_sha256") != intent_plan_digest(proposed) or
                new_assessment.get("status") != "ASSESSED"):
            _fail("INVALID_INTENT_AUTHORIZATION", "host authorization must carry a fresh assessment for the proposed plan")
    if old_assessment != new_assessment and authorization is None:
        _fail("INTENT_AUTHORIZATION_REQUIRED", "planning assessment transition requires explicit host authorization")
    if changed_digest and authorization is None:
        _fail("INTENT_AUTHORIZATION_REQUIRED", "original intent or plan changes require explicit host authorization")
    # Systemic transition authorization is validated above against the complete
    # phase-owned payload, including nested edits.


def _install_intent_assessment(proposed: dict[str, Any], authorization, *, old=None) -> None:
    """Install the coordinator-owned assessment and addressed decisions into a v2 plan."""
    if proposed.get("schema_version") != 2:
        if authorization is not None:
            _fail("INVALID_INTENT_ASSESSMENT", "intent assessment input is supported only for schema v2")
        return
    if authorization is None:
        _fail("INTENT_PLANNING_ASSESSMENT_REQUIRED", "schema v2 acceptance requires a separate host assessment")
    if not isinstance(authorization, dict) or set(authorization) != {"assessment", "decisions"} or not isinstance(authorization.get("decisions"), list):
        _fail("INVALID_INTENT_AUTHORIZATION", "host input must contain assessment and decisions")
    intent = proposed.get("original_intent")
    if not isinstance(intent, dict):
        _fail("INVALID_ORIGINAL_INTENT", "schema v2 plan requires original_intent")
    planner_state = intent.get("planning_assessment")
    if old is None:
        if (planner_state != {"status": "NOT_ASSESSED", "assessment": None} or
                intent.get("decisions") != []):
            _fail("INVALID_INTENT_ASSESSMENT", "initial planner output must have pending assessment and no decisions")
        history = []
        previous_digest = None
        old_decisions = []
    else:
        old_intent = old.get("original_intent", {})
        old_state = old_intent.get("planning_assessment")
        if (planner_state not in ({"status": "NOT_ASSESSED", "assessment": None}, old_state) or
                intent.get("decisions") != old_intent.get("decisions")):
            _fail("INVALID_INTENT_ASSESSMENT", "replacement planner may not certify intent or add decisions")
        history = list(old_state.get("history", []))
        if old_state.get("status") == "ASSESSED":
            history.append(json.loads(json.dumps(old_state["assessment"])))
        previous_digest = intent_plan_digest(old)
        old_decisions = old_intent.get("decisions", [])
    intent["decisions"] = [*old_decisions, *json.loads(json.dumps(authorization["decisions"]))]
    intent["planning_assessment"] = {"status": "ASSESSED",
                                     "assessment": json.loads(json.dumps(authorization["assessment"])),
                                     "history": history}
    assessment = intent["planning_assessment"]["assessment"]
    if not isinstance(assessment, dict):
        _fail("INVALID_INTENT_ASSESSMENT", "planning assessment must be an object")
    if assessment.get("result") != "COMPLETE":
        _fail("INVALID_INTENT_ASSESSMENT", "only a complete planning assessment can authorize an accepted intent plan")
    if assessment.get("previous_plan_sha256") != previous_digest:
        _fail("INVALID_INTENT_ASSESSMENT", "assessment does not link to the previous accepted plan")
    if old is not None:
        _validate_intent_transition(old, proposed, authorization=authorization)


def _atomic_json(path: pathlib.Path, value: dict[str, Any], *, before_save=None, authorization=None,
                 _systemic_replacement=None, _task_acceptance_report=None) -> None:
    """Commit a validated snapshot only if its revision is still current.

    Validation/check execution may run optimistically outside this short lock:
    matching the revision binds those checks to the unchanged persisted state.
    Git/receipt mutations coupled to this save run in before_save after the CAS.
    """
    expected = _manifest_revision(value)
    with _manifest_lock(path):
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            current = {}
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("PHASE_MANIFEST_REQUIRED", f"cannot update phase manifest: {exc}")
        if not isinstance(current, dict):
            _fail("INVALID_MANIFEST", "manifest must be an object")
        _require_no_candidate_transition(current, path=path)
        actual = _manifest_revision(current)
        if actual != expected:
            _fail("PHASE_MANIFEST_CONFLICT", "phase manifest changed; reload it before retrying the operation")
        if not current and value.get("schema_version") == 2:
            _fail("INTENT_TRANSITION_UNAVAILABLE", "generic atomic writes cannot create a v2 phase manifest")
        _validate_intent_transition(current, value, authorization=authorization,
                                    _systemic_replacement=_systemic_replacement,
                                    _task_acceptance_report=_task_acceptance_report)
        state_path = path.with_name(path.name.removesuffix(".phases.json") + ".json")
        if state_path.exists():
            record = json.loads(state_path.read_text(encoding="utf-8"))
            devcrew_worktree._require_consistent_state(state_path, record)
        if before_save is not None:
            before_save()
        stored = dict(value, manifest_revision=expected + 1)
        _replace_json(path, stored)
        value["manifest_revision"] = stored["manifest_revision"]


def _replace_json(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        fs.publish(temp_name, path, replace=True)
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)


def _atomic_create_json(path: pathlib.Path, value: Any) -> None:
    """Atomically create a manifest without replacing existing task state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n"); stream.flush(); os.fsync(stream.fileno())
        try:
            fs.publish(temp_name, path, replace=False)
        except FileExistsError:
            _fail("PHASE_MANIFEST_ALREADY_EXISTS", "refusing to overwrite an existing phase manifest")
    finally:
        if os.path.exists(temp_name): os.unlink(temp_name)


def save_manifest(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any], *, authorization=None) -> dict[str, Any]:
    validated = validate_manifest(manifest, receipt)
    def check_verification():
        if path.exists():
            saved = json.loads(path.read_text(encoding="utf-8"))
            for old, new in zip(saved["phases"], validated["phases"]):
                if "verification_sha256" in old and _verification_contract(old) != _verification_contract(new):
                    _fail("VERIFICATION_PLAN_CHANGED", "save cannot replace accepted checks; use explicit replanning")
    # Store only stable linkage fields; lifecycle receipt nonce is deliberately ephemeral.
    stored = dict(validated)
    _atomic_json(path, stored, before_save=check_verification, authorization=authorization)
    manifest["manifest_revision"] = stored["manifest_revision"]
    return stored


def initialize_manifest(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                        project: pathlib.Path, spawn_receipt_id: str, *, intent_assessment=None) -> dict[str, Any]:
    manifest = json.loads(json.dumps(manifest))
    _require_no_candidate_transition(manifest)
    planner_lifecycle_fields = {"candidate_commit", "review_report", "review_cycles", "active_local_fix",
                                "local_fix_transition", "accepted_sha", "evidence"}
    if any(not isinstance(phase, dict) or planner_lifecycle_fields.intersection(phase)
           for phase in manifest.get("phases", [])):
        _fail("INVALID_INITIAL_PHASE_STATE", "planner output cannot provide host-owned phase lifecycle state")
    for index, phase in enumerate(manifest.get("phases", [])):
        phase["base_sha"] = manifest.get("worktree", {}).get("base_sha") if index == 0 else None
    validated = validate_manifest(manifest, receipt, pathlib.Path(receipt["task_worktree_root"]))
    for phase in validated["phases"]:
        pin_verification(phase)
    _install_intent_assessment(validated, intent_assessment)
    validated = validate_manifest(validated, receipt, pathlib.Path(receipt["task_worktree_root"]))
    if any(phase["status"] != "PLANNED" or "evidence" in phase for phase in validated["phases"]):
        _fail("INVALID_INITIAL_PHASE_STATE", "planner output must contain only PLANNED phases without evidence")
    spawn = _verify_dispatch(project, spawn_receipt_id, task_id=validated["task_id"], role="planner",
                             base_sha=validated["worktree"]["base_sha"])
    validated["planner_dispatch"] = {"spawn_receipt_id": spawn_receipt_id, "route_id": spawn["ROUTE_ID"],
                                     "agent_id": spawn["AGENT_ID"], "model": spawn["RESOLVED_MODEL"],
                                     "effort": spawn["REASONING_EFFORT"], "spawn_mode": spawn["SPAWN_MODE"]}
    validated["manifest_revision"] = 1
    import devcrew_ordinary
    namespace_lock = devcrew_ordinary._namespace_lock_path(project, validated["task_id"])
    # Lock order is shared task namespace, then manifest. Ordinary init uses
    # only the namespace lock; source handoff uses only its local task lock.
    with devcrew_ordinary._task_lock(namespace_lock):
        expected_ordinary = pathlib.Path(project) / ".codex" / "tasks" / f"{validated['task_id']}.ordinary.json"
        for ordinary_state in devcrew_ordinary._linked_ordinary_states(project, validated["task_id"]):
            if ((ordinary_state.exists() or fs.is_link(ordinary_state)) and
                    ordinary_state.resolve() != expected_ordinary.resolve()):
                _fail("ORDINARY_TASK_OWNERSHIP_CONFLICT",
                      "same task ID has ordinary state in another linked checkout")
        # Recheck ownership/source after acquiring the stable Git-common lock:
        # an ordinary init may have completed while validation was underway.
        _verify_planner_source_binding(project, validated, spawn)
        with _manifest_lock(path):
            validated = validate_manifest(validated, receipt, pathlib.Path(receipt["task_worktree_root"]))
            _atomic_create_json(path, validated)
    return validated


_REAPPROVAL_FIELDS = {"goal", "main_scenario", "owned_scope", "acceptance", "verification",
                      "verification_bindings", "commit_boundary", "commit_subject",
                      "known_contracts", "do_not_change"}
_REAPPROVAL_HOST_FIELDS = {"verification_sha256", "verification_environment_sha256",
                           "verification_approved_at", "planner_dispatch", "executor_dispatch",
                           "review_dispatch", "contract_generation",
                           "pre_candidate_reapprovals", "manifest_revision",
                           "candidate_commit", "review_report", "review_start", "review_cycles",
                           "active_local_fix", "local_fix_transition", "systemic_replan_transition",
                           "accepted_sha", "acceptance_evidence", "evidence"}


def _git_text(root: pathlib.Path, *args: str) -> str:
    environment = dict(os.environ)
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8", errors="surrogateescape", env=environment)
    if result.returncode:
        _fail("PRE_CANDIDATE_REAPPROVAL_GIT_ERROR", (result.stderr or result.stdout).strip())
    return result.stdout


def _pre_candidate_snapshot(task_root: pathlib.Path) -> dict[str, Any]:
    status = _git_text(task_root, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    untracked = _git_text(task_root, "ls-files", "--others", "--exclude-standard", "-z").split("\0")
    contents = {}
    for name in sorted(item for item in untracked if item):
        path = task_root / name
        if fs.is_link(path) or not path.is_file():
            _fail("PRE_CANDIDATE_REAPPROVAL_DIRTY_STATE", "untracked state contains a symlink or non-file")
        contents[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    index = _git_text(task_root, "rev-parse", "--git-path", "index").strip()
    index_path = pathlib.Path(index)
    if not index_path.is_absolute():
        index_path = task_root / index_path
    return {"status": status,
            "index": hashlib.sha256(index_path.read_bytes()).hexdigest(),
            "diff": hashlib.sha256(_git_text(task_root, "diff", "--binary", "HEAD").encode()).hexdigest(),
            "untracked": contents}


def _pre_candidate_eligible(manifest: dict[str, Any], receipt: dict[str, Any], project: pathlib.Path,
                            phase_id: str, *, environment_only: bool = False) -> tuple[dict[str, Any], pathlib.Path]:
    generation = _contract_generation(manifest)
    phases = manifest.get("phases")
    if (not isinstance(phases, list) or not phases or manifest.get("current_phase_id") != phases[0].get("phase_id") or
            phase_id != manifest.get("current_phase_id")):
        _fail("PRE_CANDIDATE_REAPPROVAL_NOT_ELIGIBLE", "only the first current phase may be reapproved")
    phase = phases[0]
    allowed_statuses = ({"EXECUTING", "VERIFYING", "READY_FOR_CLOSE"} if environment_only else
                        {"PLANNED", "EXECUTING", "VERIFYING"})
    if phase.get("status") not in allowed_statuses:
        _fail("PRE_CANDIDATE_REAPPROVAL_NOT_ELIGIBLE", "current phase status is beyond pre-candidate reapproval")
    lifecycle = {"candidate_commit", "review_report", "review_start", "review_cycles", "active_local_fix",
                 "local_fix_transition", "systemic_replan_transition", "accepted_sha", "acceptance_evidence"}
    def has_history(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in lifecycle and child not in (None, [], {}, ""):
                    return True
                if has_history(child):
                    return True
        elif isinstance(value, list):
            return any(has_history(child) for child in value)
        return False
    if environment_only:
        if (not isinstance(phase.get("review_cycles"), list) or not phase["review_cycles"] or
                not isinstance(phase.get("active_local_fix"), dict)):
            _fail("PRE_CANDIDATE_REAPPROVAL_NOT_ELIGIBLE", "environment-only reapproval requires active archived local-fix history")
        if any(phase.get(key) not in (None, [], {}) for key in
               ("candidate_commit", "review_report", "review_start", "local_fix_transition",
                "systemic_replan_transition", "accepted_sha", "acceptance_evidence")):
            _fail("PRE_CANDIDATE_REAPPROVAL_NOT_ELIGIBLE", "a current candidate, report, acceptance, or transition exists")
    elif has_history(manifest):
        _fail("PRE_CANDIDATE_REAPPROVAL_NOT_ELIGIBLE", "candidate, review, repair, or acceptance history exists")
    if (receipt.get("expected_head") != manifest.get("worktree", {}).get("base_sha") or
            receipt.get("branch") != manifest.get("worktree", {}).get("branch")):
        _fail("PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH", "task checkout is not at the original branch and base")
    root = pathlib.Path(receipt["task_worktree_root"])
    if (_git_text(root, "rev-parse", "HEAD").strip() != receipt["expected_head"] or
            _git_text(root, "symbolic-ref", "--short", "HEAD").strip() != receipt["branch"]):
        _fail("PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH", "task branch or HEAD differs from original base")
    return phase, root


def _verify_environment_reapproval_writer(project: pathlib.Path, manifest: dict[str, Any],
                                          phase: dict[str, Any]) -> None:
    dispatch = phase.get("executor_dispatch")
    if phase.get("status") == "READY_FOR_CLOSE" and not isinstance(dispatch, dict):
        _fail("INVALID_EXECUTOR_ROUTE", "ready phase must retain its current writer authority")
    if dispatch is None:
        return
    active_fix = phase.get("active_local_fix")
    if not isinstance(active_fix, dict):
        _fail("INVALID_EXECUTOR_ROUTE", "environment archive writer must belong to the active local fix")
    spawn = _verify_dispatch(project, dispatch.get("spawn_receipt_id"), task_id=manifest["task_id"],
        role="executor", phase_id=phase["phase_id"],
        base_sha=phase.get("base_sha", manifest["worktree"]["base_sha"]),
        writer_cycle=active_fix["cycle_id"], rejected_candidate_sha=active_fix["rejected_candidate_sha"])
    expected = {"spawn_receipt_id": spawn["SPAWN_RECEIPT_ID"], "writer_cycle": active_fix["cycle_id"],
        "rejected_candidate_sha": active_fix["rejected_candidate_sha"], "route_id": spawn["ROUTE_ID"],
        "agent_id": spawn["AGENT_ID"], "model": spawn["RESOLVED_MODEL"],
        "effort": spawn["REASONING_EFFORT"], "spawn_mode": spawn["SPAWN_MODE"]}
    if dispatch != expected:
        _fail("INVALID_EXECUTOR_ROUTE", "current writer authority metadata does not match its routed receipt")


def reapprove_pre_candidate(path: pathlib.Path, replacement: dict[str, Any], receipt: dict[str, Any],
                            project: pathlib.Path, phase_id: str, spawn_receipt_id: str, *,
                            intent_assessment=None, environment_only: bool = False,
                            approved_environment_sha256: str | None = None, verifier=None) -> dict[str, Any]:
    """Atomically replace only the initial pre-candidate phase contract."""
    try:
        old = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("PHASE_MANIFEST_REQUIRED", f"cannot load existing phase manifest: {exc}")
    old = validate_manifest(old, receipt, pathlib.Path(receipt["task_worktree_root"]))
    verify_planner_dispatch(project, old)
    if environment_only:
        if intent_assessment is not None:
            _fail("INVALID_INTENT_ASSESSMENT", "environment-only reapproval cannot replace or recertify intent assessment")
        if (not isinstance(approved_environment_sha256, str) or
                not re.fullmatch(r"[0-9a-f]{64}", approved_environment_sha256) or
                approved_environment_sha256 != environment_digest()):
            _fail("INVALID_ENVIRONMENT_APPROVAL", "explicit environment approval must match the current process environment")
        _require_no_candidate_transition(old, path=path)
    elif approved_environment_sha256 is not None:
        _fail("INVALID_ENVIRONMENT_APPROVAL", "environment approval is supported only with environment-only reapproval")
    old_phase, task_root = _pre_candidate_eligible(old, receipt, project, phase_id,
                                                   environment_only=environment_only)
    if environment_only:
        if verifier is None:
            _fail("PHASE_REVIEW_HISTORY_VALIDATION_REQUIRED", "environment-only reapproval requires verified archived review cycles")
        _verify_all_review_cycles(project, task_root, old, verifier)
        _verify_pre_candidate_history(project, old)
        _verify_environment_reapproval_writer(project, old, old_phase)
    if not isinstance(replacement, dict):
        _fail("INVALID_PLANNER_OUTPUT", "replacement planner output must be an object")
    def reject_host_fields(value, parent=None):
        if isinstance(value, dict):
            if _REAPPROVAL_HOST_FIELDS.intersection(value):
                _fail("INVALID_PLANNER_OUTPUT", "replacement planner output contains host-owned lifecycle fields")
            if parent == "phases" and "base_sha" in value:
                _fail("INVALID_PLANNER_OUTPUT", "replacement planner output contains host-owned phase base")
            for key, child in value.items(): reject_host_fields(child, key)
        elif isinstance(value, list):
            for child in value: reject_host_fields(child, parent)
    # Domain keys such as technical_decision.evidence are not lifecycle state.
    # The complete semantic contract is compared for exact equality below.
    replacement_without_phases = {key: value for key, value in replacement.items()
                                  if key not in {"phases", "semantic_contract"}}
    reject_host_fields(replacement_without_phases)
    reject_host_fields(replacement.get("phases", [{}])[0] if replacement.get("phases") else {})
    if replacement.get("phases") and "base_sha" in replacement["phases"][0]:
        _fail("INVALID_PLANNER_OUTPUT", "replacement planner output contains host-owned phase base")
    for key in ("schema_version", "task_id", "task_mode", "semantic_contract", "current_phase_id", "worktree"):
        if replacement.get(key) != old.get(key):
            _fail("PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH", f"replacement changes immutable task field {key}")
    incoming_phases = replacement.get("phases")
    if not isinstance(incoming_phases, list) or len(incoming_phases) != len(old["phases"]):
        _fail("INVALID_PLANNER_OUTPUT", "replacement must preserve the complete phase list")
    for index, (before, after) in enumerate(zip(old["phases"], incoming_phases)):
        if not isinstance(after, dict):
            _fail("INVALID_PLANNER_OUTPUT", "replacement phases must be objects")
        immutable = ("phase_id", "depends_on", "commit_boundary", "commit_subject")
        if any(after.get(key) != before.get(key) for key in immutable):
            _fail("PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH", "replacement changes phase order, dependency, or semantics")
        if index and after != before:
            _fail("INVALID_PLANNER_OUTPUT", "noncurrent phase contracts must remain unchanged")
    new_phase = incoming_phases[0]
    if set(new_phase) - _REAPPROVAL_FIELDS - {"phase_id", "depends_on", "status"}:
        _fail("INVALID_PLANNER_OUTPUT", "replacement current phase contains unsupported fields")
    if new_phase.get("status", "PLANNED") != "PLANNED":
        _fail("INVALID_PLANNER_OUTPUT", "replacement current phase must start PLANNED")
    if environment_only:
        for key in _REAPPROVAL_FIELDS:
            if ((key in old_phase) != (key in new_phase) or old_phase.get(key) != new_phase.get(key)):
                _fail("PRE_CANDIDATE_REAPPROVAL_IDENTITY_MISMATCH",
                      "environment-only reapproval cannot change the phase contract or verification bindings")
        if old.get("schema_version") == 2 and replacement.get("original_intent") != old.get("original_intent"):
            _fail("INTENT_IMMUTABILITY_VIOLATION", "environment-only reapproval must preserve original intent exactly")
    generation = _contract_generation(old)
    archived_ids = {item.get("planner_dispatch", {}).get("spawn_receipt_id") for item in old.get("pre_candidate_reapprovals", [])}
    if spawn_receipt_id == old.get("planner_dispatch", {}).get("spawn_receipt_id") or spawn_receipt_id in archived_ids:
        _fail("INVALID_PLANNER_ROUTE", "reapproval requires a new planner receipt")
    _verify_dispatch(project, spawn_receipt_id, task_id=old["task_id"], role="planner",
                     base_sha=old["worktree"]["base_sha"])
    dispatch_path = devcrew_capacity.dispatch_receipt_path(project, old["task_id"])
    store = devcrew_dispatch._read(dispatch_path)
    spawn = next((item for item in store["receipts"] if item.get("SPAWN_RECEIPT_ID") == spawn_receipt_id), None)
    if (not isinstance(spawn, dict) or spawn.get("ROLE") != "planner" or
            spawn.get("BASE_SHA") != old["worktree"]["base_sha"] or
            spawn.get("CONTRACT_GENERATION", 0) != generation):
        _fail("INVALID_PLANNER_ROUTE", "planner receipt does not match task and original base")
    before_snapshot = _pre_candidate_snapshot(task_root)
    updated = json.loads(json.dumps(old))
    target = updated["phases"][0]
    old_contract = (json.loads(json.dumps(old_phase)) if environment_only else {key: json.loads(json.dumps(old_phase[key])) for key in
                    (set(_REAPPROVAL_FIELDS) | {"phase_id", "depends_on", "status", "base_sha", "verification_sha256",
                     "verification_environment_sha256", "verification_approved_at", "evidence", "executor_dispatch", "planner_dispatch"})
                    if key in old_phase})
    if old.get("schema_version") == 2:
        if "original_intent" not in replacement:
            _fail("INVALID_INTENT_ASSESSMENT", "schema v2 replacement must preserve planner intent state")
        updated["original_intent"] = json.loads(json.dumps(replacement["original_intent"]))
    target.update({key: json.loads(json.dumps(new_phase[key])) for key in _REAPPROVAL_FIELDS if key in new_phase})
    target["status"] = "EXECUTING" if environment_only else "PLANNED"
    clear_fields = ("evidence", "executor_dispatch", "review_dispatch", "review_start", "candidate_commit", "review_report",
                    "local_fix_transition", "systemic_replan_transition", "accepted_sha", "acceptance_evidence")
    if not environment_only:
        clear_fields += ("review_cycles", "active_local_fix")
    for key in clear_fields:
        target.pop(key, None)
    pin_verification(target)
    if old.get("schema_version") == 2:
        if not environment_only:
            _install_intent_assessment(updated, intent_assessment, old=old)
    elif intent_assessment is not None:
        _fail("INVALID_INTENT_ASSESSMENT", "intent assessment input is supported only for schema v2")
    history = list(updated.get("pre_candidate_reapprovals", []))
    entry = {"generation": generation + 1, "prior_generation": generation,
             "task_id": old["task_id"], "phase_id": phase_id, "base_sha": old["worktree"]["base_sha"],
             "previous_contract": old_contract, "planner_dispatch": json.loads(json.dumps(old.get("planner_dispatch"))),
             "authorizing_spawn_receipt_id": spawn_receipt_id, "previous_verification_sha256": old_phase.get("verification_sha256")}
    if environment_only:
        entry["reapproval_mode"] = "ENVIRONMENT_ONLY"
    if old.get("schema_version") == 2:
        entry["original_intent"] = json.loads(json.dumps(old["original_intent"]))
        entry["technical_projection"] = _intent_technical_projection(old)
    entry["archive_sha256"] = hashlib.sha256(json.dumps(entry, sort_keys=True, separators=(",", ":"),
                                                     ensure_ascii=False).encode()).hexdigest()
    history.append(entry)
    updated["pre_candidate_reapprovals"] = history
    updated["contract_generation"] = generation + 1
    _verify_planner_source_binding(project, updated, spawn)
    updated["planner_dispatch"] = {"spawn_receipt_id": spawn_receipt_id, "route_id": spawn.get("ROUTE_ID"),
                                    "agent_id": spawn.get("AGENT_ID"), "model": spawn.get("RESOLVED_MODEL"),
                                    "effort": spawn.get("REASONING_EFFORT"), "spawn_mode": spawn.get("SPAWN_MODE"),
                                    "authorization_generation": generation, "reapproval_authorizer": True}
    validate_manifest(updated, receipt, task_root)
    state_path = manifest_path(project, old["task_id"]).with_name(f"{old['task_id']}.json")
    with _manifest_lock(path), devcrew_worktree._receipt_lock(state_path), devcrew_dispatch._locked(dispatch_path):
        current = json.loads(path.read_text(encoding="utf-8"))
        if current != old or _manifest_revision(current) != _manifest_revision(old):
            _fail("PHASE_MANIFEST_CONFLICT", "manifest changed during pre-candidate reapproval")
        fresh_receipt = devcrew_worktree.resolve(project, old["task_id"], read_only=True)
        if fresh_receipt != receipt or _pre_candidate_snapshot(task_root) != before_snapshot:
            _fail("PRE_CANDIDATE_REAPPROVAL_STATE_CHANGED", "branch, receipt, index, or dirty files changed during reapproval")
        if environment_only:
            if approved_environment_sha256 != environment_digest():
                _fail("INVALID_ENVIRONMENT_APPROVAL", "current process environment changed before reapproval commit")
            _verify_all_review_cycles(project, task_root, old, verifier)
            _verify_environment_reapproval_writer(project, old, old_phase)
        validate_manifest(updated, fresh_receipt, task_root)
        if old.get("schema_version") == 2:
            _validate_intent_transition(old, updated, authorization=intent_assessment)
        _verify_dispatch(project, spawn_receipt_id, task_id=old["task_id"], role="planner",
                         base_sha=old["worktree"]["base_sha"])
        _verify_pre_candidate_history(project, updated)
        updated["manifest_revision"] = _manifest_revision(old) + 1
        _replace_json(path, updated)
    return updated


def _verify_dispatch(project: pathlib.Path, receipt_id: str, *, task_id: str, role: str,
                    phase_id: str | None = None, base_sha: str | None = None,
                    target_sha: str | None = None,
                    writer_cycle: int | None = None, rejected_candidate_sha: str | None = None,
                    agent_id: str | None = None, route_id: str | None = None,
                    model: str | None = None, effort: str | None = None,
                    spawn_mode: str | None = None) -> dict[str, Any]:
    try:
        return devcrew_dispatch.verify_spawn(
            project, receipt_id, task_id=task_id, role=role, phase_id=phase_id,
            base_sha=base_sha, target_sha=target_sha, agent_id=agent_id, route_id=route_id, model=model,
            effort=effort, spawn_mode=spawn_mode, writer_cycle=writer_cycle,
            rejected_candidate_sha=rejected_candidate_sha,
            store_path=devcrew_capacity.dispatch_receipt_path(project, task_id))
    except devcrew_dispatch.DispatchError as exc:
        _fail("INVALID_DISPATCH_ROUTE", str(exc))


def _verify_planner_source_binding(project, manifest, spawn):
    """Keep the source approved before planning in every accepted v2 plan."""
    try:
        store = devcrew_dispatch._read(devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
        issued = [row for row in store["issued_routes"] if row.get("ROUTE_ID") == spawn["ROUTE_ID"]]
        if len(issued) != 1:
            _fail("INVALID_PLANNER_ROUTE", "planner source route is missing or ambiguous")
        payload = issued[0].get("SPAWN_PAYLOAD")
        if (not isinstance(payload, dict) or
                devcrew_dispatch._canonical_digest(payload) != issued[0].get("SPAWN_PAYLOAD_SHA256")):
            _fail("INVALID_PLANNER_ROUTE", "planner source payload digest is invalid")
        matches = re.findall(r"(?:^|\n)INTENT_SOURCE: (\{[^\n]*\})", payload.get("message", ""))
        if len(matches) > 1:
            _fail("INVALID_PLANNER_ROUTE", "planner source is ambiguous")
        ordinary_path = pathlib.Path(project) / ".codex" / "tasks" / f"{manifest['task_id']}.ordinary.json"
        link_path = _ordinary_handoff_path(pathlib.Path(project), manifest["task_id"])
        has_ordinary_history = (ordinary_path.exists() or fs.is_link(ordinary_path) or
                                link_path.exists() or fs.is_link(link_path))
        # Legacy routes have no source marker. They may use the explicitly
        # linked source only when the same-task handoff was already registered.
        if manifest.get("schema_version") != 2:
            if matches and json.loads(matches[0]).get("source_type") == "intent_contract":
                _fail("INTENT_SOURCE_BINDING_MISMATCH", "confirmed contracts require schema-v2 Intent Review")
            if has_ordinary_history:
                if not link_path.is_file() or fs.is_link(link_path):
                    _fail("ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED",
                          "same-task ordinary history requires a registered verified source handoff")
                existing_path = manifest_path(pathlib.Path(project), manifest["task_id"])
                try:
                    existing = (json.loads(existing_path.read_text(encoding="utf-8"))
                                if existing_path.is_file() and not fs.is_link(existing_path) else None)
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    existing = None
                if existing != manifest:
                    _fail("ORDINARY_SOURCE_GAP_SCHEMA_V2_REQUIRED",
                          "a new formal planner after ordinary handoff requires schema-v2 original intent and assessment")
                task_root = manifest["worktree"]["task_worktree_root"]
                intent_source_preflight(project, {
                    "task_id": manifest["task_id"], "owner": "DEVCREW", "state": "RESOLVED",
                    "task_worktree_root": task_root, "branch": manifest["worktree"]["branch"],
                    "expected_head": manifest["phases"][0].get("base_sha") or manifest["worktree"]["base_sha"]})
            return
        if matches and json.loads(matches[0]) not in manifest["original_intent"]["sources"]:
            _fail("INTENT_SOURCE_BINDING_MISMATCH", "accepted plan must retain the exact source pinned before planner dispatch")
        task_root = manifest["worktree"]["task_worktree_root"]
        if (ordinary_path.exists() or fs.is_link(ordinary_path) or link_path.exists() or fs.is_link(link_path)):
            if not matches:
                _fail("ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED", "same-task ordinary history requires a pinned source and verified handoff")
            pinned_source = json.loads(matches[0])
            intent_source_preflight(project, {
                "task_id": manifest["task_id"], "owner": "DEVCREW", "state": "RESOLVED",
                "task_worktree_root": task_root, "branch": manifest["worktree"]["branch"],
                "expected_head": manifest["phases"][0].get("base_sha") or manifest["worktree"]["base_sha"]},
                source=pinned_source)
    except (devcrew_dispatch.DispatchError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, PhaseError):
            raise
        _fail("INVALID_PLANNER_ROUTE", str(exc))


def verify_planner_dispatch(project: pathlib.Path, manifest: dict[str, Any]) -> None:
    _verify_pre_candidate_history(project, manifest)
    dispatch = manifest.get("planner_dispatch")
    if not isinstance(dispatch, dict):
        _fail("INVALID_PLANNER_ROUTE", "phase manifest has no persisted planner dispatch receipt")
    generation = _contract_generation(manifest)
    if generation:
        history = manifest.get("pre_candidate_reapprovals", [])
        if (dispatch.get("reapproval_authorizer") is not True or
                dispatch.get("authorization_generation") != generation - 1 or
                dispatch.get("spawn_receipt_id") != history[-1].get("authorizing_spawn_receipt_id")):
            _fail("INVALID_PLANNER_ROUTE", "active planner receipt is not the verified reapproval authorizer")
    spawn = _verify_dispatch(project, dispatch.get("spawn_receipt_id"), task_id=manifest["task_id"],
                     role="planner", base_sha=manifest["worktree"]["base_sha"],
                     agent_id=dispatch.get("agent_id"), route_id=dispatch.get("route_id"),
                     model=dispatch.get("model"), effort=dispatch.get("effort"),
                     spawn_mode=dispatch.get("spawn_mode"))

    _verify_planner_source_binding(project, manifest, spawn)

def _validate_evidence_ref(reference: Any) -> str:
    if not isinstance(reference, str) or not reference or "\\" in reference or "\x00" in reference:
        _fail("INVALID_EVIDENCE_REFERENCE", "evidence references must be normalized host-relative paths")
    path = pathlib.PurePosixPath(reference)
    if (fs.unsafe_parts(path.parts) or path.is_absolute() or any(part in ("", ".", "..") for part in path.parts) or
            path.as_posix() != reference):
        _fail("INVALID_EVIDENCE_REFERENCE", "evidence references must be normalized host-relative paths")
    return reference


def environment_digest(environment: dict[str, str] | None = None) -> str:
    # Keep values (including credentials) out of receipts.
    values = dict(os.environ) if environment is None else dict(environment)
    values.pop("CODEX_TOOL_CALL_ID", None)
    return hashlib.sha256(json.dumps(values, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def verification_bindings(verification: list[str], bindings: Any) -> list[dict[str, Any]]:
    """Normalize the existing ordered bindings, independently of the test stack."""
    if not isinstance(bindings, list) or not bindings or len(bindings) != len(verification):
        _fail("INVALID_VERIFICATION_BINDINGS", "bindings must exactly cover ordered verification items")
    normalized = []
    for index, item in enumerate(bindings):
        if (not isinstance(item, dict) or
                not {"index", "verification", "argv"}.issubset(item) or
                set(item) - {"index", "verification", "argv", "timeout", "idle_timeout"} or
                type(item["index"]) is not int or item["index"] != index or
                item["verification"] != verification[index] or
                not isinstance(item["argv"], list) or not item["argv"] or
                any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in item["argv"])):
            _fail("INVALID_VERIFICATION_BINDINGS", "bindings must exactly cover ordered verification items")
        binding = dict(item, timeout=item.get("timeout", 300.0), idle_timeout=item.get("idle_timeout"))
        for key in ("timeout", "idle_timeout"):
            value = binding[key]
            if value is None and key == "idle_timeout":
                continue
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                _fail("INVALID_VERIFICATION_BINDINGS", f"{key} must be finite and positive")
        normalized.append(binding)
    return normalized


def _verification_digest(phase: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps({key: phase.get(key) for key in
        ("verification", "verification_bindings", "verification_environment_sha256", "verification_approved_at")},
        sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def pin_verification(phase: dict[str, Any]) -> None:
    phase["verification_bindings"] = verification_bindings(phase["verification"], phase.get("verification_bindings"))
    phase["verification_environment_sha256"] = environment_digest()
    phase["verification_approved_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")
    phase["verification_sha256"] = _verification_digest(phase)


def required_verification(phase: dict[str, Any]) -> list[dict[str, Any]]:
    bindings = verification_bindings(phase["verification"], phase.get("verification_bindings"))
    try:
        approved = datetime.datetime.fromisoformat(phase.get("verification_approved_at", ""))
    except (TypeError, ValueError):
        _fail("VERIFICATION_PLAN_CHANGED", "verification approval time is missing or invalid")
    if (approved.tzinfo is None or not isinstance(phase.get("verification_environment_sha256"), str) or
            not re.fullmatch(r"[0-9a-f]{64}", phase["verification_environment_sha256"])):
        _fail("VERIFICATION_PLAN_CHANGED", "verification execution environment is missing or invalid")
    if phase.get("verification_sha256") != _verification_digest(phase):
        _fail("VERIFICATION_PLAN_CHANGED", "required checks must be agreed before execution; use explicit replanning")
    return bindings


def match_verification(data: dict[str, Any], binding: dict[str, Any], contract: dict[str, Any]) -> bool:
    return ("idle_timeout_seconds" in data and
            type(data.get("timeout_seconds")) in (int, float) and
            (data["idle_timeout_seconds"] is None or type(data["idle_timeout_seconds"]) in (int, float)) and
            data.get("command") == binding["argv"] and
            data.get("timeout_seconds") == binding["timeout"] and
            data.get("idle_timeout_seconds") == binding["idle_timeout"] and
            data.get("environment_sha256") == contract.get("verification_environment_sha256") and
            isinstance(data.get("started_at"), str) and
            datetime.datetime.fromisoformat(data["started_at"]) >=
            datetime.datetime.fromisoformat(contract["verification_approved_at"]))


def _verification_contract(phase: dict[str, Any]) -> dict[str, Any]:
    required_verification(phase)
    return json.loads(json.dumps({key: phase[key] for key in
        ("verification", "verification_bindings", "verification_environment_sha256",
         "verification_approved_at", "verification_sha256")}))


def _verify_acceptance_evidence(records: list[str], project: pathlib.Path, task_root: pathlib.Path,
                                task_id: str, candidate: dict[str, Any], verifier) -> list[str]:
    if not isinstance(records, list) or not records:
        _fail("PHASE_ACCEPTANCE_EVIDENCE_REQUIRED", "acceptance requires fresh --evidence-record entries for all approved checks")
    contract = candidate.get("verification_contract")
    if not isinstance(contract, dict):
        _fail("VERIFICATION_PLAN_CHANGED", "candidate has no approved verification contract")
    # Historical acceptance is rechecked after later phases advance HEAD. H4 still
    # validates complete coverage; the immutable candidate identity is checked below.
    references = verify_evidence_records(records, project, task_root, task_id, verifier,
                                         contract, current=False)
    for reference in references:
        try:
            _record, data, _host = verifier(str(project / reference), current=False,
                                            expected_execution_root=str(task_root), expected_task_id=task_id)
        except Exception as exc:
            _fail("INVALID_PHASE_EVIDENCE", str(exc))
        if (data.get("git_head_before") != candidate["sha"] or data.get("git_head") != candidate["sha"] or
                data.get("candidate_tree_before") != candidate["tree"] or
                data.get("candidate_tree_after") != candidate["tree"] or
                data.get("worktree_fingerprint_before") != data.get("worktree_fingerprint_after")):
            _fail("STALE_ACCEPTANCE_EVIDENCE", "approved checks are not fresh for the exact candidate commit and tree")
    return references


def verify_evidence_records(records: list[str], project: pathlib.Path, task_root: pathlib.Path,
                            task_id: str, verifier, phase: dict[str, Any] | None = None,
                            *, current: bool = True) -> list[str]:
    """Verify H4 coverage; current=False allows rechecking immutable historical evidence."""
    if not isinstance(records, list) or not records:
        _fail("PHASE_EVIDENCE_REQUIRED", "at least one --evidence-record is required")
    host = project.resolve()
    execution = task_root.resolve()
    bindings = required_verification(phase) if phase is not None else []
    matched = set()
    references = []
    for item in records:
        if not isinstance(item, str) or not item:
            _fail("INVALID_EVIDENCE_REFERENCE", "evidence record path must be non-empty")
        candidate = pathlib.Path(item)
        if not candidate.is_absolute():
            candidate = host / candidate
        try:
            record, data, record_host = verifier(str(candidate), current=current and phase is not None,
                                                  expected_execution_root=str(execution),
                                                  expected_task_id=task_id)
        except Exception as exc:
            _fail("INVALID_PHASE_EVIDENCE", str(exc))
        if record_host.resolve() != host:
            _fail("FOREIGN_PHASE_EVIDENCE", "evidence record belongs to a different host project")
        try:
            reference = record.resolve().relative_to(host).as_posix()
        except ValueError:
            _fail("FOREIGN_PHASE_EVIDENCE", "evidence record is outside the host project")
        if phase is not None:
            if (data.get("git_head_before") != data.get("git_head") or
                    data.get("worktree_fingerprint_before") != data.get("worktree_fingerprint_after")):
                _fail("STALE_PHASE_EVIDENCE", "check changed the execution state")
            matched.update(i for i, binding in enumerate(bindings) if match_verification(data, binding, phase))
        references.append(_validate_evidence_ref(reference))
    if len(references) != len(set(references)):
        _fail("DUPLICATE_PHASE_EVIDENCE", "evidence record paths must be unique")
    if phase is not None and matched != set(range(len(bindings))):
        _fail("REQUIRED_CHECK_MISSING", "successful evidence does not cover the agreed commands and execution parameters")
    return references


def set_phase_status(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                     phase_id: str, status: str, evidence: list[str] | None = None,
                     project: pathlib.Path | None = None, verifier=None,
                     spawn_receipt_id: str | None = None) -> dict[str, Any]:
    """Apply an allowed current-phase transition and persist it atomically."""
    validate_manifest(manifest, receipt)
    if any(phase.get("review_cycles") for phase in manifest["phases"]):
        task_root = pathlib.Path(manifest["worktree"]["task_worktree_root"])
        if project is None:
            _fail("PHASE_REVIEW_HISTORY_VALIDATION_REQUIRED", "status transitions require the host project for archived-cycle checks")
        _verify_all_review_cycles(project, task_root, manifest, verifier)
    if phase_id != manifest["current_phase_id"]:
        _fail("CURRENT_PHASE_ONLY", "only CURRENT_PHASE may change status")
    phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_id)
    transitions = {
        "PLANNED": {"EXECUTING"},
        "EXECUTING": {"VERIFYING", "BLOCKED", "REPLAN_REQUIRED"},
        "VERIFYING": {"READY_FOR_CLOSE", "BLOCKED", "REPLAN_REQUIRED"},
        "BLOCKED": {"REPLAN_REQUIRED"},
        "REPLAN_REQUIRED": set(),
        "READY_FOR_CLOSE": set(),
    }
    if status not in transitions.get(phase["status"], set()):
        _fail("INVALID_STATUS_TRANSITION", f"cannot transition {phase['status']} to {status}")
    updated = json.loads(json.dumps(manifest))
    updated_phase = next(item for item in updated["phases"] if item["phase_id"] == phase_id)
    updated_phase["status"] = status
    if status == "EXECUTING" and "verification_sha256" not in phase:
        pin_verification(updated_phase)
    if status == "READY_FOR_CLOSE":
        if not evidence:
            _fail("PHASE_EVIDENCE_REQUIRED", "READY_FOR_CLOSE requires --evidence-record")
        if project is None:
            _fail("INVALID_EXECUTOR_ROUTE", "READY_FOR_CLOSE requires the host project for executor receipt validation")
        active_fix = phase.get("active_local_fix")
        cycle = active_fix["cycle_id"] if active_fix else 0
        rejected_sha = active_fix["rejected_candidate_sha"] if active_fix else None
        spawn = _verify_dispatch(project, spawn_receipt_id, task_id=manifest["task_id"], role="executor",
                                 phase_id=phase_id, base_sha=phase.get("base_sha", manifest["worktree"]["base_sha"]),
                                 writer_cycle=cycle, rejected_candidate_sha=rejected_sha)
        updated_phase["executor_dispatch"] = {
            "spawn_receipt_id": spawn_receipt_id, "writer_cycle": cycle,
            "rejected_candidate_sha": rejected_sha, "route_id": spawn["ROUTE_ID"],
            "agent_id": spawn["AGENT_ID"], "model": spawn["RESOLVED_MODEL"],
            "effort": spawn["REASONING_EFFORT"], "spawn_mode": spawn["SPAWN_MODE"]}
        updated_phase["evidence"] = verify_evidence_records(
            evidence, project, pathlib.Path(receipt["task_worktree_root"]), manifest["task_id"], verifier, phase)
    elif evidence:
        _fail("UNEXPECTED_PHASE_EVIDENCE", "evidence may be supplied only when entering READY_FOR_CLOSE")
    validate_manifest(updated, receipt)
    _atomic_json(path, updated)
    return updated


def load_for_resume(path: pathlib.Path, fresh_receipt: dict[str, Any],
                    project: pathlib.Path | None = None) -> dict[str, Any]:
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("PHASE_MANIFEST_REQUIRED", f"cannot load phase manifest: {exc}")
    validated = validate_manifest(manifest, fresh_receipt)
    if project is not None:
        verify_planner_dispatch(project, validated)
    return validated


def original_intent_status(manifest: dict[str, Any]) -> dict[str, Any]:
    if manifest.get("schema_version") == 1:
        return {"status": "UNVERIFIED_LEGACY",
                "limitations": ["schema v1 has no original-intent certification"]}
    intent = manifest.get("original_intent", {})
    sources = intent.get("sources", []) if isinstance(intent, dict) else []
    assessment = intent.get("planning_assessment", {}) if isinstance(intent, dict) else {}
    status = "PLANNING_ASSESSED" if isinstance(assessment, dict) and assessment.get("status") == "ASSESSED" else "NOT_ASSESSED"
    return {"status": status,
            "unverified_source_ids": [source.get("source_id") for source in sources
                                      if isinstance(source, dict) and source.get("status") == "UNVERIFIED"]}


def original_intent_context(manifest: dict[str, Any], phase_id: str) -> dict[str, Any] | None:
    """Return the current immutable intent contract, bound to this phase."""
    if manifest.get("schema_version") != 2:
        return None
    intent = manifest["original_intent"]
    return {
        "plan_sha256": intent_plan_digest(manifest),
        "phase_id": phase_id,
        "sources": json.loads(json.dumps(intent["sources"])),
        "requirements": json.loads(json.dumps(intent["requirements"])),
        "effective_requirements": json.loads(json.dumps(effective_intent_requirements(manifest))),
        "decisions": json.loads(json.dumps(intent["decisions"])),
        "planning_assessment": json.loads(json.dumps(intent["planning_assessment"])),
        "evidence_records": sorted({reference for row in intent["requirements"]
                                     for reference in row["evidence_records"]}),
    }


def _role_intent_context(manifest: dict[str, Any], phase_id: str) -> dict[str, Any] | None:
    """Keep full source/obligation context while bounding coordinator prose."""
    context = original_intent_context(manifest, phase_id)
    if context is None:
        return None
    state = context["planning_assessment"]
    context["planning_assessment"] = {
        "status": state["status"],
        "state_sha256": hashlib.sha256(_canonical_json(state)).hexdigest(),
    }
    return context


def original_intent_review_context(manifest: dict[str, Any], phase_id: str) -> dict[str, Any] | None:
    """Return original intent for the final reviewer only."""
    if not manifest.get("phases") or phase_id != manifest["phases"][-1]["phase_id"]:
        return None
    return _role_intent_context(manifest, phase_id)


def executor_input(manifest: dict[str, Any], task_worktree_root: pathlib.Path,
                   project: pathlib.Path | None = None, verifier=None) -> dict[str, Any]:
    _require_no_candidate_transition(manifest)
    current_id = manifest["current_phase_id"]
    phase = next(item for item in manifest["phases"] if item["phase_id"] == current_id)
    current_index = next(i for i, item in enumerate(manifest["phases"]) if item["phase_id"] == current_id)
    if any(item.get("review_cycles") for item in manifest["phases"]):
        if project is None:
            _fail("PHASE_REVIEW_HISTORY_VALIDATION_REQUIRED", "dispatch requires host validation of rejected review cycles")
        _verify_all_review_cycles(project, task_worktree_root, manifest, verifier)
    if current_index:
        if project is None or verifier is None:
            _fail("PHASE_ACCEPTED_HISTORY_VALIDATION_REQUIRED",
                  "later-phase dispatch requires host report and Git validation of accepted history")
        _verify_accepted_history(manifest, project, task_worktree_root, verifier,
                                 include_current_accepted=phase["status"] == "ACCEPTED",
                                 require_clean=phase["status"] in {"PLANNED", "ACCEPTED"})
    if any(next(item for item in manifest["phases"] if item["phase_id"] == dependency)["status"] != "ACCEPTED"
           for dependency in phase["depends_on"]):
        _fail("CLOSE_GATE_REQUIRED", "phase dependencies must be ACCEPTED before dispatch")
    if phase["status"] == "READY_FOR_CLOSE":
        _fail("PHASE_READY_FOR_CLOSE", "phase is waiting at close boundary")
    if phase["status"] not in {"PLANNED", "EXECUTING", "VERIFYING"}:
        _fail("PHASE_NOT_DISPATCHABLE", f"phase status {phase['status']} cannot be dispatched")
    if phase.get("active_local_fix"):
        fix = phase["active_local_fix"]
        result = {"task_id": manifest["task_id"], "phase_id": current_id,
                "base_sha": phase.get("base_sha") or manifest["worktree"]["base_sha"],
                "rejected_candidate_sha": fix["rejected_candidate_sha"],
                "findings": json.loads(json.dumps(fix["findings"])),
                "owned_scope": list(phase["owned_scope"]), "acceptance": list(phase["acceptance"]),
                "verification": list(phase["verification"]),
                "instruction": "Fix only these findings in this phase and stop for fresh verification."}
        intent_context = _role_intent_context(manifest, current_id)
        if intent_context is not None:
            result["original_intent"] = intent_context
        return result
    phase_base = phase.get("base_sha") or (manifest["worktree"]["base_sha"]
                                             if current_id == manifest["phases"][0]["phase_id"] else None)
    if not isinstance(phase_base, str):
        _fail("CLOSE_GATE_REQUIRED", "phase has no accepted predecessor base SHA")
    result = {"task_id": manifest["task_id"], "phase_id": current_id,
            "task_worktree_root": str(task_worktree_root), "goal": phase["goal"],
            "main_scenario": phase.get("main_scenario"),
            "base_sha": phase_base,
            "owned_scope": list(phase["owned_scope"]), "acceptance": list(phase["acceptance"]),
            "verification": list(phase["verification"]),
            "known_contracts": list(phase.get("known_contracts", [])),
            "do_not_change": list(phase.get("do_not_change", [])),
            "instruction": "Implement only this phase; stop at READY_FOR_CLOSE."}
    intent_context = _role_intent_context(manifest, current_id)
    if intent_context is not None:
        result["original_intent"] = intent_context
    return result


def resume(manifest: dict[str, Any], project: pathlib.Path | None = None,
           task_root: pathlib.Path | None = None, verifier=None) -> dict[str, Any]:
    _require_no_candidate_transition(manifest)
    phase = next(item for item in manifest["phases"] if item["phase_id"] == manifest["current_phase_id"])
    if any(item.get("review_cycles") for item in manifest["phases"]):
        if project is None or task_root is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "cycle history resume requires host and Git validation")
        _verify_all_review_cycles(project, task_root, manifest, verifier)
    current_index = next(i for i, item in enumerate(manifest["phases"])
                         if item["phase_id"] == manifest["current_phase_id"])
    if current_index:
        if project is None or task_root is None or verifier is None:
            _fail("PHASE_ACCEPTED_HISTORY_VALIDATION_REQUIRED",
                  "later-phase resume requires host report and Git validation of accepted history")
        _verify_accepted_history(manifest, project, task_root, verifier,
                                 include_current_accepted=phase["status"] == "ACCEPTED",
                                 require_clean=phase["status"] in {"PLANNED", "ACCEPTED"})
    if phase["status"] == "READY_FOR_CLOSE":
        if project is None or task_root is None or verifier is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "resume must revalidate READY_FOR_CLOSE evidence")
        verify_evidence_records(phase["evidence"], project, task_root, manifest["task_id"], verifier, phase)
        return {"status": "PHASE_READY_FOR_CLOSE", "planner_required": False, "executor_required": False}
    if phase["status"] == "COMMITTED":
        _verify_candidate_commit(manifest, phase, task_root)
        if project is not None and task_root is not None and verifier is not None:
            _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        return {"status": "PHASE_COMMITTED_AWAITING_REVIEW", "planner_required": False,
                "executor_required": False, "acceptance_required": True}
    if phase["status"] == "REVIEWING":
        _verify_candidate_commit(manifest, phase, task_root)
        if project is None or task_root is None or verifier is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "review resume must revalidate candidate evidence")
        _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        if phase.get("review_report"):
            _verify_saved_review_report(project, manifest, phase)
        report = phase.get("review_report")
        status = ("PHASE_REVIEW_ACCEPTANCE_READY" if isinstance(report, dict) and
                  report.get("verdict") == "PASS" and report.get("authoritative") is True
                  else "PHASE_REVIEW_REQUIRED")
        return {"status": status, "planner_required": False, "executor_required": False,
                "reviewer_required": status == "PHASE_REVIEW_REQUIRED",
                "acceptance_required": status == "PHASE_REVIEW_ACCEPTANCE_READY"}
    if phase["status"] == "ACCEPTED":
        _verify_candidate_commit(manifest, phase, task_root)
        if project is None or task_root is None or verifier is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "accepted phase resume must revalidate its report and evidence")
        _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        saved_report = _verify_saved_review_report(project, manifest, phase)
        _verify_saved_acceptance_evidence(project, task_root, manifest, phase, verifier)
        if (manifest.get("schema_version") == 2 and manifest["phases"][-1]["phase_id"] == phase["phase_id"] and
                all(row.get("status") == "ACCEPTED" for row in manifest["phases"])):
            expected = {"status": "ACCEPTED", "review": {"phase_id": phase["phase_id"],
                        "report_sha256": phase["review_report"]["sha256"]}}
            if manifest["original_intent"].get("task_acceptance") == expected:
                _verify_dispatch(project, saved_report.get("SPAWN_RECEIPT_ID"),
                                 task_id=manifest["task_id"], role="reviewer",
                                 phase_id=phase["phase_id"],
                                 base_sha=phase["candidate_commit"]["base"],
                                 target_sha=phase["candidate_commit"]["sha"],
                                 agent_id=saved_report.get("AGENT_ID"))
                _validate_intent_review(saved_report, manifest, phase, project, task_root, verifier)
                return {"status": "PHASE_ACCEPTED", "task_status": "TASK_DONE",
                        "planner_required": False, "executor_required": False, "reviewer_required": False}
            return {"status": "PHASE_ACCEPTED", "task_status": "INCOMPLETE",
                    "intent_limitation": "final phase is accepted without task-level intent certification",
                    "planner_required": False, "executor_required": False, "reviewer_required": False}
        return {"status": "PHASE_ACCEPTED", "task_status": "INCOMPLETE",
                "planner_required": False, "executor_required": False, "reviewer_required": False}
    if phase["status"] == "LOCAL_FIX_REQUIRED" and phase.get("candidate_commit"):
        _verify_candidate_commit(manifest, phase, task_root)
        if project is None or task_root is None or verifier is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "local-fix resume must revalidate candidate and review")
        _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        _verify_saved_review_report(project, manifest, phase)
        if stopped := _loop_stop(phase):
            return stopped
        return {"status": "PHASE_LOCAL_FIX_REQUIRED", "executor_lifecycle": "LOCAL_FIX_CONTINUATION_ALLOWED",
                "planner_required": False,
                "executor_required": True, "executor_role": "executor",
                "local_fix_input": {"phase_id": phase["phase_id"],
                                    "findings": (phase.get("review_report") or {}).get("findings", []),
                                    "base_sha": phase["candidate_commit"]["base"],
                                    "rejected_candidate_sha": phase["candidate_commit"]["sha"],
                                    "owned_scope": list(phase["owned_scope"]),
                                    "acceptance": list(phase["acceptance"]),
                                    "verification": list(phase["verification"]),
                                    **({"original_intent": original_intent_context(manifest, phase["phase_id"])}
                                       if manifest.get("schema_version") == 2 else {})}}
    active_fix = phase.get("active_local_fix")
    if active_fix and phase["status"] in {"EXECUTING", "VERIFYING"}:
        bounded_input = {"task_id": manifest["task_id"], "phase_id": phase["phase_id"],
                         "findings": json.loads(json.dumps(active_fix["findings"])),
                         "base_sha": phase.get("base_sha") or manifest["worktree"]["base_sha"],
                         "rejected_candidate_sha": active_fix["rejected_candidate_sha"],
                         "owned_scope": list(phase["owned_scope"]),
                         "acceptance": list(phase["acceptance"]),
                         "verification": list(phase["verification"])}
        intent_context = original_intent_context(manifest, phase["phase_id"])
        if intent_context is not None:
            bounded_input["original_intent"] = intent_context
        if phase["status"] == "EXECUTING":
            return {"status": "PHASE_LOCAL_FIX_EXECUTOR_REQUIRED",
                    "executor_lifecycle": "LOCAL_FIX_CONTINUATION_ALLOWED", "planner_required": False,
                    "executor_required": True, "executor_role": "executor", "local_fix_input": bounded_input}
        return {"status": "PHASE_LOCAL_FIX_VERIFYING", "planner_required": False,
                "executor_required": False, "local_fix_input": bounded_input}
    if phase["status"] in {"BLOCKED", "REPLAN_REQUIRED"} and phase.get("candidate_commit"):
        _verify_candidate_commit(manifest, phase, task_root)
        if project is None or task_root is None or verifier is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "failed phase resume must revalidate its report and evidence")
        _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        _verify_saved_review_report(project, manifest, phase)
        if stopped := _loop_stop(phase):
            return stopped
        return {"status": "PHASE_REPLAN_REQUIRED" if phase["status"] == "REPLAN_REQUIRED" else "PHASE_BLOCKED",
                "planner_required": False,
                "executor_required": False, "reviewer_required": False,
                "owner_decision_required": phase["status"] == "BLOCKED",
                "findings": (phase.get("review_report") or {}).get("findings", [])}
    executor_required = phase["status"] in {"PLANNED", "EXECUTING", "VERIFYING"}
    return {"status": phase["status"], "planner_required": False,
            "executor_required": executor_required,
            **({"executor_lifecycle": executor_lifecycle_result(manifest, phase, project)}
               if executor_required else {})}


def executor_lifecycle_result(manifest: dict[str, Any], phase: dict[str, Any],
                              project: pathlib.Path | None = None) -> str:
    """Derive current executor authority from phase and recorded dispatch history."""
    active_fix = phase.get("active_local_fix")
    if isinstance(active_fix, dict):
        return "LOCAL_FIX_CONTINUATION_ALLOWED"
    if project is not None:
        try:
            store = devcrew_dispatch._read(
                devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
            for receipt in store["receipts"]:
                if (not isinstance(receipt, dict) or receipt.get("ROLE") != "executor" or
                        receipt.get("TASK_ID") != manifest["task_id"] or
                        receipt.get("PHASE_ID") != phase["phase_id"] or
                        receipt.get("BASE_SHA") != (phase.get("base_sha") or manifest["worktree"]["base_sha"]) or
                        receipt.get("WRITER_CYCLE") != 0 or
                        receipt.get("REJECTED_CANDIDATE_SHA") is not None):
                    continue
                try:
                    devcrew_dispatch.verify_spawn(
                        project, receipt.get("SPAWN_RECEIPT_ID"), task_id=manifest["task_id"],
                        role="executor", phase_id=phase["phase_id"],
                        base_sha=phase.get("base_sha") or manifest["worktree"]["base_sha"],
                        agent_id=receipt.get("AGENT_ID"), writer_cycle=0, rejected_candidate_sha=None)
                except devcrew_dispatch.DispatchError:
                    continue
                return "CURRENT_EXECUTOR_CONTEXT_AUTHORIZED"
        except (OSError, devcrew_dispatch.DispatchError, KeyError, TypeError):
            pass
    return "NEW_CONTEXT_REQUIRED"


def review_target(manifest: dict[str, Any], phase_id: str) -> dict[str, Any]:
    phase = next((item for item in manifest["phases"] if item["phase_id"] == phase_id), None)
    if phase is None or phase_id != manifest["current_phase_id"]:
        _fail("CURRENT_PHASE_ONLY", "only the current phase can enter review")
    candidate = phase.get("candidate_commit")
    if not isinstance(candidate, dict) or phase.get("status") not in {
            "COMMITTED", "REVIEWING", "ACCEPTED", "LOCAL_FIX_REQUIRED", "BLOCKED", "REPLAN_REQUIRED"}:
        _fail("PHASE_NOT_REVIEWABLE", "phase must have an immutable candidate commit")
    result = {"TASK_ID": manifest["task_id"], "PHASE_ID": phase_id,
            "BASE_SHA": candidate["base"], "TARGET_SHA": candidate["sha"],
            "GOAL": phase["goal"], "MAIN_SCENARIO": phase.get("main_scenario"),
            "ACCEPTANCE": list(phase["acceptance"]), "VERIFICATION": list(phase["verification"]),
            "OWNED_SCOPE": list(phase["owned_scope"]),
            "KNOWN_CONTRACTS": list(phase.get("known_contracts", [])),
            "DO_NOT_CHANGE": list(phase.get("do_not_change", [])),
            "INSTRUCTION": "Review only the immutable candidate and relevant accepted task history. Read original sources independently and report any omitted obligation, even when the derived matrix appears complete."}
    intent_context = original_intent_review_context(manifest, phase_id)
    if intent_context is not None:
        result["ORIGINAL_INTENT"] = intent_context
    return result


_INTENT_REVIEW_FIELDS = {"plan_sha256", "result", "source_reads", "unavailable_sources", "decision_ids",
                         "source_outcomes", "obligation_checks", "evidence_sha256"}


def _removed_requirement_review_fragments(intent: dict[str, Any], effective: dict[str, Any]) -> dict[str, set[bytes]]:
    """Return source fragments that must be accounted for each validated removed obligation."""
    original = {row["requirement_id"]: row for row in intent["requirements"]
                if row["kind"] == "explicit_user_obligation"}
    removal_decisions: dict[str, list[dict[str, Any]]] = {}
    for decision in intent["decisions"]:
        for change in decision["changes"]:
            if change["requirement_id"] in original and change["after"] is None:
                removal_decisions.setdefault(change["requirement_id"], []).append(decision)
    assessment = intent["planning_assessment"]
    history = assessment.get("history", [])
    current = assessment.get("assessment")
    addressed: dict[str, set[bytes]] = {}
    for rid, decisions in removal_decisions.items():
        if effective.get(rid) is not None:
            continue
        checked_decisions = {check.get("decision_id") for review in [*history, current]
                             if isinstance(review, dict)
                             for check in review.get("obligation_checks", [])
                             if isinstance(check, dict) and check.get("requirement_id") == rid and
                             check.get("change") == "USER_REMOVED"}
        matching = [decision for decision in decisions if decision["decision_id"] in checked_decisions]
        if not matching:
            continue
        fragments = {_canonical_json(fragment) for fragment in original[rid]["source_fragments"]}
        for decision in matching:
            fragments.update(_canonical_json(fragment) for fragment in decision["source_fragments"])
        addressed[rid] = fragments
    return addressed


def _validate_intent_review(report, manifest, phase, project, task_root, verifier=None):
    """Validate final-review structure and freshness; semantic judgment stays with the reviewer."""
    review = report.get("INTENT_REVIEW") if isinstance(report, dict) else None
    if not isinstance(review, dict) or set(review) != _INTENT_REVIEW_FIELDS:
        _fail("INVALID_INTENT_REVIEW", "final schema-v2 PASS requires the exact INTENT_REVIEW report")
    intent = manifest["original_intent"]
    expected_digest = intent_plan_digest(manifest)
    if review.get("plan_sha256") != expected_digest:
        _fail("STALE_INTENT_REVIEW", "intent review does not bind the current source, decision and plan identities")
    if (not isinstance(review.get("result"), str) or review.get("result") not in {"COMPLETE", "INCOMPLETE"} or
            any(not isinstance(review.get(key), list) for key in
                ("source_reads", "unavailable_sources", "decision_ids", "source_outcomes", "obligation_checks")) or
            not isinstance(review.get("evidence_sha256"), dict)):
        _fail("INVALID_INTENT_REVIEW", "intent review result or collections are malformed")
    verified = {source["source_id"]: source for source in intent["sources"] if source["status"] == "VERIFIED"}
    unavailable = {source["source_id"]: source for source in intent["sources"] if source["status"] == "UNVERIFIED"}
    read_ids, unavailable_ids = set(), set()
    for row in review["source_reads"]:
        if (not isinstance(row, dict) or set(row) != {"source_id", "sha256", "findings"} or
                not isinstance(row.get("source_id"), str) or row.get("source_id") not in verified or
                row.get("sha256") != verified[row["source_id"]]["sha256"] or
                not isinstance(row.get("findings"), str) or not row["findings"].strip() or
                row["source_id"] in read_ids):
            _fail("INVALID_INTENT_REVIEW", "source_reads must identify each verified immutable source")
        read_ids.add(row["source_id"])
    for row in review["unavailable_sources"]:
        if (not isinstance(row, dict) or set(row) != {"source_id", "limitation"} or
                not isinstance(row.get("source_id"), str) or row.get("source_id") not in unavailable or
                not isinstance(row.get("limitation"), str) or
                not row["limitation"].strip() or row["source_id"] in unavailable_ids):
            _fail("INVALID_INTENT_REVIEW", "unavailable_sources must state each unverified source limitation")
        unavailable_ids.add(row["source_id"])
    if read_ids != set(verified) or unavailable_ids != set(unavailable):
        _fail("INVALID_INTENT_REVIEW", "intent review must account for every source exactly once")
    decision_ids = [row.get("decision_id") for row in intent["decisions"]]
    if review["decision_ids"] != decision_ids:
        _fail("STALE_INTENT_REVIEW", "intent review decision identities differ from the active manifest")
    effective = effective_intent_requirements(manifest)
    active = {rid: row for rid, row in effective.items() if row is not None}
    removed_fragments = _removed_requirement_review_fragments(intent, effective)
    accounted_removed = set()
    sources_with_outcomes = set()
    covered = True
    allowed_refs = set(phase.get("candidate_commit", {}).get("evidence", []))
    allowed_refs.update(reference for row in intent["requirements"] for reference in row["evidence_records"])
    referenced = set()
    source_bytes = {}
    for source_id, source in verified.items():
        path = task_root / pathlib.Path(*pathlib.PurePosixPath(source["snapshot"]).parts)
        source_bytes[source_id] = path.read_bytes()
    for row in review["source_outcomes"]:
        fields = {"source_fragments", "expected_outcome", "requirement_ids", "disposition", "rationale", "evidence_records"}
        if (not isinstance(row, dict) or set(row) != fields or
                not isinstance(row.get("disposition"), str) or
                row.get("disposition") not in {"COVERED", "OMITTED", "UNCONFIRMED"} or
                not isinstance(row.get("expected_outcome"), str) or not row["expected_outcome"].strip() or
                not isinstance(row.get("rationale"), str) or not row["rationale"].strip() or
                not isinstance(row.get("requirement_ids"), list) or
                any(not isinstance(rid, str) or (rid not in active and rid not in removed_fragments)
                    for rid in row["requirement_ids"]) or
                len(row["requirement_ids"]) != len(set(row["requirement_ids"])) or
                not isinstance(row.get("evidence_records"), list) or not row["evidence_records"]):
            _fail("INVALID_INTENT_REVIEW", "source outcome is malformed or lacks existing evidence")
        _validate_intent_fragments(row["source_fragments"], source_bytes, error_code="INVALID_INTENT_REVIEW")
        row_fragments = {_canonical_json(fragment) for fragment in row["source_fragments"]}
        for fragment in row["source_fragments"]:
            sources_with_outcomes.add(fragment["source_id"])
        for rid in row["requirement_ids"]:
            if rid in removed_fragments:
                if not removed_fragments[rid].issubset(row_fragments):
                    _fail("INVALID_INTENT_REVIEW", "removed obligation source fragment or removal answer is missing")
                accounted_removed.add(rid)
        if row["disposition"] == "COVERED" and not row["requirement_ids"]:
            _fail("INVALID_INTENT_REVIEW", "covered outcome must cite active requirements")
        if row["disposition"] != "COVERED":
            covered = False
        for reference in row["evidence_records"]:
            referenced.add(_validate_evidence_ref(reference))
    if sources_with_outcomes != set(verified):
        _fail("INVALID_INTENT_REVIEW", "each verified source needs an independently derived outcome")
    if accounted_removed != set(removed_fragments):
        _fail("INVALID_INTENT_REVIEW", "every removed obligation source fragment needs a final COVERED outcome")
    required_obligations = {rid for rid, definition in active.items()
                            if definition["kind"] == "explicit_user_obligation"}
    checked = set()
    for row in review["obligation_checks"]:
        fields = {"requirement_id", "result", "rationale", "evidence_records"}
        rid = row.get("requirement_id") if isinstance(row, dict) else None
        if (not isinstance(row, dict) or set(row) != fields or rid not in required_obligations or rid in checked or
                not isinstance(row.get("result"), str) or row.get("result") not in {"VERIFIED", "UNCONFIRMED"} or
                not isinstance(row.get("rationale"), str) or not row["rationale"].strip() or
                not isinstance(row.get("evidence_records"), list) or not row["evidence_records"]):
            _fail("INVALID_INTENT_REVIEW", "every active obligation needs one evidence-backed review check")
        checked.add(rid)
        if row["result"] != "VERIFIED":
            covered = False
        for reference in row["evidence_records"]:
            referenced.add(_validate_evidence_ref(reference))
    if checked != required_obligations:
        _fail("INVALID_INTENT_REVIEW", "intent review must check every active explicit obligation exactly once")
    if not referenced.issubset(allowed_refs):
        _fail("INVALID_INTENT_REVIEW", "intent review evidence must reference existing candidate or requirement evidence")
    evidence_digests = review["evidence_sha256"]
    if set(evidence_digests) != referenced:
        _fail("INVALID_INTENT_REVIEW", "evidence_sha256 must bind every referenced record exactly once")
    for reference, digest in evidence_digests.items():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            _fail("INVALID_INTENT_REVIEW", "evidence digest is malformed")
        path = project / pathlib.Path(*pathlib.PurePosixPath(reference).parts)
        try:
            data = path.read_bytes()
        except OSError as exc:
            _fail("PHASE_EVIDENCE_MISSING", str(exc))
        if hashlib.sha256(data).hexdigest() != digest:
            _fail("STALE_INTENT_REVIEW", "review evidence bytes changed")
        if verifier is not None:
            try:
                _record, record, host = verifier(str(path), current=False,
                    expected_execution_root=str(task_root), expected_task_id=manifest["task_id"])
            except Exception as exc:
                _fail("INVALID_PHASE_EVIDENCE", str(exc))
            candidate = phase.get("candidate_commit", {})
            if (host.resolve() != project.resolve() or
                    record.get("candidate_tree_after") != candidate.get("tree")):
                _fail("STALE_INTENT_REVIEW", "review evidence is not bound to the final candidate tree")
    complete = (not unavailable and covered and
                all(row.get("disposition") == "COVERED" for row in review["source_outcomes"]) and
                all(row.get("result") == "VERIFIED" for row in review["obligation_checks"]))
    if review["result"] == "COMPLETE" and not complete:
        _fail("INVALID_INTENT_REVIEW", "COMPLETE requires all sources, outcomes, obligations and evidence verified")
    if review["result"] == "INCOMPLETE" and complete:
        _fail("INVALID_INTENT_REVIEW", "INCOMPLETE requires a concrete source, outcome or obligation limitation")
    return review


def _verify_candidate_evidence(project: pathlib.Path, task_root: pathlib.Path, manifest: dict[str, Any],
                              phase: dict[str, Any], verifier) -> None:
    candidate = phase["candidate_commit"]
    contract = candidate.get("verification_contract")
    if not isinstance(contract, dict) or contract != _verification_contract(phase):
        _fail("VERIFICATION_PLAN_CHANGED", "candidate does not certify the accepted verification contract")
    bindings = required_verification(contract)
    matched = set()
    refs = verify_evidence_records(candidate["evidence"], project, task_root,
                                   manifest["task_id"], verifier)
    for ref in refs:
        try:
            _record, data, host = verifier(str(project / ref), current=False,
                                           expected_execution_root=str(task_root),
                                           expected_task_id=manifest["task_id"])
        except Exception as exc:
            _fail("INVALID_PHASE_EVIDENCE", str(exc))
        if (host.resolve() != project.resolve() or data.get("git_head_before") != candidate["base"] or
                data.get("git_head") != candidate["base"] or
                data.get("candidate_tree_before") != candidate["tree"] or
                data.get("candidate_tree_after") != candidate["tree"] or
                data.get("worktree_fingerprint_before") != data.get("worktree_fingerprint_after")):
            _fail("STALE_PHASE_EVIDENCE", "review evidence is not bound to the immutable candidate tree")
        matched.update(i for i, binding in enumerate(bindings) if match_verification(data, binding, contract))
    if matched != set(range(len(bindings))):
        _fail("REQUIRED_CHECK_MISSING", "candidate evidence does not cover all agreed commands")
    if _candidate_evidence_seal(candidate) is not None:
        _verify_candidate_commit_object(manifest, phase, task_root)
        for snapshot in candidate["evidence_snapshots"]:
            record = project / snapshot["record_ref"]
            if hashlib.sha256(record.read_bytes()).hexdigest() != snapshot["sha256"]:
                _fail("CANDIDATE_EVIDENCE_CHANGED", "candidate check record differs from the verified snapshot")


def _candidate_evidence_seal(candidate: dict[str, Any]) -> str | None:
    """Validate the ordered record digests bound into the existing commit identity."""
    if "evidence_snapshots" not in candidate and "evidence_sha256" not in candidate:
        return None  # Historical committed candidates predate evidence sealing.
    snapshots = candidate.get("evidence_snapshots")
    if (not isinstance(snapshots, list) or not snapshots or
            any(not isinstance(item, dict) or set(item) != {"record_ref", "sha256"} or
                not isinstance(item["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                for item in snapshots) or
            [item["record_ref"] for item in snapshots] != candidate.get("evidence") or
            _candidate_digest(snapshots) != candidate.get("evidence_sha256")):
        _fail("CANDIDATE_EVIDENCE_CHANGED", "candidate evidence snapshot references or digest are invalid")
    return candidate["evidence_sha256"]


def _verify_saved_acceptance_evidence(project: pathlib.Path, task_root: pathlib.Path,
                                      manifest: dict[str, Any], phase: dict[str, Any], verifier) -> None:
    _verify_acceptance_evidence(phase.get("acceptance_evidence"), project, task_root,
                                manifest["task_id"], phase["candidate_commit"], verifier)


def _verify_saved_review_report(project: pathlib.Path, manifest: dict[str, Any], phase: dict[str, Any]) -> dict[str, Any]:
    saved = phase.get("review_report")
    if not isinstance(saved, dict):
        _fail("PHASE_REVIEW_REPORT_REQUIRED", "phase has no saved host review report")
    reference = _validate_evidence_ref(saved.get("report_ref"))
    report_path = project / pathlib.Path(*pathlib.PurePosixPath(reference).parts)
    cursor = project
    for part in pathlib.PurePosixPath(reference).parts:
        cursor = cursor / part
        if fs.is_link(cursor):
            _fail("PHASE_REVIEW_REPORT_INVALID", "saved review report path traverses a symlink")
    try:
        report_bytes = report_path.read_bytes()
    except OSError as exc:
        _fail("PHASE_REVIEW_REPORT_MISSING", str(exc))
    digest = hashlib.sha256(report_bytes).hexdigest()
    if digest != saved.get("sha256"):
        _fail("PHASE_REVIEW_REPORT_CHANGED", "saved host review report digest differs from manifest")
    try:
        report = json.loads(report_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("PHASE_REVIEW_REPORT_INVALID", str(exc))
    candidate = phase.get("candidate_commit")
    target = {"TASK_ID": manifest["task_id"], "PHASE_ID": phase["phase_id"],
              "BASE_SHA": candidate.get("base") if isinstance(candidate, dict) else None,
              "TARGET_SHA": candidate.get("sha") if isinstance(candidate, dict) else None}
    if (not isinstance(report, dict) or
            any(report.get(key) != target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")) or
            report.get("VERDICT") != saved.get("verdict") or
            report.get("FINDINGS") != saved.get("findings") or
            report.get("ADVISORIES", []) != saved.get("advisories", []) or
            (saved.get("authoritative") is True and
             (report.get("SPAWN_RECEIPT_ID") != saved.get("spawn_receipt_id") or
              report.get("AGENT_ID") != saved.get("agent_id"))) or
            (saved.get("authoritative") is not True and
             ("SPAWN_RECEIPT_ID" in report or "AGENT_ID" in report))):
        _fail("STALE_PHASE_REVIEW", "host review report identity or result differs from saved candidate report")
    final_phase = bool(manifest.get("phases") and phase.get("phase_id") == manifest["phases"][-1]["phase_id"])
    if manifest.get("schema_version") == 2 and final_phase:
        if report.get("VERDICT") == "PASS" or "INTENT_REVIEW" in report:
            _validate_intent_review(report, manifest, phase, project,
                                    pathlib.Path(manifest["worktree"]["task_worktree_root"]))
    elif "INTENT_REVIEW" in report:
        _fail("INVALID_INTENT_REVIEW", "INTENT_REVIEW is allowed only on the final schema-v2 phase")
    return report


def _archived_cycle_generation(manifest: dict[str, Any], phase: dict[str, Any],
                               cycle_index: int) -> int | None:
    """Return the generation that durably archived this exact rejected cycle.

    Environment-only reapproval preserves a prefix of reviewed cycles.  The
    earliest archive containing this exact cycle pins the generation in which
    its executor receipt was issued; a receipt from any other generation is
    not historical evidence for this lifecycle record.
    """
    history = manifest.get("pre_candidate_reapprovals", [])
    current_cycles = phase.get("review_cycles", [])
    if not isinstance(history, list) or not isinstance(current_cycles, list) or cycle_index >= len(current_cycles):
        return None
    for entry in history:
        previous = entry.get("previous_contract") if isinstance(entry, dict) else None
        archived = previous.get("review_cycles") if isinstance(previous, dict) else None
        if (isinstance(archived, list) and cycle_index < len(archived) and
                archived[cycle_index] == current_cycles[cycle_index]):
            generation = entry.get("prior_generation")
            if type(generation) is not int or generation < 0:
                _fail("INVALID_PRE_CANDIDATE_HISTORY", "archived cycle generation is invalid")
            return generation
    return None


def _verify_review_cycles(project: pathlib.Path, task_root: pathlib.Path, manifest: dict[str, Any],
                          phase: dict[str, Any], verifier=None, recovery_receipt=None) -> None:
    expected_cycle = 0
    expected_rejected = None
    for cycle_index, cycle in enumerate(phase.get("review_cycles", [])):
        candidate, saved = cycle["candidate_commit"], cycle["review_report"]
        pinned = _git(task_root, "rev-parse", "--verify", cycle["backup_ref"], check=False)
        if pinned != candidate["sha"]:
            _fail("PHASE_COMMIT_INCONSISTENT", "historical rejected candidate ref is missing or changed")
        _verify_candidate_commit_object(manifest, {**phase, "candidate_commit": candidate}, task_root,
                                        expected_base=candidate["base"])
        executor_dispatch = cycle.get("executor_dispatch")
        if executor_dispatch is not None:
            if (not isinstance(executor_dispatch, dict) or
                    executor_dispatch.get("writer_cycle") != expected_cycle or
                    executor_dispatch.get("rejected_candidate_sha") != expected_rejected):
                _fail("INVALID_EXECUTOR_ROUTE", "archived cycle writer does not match its repair generation")
            archived_generation = _archived_cycle_generation(manifest, phase, cycle_index)
            if archived_generation is None:
                writer = _verify_dispatch(
                    project, executor_dispatch.get("spawn_receipt_id"), task_id=manifest["task_id"],
                    role="executor", phase_id=phase["phase_id"], base_sha=candidate["base"],
                    writer_cycle=expected_cycle, rejected_candidate_sha=expected_rejected,
                    agent_id=executor_dispatch.get("agent_id"), route_id=executor_dispatch.get("route_id"),
                    model=executor_dispatch.get("model"), effort=executor_dispatch.get("effort"),
                    spawn_mode=executor_dispatch.get("spawn_mode"))
            else:
                worktree = manifest.get("worktree") or {}
                expected_binding = {"repository_id": worktree.get("repository_id"),
                                    "task_id": manifest.get("task_id"),
                                    "task_worktree_root": worktree.get("task_worktree_root"),
                                    "branch": worktree.get("branch"),
                                    "expected_head": candidate["base"]}
                if archived_generation:
                    expected_binding["contract_generation"] = archived_generation
                try:
                    writer = devcrew_dispatch.verify_historical_executor(
                        project, executor_dispatch.get("spawn_receipt_id"), task_id=manifest["task_id"],
                        phase_id=phase["phase_id"], base_sha=candidate["base"],
                        writer_cycle=expected_cycle, rejected_candidate_sha=expected_rejected,
                        generation=archived_generation, executor_dispatch=executor_dispatch,
                        expected_worktree_binding=expected_binding,
                        store_path=devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
                except devcrew_dispatch.DispatchError as exc:
                    _fail("INVALID_EXECUTOR_ROUTE", str(exc))
            expected_dispatch = {"spawn_receipt_id": writer["SPAWN_RECEIPT_ID"],
                                 "writer_cycle": expected_cycle,
                                 "rejected_candidate_sha": expected_rejected,
                                 "route_id": writer["ROUTE_ID"], "agent_id": writer["AGENT_ID"],
                                 "model": writer["RESOLVED_MODEL"], "effort": writer["REASONING_EFFORT"],
                                 "spawn_mode": writer["SPAWN_MODE"]}
            if executor_dispatch != expected_dispatch:
                _fail("INVALID_EXECUTOR_ROUTE", "archived cycle writer summary differs from its receipt")
        findings = saved.get("findings")
        completion = cycle.get("systemic_replan_completion")
        systemic = isinstance(completion, dict)
        if systemic:
            expected_cycle = 0
            expected_rejected = None
        reconsidered = cycle.get("review_correction") is True
        allowed_classes = REVIEW_FAILURES if systemic or reconsidered else {"LOCAL_FIX"}
        if (not isinstance(findings, list) or not findings or
                any(not isinstance(item, dict) or set(item) != {"CLASSIFICATION", "SUMMARY", "EVIDENCE"} or
                    item.get("CLASSIFICATION") not in allowed_classes or
                    not isinstance(item.get("SUMMARY"), str) or not item["SUMMARY"].strip() or
                    not isinstance(item.get("EVIDENCE"), list) or not item["EVIDENCE"] or
                    any(not isinstance(ref, str) or not ref.strip() for ref in item["EVIDENCE"])
                    for item in findings)):
            _fail("PHASE_REVIEW_INCONSISTENT", "only well-formed LOCAL_FIX cycles may authorize repair")
        if not systemic and not reconsidered:
            # Starting a repair after an ordinary local-fix report assigns the next
            # executor the archived review-cycle id, even when legacy history has
            # no dispatch summary. A correction only archives a review event; it
            # does not create a writer generation.
            expected_cycle = cycle["cycle_id"]
            expected_rejected = candidate["sha"]
        reference = _validate_evidence_ref(saved["report_ref"])
        report_path = project / pathlib.Path(*pathlib.PurePosixPath(reference).parts)
        cursor = project
        traverses_symlink = False
        for part in pathlib.PurePosixPath(reference).parts:
            cursor = cursor / part
            if fs.is_link(cursor):
                traverses_symlink = True
                break
        if traverses_symlink or not report_path.is_file():
            _fail("PHASE_REVIEW_REPORT_MISSING", "historical failed review report is unavailable")
        data = report_path.read_bytes()
        if hashlib.sha256(data).hexdigest() != saved["sha256"]:
            _fail("PHASE_REVIEW_REPORT_CHANGED", "historical failed review report digest differs from cycle history")
        try:
            report = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            _fail("PHASE_REVIEW_REPORT_INVALID", str(exc))
        expected = {"TASK_ID": manifest["task_id"], "PHASE_ID": phase["phase_id"],
                    "BASE_SHA": candidate["base"], "TARGET_SHA": candidate["sha"],
                    "VERDICT": "FAIL", "FINDINGS": saved["findings"]}
        if (not isinstance(report, dict) or any(report.get(key) != value for key, value in expected.items()) or
                not valid_review_advisories(saved.get("advisories", [])) or
                report.get("ADVISORIES", []) != saved.get("advisories", []) or
                (systemic and not any(item.get("CLASSIFICATION") == "SYSTEMIC_REPLAN" for item in findings))):
            _fail("STALE_PHASE_REVIEW", "historical failed review no longer matches its candidate cycle")
        if systemic:
            if (completion.get("task_id") != manifest["task_id"] or
                    completion.get("phase_id") != phase["phase_id"] or
                    completion.get("failed_candidate_sha") != candidate["sha"] or
                    completion.get("report_sha256") != saved["sha256"] or
                    completion.get("replacement_contract", {}).get("task_id") != manifest["task_id"] or
                    completion.get("replacement_contract", {}).get("current_phase_id") != phase["phase_id"] or
                    completion.get("failed_contract", {}).get("phase_id") != phase["phase_id"] or
                    hashlib.sha256(json.dumps(
                        ({"failed_contract": completion.get("failed_contract"),
                          "original_intent": completion.get("original_intent")}
                         if manifest.get("schema_version") == 2 else completion.get("failed_contract")),
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() !=
                    completion.get("failed_contract_sha256") or
                    hashlib.sha256(json.dumps(completion.get("replacement_contract"), sort_keys=True,
                                             separators=(",", ":"), ensure_ascii=False).encode()).hexdigest() !=
                    completion.get("replacement_contract_sha256")):
                _fail("PHASE_REVIEW_INCONSISTENT", "systemic replan completion digest or linkage is invalid")
            replacement = completion["replacement_contract"]
            try:
                replacement_receipt = (dict(recovery_receipt) if recovery_receipt is not None else
                                       devcrew_worktree.resolve(project, manifest["task_id"], read_only=True))
            except ValueError as exc:
                _fail("PHASE_COMMIT_INCONSISTENT", str(exc))
            # Historical contract must still be a complete valid planner output.
            replacement_receipt = dict(replacement_receipt, expected_head=manifest["worktree"]["base_sha"])
            validate_manifest(replacement, replacement_receipt, task_root)
            if manifest.get("schema_version") == 2:
                authorization = completion.get("intent_assessment")
                historical = _historical_systemic_manifest(replacement, completion, phase["phase_id"])
                _validate_original_intent(historical, task_root,
                                          {row["phase_id"]: row for row in historical["phases"]})
                reconstructed = json.loads(json.dumps(replacement))
                reconstructed["original_intent"]["decisions"] = json.loads(json.dumps(
                    historical["original_intent"]["decisions"]))
                reconstructed["original_intent"]["planning_assessment"] = json.loads(json.dumps(
                    historical["original_intent"]["planning_assessment"]))
                _install_intent_assessment(reconstructed, authorization, old=historical)
                if reconstructed.get("original_intent") != replacement.get("original_intent"):
                    _fail("PHASE_REVIEW_INCONSISTENT", "historical replacement is not the exact host-validated merge")
                _validate_intent_transition(historical, replacement, authorization=authorization)
            _verify_systemic_planner_history(project, manifest, completion, candidate, replacement_receipt)
        if verifier is None:
            _fail("PHASE_EVIDENCE_VALIDATION_REQUIRED", "archived rejected candidates require verification receipt validation")
        _verify_candidate_evidence(project, task_root, manifest,
                                   {**phase, **candidate.get("verification_contract", {}), "candidate_commit": candidate}, verifier)
        if "continuation" in cycle:
            historical = {**phase, "candidate_commit": candidate, "review_report": saved,
                          "review_cycles": []}
            _verify_continuation(cycle["continuation"], manifest, historical, project, task_root,
                                 verifier, persisted=True)
        elif reconsidered:
            _fail("INVALID_CONTINUATION", "reconsidered review requires preserved evidence")


def _verify_all_review_cycles(project: pathlib.Path, task_root: pathlib.Path, manifest: dict[str, Any], verifier=None,
                              recovery_receipt=None) -> None:
    # Historical executor authority is meaningful only while the archive chain
    # that preserves the corresponding rejected cycle remains authenticated.
    if manifest.get("pre_candidate_reapprovals"):
        _verify_pre_candidate_history(project, manifest)
    for phase in manifest["phases"]:
        if phase.get("review_cycles"):
            _verify_review_cycles(project, task_root, manifest, phase, verifier, recovery_receipt)


def _verify_systemic_planner_history(project, manifest, completion, candidate, recovery_receipt):
    """Validate persisted systemic planner authority against the registered checkout identity."""
    dispatch = completion.get("planner_dispatch")
    replacement = completion.get("replacement_contract")
    generation = _contract_generation(replacement) if isinstance(replacement, dict) else None
    if (not isinstance(dispatch, dict) or not isinstance(replacement, dict) or
            dispatch.get("base_sha") != candidate.get("sha")):
        _fail("INVALID_SYSTEMIC_REPLAN", "historical planner summary differs from failed candidate")
    expected_binding = {"repository_id": recovery_receipt.get("repository_id"),
                        "task_id": manifest.get("task_id"),
                        "task_worktree_root": recovery_receipt.get("task_worktree_root"),
                        "branch": recovery_receipt.get("branch"),
                        "expected_head": candidate.get("sha")}
    if generation:
        expected_binding["contract_generation"] = generation
    try:
        receipt = devcrew_dispatch.verify_historical_planner(
            project, dispatch.get("spawn_receipt_id"), task_id=manifest["task_id"],
            base_sha=candidate["sha"], generation=generation, planner_dispatch=dispatch,
            expected_worktree_binding=expected_binding,
            store_path=devcrew_capacity.dispatch_receipt_path(project, manifest["task_id"]))
    except devcrew_dispatch.DispatchError as exc:
        _fail("INVALID_SYSTEMIC_REPLAN", str(exc))
    if any(dispatch.get(key) != receipt.get(source_key) for key, source_key in (
            ("agent_id", "AGENT_ID"), ("route_id", "ROUTE_ID"), ("model", "RESOLVED_MODEL"),
            ("effort", "REASONING_EFFORT"), ("spawn_mode", "SPAWN_MODE"))):
        _fail("INVALID_SYSTEMIC_REPLAN", "historical planner summary differs from its authoritative receipt")


def _validate_systemic_recovery_checkout(task_root, record, manifest, candidate, base):
    link = manifest.get("worktree", {})
    try:
        actual_head = _git(task_root, "rev-parse", "HEAD")
        branch = _git(task_root, "symbolic-ref", "--short", "HEAD", check=False)
        dirty = _git(task_root, "status", "--porcelain", "--untracked-files=all")
    except PhaseError:
        raise
    if (devcrew_worktree._has_symlink_component(task_root) or pathlib.Path(task_root).resolve() != pathlib.Path(task_root) or
            record.get("repository_id") != link.get("repository_id") or
            record.get("task_worktree_root") != str(task_root) or record.get("branch") != link.get("branch") or
            branch != link.get("branch") or dirty or
            (actual_head, record.get("expected_head")) not in {
                (candidate.get("sha"), candidate.get("sha")),
                (base, candidate.get("sha")), (base, base)}):
        _fail("PHASE_COMMIT_INCONSISTENT", "recovery checkout identity, cleanliness or interrupted-reset state is invalid")


def _historical_systemic_manifest(replacement, completion, phase_id):
    historical = json.loads(json.dumps(replacement))
    historical["original_intent"] = json.loads(json.dumps(completion.get("original_intent")))
    historical_phase = next(row for row in historical["phases"] if row["phase_id"] == phase_id)
    failed_contract = completion.get("failed_contract", {})
    for field in _REAPPROVAL_FIELDS:
        if field not in failed_contract:
            historical_phase.pop(field, None)
    historical_phase.update(json.loads(json.dumps(failed_contract)))
    return historical


def start_local_fix(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                    project: pathlib.Path, task_root: pathlib.Path, phase_id: str, verifier,
                    *, owner_decision: str | None = None, continuation=None) -> dict[str, Any]:
    """Preserve the rejected snapshot, then resume this phase from its immutable base."""
    validate_manifest(manifest, receipt, task_root)
    phase = next((item for item in manifest["phases"] if item["phase_id"] == phase_id), None)
    if (phase is None or phase_id != manifest["current_phase_id"] or
            phase["status"] != "LOCAL_FIX_REQUIRED"):
        _fail("LOCAL_FIX_NOT_REQUIRED", "local-fix start requires the current phase's LOCAL_FIX_REQUIRED state")
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    phase_index = next(i for i, item in enumerate(manifest["phases"]) if item["phase_id"] == phase_id)
    if phase_index:
        _verify_accepted_history(manifest, project, task_root, verifier,
                                 expected_current_head=phase["candidate_commit"]["sha"])
    _verify_candidate_commit(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    report = _verify_saved_review_report(project, manifest, phase)
    if continuation is not None:
        continuation = _verify_continuation(continuation, manifest, phase, project, task_root, verifier)
    decision = _repair_owner_decision(phase, owner_decision, continuation)
    rejected = phase["candidate_commit"]
    cycle = {"cycle_id": len(phase.get("review_cycles", [])) + 1,
             "candidate_commit": json.loads(json.dumps(rejected)),
             "review_report": json.loads(json.dumps(phase["review_report"]))}
    if isinstance(phase.get("executor_dispatch"), dict):
        cycle["executor_dispatch"] = json.loads(json.dumps(phase["executor_dispatch"]))
    if decision is not None:
        cycle["owner_decision"] = decision
    if continuation is not None:
        cycle["continuation"] = continuation
    cycle["backup_ref"] = _rejected_candidate_ref(manifest["task_id"], phase_id, cycle["cycle_id"])
    base = rejected["base"]
    updated = json.loads(json.dumps(manifest))
    target = next(item for item in updated["phases"] if item["phase_id"] == phase_id)
    backup_ref = cycle["backup_ref"]

    def preserve_candidate():
        backup = subprocess.run(["git", "-C", str(task_root), "update-ref", backup_ref,
                                 rejected["sha"], "0" * len(rejected["sha"])],
                                capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
        if backup.returncode and _git(task_root, "rev-parse", "--verify", backup_ref, check=False) != rejected["sha"]:
            _fail("PHASE_COMMIT_INCONSISTENT", (backup.stderr or backup.stdout).strip())

    target["local_fix_transition"] = {"cycle": cycle, "base_sha": base,
                                      "rejected_candidate_sha": rejected["sha"],
                                      "backup_ref": backup_ref}
    _atomic_json(path, updated, before_save=preserve_candidate)
    return recover_local_fix(project, manifest["task_id"], path=path, task_root=task_root,
                             verifier=verifier)


def complete_systemic_replan(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                             project: pathlib.Path, task_root: pathlib.Path,
                             replacement_contract: dict[str, Any], planner_spawn_receipt_id: str,
                             verifier=None, *, owner_decision: str | None = None, continuation=None,
                             intent_assessment=None) -> dict[str, Any]:
    """Persist and finish a validated replacement contract for a systemic review failure."""
    phase_id = manifest.get("current_phase_id")
    phase = next((item for item in manifest.get("phases", []) if item.get("phase_id") == phase_id), None)
    if phase is None or phase.get("status") != "REPLAN_REQUIRED":
        _fail("PHASE_REPLAN_NOT_REQUIRED", "systemic replanning requires the halted current phase")
    candidate = phase.get("candidate_commit")
    if not isinstance(candidate, dict):
        _fail("PHASE_REVIEW_INCONSISTENT", "halted phase has no failed candidate")
    _verify_candidate_commit(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    _verify_saved_review_report(project, manifest, phase)
    saved = phase["review_report"]
    if saved.get("verdict") != "FAIL" or not any(
            item.get("CLASSIFICATION") == "SYSTEMIC_REPLAN" for item in saved.get("findings", [])):
        _fail("PHASE_REPLAN_NOT_REQUIRED", "current failed report does not require systemic replanning")
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    if continuation is not None:
        continuation = _verify_continuation(continuation, manifest, phase, project, task_root, verifier)
    decision = _repair_owner_decision(phase, owner_decision, continuation)
    try:
        proposed = json.loads(json.dumps(replacement_contract))
    except (TypeError, ValueError):
        _fail("INVALID_REPLACEMENT_CONTRACT", "replacement contract must be JSON data")
    if (not isinstance(proposed, dict) or proposed.get("task_id") != manifest["task_id"] or
            proposed.get("current_phase_id") != phase_id or proposed.get("task_mode") != manifest["task_mode"] or
            proposed.get("semantic_contract") != manifest.get("semantic_contract") or
            proposed.get("worktree") != manifest.get("worktree") or
            not isinstance(proposed.get("phases"), list) or
            [item.get("phase_id") for item in proposed["phases"] if isinstance(item, dict)] !=
            [item["phase_id"] for item in manifest["phases"]]):
        _fail("INVALID_REPLACEMENT_CONTRACT", "replacement must preserve task, phase order, semantics, and worktree linkage")
    lifecycle_fields = {"candidate_commit", "review_report", "review_cycles", "active_local_fix",
                        "local_fix_transition", "systemic_replan_transition", "accepted_sha", "evidence"}
    for old, new in zip(manifest["phases"], proposed["phases"]):
        if old["phase_id"] != phase_id:
            if old != new:
                _fail("INVALID_REPLACEMENT_CONTRACT", "replacement may change only the current phase contract")
            continue
        if lifecycle_fields.intersection(new):
            _fail("INVALID_REPLACEMENT_CONTRACT", "planner replacement cannot supply host-owned lifecycle fields")
    revised = next(item for item in proposed["phases"] if item["phase_id"] == phase_id)
    if revised.get("status") != "PLANNED" or revised.get("depends_on") != phase.get("depends_on"):
        _fail("INVALID_REPLACEMENT_CONTRACT", "replacement current phase must be PLANNED with unchanged dependencies")
    pin_verification(revised)
    if proposed.get("schema_version") == 2:
        _install_intent_assessment(proposed, intent_assessment, old=manifest)
    elif intent_assessment is not None:
        _fail("INVALID_INTENT_ASSESSMENT", "systemic intent assessment input is supported only for schema v2")
    # Validate the complete proposed manifest against authenticated worktree identity.
    proposed["planner_dispatch"] = manifest["planner_dispatch"]
    proposed["phases"][[item["phase_id"] for item in proposed["phases"]].index(phase_id)]["base_sha"] = phase.get("base_sha") or manifest["worktree"]["base_sha"]
    proposed_receipt = dict(receipt, expected_head=manifest["worktree"]["base_sha"])
    validate_manifest(proposed, proposed_receipt, task_root)
    if proposed.get("schema_version") == 2:
        _validate_intent_transition(manifest, proposed, authorization=intent_assessment)
    spawn = _verify_dispatch(project, planner_spawn_receipt_id, task_id=manifest["task_id"], role="planner",
                             base_sha=candidate["sha"])
    _verify_planner_source_binding(project, proposed, spawn)
    cycle_id = len(phase.get("review_cycles", [])) + 1
    backup_ref = _rejected_candidate_ref(manifest["task_id"], phase_id, cycle_id)
    completion = {"task_id": manifest["task_id"], "phase_id": phase_id,
                  "failed_candidate_sha": candidate["sha"], "report_sha256": saved["sha256"],
                  "failed_contract": {key: json.loads(json.dumps(phase[key])) for key in
                                      ("phase_id", "goal", "main_scenario", "depends_on", "owned_scope", "acceptance",
                                       "verification", "verification_bindings", "verification_environment_sha256",
                                       "verification_approved_at", "verification_sha256", "commit_boundary", "commit_subject", "known_contracts",
                                       "do_not_change") if key in phase},
                  "replacement_contract": proposed,
                  "replacement_contract_sha256": hashlib.sha256(json.dumps(
                      proposed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest(),
                  "planner_dispatch": {"spawn_receipt_id": planner_spawn_receipt_id,
                                       "agent_id": spawn["AGENT_ID"], "route_id": spawn["ROUTE_ID"],
                                       "model": spawn["RESOLVED_MODEL"], "effort": spawn["REASONING_EFFORT"],
                                       "spawn_mode": spawn["SPAWN_MODE"], "base_sha": candidate["sha"]}}
    if manifest.get("schema_version") == 2:
        completion["original_intent"] = json.loads(json.dumps(manifest["original_intent"]))
        completion["intent_assessment"] = json.loads(json.dumps(intent_assessment))
        failed_projection = {"failed_contract": completion["failed_contract"],
                             "original_intent": completion["original_intent"]}
    else:
        failed_projection = completion["failed_contract"]
    completion["failed_contract_sha256"] = hashlib.sha256(json.dumps(
        failed_projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    cycle = {"cycle_id": cycle_id, "candidate_commit": json.loads(json.dumps(candidate)),
             "review_report": json.loads(json.dumps(saved)), "backup_ref": backup_ref,
             "systemic_replan_completion": completion}
    if decision is not None:
        cycle["owner_decision"] = decision
    if continuation is not None:
        cycle["continuation"] = continuation

    def preserve_candidate():
        backup = subprocess.run(["git", "-C", str(task_root), "update-ref", backup_ref,
                                 candidate["sha"], "0" * len(candidate["sha"])], capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
        if backup.returncode and _git(task_root, "rev-parse", "--verify", backup_ref, check=False) != candidate["sha"]:
            _fail("PHASE_COMMIT_INCONSISTENT", (backup.stderr or backup.stdout).strip())

    pending = json.loads(json.dumps(manifest))
    target = next(item for item in pending["phases"] if item["phase_id"] == phase_id)
    target["systemic_replan_transition"] = {"cycle": cycle, "base_sha": candidate["base"],
                                             "replacement_contract": proposed}
    _atomic_json(path, pending, before_save=preserve_candidate,
                 authorization=intent_assessment if manifest.get("schema_version") == 2 else None,
                 _systemic_replacement=proposed if manifest.get("schema_version") == 2 else None)
    return recover_systemic_replan(project, manifest["task_id"], path=path, task_root=task_root,
                                   verifier=verifier)


def recover_systemic_replan(project: pathlib.Path, task_id: str, *, path: pathlib.Path | None = None,
                            task_root: pathlib.Path | None = None, verifier=None) -> dict[str, Any] | None:
    path = path or manifest_path(project, task_id)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("PHASE_MANIFEST_REQUIRED", f"cannot recover phase manifest: {exc}")
    phase = next((item for item in manifest.get("phases", [])
                  if item.get("phase_id") == manifest.get("current_phase_id")), None)
    transition = phase.get("systemic_replan_transition") if isinstance(phase, dict) else None
    if not isinstance(transition, dict):
        return None
    cycle = transition.get("cycle")
    candidate = phase.get("candidate_commit")
    if (phase.get("status") != "REPLAN_REQUIRED" or not isinstance(candidate, dict) or
            not isinstance(cycle, dict) or cycle.get("candidate_commit") != candidate or
            cycle.get("review_report") != phase.get("review_report") or
            cycle.get("cycle_id") != len(phase.get("review_cycles", [])) + 1 or
            cycle.get("backup_ref") != _rejected_candidate_ref(task_id, phase["phase_id"], cycle["cycle_id"]) or
            transition.get("base_sha") != candidate.get("base")):
        _fail("PHASE_COMMIT_INCONSISTENT", "interrupted systemic replan does not match failed candidate")
    try:
        repository_root, _, identity, authoritative_root, branch, state_path = devcrew_worktree.location(project, task_id)
        record = devcrew_worktree._read(state_path, identity, task_id, authoritative_root, branch)
        if record is None:
            _fail("PHASE_COMMIT_INCONSISTENT", "interrupted systemic replan worktree receipt is invalid")
        receipt = devcrew_worktree._receipt(record, "RESOLVED")
    except ValueError as exc:
        _fail("PHASE_COMMIT_INCONSISTENT", str(exc))
    if authoritative_root != repository_root and not record.get("worktree_request"):
        _fail("WORKTREE_REQUEST_REQUIRED", "recovery requires the explicit user worktree request")
    task_root = authoritative_root
    if pathlib.Path(manifest["worktree"]["task_worktree_root"]) != task_root:
        _fail("PHASE_COMMIT_INCONSISTENT", "systemic replan task root differs from registered worktree")
    base = candidate["base"]
    _validate_systemic_recovery_checkout(task_root, record, manifest, candidate, base)
    validate_manifest(manifest, receipt, task_root)
    _verify_all_review_cycles(project, task_root, manifest, verifier, recovery_receipt=receipt)
    _verify_candidate_commit_object(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    _verify_saved_review_report(project, manifest, phase)
    continuation = cycle.get("continuation")
    if continuation is not None:
        _verify_continuation(continuation, manifest, phase, project, task_root, verifier, persisted=True)
    _repair_owner_decision(phase, cycle.get("owner_decision"), continuation)
    backup_ref, rejected_sha = cycle["backup_ref"], candidate["sha"]
    replacement = transition.get("replacement_contract")
    authorization = cycle.get("systemic_replan_completion", {}).get("intent_assessment")
    if manifest.get("schema_version") == 2:
        completion = cycle.get("systemic_replan_completion", {})
        if (completion.get("original_intent") != manifest.get("original_intent") or
                replacement != completion.get("replacement_contract") or
                hashlib.sha256(json.dumps(replacement, sort_keys=True, separators=(",", ":"),
                                   ensure_ascii=False).encode()).hexdigest() !=
                completion.get("replacement_contract_sha256")):
            _fail("INVALID_SYSTEMIC_REPLAN", "pending replacement or old intent identity changed")
        historical = _historical_systemic_manifest(replacement, completion, phase["phase_id"])
        _validate_original_intent(historical, task_root,
                                  {row["phase_id"]: row for row in historical["phases"]})
        replacement_receipt = dict(receipt, expected_head=manifest["worktree"]["base_sha"])
        validate_manifest(replacement, replacement_receipt, task_root)
        reconstructed = json.loads(json.dumps(replacement))
        reconstructed["original_intent"]["decisions"] = json.loads(json.dumps(
            historical["original_intent"]["decisions"]))
        reconstructed["original_intent"]["planning_assessment"] = json.loads(json.dumps(
            historical["original_intent"]["planning_assessment"]))
        _install_intent_assessment(reconstructed, authorization, old=historical)
        if reconstructed.get("original_intent") != replacement.get("original_intent"):
            _fail("INVALID_SYSTEMIC_REPLAN", "pending replacement does not match its host assessment merge")
        _validate_intent_transition(historical, replacement, authorization=authorization)
    completion = cycle.get("systemic_replan_completion", {})
    if _contract_generation(replacement) != _contract_generation(manifest):
        _fail("INVALID_SYSTEMIC_REPLAN", "pending replacement generation differs from current manifest")
    _verify_systemic_planner_history(project, manifest, completion, candidate, receipt)
    if _git(task_root, "rev-parse", "--verify", backup_ref, check=False) != rejected_sha:
        _fail("PHASE_COMMIT_INCONSISTENT", "rejected candidate recovery ref is missing or changed")
    head = _git(task_root, "rev-parse", "HEAD")
    if _git(task_root, "symbolic-ref", "--short", "HEAD", check=False) != manifest["worktree"]["branch"] or _git(task_root, "status", "--porcelain", "--untracked-files=all"):
        _fail("PHASE_COMMIT_INCONSISTENT", "interrupted systemic replan checkout is changed or on another branch")
    updated = json.loads(json.dumps(manifest))
    target = next(item for item in updated["phases"] if item["phase_id"] == updated["current_phase_id"])
    target.pop("systemic_replan_transition")
    replacement_phase = next(item for item in replacement["phases"]
                             if item["phase_id"] == target["phase_id"])
    for key in ("goal", "main_scenario", "owned_scope", "acceptance", "verification", "verification_bindings",
                "verification_environment_sha256", "verification_approved_at", "verification_sha256", "commit_boundary", "commit_subject",
                "known_contracts", "do_not_change"):
        if key in replacement_phase:
            target[key] = replacement_phase[key]
        else:
            target.pop(key, None)
    target["base_sha"] = base
    if updated.get("schema_version") == 2:
        updated["original_intent"] = json.loads(json.dumps(replacement["original_intent"]))
    target["candidate_commit"] = None
    target["review_report"] = None
    target["review_start"] = None
    target["evidence"] = []
    target.pop("executor_dispatch", None)
    target.pop("active_local_fix", None)
    target.pop("accepted_sha", None)
    target.setdefault("review_cycles", []).append(cycle)
    target["status"] = "PLANNED"
    proposed_receipt = dict(receipt, expected_head=base)
    validate_manifest(updated, proposed_receipt, task_root)

    def finish_transition():
        locked_record = record
        if manifest.get("schema_version") == 2:
            try:
                locked = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                _fail("PHASE_MANIFEST_REQUIRED", f"cannot revalidate pending systemic intent: {exc}")
            locked_phase = next(row for row in locked["phases"] if row["phase_id"] == phase["phase_id"])
            if (locked.get("original_intent") != manifest.get("original_intent") or
                    locked_phase.get("systemic_replan_transition") != transition):
                _fail("PHASE_MANIFEST_CONFLICT", "pending systemic intent changed before recovery effects")
            locked_record = devcrew_worktree._read(state_path, identity, task_id, authoritative_root, branch)
            if locked_record is None:
                _fail("PHASE_COMMIT_INCONSISTENT", "registered recovery receipt changed before transition effects")
            _validate_systemic_recovery_checkout(task_root, locked_record, manifest, candidate, base)
            locked_receipt = devcrew_worktree._receipt(locked_record, "RESOLVED")
            # Recheck source bytes, host authorization and planner receipt at the
            # locked CAS boundary immediately before reset/ref/receipt changes.
            _validate_original_intent(historical, task_root,
                                      {row["phase_id"]: row for row in historical["phases"]})
            _validate_intent_transition(historical, replacement, authorization=authorization)
            completion = cycle["systemic_replan_completion"]
            _verify_systemic_planner_history(project, manifest, completion, candidate, locked_receipt)
        head = _git(task_root, "rev-parse", "HEAD")
        _validate_systemic_recovery_checkout(task_root, locked_record, manifest, candidate, base)
        if head == rejected_sha:
            result = subprocess.run(["git", "-C", str(task_root), "reset", "--hard", base], capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
            if result.returncode:
                _fail("PHASE_COMMIT_INCONSISTENT", (result.stderr or result.stdout).strip())
        elif head != base:
            _fail("PHASE_COMMIT_INCONSISTENT", "interrupted systemic replan checkout is neither failed candidate nor phase base")
        try:
            devcrew_worktree.advance_expected_head(project, task_id, rejected_sha, base)
        except ValueError as exc:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                state = {}
            if state.get("expected_head") != base:
                _fail("PHASE_COMMIT_INCONSISTENT", str(exc))
        fresh = devcrew_worktree.resolve(project, task_id)
        validate_manifest(updated, fresh, task_root)

    _atomic_json(path, updated, before_save=finish_transition,
                 authorization=authorization if manifest.get("schema_version") == 2 else None,
                 _systemic_replacement=replacement if manifest.get("schema_version") == 2 else None)
    return {"status": "PHASE_REPLAN_COMPLETED", "planner_required": False,
            "executor_required": True, "executor_role": "executor",
            "executor_input": executor_input(updated, task_root, project, verifier)}


def _rejected_candidate_ref(task_id: str, phase_id: str, cycle_id: int) -> str:
    safe_phase = hashlib.sha256(phase_id.encode("utf-8")).hexdigest()[:16]
    return f"refs/devcrew/rejected/{task_id}/{safe_phase}/cycle-{cycle_id}"


def recover_local_fix(project: pathlib.Path, task_id: str, *, path: pathlib.Path | None = None,
                      task_root: pathlib.Path | None = None, verifier=None) -> dict[str, Any] | None:
    """Finish a durable local-fix transition after interruption at a Git reset boundary."""
    path = path or manifest_path(project, task_id)
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("PHASE_MANIFEST_REQUIRED", f"cannot recover phase manifest: {exc}")
    phase = next((item for item in manifest.get("phases", [])
                  if item.get("phase_id") == manifest.get("current_phase_id")), None)
    transition = phase.get("local_fix_transition") if isinstance(phase, dict) else None
    if not isinstance(transition, dict):
        return None
    rejected_record = phase.get("candidate_commit")
    cycle = transition.get("cycle")
    expected_ref = _rejected_candidate_ref(task_id, phase["phase_id"], len(phase.get("review_cycles", [])) + 1)
    if (not isinstance(rejected_record, dict) or rejected_record.get("sha") != transition.get("rejected_candidate_sha") or
            rejected_record.get("base") != transition.get("base_sha") or
            not isinstance(cycle, dict) or cycle.get("candidate_commit") != rejected_record or
            cycle.get("review_report") != phase.get("review_report") or
            cycle.get("cycle_id") != len(phase.get("review_cycles", [])) + 1 or
            cycle.get("backup_ref") != expected_ref or transition.get("backup_ref") != expected_ref):
        _fail("PHASE_COMMIT_INCONSISTENT", "interrupted local-fix transition does not match its rejected candidate")
    try:
        repository_root, _, identity, authoritative_task_root, branch, state_path = devcrew_worktree.location(project, task_id)
    except ValueError as exc:
        _fail("PHASE_COMMIT_INCONSISTENT", str(exc))
    if pathlib.Path(manifest.get("worktree", {}).get("task_worktree_root", "")) != authoritative_task_root:
        _fail("PHASE_COMMIT_INCONSISTENT", "interrupted local-fix task root differs from host worktree identity")
    task_root = authoritative_task_root
    try:
        # During the reset/receipt-update interruption window, resolve() correctly
        # rejects the branch because its durable expected_head still names the
        # rejected commit. Validate the manifest against that authenticated durable
        # receipt without requiring the worktree and receipt heads to agree yet.
        record = devcrew_worktree._read(state_path, identity, task_id, authoritative_task_root, branch)
        if record is None:
            _fail("PHASE_COMMIT_INCONSISTENT", "interrupted local-fix worktree receipt is invalid")
        receipt = devcrew_worktree._receipt(record, "RESOLVED")
    except (OSError, ValueError) as exc:
        _fail("PHASE_COMMIT_INCONSISTENT", str(exc))
    if authoritative_task_root != repository_root and not record.get("worktree_request"):
        _fail("WORKTREE_REQUEST_REQUIRED", "recovery requires the explicit user worktree request")
    validate_manifest(manifest, receipt, task_root)
    if phase.get("status") != "LOCAL_FIX_REQUIRED":
        _fail("LOCAL_FIX_NOT_REQUIRED", "interrupted local-fix transition no longer has LOCAL_FIX_REQUIRED status")
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    rejected_sha, base = transition["rejected_candidate_sha"], transition["base_sha"]
    head = _git(task_root, "rev-parse", "HEAD")
    branch = _git(task_root, "symbolic-ref", "--short", "HEAD", check=False)
    dirty = _git(task_root, "status", "--porcelain", "--untracked-files=all")
    expected_branch = manifest.get("worktree", {}).get("branch")
    if branch != expected_branch:
        _fail("PHASE_COMMIT_INCONSISTENT", "interrupted local-fix checkout is on a different branch")
    if dirty:
        _fail("PHASE_COMMIT_INCONSISTENT", "interrupted local-fix checkout contains uncommitted changes")
    phase_index = next(i for i, item in enumerate(manifest["phases"]) if item["phase_id"] == phase["phase_id"])
    if phase_index:
        _verify_accepted_history(manifest, project, task_root, verifier,
                                 expected_current_head=head)
    _verify_candidate_commit_object(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    saved_report = _verify_saved_review_report(project, manifest, phase)
    findings = saved_report.get("FINDINGS")
    if (saved_report.get("VERDICT") != "FAIL" or not isinstance(findings, list) or not findings or
            any(not isinstance(item, dict) or item.get("CLASSIFICATION") != "LOCAL_FIX"
                for item in findings)):
        _fail("LOCAL_FIX_NOT_REQUIRED", "interrupted local-fix report must contain only LOCAL_FIX findings")
    continuation = transition["cycle"].get("continuation")
    if continuation is not None:
        _verify_continuation(continuation, manifest, phase, project, task_root, verifier, persisted=True)
    _repair_owner_decision(phase, transition["cycle"].get("owner_decision"), continuation)
    backup = _git(task_root, "rev-parse", "--verify", transition["backup_ref"], check=False)
    if backup != rejected_sha:
        _fail("PHASE_COMMIT_INCONSISTENT", "rejected candidate recovery ref is missing or changed")
    updated = json.loads(json.dumps(manifest))

    def finish_transition():
        head = _git(task_root, "rev-parse", "HEAD")
        if (_git(task_root, "symbolic-ref", "--short", "HEAD", check=False) != manifest["worktree"]["branch"] or
                _git(task_root, "status", "--porcelain", "--untracked-files=all")):
            _fail("PHASE_COMMIT_INCONSISTENT", "recovery checkout changed before saving the transition")
        if head == rejected_sha:
            reset = subprocess.run(["git", "-C", str(task_root), "reset", "--hard", base],
                                   capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
            if reset.returncode:
                _fail("PHASE_COMMIT_INCONSISTENT", (reset.stderr or reset.stdout).strip())
        elif head != base:
            _fail("PHASE_COMMIT_INCONSISTENT", "interrupted local-fix checkout is neither rejected candidate nor phase base")
        try:
            devcrew_worktree.advance_expected_head(project, task_id, rejected_sha, base)
        except ValueError as exc:
            try:
                *_, state_path = devcrew_worktree.location(project, task_id)
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (ValueError, OSError, UnicodeDecodeError, json.JSONDecodeError):
                state = {}
            if state.get("expected_head") != base:
                _fail("PHASE_COMMIT_INCONSISTENT", str(exc))
        fresh_receipt = devcrew_worktree.resolve(project, task_id)
        target = next(item for item in updated["phases"] if item["phase_id"] == updated["current_phase_id"])
        transition = target.pop("local_fix_transition")
        cycle = transition["cycle"]
        target.setdefault("review_cycles", []).append(cycle)
        target["candidate_commit"] = None
        target["review_report"] = None
        target["review_start"] = None
        target["evidence"] = []
        target["active_local_fix"] = {"cycle_id": cycle["cycle_id"],
                                       "rejected_candidate_sha": rejected_sha,
                                       "findings": cycle["review_report"]["findings"]}
        target["status"] = "EXECUTING"
        validate_manifest(updated, fresh_receipt, task_root)

    _atomic_json(path, updated, before_save=finish_transition)
    return {"status": "PHASE_LOCAL_FIX_DISPATCH_READY", "planner_required": False,
            "executor_required": True, "executor_role": "executor",
            "executor_input": executor_input(updated, task_root, project, verifier)}


def _verify_accepted_history(manifest: dict[str, Any], project: pathlib.Path, task_root: pathlib.Path,
                             verifier, *, include_current_accepted: bool = False,
                             require_clean: bool = False,
                             expected_current_head: str | None = None) -> None:
    """Revalidate the linear accepted commit/report/evidence chain before later-phase use."""
    current_index = next(i for i, phase in enumerate(manifest["phases"])
                         if phase["phase_id"] == manifest["current_phase_id"])
    current = manifest["phases"][current_index]
    if current_index:
        expected_current_base = manifest["worktree"]["base_sha"]
        for predecessor in manifest["phases"][:current_index]:
            if predecessor.get("status") != "ACCEPTED":
                _fail("PHASE_ACCEPTED_HISTORY_INCONSISTENT", "every predecessor phase must remain ACCEPTED")
            expected_current_base = predecessor.get("accepted_sha")
        current_candidate = current.get("candidate_commit")
        if (current.get("base_sha") != expected_current_base or
                (isinstance(current_candidate, dict) and current_candidate.get("base") != expected_current_base)):
            _fail("PHASE_ACCEPTED_HISTORY_INCONSISTENT",
                  "current phase base and candidate must extend the preceding accepted SHA")
    accepted_end = current_index if include_current_accepted and current["status"] == "ACCEPTED" else current_index - 1
    expected_base = manifest["worktree"]["base_sha"]
    for phase in manifest["phases"][:accepted_end + 1]:
        if phase["status"] != "ACCEPTED":
            _fail("PHASE_ACCEPTED_HISTORY_INCONSISTENT", "every predecessor phase must remain ACCEPTED")
        candidate = phase.get("candidate_commit")
        if (not isinstance(candidate, dict) or phase.get("accepted_sha") != candidate.get("sha") or
                candidate.get("base") != expected_base):
            _fail("PHASE_ACCEPTED_HISTORY_INCONSISTENT", "accepted SHA or candidate parent breaks the phase chain")
        _verify_candidate_commit_object(manifest, phase, task_root, expected_base)
        _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
        _verify_saved_review_report(project, manifest, phase)
        _verify_saved_acceptance_evidence(project, task_root, manifest, phase, verifier)
        expected_base = phase["accepted_sha"]
    current_candidate = current.get("candidate_commit")
    if current["status"] == "ACCEPTED" and current_index == accepted_end:
        expected_head = current["accepted_sha"]
    elif current["status"] in {"COMMITTED", "REVIEWING", "LOCAL_FIX_REQUIRED", "BLOCKED", "REPLAN_REQUIRED"} and isinstance(current_candidate, dict):
        expected_head = current_candidate["sha"]
    else:
        expected_head = current.get("base_sha") or expected_base
    try:
        actual_head = _git(task_root, "rev-parse", "HEAD")
        branch = _git(task_root, "symbolic-ref", "--short", "HEAD")
        dirty = _git(task_root, "status", "--porcelain", "--untracked-files=all")
    except PhaseError as exc:
        _fail("PHASE_ACCEPTED_HISTORY_INCONSISTENT", exc.message)
    if expected_current_head is not None:
        expected_head = expected_current_head
    if actual_head != expected_head or branch != manifest["worktree"]["branch"]:
        _fail("PHASE_ACCEPTED_HISTORY_INCONSISTENT", "task HEAD or branch differs from the accepted phase boundary")
    if require_clean and dirty:
        _fail("PHASE_ACCEPTED_BOUNDARY_DIRTY", "task worktree is dirty at the accepted phase boundary")


def reconsider_review(path, manifest, receipt, project, task_root, phase_id, verifier, continuation):
    """Preserve a disputed FAIL and reopen independent review of the unchanged candidate."""
    validate_manifest(manifest, receipt, task_root)
    phase = next((p for p in manifest["phases"] if p["phase_id"] == phase_id), None)
    if phase is None or phase_id != manifest["current_phase_id"] or phase["status"] not in {
            "LOCAL_FIX_REQUIRED", "REPLAN_REQUIRED", "BLOCKED"}:
        _fail("PHASE_NOT_REVIEWABLE", "reconsideration requires a failed current review")
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    if manifest["phases"].index(phase):
        _verify_accepted_history(manifest, project, task_root, verifier,
                                 expected_current_head=phase["candidate_commit"]["sha"])
    _verify_candidate_commit(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    _verify_saved_review_report(project, manifest, phase)
    assessment = _verify_continuation(continuation, manifest, phase, project, task_root, verifier)
    cycle_id = len(phase.get("review_cycles", [])) + 1
    backup_ref = _rejected_candidate_ref(manifest["task_id"], phase_id, cycle_id)
    cycle = {"cycle_id": cycle_id, "candidate_commit": phase["candidate_commit"],
             "review_report": phase["review_report"], "backup_ref": backup_ref,
             "review_correction": True, "continuation": assessment}
    updated = json.loads(json.dumps(manifest))
    target = next(p for p in updated["phases"] if p["phase_id"] == phase_id)
    target.setdefault("review_cycles", []).append(cycle)
    target.pop("review_report", None)
    target.pop("review_start", None)
    target["status"] = "COMMITTED"
    validate_manifest(updated, receipt, task_root)

    def preserve_candidate():
        _git(task_root, "update-ref", backup_ref, phase["candidate_commit"]["sha"],
             "0" * len(phase["candidate_commit"]["sha"]))

    _atomic_json(path, updated, before_save=preserve_candidate)
    return {"status": "PHASE_REVIEW_RECONSIDERATION_READY", "reviewer_required": True,
            "executor_required": False, "owner_decision_required": False,
            "target_sha": phase["candidate_commit"]["sha"]}


def begin_review(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                 project: pathlib.Path, task_root: pathlib.Path, phase_id: str, verifier) -> dict[str, Any]:
    validate_manifest(manifest, receipt, task_root)
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    phase = next((item for item in manifest["phases"] if item["phase_id"] == phase_id), None)
    if phase is None or phase_id != manifest["current_phase_id"] or phase["status"] not in {"COMMITTED", "REVIEWING"}:
        _fail("PHASE_NOT_REVIEWABLE", "review start requires the current COMMITTED candidate")
    _verify_candidate_commit(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    target = review_target(manifest, phase_id)
    if phase["status"] == "COMMITTED":
        updated = json.loads(json.dumps(manifest))
        updated_phase = next(p for p in updated["phases"] if p["phase_id"] == phase_id)
        updated_phase["status"] = "REVIEWING"
        updated_phase["review_start"] = {key: target[key] for key in
                                          ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")}
        validate_manifest(updated, receipt, task_root)
        _atomic_json(path, updated)
    return {"status": "PHASE_REVIEW_READY", "review_target": target}


def valid_review_advisories(advisories: Any) -> bool:
    return isinstance(advisories, list) and all(
        isinstance(item, dict) and set(item) == {"SUMMARY", "EVIDENCE"} and
        isinstance(item["SUMMARY"], str) and bool(item["SUMMARY"].strip()) and
        isinstance(item["EVIDENCE"], list) and bool(item["EVIDENCE"]) and
        all(isinstance(ref, str) and ref.strip() for ref in item["EVIDENCE"])
        for item in advisories
    )


def submit_review(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                  project: pathlib.Path, task_root: pathlib.Path, report: Any, verifier,
                  report_ref: str, report_bytes: bytes | None = None) -> dict[str, Any]:
    validate_manifest(manifest, receipt, task_root)
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    phase_id = manifest["current_phase_id"]
    phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_id)
    if phase["status"] != "REVIEWING":
        _fail("PHASE_NOT_REVIEWING", "review report requires phase status REVIEWING")
    _verify_candidate_commit(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    required = {"TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA", "VERDICT", "FINDINGS"}
    routed_fields = {"SPAWN_RECEIPT_ID", "AGENT_ID"}
    final_v2 = (manifest.get("schema_version") == 2 and manifest.get("phases") and
                phase_id == manifest["phases"][-1]["phase_id"])
    allowed_report_fields = (required, required | routed_fields)
    if final_v2:
        allowed_report_fields = (*allowed_report_fields,
                                 *(fields | {"INTENT_REVIEW"} for fields in allowed_report_fields))
    allowed_report_fields += tuple(fields | {"ADVISORIES"} for fields in allowed_report_fields)
    if not isinstance(report, dict) or set(report) not in allowed_report_fields:
        _fail("INVALID_PHASE_REVIEW", "review report must contain exact identity, VERDICT and FINDINGS fields, with optional routed reviewer receipt")
    target = review_target(manifest, phase_id)
    if any(report.get(key) != target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")):
        _fail("STALE_PHASE_REVIEW", "review report task, phase, base or target SHA is stale")
    authoritative = routed_fields <= report.keys()
    if authoritative:
        try:
            devcrew_dispatch.verify_spawn(project, report["SPAWN_RECEIPT_ID"],
                                          task_id=target["TASK_ID"], role="reviewer", phase_id=target["PHASE_ID"],
                                          base_sha=target["BASE_SHA"], target_sha=target["TARGET_SHA"],
                                          agent_id=report["AGENT_ID"],
                                          store_path=devcrew_capacity.dispatch_receipt_path(project, target["TASK_ID"]))
        except devcrew_dispatch.DispatchError as exc:
            _fail("INVALID_REVIEW_ROUTE", str(exc))
    verdict, findings = report.get("VERDICT"), report.get("FINDINGS")
    advisories = report.get("ADVISORIES", [])
    if not valid_review_advisories(advisories):
        _fail("INVALID_PHASE_REVIEW", "ADVISORIES must contain summaries with nonempty evidence")
    if not isinstance(verdict, str) or verdict not in {"PASS", "FAIL"} or not isinstance(findings, list):
        _fail("INVALID_PHASE_REVIEW", "VERDICT must be PASS or FAIL and FINDINGS must be an array")
    if verdict == "PASS" and findings:
        _fail("INVALID_PHASE_REVIEW", "PASS requires an empty FINDINGS array")
    if verdict == "FAIL" and not findings:
        _fail("INVALID_PHASE_REVIEW", "FAIL requires at least one substantive finding")
    if final_v2 and (verdict == "PASS" or "INTENT_REVIEW" in report):
        _validate_intent_review(report, manifest, phase, project, task_root, verifier)
    elif "INTENT_REVIEW" in report:
        _fail("INVALID_INTENT_REVIEW", "INTENT_REVIEW is allowed only on the final schema-v2 phase")
    for finding in findings:
        if (not isinstance(finding, dict) or set(finding) != {"CLASSIFICATION", "SUMMARY", "EVIDENCE"} or
                not isinstance(finding.get("CLASSIFICATION"), str) or finding.get("CLASSIFICATION") not in REVIEW_FAILURES or
                not isinstance(finding.get("SUMMARY"), str) or not finding["SUMMARY"].strip() or
                not isinstance(finding.get("EVIDENCE"), list) or not finding["EVIDENCE"] or
                any(not isinstance(item, str) or not item.strip() for item in finding["EVIDENCE"])):
            _fail("INVALID_PHASE_REVIEW", "each finding needs CLASSIFICATION, SUMMARY and nonempty EVIDENCE")
    report_ref = _validate_evidence_ref(report_ref)
    report_path = project / pathlib.Path(*pathlib.PurePosixPath(report_ref).parts)
    cursor = project
    for part in pathlib.PurePosixPath(report_ref).parts:
        cursor = cursor / part
        if fs.is_link(cursor):
            _fail("PHASE_REVIEW_REPORT_INVALID", "review report path traverses a symlink")
    try:
        host_bytes = report_path.read_bytes()
    except OSError as exc:
        _fail("PHASE_REVIEW_REPORT_MISSING", str(exc))
    if report_bytes is not None and report_bytes != host_bytes:
        _fail("PHASE_REVIEW_REPORT_CHANGED", "submitted report bytes differ from host report")
    report_bytes = host_bytes
    try:
        if json.loads(report_bytes.decode("utf-8")) != report:
            _fail("PHASE_REVIEW_REPORT_CHANGED", "parsed report differs from host report bytes")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("INVALID_PHASE_REVIEW", str(exc))
    digest = hashlib.sha256(report_bytes).hexdigest()
    if not authoritative:
        return {"status": "PHASE_REVIEW_ADVISORY", "authority": "ADVISORY",
                "verdict": verdict, "findings": findings, "advisories": advisories,
                "report_sha256": digest, "report_ref": report_ref}
    updated = json.loads(json.dumps(manifest))
    updated_phase = next(p for p in updated["phases"] if p["phase_id"] == phase_id)
    updated_phase["review_report"] = {"task_id": report["TASK_ID"], "phase_id": report["PHASE_ID"],
                                       "base_sha": report["BASE_SHA"], "target_sha": report["TARGET_SHA"],
                                       "verdict": verdict, "findings": findings, "advisories": advisories,
                                       "report_ref": report_ref, "sha256": digest,
                                       "authoritative": True,
                                       "review_started": True,
                                       "spawn_receipt_id": report.get("SPAWN_RECEIPT_ID"),
                                       "agent_id": report.get("AGENT_ID")}
    if verdict == "FAIL":
        updated_phase["review_start"] = None
        classes = {item["CLASSIFICATION"] for item in findings}
        updated_phase["status"] = ("BLOCKED" if "OWNER_DECISION_REQUIRED" in classes else
                                    "REPLAN_REQUIRED" if "SYSTEMIC_REPLAN" in classes else "LOCAL_FIX_REQUIRED")
    validate_manifest(updated, receipt, task_root)
    _atomic_json(path, updated)
    return {"status": "PHASE_REVIEW_RECORDED", "authority": "AUTHORITATIVE",
            "verdict": verdict,
            "findings": findings, "advisories": advisories, "report_sha256": digest, "report_ref": report_ref}


def accept_review(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any],
                  project: pathlib.Path, task_root: pathlib.Path, phase_id: str, verifier,
                  acceptance_evidence: list[str] | None = None) -> dict[str, Any]:
    validate_manifest(manifest, receipt, task_root)
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    if phase_id != manifest["current_phase_id"]:
        _fail("CURRENT_PHASE_ONLY", "only the current phase can be accepted")
    phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_id)
    report = phase.get("review_report")
    if (phase["status"] != "REVIEWING" or not isinstance(report, dict) or
            report.get("verdict") != "PASS" or report.get("authoritative") is not True):
        _fail("PHASE_REVIEW_REQUIRED", "acceptance requires a recorded PASS report for this candidate")
    target = review_target(manifest, phase_id)
    if target["TARGET_SHA"] in _rejected_candidate_shas(phase):
        _fail("REJECTED_CANDIDATE_REUSE", "a rejected candidate SHA cannot be accepted as its replacement")
    if any(report.get(key.lower()) != target[key] for key in ("TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA")):
        _fail("STALE_PHASE_REVIEW", "saved review report identity no longer matches candidate")
    _verify_candidate_commit(manifest, phase, task_root)
    _verify_candidate_evidence(project, task_root, manifest, phase, verifier)
    _verify_saved_review_report(project, manifest, phase)
    final_v2 = (manifest.get("schema_version") == 2 and manifest["phases"][-1]["phase_id"] == phase_id)
    if final_v2:
        saved_intent_review = _validate_intent_review(
            _verify_saved_review_report(project, manifest, phase), manifest, phase, project, task_root, verifier)
        if saved_intent_review["result"] != "COMPLETE":
            _fail("INTENT_REVIEW_INCOMPLETE", "final task acceptance requires a complete independent intent review")
    acceptance_refs = _verify_acceptance_evidence(acceptance_evidence or [], project, task_root,
                                                  manifest["task_id"], phase["candidate_commit"], verifier)
    try:
        devcrew_dispatch.verify_spawn(project, report.get("spawn_receipt_id"),
                                      task_id=target["TASK_ID"], role="reviewer", phase_id=target["PHASE_ID"],
                                      base_sha=target["BASE_SHA"], target_sha=target["TARGET_SHA"],
                                      agent_id=report.get("agent_id"),
                                      store_path=devcrew_capacity.dispatch_receipt_path(project, target["TASK_ID"]))
    except devcrew_dispatch.DispatchError as exc:
        _fail("INVALID_REVIEW_ROUTE", str(exc))
    updated = json.loads(json.dumps(manifest))
    updated_phase = next(p for p in updated["phases"] if p["phase_id"] == phase_id)
    updated_phase["status"] = "ACCEPTED"
    updated_phase["accepted_sha"] = target["TARGET_SHA"]
    updated_phase["acceptance_evidence"] = acceptance_refs
    task_acceptance_report = None
    if final_v2:
        if not all(item.get("status") == "ACCEPTED" for item in updated["phases"]):
            _fail("PHASE_ACCEPTANCE_REQUIRED", "all phases must be accepted before task completion")
        task_acceptance_report = report["sha256"]
        updated["original_intent"]["task_acceptance"] = {
            "status": "ACCEPTED", "review": {"phase_id": phase_id, "report_sha256": task_acceptance_report}}
    validate_manifest(updated, receipt, task_root)
    def revalidate_locked():
        validate_manifest(updated, receipt, task_root)
        _verify_candidate_commit(updated, updated_phase, task_root)
        _verify_candidate_evidence(project, task_root, updated, updated_phase, verifier)
        locked_report = _verify_saved_review_report(project, updated, updated_phase)
        if final_v2:
            _validate_intent_review(locked_report, updated, updated_phase, project, task_root, verifier)
        _verify_acceptance_evidence(acceptance_refs, project, task_root, manifest["task_id"],
                                    updated_phase["candidate_commit"], verifier)
        devcrew_dispatch.verify_spawn(project, report.get("spawn_receipt_id"), task_id=target["TASK_ID"],
            role="reviewer", phase_id=target["PHASE_ID"], base_sha=target["BASE_SHA"],
            target_sha=target["TARGET_SHA"], agent_id=report.get("agent_id"),
            store_path=devcrew_capacity.dispatch_receipt_path(project, target["TASK_ID"]))
    _atomic_json(path, updated, before_save=revalidate_locked,
                 _task_acceptance_report=task_acceptance_report)
    return {"status": "PHASE_ACCEPTED", "phase_id": phase_id, "accepted_sha": target["TARGET_SHA"]}


def advance_phase(path: pathlib.Path, manifest: dict[str, Any], receipt: dict[str, Any], phase_id: str,
                  project: pathlib.Path | None = None, task_root: pathlib.Path | None = None,
                  verifier=None) -> dict[str, Any]:
    validate_manifest(manifest, receipt)
    if any(phase.get("review_cycles") for phase in manifest["phases"]):
        if project is None or task_root is None:
            _fail("PHASE_REVIEW_HISTORY_VALIDATION_REQUIRED", "phase advancement requires rejected-cycle history validation")
        _verify_all_review_cycles(project, task_root, manifest, verifier)
    current_id = manifest["current_phase_id"]
    current = next(item for item in manifest["phases"] if item["phase_id"] == current_id)
    if current["status"] != "ACCEPTED":
        _fail("PHASE_ACCEPTANCE_REQUIRED", "current phase must be ACCEPTED before advancing")
    if project is None or task_root is None or verifier is None:
        _fail("PHASE_ACCEPTED_HISTORY_VALIDATION_REQUIRED",
              "phase advancement requires host report and Git validation of accepted history")
    _verify_accepted_history(manifest, project, task_root, verifier,
                             include_current_accepted=True, require_clean=True)
    ids = [item["phase_id"] for item in manifest["phases"]]
    if ids.index(phase_id) != ids.index(current_id) + 1:
        _fail("INVALID_PHASE_ADVANCE", "only the next sequential phase may be advanced")
    phase = next(item for item in manifest["phases"] if item["phase_id"] == phase_id)
    if phase["status"] != "PLANNED":
        _fail("INVALID_PHASE_ADVANCE", "next phase must be PLANNED")
    dependency_ids = phase["depends_on"]
    dependencies = [next(item for item in manifest["phases"] if item["phase_id"] == dep) for dep in dependency_ids]
    if any(dep["status"] != "ACCEPTED" or not dep.get("accepted_sha") for dep in dependencies):
        _fail("CLOSE_GATE_REQUIRED", "all dependencies must have accepted SHAs")
    predecessor = current
    base_sha = predecessor.get("accepted_sha")
    if (not isinstance(base_sha, str) or
            base_sha != predecessor.get("candidate_commit", {}).get("sha")):
        _fail("AMBIGUOUS_ACCEPTED_BASE", "immediately preceding accepted phase has no unique candidate base SHA")
    updated = json.loads(json.dumps(manifest))
    updated["current_phase_id"] = phase_id
    next(item for item in updated["phases"] if item["phase_id"] == phase_id)["base_sha"] = base_sha
    validate_manifest(updated, receipt)
    _atomic_json(path, updated)
    return updated


def _git(root: pathlib.Path, *args: str, check=True, strip_output=True) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], text=True,
                            encoding="utf-8", errors="surrogateescape", capture_output=True)
    if check and result.returncode:
        _fail("PHASE_COMMIT_INCONSISTENT", (result.stderr or result.stdout).strip())
    return result.stdout.strip() if strip_output else result.stdout


def _verify_candidate_commit_object(manifest, phase, task_root, expected_base=None):
    record = phase.get("candidate_commit")
    if not isinstance(record, dict) or task_root is None:
        _fail("PHASE_COMMIT_INCONSISTENT", "committed phase has no durable candidate record")
    sha, tree = record.get("sha"), record.get("tree")
    base = expected_base or record.get("base") or phase.get("base_sha", manifest["worktree"]["base_sha"])
    try:
        evidence_sha256 = _candidate_evidence_seal(record)
        metadata_fields = None
        if isinstance(sha, str) and re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", sha):
            metadata = _git(task_root, "show", "-s", "--format=%H%n%P%n%T%n%B",
                            f"{sha}^{{commit}}", check=False, strip_output=False)
            actual_sha, separator1, remaining = metadata.partition("\n")
            parent, separator2, remaining = remaining.partition("\n")
            actual_tree, separator3, message = remaining.partition("\n")
            if (separator1 and separator2 and separator3 and actual_sha.strip().lower() == sha.lower()):
                metadata_fields = (parent.strip(), actual_tree.strip(), message.strip())

        if metadata_fields is None:
            if not isinstance(sha, str) or _git(task_root, "cat-file", "-t", sha) != "commit":
                raise ValueError("recorded candidate SHA is not a commit object")
            parent = _git(task_root, "show", "-s", "--format=%P", sha)
            actual_tree = _git(task_root, "show", "-s", "--format=%T", sha)
            message = _git(task_root, "show", "-s", "--format=%B", sha)
        else:
            parent, actual_tree, message = metadata_fields
        if parent.strip() != base:
            raise ValueError("candidate commit parent differs from immutable phase base")
        if actual_tree.strip() != tree:
            raise ValueError("candidate commit tree differs from recorded tree")
        message = message.strip()
        expected = (phase["commit_subject"] + f"\n\nDevCrew-Task: {manifest['task_id']}"
                    f"\nDevCrew-Phase: {phase['phase_id']}\nDevCrew-Base: {base}\nDevCrew-Tree: {tree}"
                    f"\nDevCrew-Verification: {record.get('verification_contract', {}).get('verification_sha256')}")
        if evidence_sha256 is not None:
            expected += f"\nDevCrew-Evidence: {evidence_sha256}"
        if "operation_id" in record:
            if not re.fullmatch(r"[0-9a-f]{48}", str(record["operation_id"])):
                raise ValueError("candidate operation ID is invalid")
            expected += f"\nDevCrew-Operation: {record['operation_id']}"
        if message.rstrip("\n") != expected:
            raise ValueError("candidate commit subject or DevCrew trailers are invalid")
    except (ValueError, OSError) as exc:
        _fail("PHASE_COMMIT_INCONSISTENT", str(exc))


def _verify_candidate_commit(manifest, phase, task_root):
    _verify_candidate_commit_object(manifest, phase, task_root)
    try:
        actual = _git(task_root, "rev-parse", "HEAD")
        branch = _git(task_root, "symbolic-ref", "--short", "HEAD")
        if actual != phase["candidate_commit"].get("sha") or branch != manifest["worktree"]["branch"]:
            raise ValueError("task worktree HEAD or branch differs from recorded candidate")
        if _git(task_root, "status", "--porcelain", "--untracked-files=all"):
            raise ValueError("committed task worktree is dirty")
    except (ValueError, OSError) as exc:
        _fail("PHASE_COMMIT_INCONSISTENT", str(exc))


def candidate_commit(project: pathlib.Path, task_root: pathlib.Path, manifest: dict[str, Any],
                     receipt: dict[str, Any], bindings_path: pathlib.Path, verifier, run_check):
    """Validate, stage, verify and atomically publish the current phase candidate commit."""
    *_, state_path = devcrew_worktree.location(project, manifest["task_id"])
    path = state_path.with_name(f"{manifest['task_id']}.phases.json")
    devcrew_worktree._require_no_candidate_transition(state_path)
    validate_manifest(manifest, receipt, task_root)
    _verify_all_review_cycles(project, task_root, manifest, verifier)
    phase = next(p for p in manifest["phases"] if p["phase_id"] == manifest["current_phase_id"])
    if phase["status"] != "READY_FOR_CLOSE":
        _fail("PHASE_NOT_READY_FOR_CLOSE", "candidate commit requires current phase status READY_FOR_CLOSE")
    dispatch = phase.get("executor_dispatch")
    active_fix = phase.get("active_local_fix")
    cycle = active_fix["cycle_id"] if active_fix else 0
    rejected_sha = active_fix["rejected_candidate_sha"] if active_fix else None
    if not isinstance(dispatch, dict) or dispatch.get("writer_cycle") != cycle or dispatch.get("rejected_candidate_sha") != rejected_sha:
        _fail("INVALID_EXECUTOR_ROUTE", "executor dispatch receipt does not match the current writer cycle")
    _verify_dispatch(project, dispatch.get("spawn_receipt_id"), task_id=manifest["task_id"], role="executor",
                     phase_id=phase["phase_id"], base_sha=phase.get("base_sha", manifest["worktree"]["base_sha"]),
                     writer_cycle=cycle, rejected_candidate_sha=rejected_sha,
                     agent_id=dispatch.get("agent_id"), route_id=dispatch.get("route_id"),
                     model=dispatch.get("model"), effort=dispatch.get("effort"),
                     spawn_mode=dispatch.get("spawn_mode"))
    base = phase.get("base_sha", manifest["worktree"]["base_sha"])
    if _git(task_root, "rev-parse", "HEAD") != base or _git(task_root, "symbolic-ref", "--short", "HEAD") != receipt["branch"]:
        _fail("PHASE_BASE_MISMATCH", "task worktree must remain on immutable phase base")
    try:
        bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        _fail("INVALID_VERIFICATION_BINDINGS", str(exc))
    bindings = verification_bindings(phase["verification"], bindings)
    if bindings != required_verification(phase):
        _fail("VERIFICATION_PLAN_CHANGED", "candidate bindings differ from the accepted plan")
    changed = _git(task_root, "diff", "--no-renames", "--name-only", "-z", "HEAD",
                   strip_output=False).split("\0")
    untracked = _git(task_root, "ls-files", "--others", "--exclude-standard", "-z",
                     strip_output=False).split("\0")
    paths = sorted({p for p in changed + untracked if p})
    if not paths:
        _fail("EMPTY_PHASE_CANDIDATE", "no task changes are available to commit")
    codex_paths = [p for p in paths if p == ".codex" or p.startswith(".codex/")]
    if codex_paths:
        _fail("PHASE_SCOPE_VIOLATION", "reinstallable .codex runtime state cannot be committed: " + ", ".join(codex_paths))
    scopes = phase["owned_scope"]
    def allowed(path):
        return any(path == scope or path.startswith(scope.rstrip("/") + "/") for scope in scopes)
    outside = [p for p in paths if not allowed(p)]
    if outside:
        _fail("PHASE_SCOPE_VIOLATION", "changed paths outside owned_scope: " + ", ".join(outside))
    verify_evidence_records(phase.get("evidence"), project, task_root, manifest["task_id"], verifier, phase)
    _git(task_root, "add", "-A", "--", *[":(literal)" + path for path in paths])
    if _git(task_root, "diff", "--name-only"):
        _fail("PHASE_INDEX_WORKTREE_MISMATCH", "unstaged changes remain after staging candidate")
    tree = _git(task_root, "write-tree")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", tree):
        _fail("PHASE_CANDIDATE_TREE_INVALID", "Git returned an invalid candidate tree")
    refs = []
    snapshots = []
    for binding in bindings:
        ref = run_check(binding["argv"], binding["verification"])
        try:
            record, data, host = verifier(ref, current=True, expected_execution_root=str(task_root),
                                          expected_task_id=manifest["task_id"])
        except Exception as exc:
            _fail("INVALID_PHASE_EVIDENCE", str(exc))
        if host.resolve() != project.resolve() or not match_verification(data, binding, phase) or data.get("execution_root") != str(task_root):
            _fail("FOREIGN_PHASE_EVIDENCE", "verification receipt task root, host, or command differs")
        if (data.get("git_head_before") != base or data.get("git_head") != base or
                data.get("candidate_tree_before") != tree or data.get("candidate_tree_after") != tree or
                data.get("worktree_fingerprint_before") != data.get("worktree_fingerprint_after")):
            _fail("STALE_PHASE_EVIDENCE", "verification receipt is not bound to the current candidate tree")
        reference = record.resolve().relative_to(project.resolve()).as_posix()
        raw = record.read_bytes()
        if json.loads(raw) != data:
            _fail("CANDIDATE_EVIDENCE_CHANGED", "check record changed while freezing verified evidence")
        refs.append(reference)
        snapshots.append({"record_ref": reference, "sha256": hashlib.sha256(raw).hexdigest()})
    if (_git(task_root, "write-tree") != tree or _git(task_root, "diff", "--name-only") or
            _git(task_root, "ls-files", "--others", "--exclude-standard")):
        _fail("PHASE_CANDIDATE_CHANGED", "candidate changed during verification")
    evidence_sha256 = _candidate_digest(snapshots)
    trailers = (f"\n\nDevCrew-Task: {manifest['task_id']}\nDevCrew-Phase: {phase['phase_id']}"
                f"\nDevCrew-Base: {base}\nDevCrew-Tree: {tree}"
                f"\nDevCrew-Verification: {phase['verification_sha256']}"
                f"\nDevCrew-Evidence: {evidence_sha256}")
    env = os.environ.copy()
    ident_name = _git(task_root, "config", "user.name")
    ident_email = _git(task_root, "config", "user.email")
    # Checks run outside the locks. Publish intent before creating an object so
    # an uncertain commit-tree result can never cause a second candidate attempt.
    with _manifest_lock(path), devcrew_worktree._receipt_lock(state_path):
        current = json.loads(path.read_text(encoding="utf-8"))
        _require_no_candidate_transition(current, path=path)
        if _manifest_revision(current) != _manifest_revision(manifest):
            _fail("PHASE_MANIFEST_CONFLICT", "phase manifest changed; reload before retrying")
        if (_git(task_root, "rev-parse", "HEAD") != base or
                _git(task_root, "symbolic-ref", "--short", "HEAD") != receipt["branch"] or
                _git(task_root, "write-tree") != tree or _git(task_root, "diff", "--name-only") or
                _git(task_root, "ls-files", "--others", "--exclude-standard")):
            _fail("PHASE_CANDIDATE_CHANGED", "candidate changed before saving the commit")
        _, _, identity, checkout, branch, _ = devcrew_worktree.location(project, manifest["task_id"])
        record = devcrew_worktree._read(state_path, identity, manifest["task_id"], checkout, branch)
        if record is None or record["expected_head"] != base or record["receipt_nonce"] != receipt["receipt_nonce"]:
            _fail("PHASE_COMMIT_INCONSISTENT", "candidate lifecycle receipt changed")
        pending = json.loads(json.dumps(current))
        pending["candidate_transition"] = {"phase_id": phase["phase_id"], "base": base, "tree": tree}
        pending["manifest_revision"] = _manifest_revision(current) + 1
        try:
            _replace_json(path, pending)
            commit = subprocess.run(["git", "-C", str(task_root), "commit-tree", tree, "-p", base, "-m",
                                     phase["commit_subject"] + trailers], text=True, encoding="utf-8", errors="surrogateescape", capture_output=True,
                                    env={**env, "GIT_AUTHOR_NAME": ident_name, "GIT_AUTHOR_EMAIL": ident_email,
                                         "GIT_COMMITTER_NAME": ident_name, "GIT_COMMITTER_EMAIL": ident_email})
            if commit.returncode:
                _fail("PHASE_COMMIT_FAILED", (commit.stderr or commit.stdout).strip())
            sha = commit.stdout.strip().lower()
            if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
                _fail("PHASE_COMMIT_FAILED", "Git returned an invalid candidate SHA")
            # Save even a rejected SHA before stopping; never guess or recreate it.
            pending["candidate_transition"]["sha"] = sha
            pending["manifest_revision"] += 1
            _replace_json(path, pending)
            if sha in _rejected_candidate_shas(phase):
                _fail("REJECTED_CANDIDATE_REUSE", "replacement candidate SHA must differ from every rejected cycle target")
            candidate = {"sha": sha, "tree": tree, "base": base, "evidence": refs,
                         "verification_contract": _verification_contract(phase),
                         "evidence_snapshots": snapshots, "evidence_sha256": evidence_sha256}
            updated = json.loads(json.dumps(current))
            target = next(p for p in updated["phases"] if p["phase_id"] == phase["phase_id"])
            target["candidate_commit"] = candidate
            target.pop("active_local_fix", None)
            target["status"] = "COMMITTED"
            _verify_candidate_commit_object(updated, target, task_root)
            _verify_candidate_evidence(project, task_root, updated, target, verifier)
            update = subprocess.run(["git", "-C", str(task_root), "update-ref",
                                     f"refs/heads/{branch}", sha, base], capture_output=True, text=True, encoding="utf-8", errors="surrogateescape")
            if update.returncode:
                _fail("PHASE_COMMIT_CAS_FAILED", (update.stderr or update.stdout).strip())
            _verify_candidate_commit(updated, target, task_root)
            _verify_candidate_evidence(project, task_root, updated, target, verifier)
            # This is the sole authorized receipt write while our pending marker
            # is present. Both locks and the original snapshot remain held.
            advanced = dict(record, expected_head=sha, last_known_clean=True,
                            writer_dispatched_since_snapshot=True,
                            receipt_nonce=secrets.token_hex(24),
                            receipt_revision=devcrew_worktree._receipt_revision(record) + 1)
            devcrew_worktree._check_snapshot(state_path, record)
            devcrew_worktree._write_json(state_path, advanced)
            validate_manifest(updated, devcrew_worktree._receipt(advanced, "RESOLVED"), task_root)
            _verify_candidate_evidence(project, task_root, updated, target, verifier)
            updated["manifest_revision"] = pending["manifest_revision"] + 1
            # Removing pending is the last write, with no replay/acknowledgement.
            _replace_json(path, updated)
            return {"status": "PHASE_COMMITTED_AWAITING_REVIEW", "commit": candidate, "lifecycle": advanced}
        except (OSError, ValueError) as exc:
            _fail("CANDIDATE_RECONCILIATION_REQUIRED",
                  f"candidate outcome requires manual reconciliation: {exc}; manifest={path}, receipt={state_path}, "
                  f"branch={branch}, base={base}, candidate_sha={pending['candidate_transition'].get('sha', 'unknown (not saved)')}. "
                  "Preserve Git, receipt, manifest and evidence; compare saved state with actual files and Git. "
                  "Do not retry candidate creation, guess a SHA, reset Git, or delete recovery state.")


def _rejected_candidate_shas(phase: dict[str, Any]) -> set[str]:
    return {cycle["candidate_commit"]["sha"] for cycle in phase.get("review_cycles", [])
            if cycle.get("review_correction") is not True}
