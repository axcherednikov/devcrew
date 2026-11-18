"""Host-owned receipts for runtime agent-capacity rejection and manual resume."""

from __future__ import annotations

import devcrew_fs as fs
import json
import hashlib
import os
import pathlib
import re
import tempfile
import uuid
from typing import Any

import devcrew_dispatch

TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ROLES = {"planner", "executor", "reviewer"}
CAPACITY_ERROR = "agent thread limit reached"
DEFAULT_SESSIONS = ".codex/sessions"


class CapacityError(ValueError):
    """Invalid host dispatch report or capacity receipt."""


def normalize_report(report: Any) -> dict[str, Any]:
    """Classify only an explicit failed spawn with the exact runtime error."""
    if not isinstance(report, dict):
        raise CapacityError("spawn report must be an object")
    status = report.get("STATUS")
    if status not in {"SPAWN_SUCCESS", "SPAWN_FAILED"}:
        raise CapacityError("spawn report STATUS must be SPAWN_SUCCESS or SPAWN_FAILED")
    normalized = dict(report)
    if status == "SPAWN_FAILED" and report.get("RUNTIME_ERROR") == CAPACITY_ERROR:
        normalized["STATUS"] = "AGENT_CAPACITY_BLOCKED"
    return normalized


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value) or fs.unsafe_parts([value]):
        raise CapacityError("TASK_ID must be a safe task identifier")
    return value


