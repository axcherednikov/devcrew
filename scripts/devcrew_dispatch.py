"""Persist and verify receipts for actual routed agent spawns."""

from __future__ import annotations

import devcrew_fs as fs
import json
import hashlib
import os
import pathlib
import shlex
import sys
import tempfile
import uuid

from typing import Any


class DispatchError(ValueError):
    pass


def _validate_authority(
    project_root,
    task_id,
    role,
    identity,
    spawn_payload,
    expected_binding=None,
    *,
    already_issued=False,
):
    try:
        from devcrew_context import _validate_dispatch_authority

        return _validate_dispatch_authority(
            project_root,
            task_id,
            role,
            identity,
            spawn_payload,
            expected_binding=expected_binding,
            already_issued=already_issued,
        )
    except (ImportError, ValueError, OSError) as exc:
        raise DispatchError(
            str(exc) or "DISPATCH_ROUTE_REQUIRED: role authority validation failed"
        ) from None


def _store_path(
    project: pathlib.Path, task_id: str, configured_path: pathlib.Path | None = None
) -> pathlib.Path:
    if (
        not isinstance(task_id, str)
        or not task_id
        or any(
            c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
            for c in task_id
        )
    ):
        raise DispatchError("invalid TASK_ID")
    if configured_path is not None:
        return configured_path
    from devcrew_capacity import dispatch_receipt_path

    return dispatch_receipt_path(project, task_id)


def _read(path: pathlib.Path) -> dict:
    if fs.is_link(path):
        raise DispatchError("dispatch receipt store must not be a symlink")
    if not path.exists():
        return {"schema_version": 1, "receipts": [], "issued_routes": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchError(f"invalid dispatch receipt store: {exc}") from None
    if (
        not isinstance(payload, dict)
        or type(payload.get("schema_version")) is not int
        or payload.get("schema_version") != 1
        or not isinstance(payload.get("receipts"), list)
    ):
        raise DispatchError("invalid dispatch receipt store schema")
    payload.setdefault("issued_routes", [])
    if not isinstance(payload["issued_routes"], list):
        raise DispatchError("invalid dispatch issued route registry")
    return payload


def _canonical_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _envelope_read(command, path, issued, digest, reader):
    args = ["-B", "-c", command, str(path), issued["ROUTE_ID"], digest]
    if reader is None:
        # Previously issued deliveries bind the exact original text in their receipts.
        return shlex.join(["python3", *args])
    python = reader["python"]
    args = [python, "-X", "utf8", *args]
    if reader["shell"] == "posix":
        return "Run in a POSIX shell:\n" + shlex.join(args)

    # Keep arbitrary path characters outside PowerShell syntax. ASCII JSON also
    # prevents typographic quotation marks in Windows paths from ending a string.
    argv_json = json.dumps(args, ensure_ascii=True).replace("'", "''")
    return (
        "Run in Windows PowerShell or PowerShell 7:\n"
        "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
        f"$dcArgv = ConvertFrom-Json '{argv_json}'; "
        "$dcPython = $dcArgv[0]; $dcArgs = $dcArgv[1..($dcArgv.Length - 1)]; "
        "& $dcPython @dcArgs"
    )


def _executor_delivery(issued: dict, path: pathlib.Path, reader) -> dict:
    """Derive the exact transport arguments from the original issued package."""
    full = issued["SPAWN_PAYLOAD"]
    digest = issued["SPAWN_PAYLOAD_SHA256"]
    if issued["ROLE"] != "executor" or _canonical_digest(full) != digest:
        raise DispatchError("INVALID_DISPATCH_ROUTE: executor package digest does not match")
    command = (
        "import hashlib,json,pathlib,sys; "
        "rows=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['issued_routes']; "
        "matches=[r for r in rows if r.get('ROUTE_ID')==sys.argv[2]]; "
        "(row,)=matches; "
        "p=row['SPAWN_PAYLOAD']; "
        "actual=hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True,"
        "separators=(',',':')).encode('utf-8')).hexdigest(); "
        "actual==sys.argv[3] and row.get('SPAWN_PAYLOAD_SHA256')==actual "
        "or sys.exit('Dispatch package digest mismatch'); "
        "message=p['message']; start=int(sys.argv[4]) if len(sys.argv)>4 else 0; "
        "0<=start<len(message) or sys.exit('Invalid dispatch package offset'); "
        "end=min(start+4000,len(message)); "
        "print(f'Characters {start}:{end} of {len(message)}'); "
        "print(message[start:end],end='')"
    )
    read = _envelope_read(command, path, issued, digest, reader)
    identity = json.dumps(issued["IDENTITY"], sort_keys=True)
    return {
        **full,
        "message": (
            f"DevCrew executor file delivery. Exact assignment identity: {identity}\n"
            "Before editing, run the following read-only command. It verifies the full package and "
            "prints the first segment. For each next segment, append the end offset from the header "
            "as a final argument to the same command. Read through the declared total. "
            "Stop on a missing file, digest error, or incomplete read. "
            "Apply the full package as the executor instructions, including ownership and verification.\n"
            f"{read}"
        ),
    }


def _reviewer_delivery(issued: dict, path: pathlib.Path, reader) -> dict:
    """Derive a compact reviewer spawn that reads the exact issued package."""
    full = issued["SPAWN_PAYLOAD"]
    digest = issued["SPAWN_PAYLOAD_SHA256"]
    if issued["ROLE"] != "reviewer" or _canonical_digest(full) != digest:
        raise DispatchError("INVALID_DISPATCH_ROUTE: reviewer package digest does not match")
    identity = issued["IDENTITY"]
    command = (
        "import hashlib,json,pathlib,sys; "
        "rows=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8'))['issued_routes']; "
        "matches=[r for r in rows if r.get('ROUTE_ID')==sys.argv[2]]; "
        "(row,)=matches; "
        "p=row['SPAWN_PAYLOAD']; "
        "actual=hashlib.sha256(json.dumps(p,ensure_ascii=False,sort_keys=True,"
        "separators=(',',':')).encode('utf-8')).hexdigest(); "
        "actual==sys.argv[3] and row.get('SPAWN_PAYLOAD_SHA256')==actual "
        "or sys.exit('Review package digest mismatch'); "
        "message=p['message']; start=int(sys.argv[4]) if len(sys.argv)>4 else 0; "
        "0<=start<len(message) or sys.exit('Invalid review package offset'); "
        "end=min(start+4000,len(message)); "
        "print(f'Characters {start}:{end} of {len(message)}'); "
        "print(message[start:end],end='')"
    )
    read = _envelope_read(command, path, issued, digest, reader)
    return {
        **full,
        "message": (
            f"DevCrew reviewer file delivery. Exact candidate identity: "
            f"{json.dumps(identity, sort_keys=True)}\n"
            "Before reviewing, run the following read-only command. It verifies the complete issued "
            "candidate review package and prints its first segment. For each next segment, append "
            "the end offset from the header as a final argument to the same command. Read through "
            "the declared total, including ORIGINAL_INTENT, the source, acceptance criteria and "
            "verification evidence. Stop on a missing file, digest error, or incomplete read. "
            "Then perform the independent review using the full package.\n"
            f"{read}"
        ),
    }


def _file_delivery(issued: dict, path: pathlib.Path) -> tuple[dict, dict]:
    if "FILE_DELIVERY" in issued:
        stored = issued["FILE_DELIVERY"]
        if not isinstance(stored, dict):
            raise DispatchError("INVALID_DISPATCH_ROUTE: file delivery is corrupt")
        reader = stored.get("reader")
        if "reader" in stored and (
            not isinstance(reader, dict)
            or set(reader) != {"shell", "python"}
            or reader["shell"] not in ("posix", "powershell")
            or not isinstance(reader["python"], str)
            or not reader["python"]
        ):
            raise DispatchError("INVALID_DISPATCH_ROUTE: file delivery reader is corrupt")
    else:
        reader = {"python": sys.executable, "shell": "powershell" if os.name == "nt" else "posix"}
    role = issued.get("ROLE")
    if role == "executor":
        spawn = _executor_delivery(issued, path, reader)
    elif role == "reviewer":
        spawn = _reviewer_delivery(issued, path, reader)
    else:
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: file delivery supports executor or reviewer routes"
        )
    delivery = {"path": str(path), "spawn_sha256": _canonical_digest(spawn)}
    if reader is not None:
        delivery["reader"] = reader
    return spawn, delivery


def prepare_executor_delivery(
    project_root: str | pathlib.Path, task_id: str, route_id: str
) -> dict:
    """Register file delivery for an existing unconsumed executor issuance."""
    project = pathlib.Path(project_root).resolve()
    path = _store_path(project, task_id)
    with _locked(path):
        store = _read(path)
        matches = [
            row
            for row in store["issued_routes"]
            if isinstance(row, dict) and row.get("ROUTE_ID") == route_id
        ]
        if (
            len(matches) != 1
            or matches[0].get("consumed") is not False
            or matches[0].get("ROLE") != "executor"
            or matches[0].get("TASK_ID") != task_id
        ):
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: an unconsumed executor issuance is required"
            )
        issued = matches[0]
        _validate_authority(
            project,
            task_id,
            "executor",
            issued["IDENTITY"],
            issued["SPAWN_PAYLOAD"],
            issued.get("WORKTREE_BINDING"),
            already_issued=True,
        )
        spawn, delivery = _file_delivery(issued, path)
        if "FILE_DELIVERY" not in issued:
            from devcrew_capacity import _session_file

            blocker_path = _session_file(project, task_id)
            if fs.is_link(blocker_path):
                raise DispatchError("INVALID_DISPATCH_ROUTE: capacity state is unsafe")
            if blocker_path.exists():
                raise DispatchError(
                    "INVALID_DISPATCH_ROUTE: preserve the active capacity retry transport"
                )
            issued["FILE_DELIVERY"] = delivery
            _write(path, store)
        elif issued["FILE_DELIVERY"] != delivery:
            raise DispatchError("INVALID_DISPATCH_ROUTE: existing executor file delivery differs")
    return {
        "spawn": spawn,
        "route": issued["PREPARED"]["route"],
        "dispatch": issued["PREPARED"]["dispatch"],
        "identity": issued["IDENTITY"],
        "spawn_payload_sha256": delivery["spawn_sha256"],
    }


def prepare_reviewer_delivery(
    project_root: str | pathlib.Path, task_id: str, route_id: str
) -> dict:
    """Register file delivery for an exact unconsumed formal reviewer issuance."""
    project = pathlib.Path(project_root).resolve()
    path = _store_path(project, task_id)
    with _locked(path):
        store = _read(path)
        matches = [
            row
            for row in store["issued_routes"]
            if isinstance(row, dict) and row.get("ROUTE_ID") == route_id
        ]
        if (
            len(matches) != 1
            or matches[0].get("consumed") is not False
            or matches[0].get("ROLE") != "reviewer"
            or matches[0].get("TASK_ID") != task_id
        ):
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: an unconsumed reviewer issuance is required"
            )
        issued = matches[0]
        _validate_authority(
            project,
            task_id,
            "reviewer",
            issued["IDENTITY"],
            issued["SPAWN_PAYLOAD"],
            issued.get("WORKTREE_BINDING"),
            already_issued=True,
        )
        spawn, delivery = _file_delivery(issued, path)
        if "FILE_DELIVERY" not in issued:
            from devcrew_capacity import _session_file

            blocker_path = _session_file(project, task_id)
            if fs.is_link(blocker_path):
                raise DispatchError("INVALID_DISPATCH_ROUTE: capacity state is unsafe")
            if blocker_path.exists():
                raise DispatchError(
                    "INVALID_DISPATCH_ROUTE: preserve the active capacity retry transport"
                )
            issued["FILE_DELIVERY"] = delivery
            _write(path, store)
        elif issued["FILE_DELIVERY"] != delivery:
            raise DispatchError("INVALID_DISPATCH_ROUTE: existing reviewer file delivery differs")
    return {
        "spawn": spawn,
        "route": issued["PREPARED"]["route"],
        "dispatch": issued["PREPARED"]["dispatch"],
        "identity": issued["IDENTITY"],
        "spawn_payload_sha256": delivery["spawn_sha256"],
    }


def _issued_spawn_payload(issued: dict, path: pathlib.Path) -> dict:
    if "FILE_DELIVERY" not in issued:
        return issued["SPAWN_PAYLOAD"]
    delivery = issued["FILE_DELIVERY"]
    payload, expected = _file_delivery(issued, path)
    if delivery != expected:
        raise DispatchError("INVALID_DISPATCH_ROUTE: file delivery is corrupt")
    return payload


def _verify_file_delivery_receipt(issued: dict, receipt: dict, path: pathlib.Path) -> None:
    if "FILE_DELIVERY" in issued or "DELIVERED_SPAWN_SHA256" in receipt:
        payload = _issued_spawn_payload(issued, path)
        if (
            "FILE_DELIVERY" not in issued
            or issued.get("consumed") is not True
            or receipt.get("SPAWN_PAYLOAD_SHA256") != issued["SPAWN_PAYLOAD_SHA256"]
            or receipt.get("DELIVERED_SPAWN_SHA256") != _canonical_digest(payload)
        ):
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: file delivery receipt does not match issuance"
            )


def _validate_service_markers(role: str, payload: dict) -> None:
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str):
        raise DispatchError("DISPATCH_ROUTE_REQUIRED: final payload message is missing")
    expected = {"MODEL_ROUTE:": 1, "DISPATCH_SELECTION:": 1}
    if role == "executor":
        expected["PHASE_EXECUTION_CONTRACT:"] = 1
    elif role == "reviewer":
        expected.update({"PHASE_REVIEW_CONTRACT:": 1, "VERIFICATION_EVIDENCE:": 1})
    # Empty registries omit this optional envelope; when present it is canonical
    # and unique, so diagnostics and authority validation see the same marker.
    if message.count("ENGINEERING_REQUIREMENTS:") > 1:
        raise DispatchError(
            "DISPATCH_SERVICE_MARKER_DUPLICATE: final payload must contain at most one requirements marker"
        )
    if any(message.count(marker) != count for marker, count in expected.items()):
        raise DispatchError(
            "DISPATCH_SERVICE_MARKER_DUPLICATE: final payload must contain each required service marker once"
        )