def _absolute_root(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CapacityError("project and task roots must be absolute paths")
    path = pathlib.Path(value)
    if not path.is_absolute():
        raise CapacityError("project and task roots must be absolute paths")
    return str(path.resolve())


def _attempt(value: Any) -> int:
    if type(value) is not int or value < 1:
        raise CapacityError("ATTEMPT_NUMBER must be a positive integer")
    return value


def _prepared(value: Any, role: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(value, dict):
        raise CapacityError("PREPARED must contain route and dispatch objects")
    route, dispatch = value.get("route"), value.get("dispatch")
    if not isinstance(route, dict) or not isinstance(dispatch, dict):
        raise CapacityError("PREPARED must contain route and dispatch objects")
    if route.get("ROLE") != role or not route.get("RESOLVED_MODEL") or not route.get("REASONING_EFFORT"):
        raise CapacityError("prepared route must match the requested role and resolve model/effort")
    required_route = {"ROUTE_ID", "TASK_CLASS", "COORDINATION_COMPLEXITY", "DECISION_COMPLEXITY",
                      "IMPLEMENTATION_COMPLEXITY", "REVIEW_COMPLEXITY", "RISK", "MODEL_CLASS",
                      "SELECTION_REASON"}
    if required_route - set(route):
        raise CapacityError("DISPATCH_ROUTE_REQUIRED: prepared route is incomplete")
    if dispatch.get("SPAWN_MODE") not in {"typed", "generic"}:
        raise CapacityError("prepared dispatch must contain its validated spawn mode")
    return dict(route), dict(dispatch)


def _atomic_json(path: pathlib.Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _session_file(project: pathlib.Path, task_id: str, *, create: bool = False) -> pathlib.Path:
    sessions_relative = DEFAULT_SESSIONS
    state_file = project / ".codex" / "devcrew.json"
    if state_file.exists() or fs.is_link(state_file):
        if fs.is_link(state_file) or not state_file.is_file():
            raise CapacityError("state configuration is not a regular file")
        try:
            state = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CapacityError(f"invalid state configuration: {error}") from None
        paths = state.get("paths") if isinstance(state, dict) else None
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or state["schema_version"] != 1 or not isinstance(paths, dict)):
            raise CapacityError("invalid state configuration schema")
        sessions_relative = paths.get("sessions")
    raw_sessions = sessions_relative.replace("\\", "/") if isinstance(sessions_relative, str) else ""
    if (not raw_sessions or fs.unsafe_parts(pathlib.PurePosixPath(raw_sessions).parts)
            or pathlib.PurePosixPath(raw_sessions).is_absolute()
            or re.match(r"^[A-Za-z]:", raw_sessions)
            or any(part in ("", ".", "..") for part in raw_sessions.split("/"))
            or any(char in raw_sessions for char in "*?[") or "\x00" in raw_sessions):
        raise CapacityError("configured sessions path must stay inside the project")
    sessions = project
    for part in pathlib.PurePosixPath(raw_sessions).parts:
        sessions = sessions / part
        if fs.is_link(sessions):
            raise CapacityError("refusing to follow symlink in configured sessions state")
        if sessions.exists() and not sessions.is_dir():
            raise CapacityError("configured sessions state contains a non-directory")
        if create and not sessions.exists():
            sessions.mkdir()
    return sessions / f"agent-capacity-{task_id}.json"


def dispatch_receipt_path(project_root: str | pathlib.Path, task_id: str, *, create: bool = False) -> pathlib.Path:
    """Return the task's dispatch receipt path under configured sessions state."""
    project = pathlib.Path(_absolute_root(str(project_root)))
    return _session_file(project, _task_id(task_id), create=create).with_name(
        f"agent-dispatch-{task_id}.json")


def _remove_active(path: pathlib.Path) -> None:
    if fs.is_link(path):
        raise CapacityError("refusing to remove symlink capacity state")
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def report(project_root: str, report_data: Any) -> dict[str, Any]:
    """Persist an exact capacity blocker or clear it after successful spawn."""
    normalized = normalize_report(report_data)
    role = normalized.get("REQUESTED_ROLE")
    if role not in ROLES:
        raise CapacityError("REQUESTED_ROLE must be planner, executor, or reviewer")
    task_id = _task_id(normalized.get("TASK_ID"))
    project = pathlib.Path(_absolute_root(project_root))
    if not project.is_dir():
        raise CapacityError("HOST_PROJECT_ROOT must exist as a directory")
    supplied_host = _absolute_root(normalized.get("HOST_PROJECT_ROOT"))
    if supplied_host != str(project.resolve()):
        raise CapacityError("HOST_PROJECT_ROOT does not match --project")
    task_root = _absolute_root(normalized.get("TASK_WORKTREE_ROOT"), nullable=True)
    if role != "planner" and task_root is None:
        raise CapacityError("executor and reviewer reports require TASK_WORKTREE_ROOT")
    stage = normalized.get("CURRENT_STAGE")
    if not isinstance(stage, str) or not stage.strip():
        raise CapacityError("CURRENT_STAGE is required")
    attempt = _attempt(normalized.get("ATTEMPT_NUMBER"))
    route, dispatch = _prepared(normalized.get("PREPARED"), role)
    if normalized.get("HOST_INITIATED") is not True:
        raise CapacityError("spawn report requires explicit host-initiated call confirmation")
    try:
        issued = devcrew_dispatch.validate_issued(
            project, normalized, store_path=dispatch_receipt_path(project, task_id))
    except devcrew_dispatch.DispatchError as exc:
        raise CapacityError(str(exc)) from None
    path = _session_file(project, task_id,
                         create=normalized["STATUS"] == "AGENT_CAPACITY_BLOCKED")
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise CapacityError("existing capacity receipt is invalid") from None
        if (not isinstance(previous, dict) or previous.get("STATUS") != "AGENT_CAPACITY_BLOCKED"
                or previous.get("TASK_ID") != task_id):
            raise CapacityError("existing capacity receipt is not a valid blocker")
        if normalized.get("BLOCKER_ID") != previous.get("BLOCKER_ID"):
            raise CapacityError("spawn report does not reference the active capacity blocker")
        expected_identity = {
            "TASK_ID": previous["TASK_ID"],
            "REQUESTED_ROLE": previous["REQUESTED_ROLE"],
            "CURRENT_STAGE": previous["CURRENT_STAGE"],
            "HOST_PROJECT_ROOT": previous["HOST_PROJECT_ROOT"],
            "TASK_WORKTREE_ROOT": previous["TASK_WORKTREE_ROOT"],
            "PREPARED": previous["PREPARED"],
            "SPAWN_PAYLOAD": previous["SPAWN_PAYLOAD"],
            "SPAWN_PAYLOAD_SHA256": previous["SPAWN_PAYLOAD_SHA256"],
            "DISPATCH_IDENTITY": previous["DISPATCH_IDENTITY"],
        }
        actual_identity = {
            "TASK_ID": task_id,
            "REQUESTED_ROLE": role,
            "CURRENT_STAGE": stage,
            "HOST_PROJECT_ROOT": supplied_host,
            "TASK_WORKTREE_ROOT": task_root,
            "PREPARED": {"route": route, "dispatch": dispatch},
            "SPAWN_PAYLOAD": normalized.get("SPAWN_PAYLOAD"),
            "SPAWN_PAYLOAD_SHA256": normalized.get("SPAWN_PAYLOAD_SHA256"),
            "DISPATCH_IDENTITY": {key: normalized[key] for key in issued["IDENTITY"]},
        }
        if actual_identity != expected_identity:
            raise CapacityError("spawn report does not match the active blocker identity")
        if attempt != _attempt(previous.get("ATTEMPT_NUMBER")) + 1:
            raise CapacityError("attempt number must increment once for each host-initiated spawn")
    elif normalized.get("BLOCKER_ID") is not None:
        raise CapacityError("spawn report references a blocker that is not active for this task")
    elif attempt != 1:
        raise CapacityError("first host-initiated spawn attempt must be 1")

    if normalized["STATUS"] == "SPAWN_SUCCESS":
        try:
            normalized["SPAWN_RECEIPT"] = devcrew_dispatch.record_spawn(
                project, normalized, store_path=dispatch_receipt_path(project, task_id, create=True))
        except devcrew_dispatch.DispatchError as exc:
            raise CapacityError(str(exc)) from None
        normalized["ROUTE_APPLIED"] = True
        _remove_active(path)
        return normalized
    if normalized["STATUS"] != "AGENT_CAPACITY_BLOCKED":
        _remove_active(path)
        return normalized
    blocker = {
        "STATUS": "AGENT_CAPACITY_BLOCKED",
        "REQUESTED_ROLE": role,
        "TASK_ID": task_id,
        "HOST_PROJECT_ROOT": str(project.resolve()),
        "TASK_WORKTREE_ROOT": task_root,
        "CURRENT_STAGE": stage,
        "RESOLVED_MODEL": route["RESOLVED_MODEL"],
        "REASONING_EFFORT": route["REASONING_EFFORT"],
        "ATTEMPT_NUMBER": attempt,
        "RUNTIME_ERROR": CAPACITY_ERROR,
        "RESUMABLE": True,
        "BLOCKER_ID": previous["BLOCKER_ID"] if path.exists() else uuid.uuid4().hex,
        "PREPARED": {"route": route, "dispatch": dispatch},
        "SPAWN_PAYLOAD": normalized["SPAWN_PAYLOAD"],
        "SPAWN_PAYLOAD_SHA256": normalized["SPAWN_PAYLOAD_SHA256"],
        "DISPATCH_IDENTITY": {key: normalized[key] for key in issued["IDENTITY"]},
    }
    # File delivery and retry persistence must agree on the same issued
    # transport even if the route was converted after initial validation.
    dispatch_path = dispatch_receipt_path(project, task_id)
    try:
        with devcrew_dispatch._locked(dispatch_path):
            devcrew_dispatch._issued_match(devcrew_dispatch._read(dispatch_path), normalized, dispatch_path)
            _atomic_json(path, blocker)
    except devcrew_dispatch.DispatchError as exc:
        raise CapacityError(str(exc)) from None
    return blocker


def resume(project_root: str, task_id: str, *, host_initiated: bool) -> dict[str, Any]:
    """Return one saved role selection for an explicit manual host resume."""
    task_id = _task_id(task_id)
    project = pathlib.Path(_absolute_root(project_root))
    path = _session_file(project, task_id)
    if fs.is_link(path) or not path.is_file():
        raise CapacityError("no resumable capacity blocker exists for this task")
    try:
        blocker = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CapacityError(f"invalid capacity receipt: {error}") from None
    if (not isinstance(blocker, dict) or blocker.get("STATUS") != "AGENT_CAPACITY_BLOCKED"
            or blocker.get("TASK_ID") != task_id or blocker.get("RESUMABLE") is not True):
        raise CapacityError("capacity receipt is not resumable")
    if blocker.get("HOST_PROJECT_ROOT") != str(project.resolve()):
        raise CapacityError("capacity receipt belongs to another host project")
    if host_initiated is not True:
        raise CapacityError("resume requires an explicit manual host initiation")
    role = blocker.get("REQUESTED_ROLE")
    if role not in ROLES:
        raise CapacityError("capacity receipt has an invalid requested role")
    route, dispatch = _prepared(blocker.get("PREPARED"), role)
    # The original issuance attests the selected model and capability. Never
    # resume an edited blocker or infer eligibility from a model identifier.
    identity = blocker.get("DISPATCH_IDENTITY")
    if not isinstance(identity, dict):
        raise CapacityError("capacity receipt has no dispatch identity")
    report = {**identity, **blocker, "SPAWN_MODEL": blocker.get("RESOLVED_MODEL"),
              "SPAWN_REASONING_EFFORT": blocker.get("REASONING_EFFORT"),
              "SPAWN_MODE": dispatch["SPAWN_MODE"]}
    try:
        issued = devcrew_dispatch.validate_issued(
            project, report, store_path=dispatch_receipt_path(project, task_id))
    except devcrew_dispatch.DispatchError as exc:
        raise CapacityError(str(exc)) from None
    if identity != issued["IDENTITY"]:
        raise CapacityError("INVALID_DISPATCH_ROUTE: saved dispatch identity differs from issuance")
    # Do not regenerate routing or prior stages here. The host makes one spawn call.
    blocker_id = blocker.get("BLOCKER_ID")
    if not isinstance(blocker_id, str) or not blocker_id:
        raise CapacityError("capacity receipt has no blocker identity")
    return {"STATUS": "RESUME_READY", "BLOCKER_ID": blocker_id,
            "TASK_ID": task_id, "REQUESTED_ROLE": role,
            "CURRENT_STAGE": blocker["CURRENT_STAGE"],
            "ATTEMPT_NUMBER": _attempt(blocker.get("ATTEMPT_NUMBER")) + 1,
            "HOST_PROJECT_ROOT": blocker["HOST_PROJECT_ROOT"],
            "TASK_WORKTREE_ROOT": blocker.get("TASK_WORKTREE_ROOT"),
            "RESOLVED_MODEL": route["RESOLVED_MODEL"],
            "REASONING_EFFORT": route["REASONING_EFFORT"],
            "PREPARED": {"route": route, "dispatch": dispatch},
            "SPAWN_PAYLOAD": blocker["SPAWN_PAYLOAD"],
            "SPAWN_PAYLOAD_SHA256": blocker["SPAWN_PAYLOAD_SHA256"],
            **blocker["DISPATCH_IDENTITY"]}