def _write(path: pathlib.Path, store: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(store, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fs.publish(temp_name, path, replace=True)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _locked(path: pathlib.Path):
    class Lock:
        def __enter__(self):
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = path.with_name(path.name + ".lock")
            if fs.is_link(lock_path):
                raise DispatchError("dispatch registry lock must not be a symlink")
            flags = os.O_CREAT | os.O_RDWR
            try:
                self.fd = fs.open_regular(lock_path, flags, 0o600)
                fs.lock(self.fd)
            except OSError as exc:
                raise DispatchError(f"cannot lock dispatch registry: {exc}") from None
            return self

        def __exit__(self, *_exc):
            fs.unlock(self.fd)
            os.close(self.fd)

    return Lock()


def issue_route(
    project_root: str | pathlib.Path,
    *,
    task_id: str,
    role: str,
    identity: dict,
    intent: dict,
    store_path: pathlib.Path | None = None,
) -> dict:
    """Derive and persist the final route and payload from bounded dispatch intent."""
    if role not in {"planner", "executor", "reviewer"} or not isinstance(identity, dict):
        raise DispatchError("DISPATCH_ROUTE_REQUIRED: role and dispatch identity are required")
    if identity.get("TASK_ID") != task_id:
        raise DispatchError("DISPATCH_ROUTE_REQUIRED: identity TASK_ID does not match route owner")
    expected_identity = {
        "planner": {"TASK_ID", "BASE_SHA"},
        "executor": {"TASK_ID", "PHASE_ID", "BASE_SHA", "WRITER_CYCLE", "REJECTED_CANDIDATE_SHA"},
        "reviewer": {"TASK_ID", "PHASE_ID", "BASE_SHA", "TARGET_SHA"},
    }[role]
    if set(identity) != expected_identity:
        raise DispatchError("DISPATCH_ROUTE_REQUIRED: identity fields do not match role contract")
    if not isinstance(intent, dict):
        raise DispatchError("DISPATCH_ROUTE_REQUIRED: bounded dispatch intent is required")
    try:
        from devcrew_context import _derive_dispatch_issuance

        derived = _derive_dispatch_issuance(project_root, task_id, role, identity, intent)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        raise DispatchError(
            str(exc) or "DISPATCH_ROUTE_REQUIRED: dispatch intent could not be derived"
        ) from None
    route, dispatch, spawn_payload = (
        derived.get("route"),
        derived.get("dispatch"),
        derived.get("spawn"),
    )
    route_fields = {
        "ROUTE_ID",
        "ROLE",
        "TASK_CLASS",
        "COORDINATION_COMPLEXITY",
        "DECISION_COMPLEXITY",
        "IMPLEMENTATION_COMPLEXITY",
        "REVIEW_COMPLEXITY",
        "RISK",
        "MODEL_CLASS",
        "RESOLVED_MODEL",
        "REASONING_EFFORT",
        "SELECTION_REASON",
    }
    if (
        not isinstance(route, dict)
        or set(route_fields) - set(route)
        or route.get("ROLE") != role
        or not route.get("ROUTE_ID")
        or not isinstance(dispatch, dict)
        or dispatch.get("SPAWN_MODE") not in {"typed", "generic"}
        or not isinstance(spawn_payload, dict)
        or spawn_payload.get("model") != route.get("RESOLVED_MODEL")
        or spawn_payload.get("reasoning_effort") != route.get("REASONING_EFFORT")
    ):
        raise DispatchError(
            "DISPATCH_ROUTE_REQUIRED: canonical builder returned an incomplete route"
        )
    _validate_service_markers(role, spawn_payload)
    prepared = {"route": route, "dispatch": dispatch}
    record = {
        "ROUTE_ID": route["ROUTE_ID"],
        "TASK_ID": task_id,
        "ROLE": role,
        "IDENTITY": identity,
        "PREPARED": prepared,
        "SPAWN_PAYLOAD": spawn_payload,
        "SPAWN_PAYLOAD_SHA256": _canonical_digest(spawn_payload),
        "consumed": False,
    }
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    with _locked(path):
        store = _read(path)
        matches = [
            item
            for item in store["issued_routes"]
            if isinstance(item, dict) and item.get("ROUTE_ID") == record["ROUTE_ID"]
        ]
        if matches:
            existing = matches[0] if len(matches) == 1 else None
            candidate = (
                {**record, "WORKTREE_BINDING": existing.get("WORKTREE_BINDING")}
                if existing
                else None
            )
            if existing and existing == candidate and existing.get("consumed") is False:
                _validate_authority(
                    project_root,
                    task_id,
                    role,
                    identity,
                    spawn_payload,
                    existing.get("WORKTREE_BINDING"),
                    already_issued=True,
                )
                return dict(existing)
            if existing and existing == candidate and existing.get("consumed") is True:
                raise DispatchError("ROUTE_ID has already been consumed by a spawn")
            raise DispatchError("ROUTE_ID has already been issued with a different dispatch")
        binding = _validate_authority(project_root, task_id, role, identity, spawn_payload)
        record["WORKTREE_BINDING"] = binding
        store["issued_routes"].append(record)
        _write(path, store)
    return record


def issued_for_report(
    project_root: str | pathlib.Path, report: dict, *, store_path: pathlib.Path | None = None
) -> dict:
    task_id = report.get("TASK_ID") if isinstance(report, dict) else None
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    with _locked(path):
        store = _read(path)
        issued = _issued_match(store, report, path)
        return dict(issued)


def _issued_match(store: dict, report: dict, path: pathlib.Path) -> dict:
    prepared = report.get("PREPARED")
    route = prepared.get("route") if isinstance(prepared, dict) else None
    route_id = route.get("ROUTE_ID") if isinstance(route, dict) else None
    matches = [
        item
        for item in store["issued_routes"]
        if isinstance(item, dict) and item.get("ROUTE_ID") == route_id
    ]
    if len(matches) != 1:
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: route was not issued by the final payload builder"
        )
    issued = matches[0]
    identity = issued["IDENTITY"]
    spawn_payload = report.get("SPAWN_PAYLOAD")
    expected_spawn = _issued_spawn_payload(issued, path)
    expected_digest = (
        issued["FILE_DELIVERY"]["spawn_sha256"]
        if "FILE_DELIVERY" in issued
        else issued.get("SPAWN_PAYLOAD_SHA256")
    )
    marker_fields = {
        "TASK_ID",
        "PHASE_ID",
        "BASE_SHA",
        "TARGET_SHA",
        "WRITER_CYCLE",
        "REJECTED_CANDIDATE_SHA",
    }
    substituted_markers = any(
        key not in identity and report.get(key) is not None for key in marker_fields
    )
    reported_root = report.get("TASK_WORKTREE_ROOT")
    binding = issued.get("WORKTREE_BINDING")
    if (
        issued.get("ROLE") in {"executor", "reviewer"}
        and reported_root is not None
        and (
            not isinstance(reported_root, str)
            or not isinstance(binding, dict)
            or not isinstance(binding.get("task_worktree_root"), str)
            or not pathlib.Path(reported_root).is_absolute()
            or pathlib.Path(reported_root).resolve()
            != pathlib.Path(binding["task_worktree_root"]).resolve()
        )
    ):
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: TASK_WORKTREE_ROOT differs from issued worktree binding"
        )
    if (
        issued.get("consumed") is True
        or issued.get("TASK_ID") != report.get("TASK_ID")
        or issued.get("ROLE") != report.get("REQUESTED_ROLE")
        or issued.get("PREPARED") != prepared
        or substituted_markers
        or any(report.get(key) != value for key, value in identity.items())
        or spawn_payload != expected_spawn
        or report.get("SPAWN_PAYLOAD_SHA256") != expected_digest
        or _canonical_digest(spawn_payload) != expected_digest
        or report.get("SPAWN_MODEL") != route.get("RESOLVED_MODEL")
        or report.get("SPAWN_REASONING_EFFORT") != route.get("REASONING_EFFORT")
        or report.get("SPAWN_MODE") != (prepared.get("dispatch") or {}).get("SPAWN_MODE")
    ):
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: postflight differs from issued route, identity or payload"
        )
    return issued


def _require_fresh_executor_context(
    store: dict,
    *,
    task_id: str,
    phase_id: str,
    agent_id: str,
    fork_turns: Any,
    reject_same_phase_reuse: bool = False,
) -> None:
    """Reject executor authority inherited from a different independent phase."""
    if fork_turns != "none":
        raise DispatchError("authoritative executor requires a fresh executor context")
    for receipt in store.get("receipts", []):
        if (
            isinstance(receipt, dict)
            and receipt.get("ROLE") == "executor"
            and receipt.get("TASK_ID") == task_id
            and receipt.get("AGENT_ID") == agent_id
        ):
            if receipt.get("PHASE_ID") != phase_id:
                raise DispatchError("executor agent was already used for a different phase")
            if reject_same_phase_reuse:
                raise DispatchError(
                    "same-phase executor followup requires a linked continuation receipt"
                )


def _require_independent_reviewer(
    store: dict, *, task_id: str, agent_id: str, current_receipt_id: str | None = None
) -> None:
    for receipt in store.get("receipts", []):
        if (
            isinstance(receipt, dict)
            and receipt.get("TASK_ID") == task_id
            and receipt.get("AGENT_ID") == agent_id
            and (
                current_receipt_id is None or receipt.get("SPAWN_RECEIPT_ID") != current_receipt_id
            )
        ):
            if receipt.get("ROLE") == "executor":
                raise DispatchError("reviewer agent must be independent from the executor")
            if receipt.get("ROLE") == "reviewer":
                raise DispatchError("reviewer AGENT_ID has already been used for this task")


def verify_historical_planner(
    project_root: str | pathlib.Path,
    receipt_id: str,
    *,
    task_id: str,
    base_sha: str,
    generation: int,
    planner_dispatch: dict | None = None,
    expected_worktree_binding: dict | None = None,
    store_path: pathlib.Path | None = None,
) -> dict:
    """Validate archived planner authority using only the persisted local stores.

    This is deliberately a leaf: historical checks must not resolve the live
    worktree or ask current role authority to validate a route from the past.
    """
    if (
        not isinstance(receipt_id, str)
        or not receipt_id
        or type(generation) is not int
        or generation < 0
    ):
        raise DispatchError("INVALID_DISPATCH_ROUTE: historical planner identity is invalid")
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    try:
        store = _read(path)
        receipts = [
            item
            for item in store["receipts"]
            if isinstance(item, dict) and item.get("SPAWN_RECEIPT_ID") == receipt_id
        ]
        if len(receipts) != 1:
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical planner receipt is missing or ambiguous"
            )
        receipt = receipts[0]
        issued_matches = [
            item
            for item in store["issued_routes"]
            if isinstance(item, dict) and item.get("ROUTE_ID") == receipt.get("ROUTE_ID")
        ]
        if len(issued_matches) != 1:
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical planner issuance is missing or ambiguous"
            )
        issued = issued_matches[0]
        payload = issued.get("SPAWN_PAYLOAD")
        prepared = issued.get("PREPARED")
        route = prepared.get("route") if isinstance(prepared, dict) else None
        dispatch = prepared.get("dispatch") if isinstance(prepared, dict) else None
        binding = issued.get("WORKTREE_BINDING")
        expected_summary = {
            "spawn_receipt_id": receipt_id,
            "route_id": receipt.get("ROUTE_ID"),
            "agent_id": receipt.get("AGENT_ID"),
            "model": receipt.get("RESOLVED_MODEL"),
            "effort": receipt.get("REASONING_EFFORT"),
            "spawn_mode": receipt.get("SPAWN_MODE"),
        }
        binding_keys = {"repository_id", "task_id", "task_worktree_root", "branch", "expected_head"}
        if generation:
            binding_keys.add("contract_generation")
        binding_ok = isinstance(binding, dict) and set(binding) == binding_keys
        binding_ok = (
            binding_ok
            and binding.get("task_id") == task_id
            and binding.get("repository_id")
            and isinstance(binding.get("task_worktree_root"), str)
            and pathlib.Path(binding["task_worktree_root"]).is_absolute()
            and isinstance(binding.get("branch"), str)
            and binding.get("expected_head") == base_sha
            and binding.get("contract_generation", 0) == generation
        )
        if expected_worktree_binding is not None:
            binding_ok = binding_ok and binding == expected_worktree_binding
        valid = (
            receipt.get("ROLE") == "planner"
            and receipt.get("TASK_ID") == task_id
            and receipt.get("BASE_SHA") == base_sha
            and receipt.get("PHASE_ID") is None
            and receipt.get("TARGET_SHA") is None
            and receipt.get("WRITER_CYCLE") is None
            and receipt.get("REJECTED_CANDIDATE_SHA") is None
            and receipt.get("CONTRACT_GENERATION", 0) == generation
            and isinstance(route, dict)
            and route.get("ROLE") == "planner"
            and route.get("ROUTE_ID") == receipt.get("ROUTE_ID")
            and route.get("RESOLVED_MODEL") == receipt.get("RESOLVED_MODEL")
            and route.get("REASONING_EFFORT") == receipt.get("REASONING_EFFORT")
            and isinstance(dispatch, dict)
            and dispatch.get("SPAWN_MODE") == receipt.get("SPAWN_MODE")
            and receipt.get("route") == route
            and receipt.get("dispatch") == dispatch
            and issued.get("TASK_ID") == task_id
            and issued.get("ROLE") == "planner"
            and issued.get("IDENTITY") == {"TASK_ID": task_id, "BASE_SHA": base_sha}
            and issued.get("consumed") is True
            and isinstance(payload, dict)
            and issued.get("SPAWN_PAYLOAD_SHA256") == _canonical_digest(payload)
            and receipt.get("SPAWN_PAYLOAD_SHA256") == issued.get("SPAWN_PAYLOAD_SHA256")
            and isinstance(receipt.get("AGENT_ID"), str)
            and bool(receipt["AGENT_ID"].strip())
            and isinstance(receipt.get("FORK_TURNS"), (str, type(None)))
            and binding_ok
        )
        if planner_dispatch is not None:
            valid = (
                valid
                and planner_dispatch.get("spawn_receipt_id") == receipt_id
                and all(
                    planner_dispatch.get(key) == value
                    for key, value in expected_summary.items()
                    if key in planner_dispatch
                )
                and (
                    "authorization_generation" not in planner_dispatch
                    or (
                        planner_dispatch.get("authorization_generation") == generation
                        and planner_dispatch.get("reapproval_authorizer") is True
                    )
                )
                and (
                    "reapproval_authorizer" not in planner_dispatch
                    or planner_dispatch.get("reapproval_authorizer") is True
                )
            )
        if not valid:
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical planner receipt or issuance is inconsistent"
            )
        return dict(receipt)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DispatchError):
            raise
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: historical planner authority is corrupt"
        ) from None


def verify_historical_executor(
    project_root: str | pathlib.Path,
    receipt_id: str,
    *,
    task_id: str,
    phase_id: str,
    base_sha: str,
    writer_cycle: int,
    rejected_candidate_sha: str | None,
    generation: int,
    executor_dispatch: dict | None = None,
    expected_worktree_binding: dict | None = None,
    store_path: pathlib.Path | None = None,
) -> dict:
    """Validate an archived executor receipt without treating it as current authority.

    Historical rejected-candidate cycles must remain auditable after a contract
    generation bump. This leaf reads only the persisted issuance/receipt pair;
    callers must never use it to authorize a new writer.
    """
    if (
        not isinstance(receipt_id, str)
        or not receipt_id
        or not isinstance(phase_id, str)
        or not phase_id
        or not isinstance(base_sha, str)
        or type(writer_cycle) is not int
        or writer_cycle < 0
        or type(generation) is not int
        or generation < 0
    ):
        raise DispatchError("INVALID_DISPATCH_ROUTE: historical executor identity is invalid")
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    try:
        store = _read(path)
        receipts = [
            item
            for item in store["receipts"]
            if isinstance(item, dict) and item.get("SPAWN_RECEIPT_ID") == receipt_id
        ]
        if len(receipts) != 1:
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical executor receipt is missing or ambiguous"
            )
        receipt = receipts[0]
        issued_matches = [
            item
            for item in store["issued_routes"]
            if isinstance(item, dict) and item.get("ROUTE_ID") == receipt.get("ROUTE_ID")
        ]
        if len(issued_matches) != 1:
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical executor issuance is missing or ambiguous"
            )
        issued = issued_matches[0]
        payload = issued.get("SPAWN_PAYLOAD")
        prepared = issued.get("PREPARED")
        route = prepared.get("route") if isinstance(prepared, dict) else None
        dispatch = prepared.get("dispatch") if isinstance(prepared, dict) else None
        binding = issued.get("WORKTREE_BINDING")
        binding_keys = {"repository_id", "task_id", "task_worktree_root", "branch", "expected_head"}
        if generation:
            binding_keys.add("contract_generation")
        binding_ok = (
            isinstance(binding, dict)
            and set(binding) == binding_keys
            and binding.get("task_id") == task_id
            and binding.get("repository_id")
            and isinstance(binding.get("task_worktree_root"), str)
            and pathlib.Path(binding["task_worktree_root"]).is_absolute()
            and isinstance(binding.get("branch"), str)
            and binding.get("expected_head") == base_sha
            and binding.get("contract_generation", 0) == generation
        )
        if expected_worktree_binding is not None:
            binding_ok = binding_ok and binding == expected_worktree_binding
        identity = {
            "TASK_ID": task_id,
            "PHASE_ID": phase_id,
            "BASE_SHA": base_sha,
            "WRITER_CYCLE": writer_cycle,
            "REJECTED_CANDIDATE_SHA": rejected_candidate_sha,
        }
        valid = (
            receipt.get("ROLE") == "executor"
            and receipt.get("TASK_ID") == task_id
            and receipt.get("PHASE_ID") == phase_id
            and receipt.get("BASE_SHA") == base_sha
            and receipt.get("TARGET_SHA") is None
            and receipt.get("WRITER_CYCLE") == writer_cycle
            and receipt.get("REJECTED_CANDIDATE_SHA") == rejected_candidate_sha
            and receipt.get("CONTRACT_GENERATION", 0) == generation
            and receipt.get("FORK_TURNS") == "none"
            and isinstance(route, dict)
            and route.get("ROLE") == "executor"
            and route.get("ROUTE_ID") == receipt.get("ROUTE_ID")
            and route.get("RESOLVED_MODEL") == receipt.get("RESOLVED_MODEL")
            and route.get("REASONING_EFFORT") == receipt.get("REASONING_EFFORT")
            and isinstance(dispatch, dict)
            and dispatch.get("SPAWN_MODE") == receipt.get("SPAWN_MODE")
            and receipt.get("route") == route
            and receipt.get("dispatch") == dispatch
            and issued.get("TASK_ID") == task_id
            and issued.get("ROLE") == "executor"
            and issued.get("IDENTITY") == identity
            and issued.get("consumed") is True
            and isinstance(payload, dict)
            and issued.get("SPAWN_PAYLOAD_SHA256") == _canonical_digest(payload)
            and receipt.get("SPAWN_PAYLOAD_SHA256") == issued.get("SPAWN_PAYLOAD_SHA256")
            and isinstance(receipt.get("AGENT_ID"), str)
            and bool(receipt["AGENT_ID"].strip())
            and binding_ok
        )
        if executor_dispatch is not None:
            summary = {
                "spawn_receipt_id": receipt_id,
                "writer_cycle": writer_cycle,
                "rejected_candidate_sha": rejected_candidate_sha,
                "route_id": receipt.get("ROUTE_ID"),
                "agent_id": receipt.get("AGENT_ID"),
                "model": receipt.get("RESOLVED_MODEL"),
                "effort": receipt.get("REASONING_EFFORT"),
                "spawn_mode": receipt.get("SPAWN_MODE"),
            }
            valid = valid and executor_dispatch == summary
        if not valid:
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical executor receipt or issuance is inconsistent"
            )
        _verify_file_delivery_receipt(issued, receipt, path)
        # A continuation is a distinct issued route, but keeps the same writer
        # identity. Authenticate its origin in the same archived generation.
        if receipt.get("CONTINUATION") is True:
            origin = verify_historical_executor(
                project_root,
                receipt.get("ORIGIN_SPAWN_RECEIPT_ID"),
                task_id=task_id,
                phase_id=phase_id,
                base_sha=base_sha,
                writer_cycle=writer_cycle,
                rejected_candidate_sha=rejected_candidate_sha,
                generation=generation,
                expected_worktree_binding=expected_worktree_binding,
                store_path=path,
            )
            if (
                origin.get("CONTINUATION") is True
                or origin.get("AGENT_ID") != receipt.get("AGENT_ID")
                or origin.get("RESOLVED_MODEL") != receipt.get("RESOLVED_MODEL")
                or origin.get("REASONING_EFFORT") != receipt.get("REASONING_EFFORT")
                or origin.get("SPAWN_MODE") != receipt.get("SPAWN_MODE")
            ):
                raise DispatchError(
                    "INVALID_DISPATCH_ROUTE: historical executor continuation is inconsistent"
                )
        elif receipt.get("CONTINUATION") not in (None, False):
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: historical executor continuation marker is invalid"
            )
        _require_fresh_executor_context(
            store,
            task_id=task_id,
            phase_id=phase_id,
            agent_id=receipt["AGENT_ID"],
            fork_turns=receipt["FORK_TURNS"],
        )
        return dict(receipt)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DispatchError):
            raise
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: historical executor authority is corrupt"
        ) from None


def validate_issued(
    project_root: str | pathlib.Path, report: dict, *, store_path: pathlib.Path | None = None
) -> dict:
    """Verify that a postflight report exactly matches an unconsumed issuance."""
    task_id = report.get("TASK_ID") if isinstance(report, dict) else None
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    with _locked(path):
        issued = dict(_issued_match(_read(path), report, path))
    _validate_authority(
        project_root,
        task_id,
        issued["ROLE"],
        issued["IDENTITY"],
        issued["SPAWN_PAYLOAD"],
        issued.get("WORKTREE_BINDING"),
        already_issued=True,
    )
    return issued


def record_spawn(
    project_root: str | pathlib.Path, report: dict, *, store_path: pathlib.Path | None = None
) -> dict:
    """Record a successful observed spawn, bound to its exact prepared route."""
    if not isinstance(report, dict) or report.get("STATUS") != "SPAWN_SUCCESS":
        raise DispatchError("spawn receipt requires confirmed SPAWN_SUCCESS")
    task_id, agent_id = report.get("TASK_ID"), report.get("AGENT_ID")
    prepared = report.get("PREPARED")
    route = prepared.get("route") if isinstance(prepared, dict) else None
    dispatch = prepared.get("dispatch") if isinstance(prepared, dict) else None
    route_fields = {
        "ROUTE_ID",
        "ROLE",
        "TASK_CLASS",
        "COORDINATION_COMPLEXITY",
        "DECISION_COMPLEXITY",
        "IMPLEMENTATION_COMPLEXITY",
        "REVIEW_COMPLEXITY",
        "RISK",
        "MODEL_CLASS",
        "RESOLVED_MODEL",
        "REASONING_EFFORT",
        "SELECTION_REASON",
    }
    if (
        not isinstance(route, dict)
        or set(route_fields) - set(route)
        or not route.get("ROUTE_ID")
        or route.get("ROLE") not in {"planner", "executor", "reviewer"}
    ):
        raise DispatchError(
            "DISPATCH_ROUTE_REQUIRED: successful spawn needs a matching route receipt"
        )
    if not isinstance(dispatch, dict) or dispatch.get("SPAWN_MODE") not in {"typed", "generic"}:
        raise DispatchError("DISPATCH_ROUTE_REQUIRED: spawn mode must come from route preflight")
    if not isinstance(agent_id, str) or not agent_id.strip():
        raise DispatchError("spawn receipt requires the runtime AGENT_ID")
    if (
        report.get("SPAWN_MODEL") != route.get("RESOLVED_MODEL")
        or report.get("SPAWN_REASONING_EFFORT") != route.get("REASONING_EFFORT")
        or report.get("SPAWN_MODE") != dispatch.get("SPAWN_MODE")
    ):
        raise DispatchError("spawn receipt actual model/effort/mode does not match prepared route")
    role = route["ROLE"]
    if report.get("REQUESTED_ROLE") != role:
        raise DispatchError("spawn receipt requested role differs from prepared route")
    fork_turns = report.get("FORK_TURNS")
    if role == "reviewer" and fork_turns != "none":
        raise DispatchError("authoritative reviewer requires a fresh reviewer context")
    if role == "executor" and fork_turns != "none":
        raise DispatchError("authoritative executor requires a fresh executor context")
    for field in ("RESOLVED_MODEL", "REASONING_EFFORT"):
        if not isinstance(route.get(field), str) or not route[field]:
            raise DispatchError(f"DISPATCH_ROUTE_REQUIRED: route must resolve {field}")
    if role == "planner":
        fields = ("BASE_SHA",)
        if report.get("PHASE_ID") is not None:
            raise DispatchError("planner dispatch receipt cannot name a phase")
    elif role == "executor":
        fields = ("BASE_SHA",)
        if not isinstance(report.get("PHASE_ID"), str) or not report["PHASE_ID"]:
            raise DispatchError("executor dispatch receipt requires PHASE_ID")
        if report.get("TARGET_SHA") is not None:
            raise DispatchError(
                "executor dispatch receipt cannot target a candidate before candidate creation"
            )
    else:
        fields = ("BASE_SHA", "TARGET_SHA")
        if not isinstance(report.get("PHASE_ID"), str) or not report["PHASE_ID"]:
            raise DispatchError("reviewer dispatch receipt requires PHASE_ID")
    for field in fields:
        value = report.get(field)
        if not isinstance(value, str) or len(value) not in (40, 64):
            raise DispatchError(f"phase dispatch receipt requires exact {field}")
    if role == "executor":
        cycle = report.get("WRITER_CYCLE")
        rejected = report.get("REJECTED_CANDIDATE_SHA")
        if type(cycle) is not int or cycle < 0:
            raise DispatchError("executor dispatch receipt requires a non-negative WRITER_CYCLE")
        if cycle == 0 and rejected is not None:
            raise DispatchError("initial executor dispatch cannot name a rejected candidate")
        if cycle > 0 and (not isinstance(rejected, str) or len(rejected) not in (40, 64)):
            raise DispatchError("repair executor dispatch requires exact REJECTED_CANDIDATE_SHA")
    receipt = {
        "SPAWN_RECEIPT_ID": uuid.uuid4().hex,
        "ROUTE_ID": route["ROUTE_ID"],
        "ROLE": role,
        "RESOLVED_MODEL": report["SPAWN_MODEL"],
        "REASONING_EFFORT": report["SPAWN_REASONING_EFFORT"],
        "SPAWN_MODE": report["SPAWN_MODE"],
        "AGENT_ID": agent_id,
        "TASK_ID": task_id,
        "PHASE_ID": report.get("PHASE_ID"),
        "BASE_SHA": report.get("BASE_SHA"),
        "TARGET_SHA": report.get("TARGET_SHA"),
        "WRITER_CYCLE": report.get("WRITER_CYCLE"),
        "REJECTED_CANDIDATE_SHA": report.get("REJECTED_CANDIDATE_SHA"),
        "CONTRACT_GENERATION": None,
        "FORK_TURNS": fork_turns,
        "route": route,
        "dispatch": dispatch,
    }
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    with _locked(path):
        store = _read(path)
        issued = _issued_match(store, report, path)
        if role == "executor":
            _require_fresh_executor_context(
                store,
                task_id=task_id,
                phase_id=report["PHASE_ID"],
                agent_id=agent_id,
                fork_turns=fork_turns,
                reject_same_phase_reuse=True,
            )
        elif role == "reviewer":
            _require_independent_reviewer(store, task_id=task_id, agent_id=agent_id)
        _validate_authority(
            project_root,
            task_id,
            role,
            issued["IDENTITY"],
            issued["SPAWN_PAYLOAD"],
            issued.get("WORKTREE_BINDING"),
            already_issued=True,
        )
        receipt["CONTRACT_GENERATION"] = (issued.get("WORKTREE_BINDING") or {}).get(
            "contract_generation", 0
        )
        if any(
            item.get("ROUTE_ID") == receipt["ROUTE_ID"]
            for item in store["receipts"]
            if isinstance(item, dict)
        ):
            raise DispatchError("route receipt has already been consumed by a spawn")
        issued["consumed"] = True
        receipt["SPAWN_PAYLOAD_SHA256"] = issued["SPAWN_PAYLOAD_SHA256"]
        if "FILE_DELIVERY" in issued:
            receipt["DELIVERED_SPAWN_SHA256"] = report["SPAWN_PAYLOAD_SHA256"]
        store["receipts"].append(receipt)
        _write(path, store)
    return receipt


def _active_local_fix(
    project_root: pathlib.Path, task_id: str, phase_id: str, cycle: int, rejected_sha: str
) -> dict:
    """Return the persisted current repair authority or fail closed."""
    try:
        import devcrew_phase

        manifest = json.loads(
            devcrew_phase.manifest_path(project_root, task_id).read_text(encoding="utf-8")
        )
        phase = next(row for row in manifest["phases"] if row.get("phase_id") == phase_id)
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        raise DispatchError(
            "INVALID_EXECUTOR_CONTINUATION: current local-fix authority is unavailable"
        ) from None
    active = phase.get("active_local_fix")
    if (
        manifest.get("current_phase_id") != phase_id
        or phase.get("status") not in {"EXECUTING", "VERIFYING"}
        or not isinstance(active, dict)
        or active.get("cycle_id") != cycle
        or active.get("rejected_candidate_sha") != rejected_sha
    ):
        raise DispatchError("INVALID_EXECUTOR_CONTINUATION: cycle or rejected candidate is stale")
    return phase


def record_executor_continuation(
    project_root: str | pathlib.Path, report: dict, *, store_path: pathlib.Path | None = None
) -> dict:
    """Record an observed same-phase followup linked to its original spawn receipt."""
    if not isinstance(report, dict) or report.get("STATUS") != "FOLLOWUP_SUCCESS":
        raise DispatchError("executor continuation requires confirmed FOLLOWUP_SUCCESS")
    task_id, phase_id = report.get("TASK_ID"), report.get("PHASE_ID")
    cycle, rejected = report.get("WRITER_CYCLE"), report.get("REJECTED_CANDIDATE_SHA")
    agent_id, origin_id = report.get("AGENT_ID"), report.get("ORIGIN_SPAWN_RECEIPT_ID")
    if (
        report.get("REQUESTED_ROLE") != "executor"
        or not isinstance(phase_id, str)
        or type(cycle) is not int
        or cycle < 1
        or not isinstance(rejected, str)
        or not isinstance(agent_id, str)
        or not agent_id.strip()
        or not isinstance(origin_id, str)
        or not origin_id
    ):
        raise DispatchError("INVALID_EXECUTOR_CONTINUATION: required identity is missing")
    project = pathlib.Path(project_root).resolve()
    path = _store_path(
        project, task_id, pathlib.Path(store_path) if store_path is not None else None
    )
    with _locked(path):
        store = _read(path)
        origin_matches = [
            row
            for row in store["receipts"]
            if isinstance(row, dict) and row.get("SPAWN_RECEIPT_ID") == origin_id
        ]
        if len(origin_matches) != 1:
            raise DispatchError(
                "INVALID_EXECUTOR_CONTINUATION: origin spawn receipt is missing or ambiguous"
            )
        origin = origin_matches[0]
        if (
            origin.get("CONTINUATION") is True
            or origin.get("ROLE") != "executor"
            or origin.get("TASK_ID") != task_id
            or origin.get("PHASE_ID") != phase_id
            or origin.get("BASE_SHA") != report.get("BASE_SHA")
            or origin.get("AGENT_ID") != agent_id
            or origin.get("FORK_TURNS") != "none"
            or origin.get("RESOLVED_MODEL") != report.get("SPAWN_MODEL")
            or origin.get("REASONING_EFFORT") != report.get("SPAWN_REASONING_EFFORT")
            or origin.get("SPAWN_MODE") != report.get("SPAWN_MODE")
        ):
            raise DispatchError(
                "INVALID_EXECUTOR_CONTINUATION: origin identity or configuration differs"
            )
        try:
            verify_spawn(
                project,
                origin_id,
                task_id=task_id,
                role="executor",
                phase_id=phase_id,
                base_sha=report.get("BASE_SHA"),
                agent_id=agent_id,
                writer_cycle=origin.get("WRITER_CYCLE"),
                rejected_candidate_sha=origin.get("REJECTED_CANDIDATE_SHA"),
                store_path=path,
            )
        except DispatchError as exc:
            raise DispatchError(
                f"INVALID_EXECUTOR_CONTINUATION: origin receipt is invalid: {exc}"
            ) from None
        issued = _issued_match(store, report, path)
        if "FILE_DELIVERY" in issued:
            raise DispatchError(
                "INVALID_EXECUTOR_CONTINUATION: file delivery requires a fresh spawn"
            )
        identity = issued.get("IDENTITY")
        if (
            issued.get("ROLE") != "executor"
            or identity
            != {
                "TASK_ID": task_id,
                "PHASE_ID": phase_id,
                "BASE_SHA": report.get("BASE_SHA"),
                "WRITER_CYCLE": cycle,
                "REJECTED_CANDIDATE_SHA": rejected,
            }
            or issued.get("consumed") is not False
            or issued.get("SPAWN_PAYLOAD_SHA256") != _canonical_digest(issued.get("SPAWN_PAYLOAD"))
            or issued.get("SPAWN_PAYLOAD_SHA256") != report.get("SPAWN_PAYLOAD_SHA256")
            or issued.get("SPAWN_PAYLOAD", {}).get("model") != origin.get("RESOLVED_MODEL")
            or issued.get("SPAWN_PAYLOAD", {}).get("reasoning_effort")
            != origin.get("REASONING_EFFORT")
            or (issued.get("PREPARED", {}).get("dispatch") or {}).get("SPAWN_MODE")
            != origin.get("SPAWN_MODE")
        ):
            raise DispatchError(
                "INVALID_EXECUTOR_CONTINUATION: route or payload differs from origin"
            )
        _validate_authority(
            project,
            task_id,
            "executor",
            identity,
            issued["SPAWN_PAYLOAD"],
            issued.get("WORKTREE_BINDING"),
            already_issued=True,
        )
        _active_local_fix(project, task_id, phase_id, cycle, rejected)
        if any(
            item.get("ROUTE_ID") == issued.get("ROUTE_ID")
            for item in store["receipts"]
            if isinstance(item, dict)
        ):
            raise DispatchError("INVALID_EXECUTOR_CONTINUATION: issued route already has a receipt")
        receipt = {
            "ROUTE_ID": issued["ROUTE_ID"],
            "ROLE": "executor",
            "RESOLVED_MODEL": origin["RESOLVED_MODEL"],
            "REASONING_EFFORT": origin["REASONING_EFFORT"],
            "SPAWN_MODE": origin["SPAWN_MODE"],
            "AGENT_ID": agent_id,
            "TASK_ID": task_id,
            "PHASE_ID": phase_id,
            "BASE_SHA": report["BASE_SHA"],
            "CONTRACT_GENERATION": (issued.get("WORKTREE_BINDING") or {}).get(
                "contract_generation", 0
            ),
            "FORK_TURNS": origin["FORK_TURNS"],
            "route": issued["PREPARED"]["route"],
            "dispatch": issued["PREPARED"]["dispatch"],
            "SPAWN_PAYLOAD_SHA256": issued["SPAWN_PAYLOAD_SHA256"],
        }
        receipt.update(
            {
                "SPAWN_RECEIPT_ID": uuid.uuid4().hex,
                "TARGET_SHA": None,
                "WRITER_CYCLE": cycle,
                "REJECTED_CANDIDATE_SHA": rejected,
                "CONTINUATION": True,
                "ORIGIN_SPAWN_RECEIPT_ID": origin_id,
            }
        )
        issued["consumed"] = True
        store["receipts"].append(receipt)
        _write(path, store)
    return receipt


def _verify_executor_continuation(
    store: dict, receipt: dict, project_root: pathlib.Path, task_id: str, phase_id: str | None
) -> None:
    origin_id = receipt.get("ORIGIN_SPAWN_RECEIPT_ID")
    origins = [
        row
        for row in store.get("receipts", [])
        if isinstance(row, dict)
        and row.get("SPAWN_RECEIPT_ID") == origin_id
        and row.get("CONTINUATION") is not True
    ]
    if len(origins) != 1:
        raise DispatchError("INVALID_DISPATCH_ROUTE: continuation origin is missing or ambiguous")
    origin = origins[0]
    issued_matches = [
        row
        for row in store.get("issued_routes", [])
        if isinstance(row, dict) and row.get("ROUTE_ID") == receipt.get("ROUTE_ID")
    ]
    issued_valid = False
    if len(issued_matches) == 1:
        issued = issued_matches[0]
        prepared = issued.get("PREPARED")
        issued_valid = (
            issued.get("consumed") is True
            and issued.get("TASK_ID") == task_id
            and issued.get("ROLE") == "executor"
            and issued.get("IDENTITY")
            == {
                "TASK_ID": task_id,
                "PHASE_ID": phase_id,
                "BASE_SHA": receipt.get("BASE_SHA"),
                "WRITER_CYCLE": receipt.get("WRITER_CYCLE"),
                "REJECTED_CANDIDATE_SHA": receipt.get("REJECTED_CANDIDATE_SHA"),
            }
            and isinstance(prepared, dict)
            and prepared.get("route") == receipt.get("route")
            and prepared.get("dispatch") == receipt.get("dispatch")
            and issued.get("SPAWN_PAYLOAD_SHA256") == receipt.get("SPAWN_PAYLOAD_SHA256")
            and issued.get("SPAWN_PAYLOAD_SHA256") == _canonical_digest(issued.get("SPAWN_PAYLOAD"))
            and issued.get("SPAWN_PAYLOAD", {}).get("model") == origin.get("RESOLVED_MODEL")
            and issued.get("SPAWN_PAYLOAD", {}).get("reasoning_effort")
            == origin.get("REASONING_EFFORT")
            and (prepared.get("dispatch") or {}).get("SPAWN_MODE") == origin.get("SPAWN_MODE")
            and issued.get("WORKTREE_BINDING") is not None
        )
    if (
        receipt.get("ROLE") != "executor"
        or receipt.get("TASK_ID") != task_id
        or receipt.get("PHASE_ID") != phase_id
        or origin.get("PHASE_ID") != phase_id
        or origin.get("TASK_ID") != task_id
        or origin.get("BASE_SHA") != receipt.get("BASE_SHA")
        or origin.get("AGENT_ID") != receipt.get("AGENT_ID")
        or origin.get("FORK_TURNS") != "none"
        or receipt.get("RESOLVED_MODEL") != origin.get("RESOLVED_MODEL")
        or receipt.get("REASONING_EFFORT") != origin.get("REASONING_EFFORT")
        or receipt.get("SPAWN_MODE") != origin.get("SPAWN_MODE")
        or not issued_valid
    ):
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: continuation does not match its original spawn"
        )
    try:
        verify_spawn(
            project_root,
            origin.get("SPAWN_RECEIPT_ID"),
            task_id=task_id,
            role="executor",
            phase_id=phase_id,
            base_sha=receipt.get("BASE_SHA"),
            agent_id=receipt.get("AGENT_ID"),
            writer_cycle=origin.get("WRITER_CYCLE"),
            rejected_candidate_sha=origin.get("REJECTED_CANDIDATE_SHA"),
        )
    except DispatchError as exc:
        raise DispatchError(
            f"INVALID_DISPATCH_ROUTE: continuation origin is no longer valid: {exc}"
        ) from None
    if not _continuation_is_manifested(project_root, task_id, phase_id, receipt):
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: continuation cycle is not active or preserved in phase history"
        )


def _continuation_is_manifested(
    project_root: pathlib.Path, task_id: str, phase_id: str | None, receipt: dict
) -> bool:
    try:
        import devcrew_phase

        manifest = json.loads(
            devcrew_phase.manifest_path(project_root, task_id).read_text(encoding="utf-8")
        )
        phase = next(row for row in manifest["phases"] if row.get("phase_id") == phase_id)
    except (OSError, ValueError, KeyError, StopIteration, TypeError):
        return False
    cycle = receipt.get("WRITER_CYCLE")
    rejected = receipt.get("REJECTED_CANDIDATE_SHA")
    active = phase.get("active_local_fix")
    if (
        manifest.get("current_phase_id") == phase_id
        and isinstance(active, dict)
        and active.get("cycle_id") == cycle
        and active.get("rejected_candidate_sha") == rejected
    ):
        return True

    def matches(dispatch):
        return (
            isinstance(dispatch, dict)
            and dispatch.get("spawn_receipt_id") == receipt.get("SPAWN_RECEIPT_ID")
            and dispatch.get("writer_cycle") == cycle
            and dispatch.get("rejected_candidate_sha") == rejected
            and dispatch.get("route_id") == receipt.get("ROUTE_ID")
            and dispatch.get("agent_id") == receipt.get("AGENT_ID")
        )

    return matches(phase.get("executor_dispatch")) or any(
        matches(row.get("executor_dispatch"))
        for row in phase.get("review_cycles", [])
        if isinstance(row, dict)
    )


def verify_spawn(
    project_root: str | pathlib.Path,
    receipt_id: str,
    *,
    task_id: str,
    role: str,
    phase_id: str | None = None,
    base_sha: str | None = None,
    target_sha: str | None = None,
    agent_id: str | None = None,
    route_id: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    spawn_mode: str | None = None,
    writer_cycle: int | None = None,
    rejected_candidate_sha: str | None = None,
    store_path: pathlib.Path | None = None,
) -> bool:
    """Require a persisted routed spawn matching the requested dispatch identity."""
    if not isinstance(receipt_id, str) or not receipt_id:
        raise DispatchError("INVALID_DISPATCH_ROUTE: routed spawn receipt is required")
    path = _store_path(
        pathlib.Path(project_root).resolve(),
        task_id,
        pathlib.Path(store_path) if store_path is not None else None,
    )
    store = _read(path)
    matches = [
        item
        for item in store["receipts"]
        if isinstance(item, dict) and item.get("SPAWN_RECEIPT_ID") == receipt_id
    ]
    if len(matches) != 1:
        raise DispatchError("INVALID_DISPATCH_ROUTE: spawn receipt is missing or ambiguous")
    receipt = matches[0]
    if receipt.get("CONTINUATION") is True:
        _verify_executor_continuation(
            store,
            receipt,
            pathlib.Path(project_root).resolve(),
            task_id,
            phase_id or receipt.get("PHASE_ID"),
        )
    if (
        receipt.get("ROLE") != role
        or receipt.get("TASK_ID") != task_id
        or (phase_id is not None and receipt.get("PHASE_ID") != phase_id)
        or (base_sha is not None and receipt.get("BASE_SHA") != base_sha)
        or (target_sha is not None and receipt.get("TARGET_SHA") != target_sha)
        or (route_id is not None and receipt.get("ROUTE_ID") != route_id)
        or (model is not None and receipt.get("RESOLVED_MODEL") != model)
        or (effort is not None and receipt.get("REASONING_EFFORT") != effort)
        or (spawn_mode is not None and receipt.get("SPAWN_MODE") != spawn_mode)
        or (writer_cycle is not None and receipt.get("WRITER_CYCLE") != writer_cycle)
        or receipt.get("REJECTED_CANDIDATE_SHA") != rejected_candidate_sha
        or (role == "reviewer" and receipt.get("FORK_TURNS") != "none")
        or (agent_id is not None and receipt.get("AGENT_ID") != agent_id)
    ):
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: spawn receipt does not match dispatch identity"
        )
    if role == "executor":
        _require_fresh_executor_context(
            store,
            task_id=task_id,
            phase_id=receipt.get("PHASE_ID"),
            agent_id=receipt.get("AGENT_ID"),
            fork_turns=receipt.get("FORK_TURNS"),
        )
    elif role == "reviewer":
        _require_independent_reviewer(
            store, task_id=task_id, agent_id=receipt.get("AGENT_ID"), current_receipt_id=receipt_id
        )
    route = receipt.get("route")
    dispatch = receipt.get("dispatch")
    if (
        not isinstance(route, dict)
        or route.get("ROUTE_ID") != receipt.get("ROUTE_ID")
        or route.get("ROLE") != role
        or route.get("RESOLVED_MODEL") != receipt.get("RESOLVED_MODEL")
        or route.get("REASONING_EFFORT") != receipt.get("REASONING_EFFORT")
        or not isinstance(dispatch, dict)
        or dispatch.get("SPAWN_MODE") != receipt.get("SPAWN_MODE")
    ):
        raise DispatchError("INVALID_DISPATCH_ROUTE: spawn receipt route binding is corrupt")
    issued_matches = [
        item
        for item in store.get("issued_routes", [])
        if isinstance(item, dict) and item.get("ROUTE_ID") == receipt.get("ROUTE_ID")
    ]
    if len(issued_matches) != 1:
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: persisted route issuance is missing or ambiguous"
        )
    issued = issued_matches[0]
    identity = issued.get("IDENTITY")
    receipt_identity = {
        "planner": {"TASK_ID": receipt.get("TASK_ID"), "BASE_SHA": receipt.get("BASE_SHA")},
        "executor": {
            "TASK_ID": receipt.get("TASK_ID"),
            "PHASE_ID": receipt.get("PHASE_ID"),
            "BASE_SHA": receipt.get("BASE_SHA"),
            "WRITER_CYCLE": receipt.get("WRITER_CYCLE"),
            "REJECTED_CANDIDATE_SHA": receipt.get("REJECTED_CANDIDATE_SHA"),
        },
        "reviewer": {
            "TASK_ID": receipt.get("TASK_ID"),
            "PHASE_ID": receipt.get("PHASE_ID"),
            "BASE_SHA": receipt.get("BASE_SHA"),
            "TARGET_SHA": receipt.get("TARGET_SHA"),
        },
    }.get(role)
    if not isinstance(identity, dict) or identity != receipt_identity:
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: spawn receipt identity differs from its issuance"
        )
    _verify_file_delivery_receipt(issued, receipt, path)
    expected_generation = (issued.get("WORKTREE_BINDING") or {}).get("contract_generation", 0)
    if receipt.get("CONTRACT_GENERATION", 0) != expected_generation:
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: persisted receipt contract generation is inconsistent"
        )
    from devcrew_context import _dispatch_worktree_binding

    current_binding, _current_receipt = _dispatch_worktree_binding(project_root, task_id)
    current_generation = current_binding.get("contract_generation", 0)
    if role == "planner" and current_generation and expected_generation != current_generation:
        import devcrew_phase

        manifest = json.loads(
            devcrew_phase.manifest_path(pathlib.Path(project_root), task_id).read_text(
                encoding="utf-8"
            )
        )
        pinned = manifest.get("planner_dispatch") or {}
        if (
            pinned.get("spawn_receipt_id") != receipt_id
            or pinned.get("reapproval_authorizer") is not True
            or pinned.get("authorization_generation") != current_generation - 1
            or pinned.get("route_id") != receipt.get("ROUTE_ID")
            or expected_generation != current_generation - 1
        ):
            raise DispatchError(
                "INVALID_DISPATCH_ROUTE: planner receipt is not the active reapproval authorizer"
            )
    elif expected_generation != current_generation:
        raise DispatchError(
            "INVALID_DISPATCH_ROUTE: persisted receipt is stale for the current contract generation"
        )
    return dict(receipt)


def inspect_issued(
    project_root: str | pathlib.Path,
    task_id: str,
    *,
    route_id: str | None = None,
    spawn_receipt_id: str | None = None,
    store_path: pathlib.Path | None = None,
) -> dict:
    """Bounded read-only view of an already issued route and optional spawn receipt."""
    project = pathlib.Path(project_root).resolve()
    path = _store_path(
        project, task_id, pathlib.Path(store_path) if store_path is not None else None
    )
    if fs.is_link(path) or not path.is_file():
        raise DispatchError("DISPATCH_DIAGNOSTIC_UNAVAILABLE: dispatch store is missing or unsafe")
    from devcrew_requirements import read_bounded_file

    try:
        raw = read_bounded_file(path, 512 * 1024, "dispatch diagnostic store")
    except ValueError as exc:
        raise DispatchError(f"DISPATCH_DIAGNOSTIC_UNAVAILABLE: {exc}") from None
    try:
        store = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DispatchError(
            f"DISPATCH_DIAGNOSTIC_UNAVAILABLE: invalid dispatch store: {exc}"
        ) from None
    if (
        not isinstance(store, dict)
        or type(store.get("schema_version")) is not int
        or store.get("schema_version") != 1
        or not isinstance(store.get("issued_routes"), list)
        or not isinstance(store.get("receipts"), list)
    ):
        raise DispatchError("DISPATCH_DIAGNOSTIC_UNAVAILABLE: invalid dispatch store schema")
    routes = [
        row
        for row in store["issued_routes"]
        if isinstance(row, dict) and (route_id is None or row.get("ROUTE_ID") == route_id)
    ]
    if route_id is None and spawn_receipt_id:
        receipts = [
            row
            for row in store["receipts"]
            if isinstance(row, dict) and row.get("SPAWN_RECEIPT_ID") == spawn_receipt_id
        ]
        if len(receipts) != 1:
            raise DispatchError(
                "DISPATCH_DIAGNOSTIC_UNAVAILABLE: spawn receipt is missing or ambiguous"
            )
        routes = [row for row in routes if row.get("ROUTE_ID") == receipts[0].get("ROUTE_ID")]
    if len(routes) != 1:
        raise DispatchError("DISPATCH_DIAGNOSTIC_UNAVAILABLE: issued route is missing or ambiguous")
    issued = routes[0]
    receipts = [
        row
        for row in store["receipts"]
        if isinstance(row, dict) and row.get("ROUTE_ID") == issued.get("ROUTE_ID")
    ]
    if spawn_receipt_id is not None and (
        len(receipts) != 1 or receipts[0].get("SPAWN_RECEIPT_ID") != spawn_receipt_id
    ):
        raise DispatchError(
            "DISPATCH_DIAGNOSTIC_UNAVAILABLE: spawn receipt does not match issued route"
        )
    payload = issued.get("SPAWN_PAYLOAD")
    message = payload.get("message") if isinstance(payload, dict) else None
    if not isinstance(message, str):
        raise DispatchError("DISPATCH_DIAGNOSTIC_UNAVAILABLE: issued payload is malformed")
    import re

    matches = re.findall(r"(?:^|\n)ENGINEERING_REQUIREMENTS: (\{[^\n]*\})", message)
    if len(matches) > 1:
        raise DispatchError(
            "DISPATCH_DIAGNOSTIC_UNAVAILABLE: issued payload has duplicate requirements markers"
        )
    envelope = json.loads(matches[0]) if matches else None
    return {
        "route_id": issued.get("ROUTE_ID"),
        "role": issued.get("ROLE"),
        "consumed": issued.get("consumed") is True,
        "registry_revision": envelope.get("registry_revision")
        if isinstance(envelope, dict)
        else None,
        "delivered_requirements": (
            [
                {"id": row.get("id"), "revision": row.get("revision")}
                for row in envelope.get("requirements", [])
            ]
            if isinstance(envelope, dict)
            else []
        ),
        "requirements_envelope_present": isinstance(envelope, dict),
        "spawn_receipt": dict(receipts[0]) if receipts else None,
    }
