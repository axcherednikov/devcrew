"""No-commit lifecycle for an ordinary, currently accessible user request.

The desktop host supplies a structured extraction of the current user message.
It is deliberately not a transcript store: the runtime persists requirement and
acceptance statements, plus event and message digests, but never prompt bytes.
"""

from __future__ import annotations

import devcrew_fs as fs
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from devcrew_phase import valid_review_advisories


class OrdinaryError(ValueError):
    pass


_held_task_locks = ContextVar("ordinary_task_locks", default=frozenset())


@contextmanager
def _task_lock(path: pathlib.Path):
    """Serialize ordinary state writes and source-gap sealing on a stable inode."""
    key = (os.getpid(), str(path.resolve()))
    held = _held_task_locks.get()
    if key in held:
        yield
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    try:
        descriptor = fs.open_regular(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise OrdinaryError(f"ORDINARY_TASK_LOCK_UNAVAILABLE: {exc}") from None
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise OrdinaryError("ORDINARY_TASK_LOCK_UNAVAILABLE: lock is not a regular file")
    with os.fdopen(descriptor, "r+") as lock:
        fs.lock(lock.fileno())
        token = _held_task_locks.set(held | {key})
        try:
            yield
        finally:
            _held_task_locks.reset(token)
            fs.unlock(lock.fileno())


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _task_path(project: pathlib.Path, task_id: str) -> pathlib.Path:
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id)
        or fs.unsafe_parts([task_id])
    ):
        raise OrdinaryError("invalid ordinary task ID")
    path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if fs.is_link(path):
        raise OrdinaryError("ordinary task state must not be a symlink")
    return path


def _namespace_lock_path(project: pathlib.Path, task_id: str) -> pathlib.Path:
    """Return one stable task lock shared by primary and linked checkouts."""
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id)
        or fs.unsafe_parts([task_id])
    ):
        raise OrdinaryError("invalid ordinary task ID")
    import devcrew_worktree

    common = devcrew_worktree._repository_details(project)[1].resolve()
    return common / "devcrew-task-locks" / f"{task_id}.lock"


def _linked_checkout_roots(project: pathlib.Path) -> list[pathlib.Path]:
    raw = subprocess.check_output(
        ["git", "-C", str(project), "worktree", "list", "--porcelain"],
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        stderr=subprocess.DEVNULL,
    )
    return [
        pathlib.Path(line[9:]).resolve()
        for line in raw.splitlines()
        if line.startswith("worktree ")
    ]


def _linked_ordinary_states(project: pathlib.Path, task_id: str) -> list[pathlib.Path]:
    return [
        root / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        for root in _linked_checkout_roots(project)
    ]


def _read(path: pathlib.Path) -> dict:
    if not path.is_file() or fs.is_link(path):
        raise OrdinaryError("ordinary task state is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OrdinaryError(f"invalid ordinary task state: {exc}") from None
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise OrdinaryError("unsupported ordinary task state")
    _verify_archive(value)
    _verify_formal_handoff(value)
    return value


def _write_unlocked(path: pathlib.Path, value: dict) -> None:
    raw = _canonical(value) + b"\n"
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        fs.private(name, 0o600, fd=fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _write(path: pathlib.Path, value: dict) -> None:
    with _task_lock(path):
        if path.exists():
            current = _read(path)
            if current.get("formal_handoff") is not None:
                raise OrdinaryError(
                    "ORDINARY_TASK_SEALED: formal source handoff has stopped this ordinary lifecycle"
                )
        _write_unlocked(path, value)


def _verify_formal_handoff(value: dict) -> None:
    seal = value.get("formal_handoff")
    if seal is None:
        return
    if (
        not isinstance(seal, dict)
        or set(seal) != {"payload", "sha256"}
        or not isinstance(seal.get("payload"), dict)
    ):
        raise OrdinaryError("ordinary formal handoff seal is malformed")
    payload = seal["payload"]
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    authorization = payload.get("authorization")
    observation = payload.get("host_observation")
    required = {
        "task_id",
        "ordinary_source_id",
        "ordinary_message_sha256",
        "cycle_id",
        "base_sha",
        "checkout_root",
        "branch",
        "history_sha256",
        "source_id",
        "source_sha256",
        "source_size_bytes",
        "authorization",
        "host_observation",
    }
    if (
        set(payload) != required
        or payload.get("task_id") != value.get("task_id")
        or payload.get("cycle_id") != _cycle_id(value)
        or payload.get("base_sha") != value.get("base_sha")
        or payload.get("ordinary_source_id") != value.get("source", {}).get("source_id")
        or payload.get("ordinary_message_sha256") != value.get("source", {}).get("message_sha256")
        or not isinstance(value.get("checkout_identity"), dict)
        or value["checkout_identity"].get("base_sha") != value.get("base_sha")
        or payload.get("checkout_root") != value["checkout_identity"].get("root")
        or not isinstance(payload.get("checkout_root"), str)
        or not pathlib.Path(payload["checkout_root"]).is_absolute()
        or not isinstance(payload.get("branch"), str)
        or not payload["branch"]
        or not isinstance(payload.get("history_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["history_sha256"])
        or not isinstance(payload.get("source_id"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["source_id"])
        or not isinstance(payload.get("source_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", payload["source_sha256"])
        or type(payload.get("source_size_bytes")) is not int
        or payload["source_size_bytes"] < 0
        or not isinstance(seal.get("sha256"), str)
        or seal["sha256"] != _digest(payload)
    ):
        raise OrdinaryError("ordinary formal handoff seal integrity is invalid")
    if (
        not isinstance(authorization, dict)
        or set(authorization) != {"source_id", "scope"}
        or authorization.get("source_id") != source.get("source_id")
        or authorization.get("scope")
        != f"formal source-gap transition for task {value.get('task_id')}"
    ):
        raise OrdinaryError("ordinary formal handoff authorization is invalid")
    if (
        not isinstance(observation, dict)
        or set(observation)
        != {
            "reader",
            "thread_id",
            "turn_id",
            "message_sha256",
            "status",
            "format",
            "attachment_reference",
            "full_source_readable",
            "active_agent_ids",
            "active_followup_ids",
            "writer_statuses",
            "ordinary_state_sha256",
        }
        or observation.get("reader") != "mcp__codex_app__read_thread"
        or observation.get("thread_id") != source.get("host_source_access", {}).get("thread_id")
        or observation.get("turn_id") != source.get("turn_id")
        or observation.get("message_sha256") != source.get("message_sha256")
        or observation.get("status") != "SOURCE_GAP"
        or not isinstance(observation.get("format"), str)
        or not observation["format"].strip()
        or not isinstance(observation.get("attachment_reference"), str)
        or not observation["attachment_reference"].strip()
        or observation.get("full_source_readable") is not False
        or not isinstance(observation.get("ordinary_state_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", observation["ordinary_state_sha256"])
        or observation.get("active_agent_ids") != []
        or observation.get("active_followup_ids") != []
    ):
        raise OrdinaryError("ordinary formal handoff source or writer observation is invalid")
    _validate_writer_statuses(value, observation)
    history = {key: item for key, item in value.items() if key != "formal_handoff"}
    if (
        _digest(history) != payload["history_sha256"]
        or observation["ordinary_state_sha256"] != payload["history_sha256"]
    ):
        raise OrdinaryError("ordinary formal handoff history digest is invalid")


def _validate_writer_statuses(value: dict, observation: dict) -> None:
    """Require terminal host evidence for every issued writer, including archived cycles."""
    writer_ids = set()
    receipt_groups = [(value.get("spawn_receipts", []), value.get("followup_receipts", []))]
    for archived in value.get("cycles", []):
        if isinstance(archived, dict):
            receipt_groups.append(
                (archived.get("spawn_receipts", []), archived.get("followup_receipts", []))
            )
            receipts = [
                item
                for group in receipt_groups[-1]
                if isinstance(group, list)
                for item in group
                if isinstance(item, dict)
            ]
            receipt_routes = {
                item.get(key)
                for item in receipts
                for key in ("ROUTE_ID", "route_id")
                if isinstance(item.get(key), str)
            }
            for dispatch in archived.get("issued_dispatches", []):
                if not isinstance(dispatch, dict):
                    raise OrdinaryError(
                        "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID: archived issuance record is malformed"
                    )
                route_id = dispatch.get("ROUTE_ID", dispatch.get("route_id"))
                if not isinstance(route_id, str) or route_id not in receipt_routes:
                    raise OrdinaryError(
                        "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID: archived issuance has no matching receipt"
                    )
    for spawn_receipts, followup_receipts in receipt_groups:
        for receipt in [
            *(spawn_receipts if isinstance(spawn_receipts, list) else []),
            *(followup_receipts if isinstance(followup_receipts, list) else []),
        ]:
            if isinstance(receipt, dict):
                agent_id = receipt.get("AGENT_ID")
                if isinstance(agent_id, str) and agent_id:
                    writer_ids.add(agent_id)
    followup = value.get("followup")
    if isinstance(followup, dict) and followup.get("issued"):
        agent_id = followup.get("origin_agent_id")
        if isinstance(agent_id, str) and agent_id:
            writer_ids.add(agent_id)
    statuses = observation.get("writer_statuses")
    terminal = {"completed", "failed", "cancelled", "stopped"}
    if (
        not isinstance(statuses, dict)
        or set(statuses) != writer_ids
        or any(status not in terminal for status in statuses.values())
    ):
        raise OrdinaryError(
            "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID: terminal status is required for every recorded writer"
        )


def validate_source_gap_observation(value: dict, observation: dict) -> None:
    """Check writer quiescence before the formal source selection can be written."""
    history = {key: item for key, item in value.items() if key != "formal_handoff"}
    source = value.get("source") if isinstance(value.get("source"), dict) else {}
    expected = {
        "reader",
        "thread_id",
        "turn_id",
        "message_sha256",
        "status",
        "format",
        "attachment_reference",
        "full_source_readable",
        "active_agent_ids",
        "active_followup_ids",
        "writer_statuses",
        "ordinary_state_sha256",
    }
    if (
        not isinstance(observation, dict)
        or set(observation) != expected
        or observation.get("reader") != "mcp__codex_app__read_thread"
        or observation.get("thread_id") != source.get("host_source_access", {}).get("thread_id")
        or observation.get("turn_id") != source.get("turn_id")
        or observation.get("message_sha256") != source.get("message_sha256")
        or observation.get("status") != "SOURCE_GAP"
        or not isinstance(observation.get("format"), str)
        or not observation["format"].strip()
        or not isinstance(observation.get("attachment_reference"), str)
        or not observation["attachment_reference"].strip()
        or observation.get("full_source_readable") is not False
        or observation.get("ordinary_state_sha256") != _digest(history)
        or observation.get("active_agent_ids") != []
        or observation.get("active_followup_ids") != []
    ):
        raise OrdinaryError(
            "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID: require an exact current quiescent-source observation"
        )
    _validate_writer_statuses(value, observation)


def _cycle_id(value: dict) -> int:
    cycle = value.get("cycle_id", 0)
    if type(cycle) is not int or cycle < 0:
        raise OrdinaryError("ordinary lifecycle cycle is invalid")
    return cycle


def _verify_archive(value: dict) -> None:
    """Make archived cycles append-only, including states written by v0.18."""
    cycles = value.get("cycles", [])
    if not isinstance(cycles, list):
        raise OrdinaryError("ordinary lifecycle archive is invalid")
    prior = None
    for index, archived in enumerate(cycles):
        if not isinstance(archived, dict) or archived.get("cycle_id") != index:
            raise OrdinaryError("ordinary lifecycle archive order is invalid")
        digest = archived.get("sha256")
        payload = {key: item for key, item in archived.items() if key != "sha256"}
        if (
            not isinstance(digest, str)
            or digest != _digest(payload)
            or archived.get("previous_sha256") != prior
        ):
            raise OrdinaryError("ordinary lifecycle archive integrity is invalid")
        prior = digest
    head = value.get("archive_head")
    if head is not None and head != prior:
        raise OrdinaryError("ordinary lifecycle archive head is invalid")
    known_archive_heads = {None, *(item.get("sha256") for item in cycles)}
    for event in value.get("migration_events", []):
        if not isinstance(event, dict) or event.get("kind") != "routing_attachment":
            raise OrdinaryError("ordinary lifecycle migration event is invalid")
        payload = {key: item for key, item in event.items() if key != "sha256"}
        if (
            event.get("sha256") != _digest(payload)
            or event.get("previous_archive_sha256") not in known_archive_heads
        ):
            raise OrdinaryError("ordinary lifecycle migration event integrity is invalid")
        _routing(event.get("routing"))
    cycle = _cycle_id(value)
    if cycles and cycle <= cycles[-1]["cycle_id"]:
        raise OrdinaryError("ordinary lifecycle cycle does not follow archive")


def _bind_cycle(value: dict, item: dict) -> dict:
    return {**item, "cycle_id": _cycle_id(value)}


def _archive_current_cycle(value: dict, *, reason: str, continuation: dict) -> dict:
    """Seal, rather than discard, the exact failed/stale cycle before recovery."""
    _verify_archive(value)
    cycle = _cycle_id(value)
    snapshot = {
        "cycle_id": cycle,
        "previous_sha256": value.get("archive_head"),
        "reason": reason,
        "continuation": continuation,
        "status": value.get("status"),
        "routing": value.get("routing"),
        "planner_requirements_scope": value.get("planner_requirements_scope"),
        "plan": value.get("plan"),
        "candidate": value.get("candidate"),
        "review": value.get("review"),
        "issued_dispatches": [
            item
            for item in value.get("issued_dispatches", [])
            if isinstance(item, dict) and item.get("cycle_id", 0) == cycle
        ],
        "spawn_receipts": [
            item
            for item in value.get("spawn_receipts", [])
            if isinstance(item, dict) and item.get("cycle_id", 0) == cycle
        ],
        "followup_receipts": [
            item
            for item in value.get("followup_receipts", [])
            if isinstance(item, dict) and item.get("cycle_id", 0) == cycle
        ],
    }
    snapshot["sha256"] = _digest(snapshot)
    value.setdefault("cycles", []).append(snapshot)
    value["archive_head"] = snapshot["sha256"]
    value["cycle_id"] = cycle + 1
    value.pop("planner_requirements_scope", None)
    return snapshot


def _routing(value: Any, *, required: bool = True) -> dict | None:
    """Validate an observed classification; never infer it from prompt text."""
    if value is None and not required:
        return None
    expected = {"risk", "decision_complexity", "implementation_complexity", "review_complexity"}
    if not isinstance(value, dict) or set(value) != expected:
        raise OrdinaryError(
            "ORDINARY_ROUTING_ASSESSMENT_REQUIRED: routing assessment is unavailable"
        )
    risk = value.get("risk")
    dimensions = {
        "decision_complexity": ("ROUTINE", "NORMAL", "HARD", "DEEP"),
        "implementation_complexity": ("MECHANICAL", "NORMAL", "COMPLEX"),
        "review_complexity": ("ROUTINE", "NORMAL", "COMPLEX"),
    }
    if not isinstance(risk, str) or risk.upper() not in {"LOW", "MEDIUM", "HIGH"}:
        raise OrdinaryError("ordinary routing risk is invalid")
    result = {"risk": risk.lower()}
    for key, choices in dimensions.items():
        item = value.get(key)
        if not isinstance(item, str) or item.upper() not in choices:
            raise OrdinaryError("ordinary routing complexity is invalid")
        result[key] = item.upper()
    return result


def _context(value: Any) -> dict:
    required = {
        "kind",
        "session_id",
        "turn_id",
        "message_sha256",
        "requirements",
        "acceptance",
        "host_source_access",
    }
    if not isinstance(value, dict) or set(value) not in (required, required | {"routing"}):
        raise OrdinaryError("ordinary chat context has unsupported fields")
    if value.get("kind") != "current_user_message":
        raise OrdinaryError("ordinary source must be the current user message")
    for key in ("session_id", "turn_id", "message_sha256"):
        item = value.get(key)
        if (
            not isinstance(item, str)
            or not item
            or (key == "message_sha256" and not re.fullmatch(r"[0-9a-f]{64}", item))
        ):
            raise OrdinaryError("ordinary chat context identity is invalid")
    access = value["host_source_access"]
    expected_access = {
        "tool",
        "thread_id",
        "turn_id",
        "turn_limit",
        "include_outputs",
        "max_output_chars_per_item",
        "attachments",
    }
    if (
        not isinstance(access, dict)
        or set(access) != expected_access
        or access.get("tool") != "mcp__codex_app__read_thread"
        or not isinstance(access.get("thread_id"), str)
        or not access["thread_id"]
        or access.get("turn_id") != value["turn_id"]
        or type(access.get("turn_limit")) is not int
        or not 1 <= access["turn_limit"] <= 100
        or type(access.get("include_outputs")) is not bool
        or type(access.get("max_output_chars_per_item")) is not int
        or not 1 <= access["max_output_chars_per_item"] <= 65536
        or not isinstance(access.get("attachments"), list)
    ):
        raise OrdinaryError(
            "ORDINARY_SOURCE_CONTEXT_REQUIRED: host current-message read access is unavailable"
        )
    for attachment in access["attachments"]:
        if (
            not isinstance(attachment, dict)
            or set(attachment) != {"tool", "page_id", "reference"}
            or attachment.get("tool") != "mcp__codex_app__read_page_reference"
            or not isinstance(attachment.get("page_id"), str)
            or not attachment["page_id"]
            or not isinstance(attachment.get("reference"), str)
            or not attachment["reference"]
        ):
            raise OrdinaryError(
                "ORDINARY_SOURCE_CONTEXT_REQUIRED: attachment access is unavailable"
            )
    requirements = value["requirements"]
    acceptance = value["acceptance"]
    if (
        not isinstance(requirements, list)
        or not requirements
        or not isinstance(acceptance, list)
        or not acceptance
    ):
        raise OrdinaryError("ordinary chat context requires requirements and acceptance")
    ids = []
    for item in requirements:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "statement"}
            or not isinstance(item["id"], str)
            or not re.fullmatch(r"R[1-9][0-9]*", item["id"])
            or not isinstance(item["statement"], str)
            or not item["statement"].strip()
        ):
            raise OrdinaryError("ordinary requirement is invalid")
        ids.append(item["id"])
    if len(ids) != len(set(ids)) or any(
        not isinstance(item, str) or not item.strip() for item in acceptance
    ):
        raise OrdinaryError("ordinary requirement IDs or acceptance is invalid")
    if "routing" in value:
        value = {**value, "routing": _routing(value["routing"])}
    return value


def _requirements_digest(source: dict) -> str:
    """Bind the persisted matrix to the message identity without persisting text."""
    return _digest({"requirements": source["requirements"], "acceptance": source["acceptance"]})


def _source(value: dict) -> dict:
    """Return the original matrix only if its durable binding is intact."""
    source = value.get("source")
    if not isinstance(source, dict):
        raise OrdinaryError("ordinary source context is unavailable")
    try:
        context = _context(
            {
                key: source[key]
                for key in (
                    "kind",
                    "session_id",
                    "turn_id",
                    "message_sha256",
                    "requirements",
                    "acceptance",
                    "host_source_access",
                )
            }
        )
    except (KeyError, OrdinaryError):
        raise OrdinaryError("ordinary source context is unavailable") from None
    identity = _digest({key: context[key] for key in ("session_id", "turn_id", "message_sha256")})
    if source.get("source_id") != identity or source.get(
        "requirements_sha256"
    ) != _requirements_digest(context):
        raise OrdinaryError("ordinary source requirements were changed after initialization")
    return source


def _effective_requirements(value: dict) -> tuple[list[dict], list[dict]]:
    """Append stable, provenance-bound requirements recovered from independent FAIL omissions."""
    source = _source(value)
    requirements = list(source["requirements"])
    provenance = []
    seen = set()
    next_id = max((int(row["id"][1:]) for row in requirements), default=0) + 1
    for cycle in value.get("cycles", []):
        review = cycle.get("review") if isinstance(cycle, dict) else None
        if not isinstance(review, dict):
            continue
        report = {k: v for k, v in review.items() if k not in {"sha256", "cycle_id"}}
        candidate = cycle.get("candidate")
        legacy = _legacy_cycle_zero_review(review, candidate, cycle.get("cycle_id"))
        cycle_valid = report.get("CYCLE_ID") == cycle.get("cycle_id") or legacy
        if (
            review.get("sha256") != _digest(report)
            or report.get("VERDICT") not in {"FAIL", "PASS"}
            or not cycle_valid
            or report.get("CANDIDATE_ID") != (candidate or {}).get("id")
        ):
            raise OrdinaryError("ordinary archived review integrity is invalid")
        expected_ids = {row["id"] for row in requirements}
        if set(report.get("REQUIREMENT_IDS", [])) != expected_ids:
            raise OrdinaryError("ordinary archived review requirement coverage is invalid")
        _source_review(source, report.get("SOURCE_REVIEW"), expected_ids)
        if report.get("VERDICT") == "PASS" and (
            report.get("FINDINGS") or report["SOURCE_REVIEW"]["OMISSIONS"]
        ):
            raise OrdinaryError("ordinary archived PASS review is inconsistent")
        omissions = report["SOURCE_REVIEW"]["OMISSIONS"]
        continuation = cycle.get("continuation", {})
        classification = report.get("CLASSIFICATION")
        if report["VERDICT"] == "PASS" and classification is not None:
            raise OrdinaryError("ordinary archived PASS review cannot carry a classification")
        if (
            report["VERDICT"] == "FAIL"
            and classification not in {"LOCAL_FIX", "SYSTEMIC_REPLAN", "OWNER_DECISION_REQUIRED"}
            and not (classification is None and legacy)
        ):
            raise OrdinaryError("ordinary archived review classification is invalid")
        if omissions and (
            classification == "OWNER_DECISION_REQUIRED"
            or continuation.get("ACTION") != "SYSTEMIC_REPLAN"
        ):
            raise OrdinaryError(
                "ordinary archived source omissions lack an authorized systemic continuation"
            )
        receipts = cycle.get("spawn_receipts", [])
        reviewer = next(
            (
                r
                for r in receipts
                if isinstance(r, dict) and r.get("id") == report.get("SPAWN_RECEIPT_ID")
            ),
            None,
        )
        executor = next(
            (
                r
                for r in receipts
                if isinstance(r, dict)
                and r.get("id") == (candidate or {}).get("executor_receipt_id")
            ),
            None,
        )
        if (
            not isinstance(reviewer, dict)
            or reviewer.get("ROLE") != "reviewer"
            or not isinstance(executor, dict)
            or executor.get("ROLE") != "executor"
            or reviewer.get("AGENT_ID") == executor.get("AGENT_ID")
        ):
            raise OrdinaryError("ordinary omission recovery lacks an independent reviewer receipt")
        if report.get("VERDICT") == "PASS":
            continue
        for index, omission in enumerate(omissions):
            if omission in seen:
                continue
            seen.add(omission)
            rid = f"R{next_id}"
            next_id += 1
            requirements.append({"id": rid, "statement": omission})
            provenance.append(
                {
                    "id": rid,
                    "cycle_id": cycle["cycle_id"],
                    "review_sha256": review["sha256"],
                    "omission_index": index,
                }
            )
    return requirements, provenance


def _legacy_candidate(candidate: Any, cycle_id: Any) -> bool:
    if (
        not isinstance(candidate, dict)
        or cycle_id != 0
        or "cycle_id" in candidate
        or "files" in candidate
    ):
        return False
    if set(candidate) != {
        "base_sha",
        "git_head",
        "worktree_fingerprint",
        "evidence",
        "plan_sha256",
        "id",
        "evidence_records",
        "planner_receipt_id",
        "executor_receipt_id",
    }:
        return False
    evidence = candidate.get("evidence")
    if (
        not isinstance(evidence, list)
        or not evidence
        or any(not isinstance(row, dict) or set(row) != {"record", "sha256"} for row in evidence)
    ):
        return False
    return True


def _legacy_cycle_zero_review(review: dict, candidate: Any, cycle_id: Any) -> bool:
    if cycle_id != 0 or not _legacy_candidate(candidate, cycle_id) or "cycle_id" in review:
        return False
    report = {key: item for key, item in review.items() if key != "sha256"}
    return (
        set(report)
        == {
            "TASK_ID",
            "CANDIDATE_ID",
            "VERDICT",
            "FINDINGS",
            "REQUIREMENT_IDS",
            "SPAWN_RECEIPT_ID",
            "SOURCE_REVIEW",
        }
        and "CYCLE_ID" not in report
    )


def _candidate_matches_cycle(value: dict, candidate: dict) -> bool:
    return (
        candidate.get("cycle_id", 0) == _cycle_id(value)
        if _legacy_candidate(candidate, _cycle_id(value))
        else candidate.get("cycle_id") == _cycle_id(value)
    )


def _candidate_identity_valid(value: dict, candidate: dict) -> bool:
    if _legacy_candidate(candidate, _cycle_id(value)):
        fields = ("base_sha", "git_head", "worktree_fingerprint", "evidence", "plan_sha256")
    else:
        fields = (
            "cycle_id",
            "base_sha",
            "git_head",
            "worktree_fingerprint",
            "files",
            "evidence",
            "plan_sha256",
        )
    try:
        return candidate.get("id") == _digest({key: candidate[key] for key in fields})
    except KeyError:
        return False


def init(project: pathlib.Path, task_id: str, context: Any, base_sha: str | None) -> dict:
    path = _task_path(project, task_id)
    try:
        import devcrew_worktree

        git_dir = devcrew_worktree._repository_details(project)[1]
    except (OSError, subprocess.CalledProcessError, ValueError):
        git_dir = None
    source = _context(context)
    if not isinstance(base_sha, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_sha):
        raise OrdinaryError("ordinary task requires the current Git base SHA")
    import devcrew_worktree

    root = devcrew_worktree._git(
        project, "rev-parse", "--show-toplevel", read_only=True
    ).stdout.strip()
    branch = (
        devcrew_worktree._git(
            project, "symbolic-ref", "--quiet", "--short", "HEAD", read_only=True, check=False
        ).stdout.strip()
        or None
    )
    value = {
        "schema_version": 1,
        "task_id": task_id,
        "mode": "ordinary_chat",
        "base_sha": base_sha,
        "source": {
            **source,
            "source_id": _digest(
                {key: source[key] for key in ("session_id", "turn_id", "message_sha256")}
            ),
        },
        "checkout_identity": {
            "root": str(pathlib.Path(root).resolve()),
            "branch": branch,
            "base_sha": base_sha,
        },
        "routing": source.get("routing"),
        "cycle_id": 0,
        "cycles": [],
        "archive_head": None,
        "routing_required": "routing" in source,
        "plan": None,
        "executor_id": None,
        "candidate": None,
        "review": None,
        "followup": None,
        "status": "PLANNING",
    }
    value["source"]["requirements_sha256"] = _requirements_digest(source)
    lock_path = _namespace_lock_path(project, task_id) if git_dir is not None else path
    with _task_lock(lock_path):
        if path.exists():
            raise OrdinaryError(
                "ordinary task already exists; resume it instead of replacing its history"
            )
        if git_dir is not None and any(
            candidate.exists() or fs.is_link(candidate)
            for candidate in _linked_ordinary_states(project, task_id)
            if candidate.resolve() != path.resolve()
        ):
            raise OrdinaryError(
                "ORDINARY_TASK_OWNERSHIP_CONFLICT: same task ID exists in a linked checkout"
            )
        if git_dir is not None:
            import devcrew_worktree

            manifests = {git_dir / "devcrew-checkouts" / f"{task_id}.phases.json"}
            for checkout_root in _linked_checkout_roots(project):
                try:
                    *_, receipt_path = devcrew_worktree.location(checkout_root, task_id)
                except ValueError:
                    raise OrdinaryError(
                        "FORMAL_TASK_OWNERSHIP_CONFLICT: task checkout namespace is ambiguous"
                    ) from None
                manifests.add(receipt_path.with_name(f"{task_id}.phases.json"))
            for manifest in manifests:
                if fs.is_link(manifest):
                    raise OrdinaryError(
                        "FORMAL_TASK_OWNERSHIP_CONFLICT: same-task formal manifest is unsafe"
                    )
                if manifest.exists():
                    raise OrdinaryError(
                        "FORMAL_TASK_OWNERSHIP_CONFLICT: same-task formal task already owns this task ID"
                    )
        _write(path, value)
    return value


def seal_for_formal_handoff(project: pathlib.Path, task_id: str, payload: Any) -> dict:
    """Append a same-task formal source handoff without rewriting prior history."""
    path = _task_path(project, task_id)
    with _task_lock(path):
        value = _read(path)
        if value.get("mode") != "ordinary_chat" or value.get("status") in {"ACCEPTED", "REJECTED"}:
            raise OrdinaryError("ORDINARY_SOURCE_GAP_NOT_HANDOFFABLE: ordinary task is not active")
        existing = value.get("formal_handoff")
        if existing is not None:
            if not isinstance(payload, dict) or existing.get("payload") != payload:
                raise OrdinaryError(
                    "ORDINARY_SOURCE_GAP_CONFLICT: task is already sealed to another source"
                )
            _verify_formal_handoff(value)
            return existing
        required = {
            "task_id",
            "ordinary_source_id",
            "ordinary_message_sha256",
            "cycle_id",
            "base_sha",
            "checkout_root",
            "branch",
            "history_sha256",
            "source_id",
            "source_sha256",
            "source_size_bytes",
            "authorization",
            "host_observation",
        }
        source = _source(value)
        if (
            not isinstance(payload, dict)
            or set(payload) != required
            or payload.get("task_id") != task_id
            or payload.get("ordinary_source_id") != source["source_id"]
            or payload.get("ordinary_message_sha256") != source["message_sha256"]
            or payload.get("cycle_id") != _cycle_id(value)
            or payload.get("base_sha") != value.get("base_sha")
            or payload.get("history_sha256") != _digest(value)
            or not isinstance(value.get("checkout_identity"), dict)
            or value["checkout_identity"].get("base_sha") != value.get("base_sha")
            or payload.get("checkout_root") != value["checkout_identity"].get("root")
            or not isinstance(payload.get("checkout_root"), str)
            or not pathlib.Path(payload["checkout_root"]).is_absolute()
            or not isinstance(payload.get("branch"), str)
            or not payload["branch"]
            or not isinstance(payload.get("source_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["source_id"])
            or not isinstance(payload.get("source_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", payload["source_sha256"])
            or type(payload.get("source_size_bytes")) is not int
            or payload["source_size_bytes"] < 0
        ):
            raise OrdinaryError(
                "ORDINARY_SOURCE_GAP_BINDING_INVALID: source, history or checkout binding differs"
            )
        authorization = payload.get("authorization")
        if (
            not isinstance(authorization, dict)
            or set(authorization) != {"source_id", "scope"}
            or authorization.get("source_id") != source["source_id"]
            or authorization.get("scope") != f"formal source-gap transition for task {task_id}"
        ):
            raise OrdinaryError(
                "ORDINARY_SOURCE_GAP_AUTHORIZATION_REQUIRED: bind the existing human authorization and scope"
            )
        observation = payload.get("host_observation")
        if (
            not isinstance(observation, dict)
            or set(observation)
            != {
                "reader",
                "thread_id",
                "turn_id",
                "message_sha256",
                "status",
                "format",
                "attachment_reference",
                "full_source_readable",
                "active_agent_ids",
                "active_followup_ids",
                "writer_statuses",
                "ordinary_state_sha256",
            }
            or observation.get("reader") != "mcp__codex_app__read_thread"
            or observation.get("thread_id") != source["host_source_access"]["thread_id"]
            or observation.get("turn_id") != source["turn_id"]
            or observation.get("message_sha256") != source["message_sha256"]
            or observation.get("status") != "SOURCE_GAP"
            or not isinstance(observation.get("format"), str)
            or not observation["format"].strip()
            or not isinstance(observation.get("attachment_reference"), str)
            or not observation["attachment_reference"].strip()
            or observation.get("full_source_readable") is not False
            or observation.get("ordinary_state_sha256") != _digest(value)
            or observation.get("active_agent_ids") != []
            or observation.get("active_followup_ids") != []
        ):
            raise OrdinaryError(
                "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID: require an exact unreadable-source and quiescent-writer observation"
            )
        _validate_writer_statuses(value, observation)
        current = _cycle_id(value)
        if any(
            isinstance(item, dict)
            and item.get("cycle_id", 0) == current
            and not item.get("consumed")
            for item in value.get("issued_dispatches", [])
        ):
            raise OrdinaryError(
                "ORDINARY_SOURCE_GAP_WRITER_ACTIVE: an issued ordinary route has no observed spawn"
            )
        followup = value.get("followup")
        if (
            isinstance(followup, dict)
            and followup.get("cycle_id") == current
            and followup.get("issued")
            and not followup.get("used")
        ):
            raise OrdinaryError(
                "ORDINARY_SOURCE_GAP_WRITER_ACTIVE: an ordinary followup is unresolved"
            )
        seal = {"payload": payload, "sha256": _digest(payload)}
        value["formal_handoff"] = seal
        _verify_formal_handoff(value)
        _write_unlocked(path, value)
        return seal


def dispatch(project: pathlib.Path, task_id: str, role: str, *, value: dict | None = None) -> dict:
    value = value if value is not None else _read(_task_path(project, task_id))
    if value.get("formal_handoff") is not None:
        raise OrdinaryError(
            "ORDINARY_TASK_SEALED: stale ordinary dispatch is not authorized after formal handoff"
        )
    source = _source(value)
    if value["mode"] != "ordinary_chat":
        raise OrdinaryError("ordinary dispatch cannot replace a formal task")
    if role not in {"planner", "executor", "reviewer"}:
        raise OrdinaryError("ordinary dispatch role is invalid")
    if value["status"] in {"ACCEPTED", "REJECTED"}:
        raise OrdinaryError("ordinary task is closed")
    requirements, provenance = _effective_requirements(value)
    result = {
        "TASK_ID": task_id,
        "ROLE": role,
        "SOURCE": {
            key: source[key]
            for key in ("kind", "session_id", "turn_id", "message_sha256", "source_id")
        },
        "REQUIREMENTS": requirements,
        "OMISSION_PROVENANCE": provenance,
        "ACCEPTANCE": source["acceptance"],
        "LIFECYCLE_BINDING": _dispatch_binding(value, source),
        "HOST_SOURCE_ACCESS": {
            **source["host_source_access"],
            "message_sha256": source["message_sha256"],
            "required_for_reviewer": role == "reviewer",
        },
    }
    if role in {"executor", "reviewer"}:
        plan = value.get("plan")
        if not isinstance(plan, dict) or plan.get("accepted") is not True:
            raise OrdinaryError("ordinary executor/reviewer requires an accepted technical plan")
        result["ACCEPTED_TECHNICAL_PLAN"] = plan["report"]
        result["PLAN_SHA256"] = plan["sha256"]
    return result


def _dispatch_binding(value: dict, source: dict) -> dict:
    candidate = value.get("candidate")
    plan = value.get("plan")
    result = {
        "task_id": value.get("task_id"),
        "cycle_id": _cycle_id(value),
        "base_sha": value.get("base_sha"),
        "source_id": source["source_id"],
        "requirements_sha256": source["requirements_sha256"],
        "status": value.get("status"),
        "candidate_id": candidate.get("id") if isinstance(candidate, dict) else None,
        "plan_sha256": plan.get("sha256")
        if isinstance(plan, dict) and plan.get("accepted") is True
        else None,
        "planner_requirements_scope": value.get("planner_requirements_scope"),
        "routing_sha256": _digest(value["routing"])
        if isinstance(value.get("routing"), dict)
        else None,
    }
    _, provenance = _effective_requirements(value)
    if provenance:
        result["effective_requirements_sha256"] = _digest(_effective_requirements(value)[0])
    return result


def _requirements_delivery(
    project: pathlib.Path,
    memory_relative: str,
    role: str,
    task_class: str,
    scope_paths: list[str] | None = None,
    authority: str | None = None,
    cycle_id: int = 0,
) -> tuple[dict, str | None]:
    """Rebuild the canonical requirement envelope for issuance and every gate."""
    from devcrew_requirements import (
        context as requirements_context,
        load,
        checked_serialized_context,
    )

    registry, _ = load(project, memory_relative)
    from devcrew_requirements import normalize_scope_paths

    paths = scope_paths
    if registry["requirements"] and (not isinstance(paths, list) or not paths):
        raise OrdinaryError(
            "ENGINEERING_REQUIREMENTS_SCOPE_REQUIRED: declare bounded task paths before dispatch"
        )
    try:
        paths = normalize_scope_paths(paths or [])
    except ValueError as exc:
        raise OrdinaryError(str(exc)) from None
    binding = {
        "memory_relative": memory_relative,
        "registry_sha256": _digest(registry),
        "scope_paths": paths,
        "authority": authority,
        "cycle_id": cycle_id,
    }
    if not registry["requirements"]:
        binding["envelope_sha256"] = None
        return binding, None
    envelope = requirements_context(
        project, memory_relative, paths, role, task_class, source_root=project
    )
    envelope.update({"role": role, "task_class": task_class, "scope_paths": paths})
    serialized, measured = checked_serialized_context(envelope)
    envelope["context_bytes"] = measured
    binding["envelope_sha256"] = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    return binding, serialized


def _trusted_requirements_scope(value: dict, role: str, cycle_id: int, *, registry_nonempty: bool):
    """Resolve an issued scope only from its current declaration or sealed cycle authority."""
    if cycle_id == _cycle_id(value):
        owner = value
    else:
        _verify_archive(value)
        matches = [
            item
            for item in value.get("cycles", [])
            if isinstance(item, dict) and item.get("cycle_id") == cycle_id
        ]
        if len(matches) != 1:
            raise OrdinaryError("ordinary requirements scope cycle authority is unavailable")
        owner = matches[0]
    if role == "planner":
        declared = owner.get("planner_requirements_scope")
        authority = "planner_declaration"
    else:
        plan = owner.get("plan")
        if (
            not isinstance(plan, dict)
            or plan.get("accepted") is not True
            or not isinstance(plan.get("report"), dict)
        ):
            if registry_nonempty:
                raise OrdinaryError(
                    "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY: preserve the existing task history and create a new ordinary task from the original message with an explicit requirements scope"
                )
            return [], "legacy_empty"
        declared = plan["report"].get("OWNED_SCOPE")
        authority = "accepted_plan"
    if declared is None:
        if registry_nonempty:
            raise OrdinaryError(
                "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY: preserve the existing task history and create a new ordinary task from the original message with an explicit requirements scope"
            )
        return [], "legacy_empty"
    from devcrew_requirements import normalize_scope_paths

    try:
        return normalize_scope_paths(declared, require_nonempty=registry_nonempty), authority
    except ValueError as exc:
        raise OrdinaryError(f"ordinary trusted requirements scope is invalid: {exc}") from None


def _validate_current_requirements(
    project: pathlib.Path, value: dict, memory_relative: str
) -> None:
    task_classes = {"planner": "PLANNER", "executor": "EXECUTOR", "reviewer": "REVIEWER"}
    from devcrew_requirements import load

    registry, _ = load(project, memory_relative)
    registry_nonempty = bool(registry["requirements"])
    for issued in value.get("issued_dispatches", []):
        if not isinstance(issued, dict) or issued.get("consumed") is not True:
            continue
        role = issued.get("role")
        if role not in task_classes:
            raise OrdinaryError("ordinary routed requirements binding is invalid")
        expected = issued.get("requirements_binding")
        if not isinstance(expected, dict) or expected.get("memory_relative") != memory_relative:
            raise OrdinaryError("ordinary routed requirements binding is unavailable")
        issue_cycle = issued.get("cycle_id", 0)
        if not all(key in expected for key in ("scope_paths", "authority", "cycle_id")):
            if not registry_nonempty:
                empty_legacy = (
                    expected.get("registry_sha256") == _digest(registry)
                    and expected.get("scope_paths") == []
                    and expected.get("envelope_sha256") is None
                )
                if empty_legacy:
                    continue
                raise OrdinaryError(
                    "ordinary legacy engineering requirements changed after issuance"
                )
            raise OrdinaryError(
                "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY: preserve the existing task history and create a new ordinary task from the original message with an explicit requirements scope"
            )
        trusted_paths, trusted_authority = _trusted_requirements_scope(
            value, role, issue_cycle, registry_nonempty=registry_nonempty
        )
        if (
            expected.get("scope_paths") != trusted_paths
            or expected.get("authority") != trusted_authority
            or expected.get("cycle_id") != issue_cycle
        ):
            raise OrdinaryError("ordinary engineering requirements scope authority is stale")
        current, _ = _requirements_delivery(
            project,
            memory_relative,
            role,
            issued.get("task_class", task_classes[role]),
            trusted_paths,
            trusted_authority,
            issue_cycle,
        )
        if current != expected:
            raise OrdinaryError("ordinary engineering requirements changed after routed issuance")


def _plan_report(value: dict, report: Any) -> dict:
    source = _source(value)
    required = {
        "TASK_ID",
        "SPAWN_RECEIPT_ID",
        "REQUIREMENT_IDS",
        "STEPS",
        "OWNED_SCOPE",
        "VERIFICATION",
    }
    routing_present = isinstance(value.get("routing"), dict) and value.get(
        "routing_required", False
    )
    expected = required | ({"ROUTING"} if routing_present else set())
    if (
        not isinstance(report, dict)
        or set(report) != expected
        or report.get("TASK_ID") != value.get("task_id")
    ):
        raise OrdinaryError("ordinary plan identity is invalid")
    _receipt(value, report.get("SPAWN_RECEIPT_ID"), "planner")
    expected_ids = {item["id"] for item in _effective_requirements(value)[0]}
    if (
        not isinstance(report.get("REQUIREMENT_IDS"), list)
        or set(report["REQUIREMENT_IDS"]) != expected_ids
    ):
        raise OrdinaryError("ordinary plan omitted an original requirement")
    for key in ("STEPS", "OWNED_SCOPE", "VERIFICATION"):
        items = report.get(key)
        if (
            not isinstance(items, list)
            or not items
            or any(not isinstance(item, str) or not item.strip() for item in items)
        ):
            raise OrdinaryError("ordinary technical plan is incomplete")
    for path in report["OWNED_SCOPE"]:
        normalized = path.replace("\\", "/")
        parsed = pathlib.PurePosixPath(normalized)
        if (
            parsed.is_absolute()
            or normalized != parsed.as_posix()
            or any(part in ("", ".", "..") for part in normalized.split("/"))
        ):
            raise OrdinaryError("ordinary plan owned scope is invalid")
    if routing_present:
        routing = _routing(report.get("ROUTING"))
        # Risk and each complexity are separately observed facts: high risk
        # does not manufacture a deeper technical decision.
        if routing != value["routing"]:
            return {**report, "ROUTING": routing}
    return report


def submit_plan(project: pathlib.Path, task_id: str, report: Any) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    if value.get("status") != "PLANNING":
        raise OrdinaryError("ordinary task is not awaiting a technical plan")
    report = _plan_report(value, report)
    if "ROUTING" in report:
        value["routing"] = report["ROUTING"]
        value["routing_required"] = True
    value["plan"] = _bind_cycle(
        value, {"report": report, "sha256": _digest(report), "accepted": False}
    )
    value["status"] = "PLAN_REPORTED"
    _write(path, value)
    return value["plan"]


def accept_plan(project: pathlib.Path, task_id: str, plan_sha256: str | None) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    plan = value.get("plan")
    if (
        value.get("status") != "PLAN_REPORTED"
        or not isinstance(plan, dict)
        or plan.get("accepted") is not False
    ):
        raise OrdinaryError("ordinary task has no pending technical plan")
    if not isinstance(plan_sha256, str) or plan_sha256 != plan.get("sha256"):
        raise OrdinaryError("ordinary technical plan acceptance identity is invalid")
    _plan_report(value, plan.get("report"))
    plan["accepted"] = True
    value["status"] = "PLAN_ACCEPTED"
    _write(path, value)
    return plan


def _plan(value: dict) -> dict:
    plan = value.get("plan")
    if (
        not isinstance(plan, dict)
        or plan.get("accepted") is not True
        or not isinstance(plan.get("report"), dict)
    ):
        raise OrdinaryError("ordinary accepted technical plan is unavailable")
    if plan.get("cycle_id", 0) != _cycle_id(value):
        raise OrdinaryError("ordinary accepted technical plan belongs to an archived cycle")
    if plan.get("sha256") != _digest(plan["report"]):
        raise OrdinaryError("ordinary accepted technical plan was changed")
    origin = plan.get("historical_origin")
    if origin is None:
        _plan_report(value, plan["report"])
    else:
        if not isinstance(origin, dict) or set(origin) != {
            "cycle_id",
            "plan_sha256",
            "planner_receipt_id",
        }:
            raise OrdinaryError("ordinary historical plan provenance is invalid")
        if (
            origin.get("plan_sha256") != plan["sha256"]
            or type(origin.get("cycle_id")) is not int
            or origin["cycle_id"] >= _cycle_id(value)
        ):
            raise OrdinaryError("ordinary historical plan provenance was changed")
        cycle_id = origin["cycle_id"]
        while True:
            archived = next(
                (item for item in value.get("cycles", []) if item.get("cycle_id") == cycle_id), None
            )
            archived_plan = archived.get("plan") if isinstance(archived, dict) else None
            if not isinstance(archived_plan, dict) or archived_plan.get("sha256") != plan["sha256"]:
                raise OrdinaryError(
                    "ordinary historical plan ancestry is unavailable or contradictory"
                )
            older = archived_plan.get("historical_origin")
            if older is None:
                if cycle_id != _planner_receipt(value, plan).get("cycle_id", 0):
                    raise OrdinaryError(
                        "ordinary historical plan ancestry does not reach its author"
                    )
                break
            if (
                not isinstance(older, dict)
                or set(older) != {"cycle_id", "plan_sha256", "planner_receipt_id"}
                or older.get("plan_sha256") != plan["sha256"]
                or type(older.get("cycle_id")) is not int
                or older["cycle_id"] >= cycle_id
                or older.get("planner_receipt_id") != plan["report"].get("SPAWN_RECEIPT_ID")
            ):
                raise OrdinaryError("ordinary historical plan ancestry is invalid")
            cycle_id = older["cycle_id"]
        _planner_receipt(value, plan)
    return plan


def _planner_receipt(value: dict, plan: dict) -> dict:
    """Resolve the exact receipt named by the accepted report through archive ancestry."""
    report = plan.get("report")
    receipt_id = report.get("SPAWN_RECEIPT_ID") if isinstance(report, dict) else None
    if not isinstance(receipt_id, str):
        raise OrdinaryError("ordinary accepted plan has no planner receipt identity")
    found = []
    for item in [
        *(
            r
            for r in value.get("spawn_receipts", [])
            if isinstance(r, dict) and r.get("cycle_id", 0) == _cycle_id(value)
        ),
        *(
            receipt
            for cycle in value.get("cycles", [])
            if isinstance(cycle, dict)
            for receipt in cycle.get("spawn_receipts", [])
        ),
    ]:
        if (
            isinstance(item, dict)
            and item.get("id") == receipt_id
            and item.get("ROLE") == "planner"
        ):
            found.append(item)
    if len(found) != 1:
        raise OrdinaryError("ordinary accepted plan planner receipt is unavailable or ambiguous")
    receipt = found[0]
    origin = plan.get("historical_origin")
    if origin is not None:
        if (
            not isinstance(origin, dict)
            or origin.get("planner_receipt_id") != receipt_id
            or origin.get("plan_sha256") != plan.get("sha256")
        ):
            raise OrdinaryError(
                "ordinary historical plan provenance contradicts its accepted report"
            )
        cycle_id = origin.get("cycle_id")
        if type(cycle_id) is not int or cycle_id < 0:
            raise OrdinaryError("ordinary historical planner receipt origin is invalid")
        if receipt.get("cycle_id", 0) != cycle_id:
            # Older 0.18.1 records can point at an intermediate archived plan.
            while True:
                archived = next(
                    (
                        c
                        for c in value.get("cycles", [])
                        if isinstance(c, dict) and c.get("cycle_id") == cycle_id
                    ),
                    None,
                )
                archived_plan = archived.get("plan") if isinstance(archived, dict) else None
                older = (
                    archived_plan.get("historical_origin")
                    if isinstance(archived_plan, dict)
                    else None
                )
                if not isinstance(archived_plan, dict) or archived_plan.get("sha256") != plan.get(
                    "sha256"
                ):
                    raise OrdinaryError("ordinary historical plan ancestry is contradictory")
                if older is None:
                    break
                next_id = older.get("cycle_id") if isinstance(older, dict) else None
                if (
                    not isinstance(older, dict)
                    or older.get("plan_sha256") != plan.get("sha256")
                    or older.get("planner_receipt_id") != receipt_id
                    or type(next_id) is not int
                    or next_id >= cycle_id
                ):
                    raise OrdinaryError("ordinary historical plan ancestry is invalid")
                cycle_id = next_id
            if receipt.get("cycle_id", 0) != cycle_id:
                raise OrdinaryError(
                    "ordinary historical planner receipt does not match the original plan"
                )
    return receipt


def issue_dispatch(
    project: pathlib.Path,
    task_id: str,
    role: str,
    runtime: Any,
    role_contract: str | None = None,
    memory_relative: str = "memory",
    requirements_scope: list[str] | None = None,
) -> dict:
    """Prepare a host-routable payload; only a later observed spawn is a receipt."""
    from devcrew_cost import route
    from devcrew_context import _build_routed_spawn
    from devcrew_requirements import context as requirements_context, load

    path = _task_path(project, task_id)
    value = _read(path)
    if value.get("formal_handoff") is not None:
        raise OrdinaryError(
            "ORDINARY_TASK_SEALED: ordinary dispatch is not authorized after formal handoff"
        )
    source = _source(value)
    if value.get("mode") != "ordinary_chat" or value.get("status") in {"ACCEPTED", "REJECTED"}:
        raise OrdinaryError("ordinary task cannot issue this dispatch")
    _validate_current_requirements(project, value, memory_relative)
    if role not in {"planner", "executor", "reviewer"}:
        raise OrdinaryError("ordinary dispatch role is invalid")
    if role_contract is not None:
        raise OrdinaryError("ordinary role contracts are fixed by the lifecycle")
    if role == "planner" and value.get("status") != "PLANNING":
        raise OrdinaryError("ordinary planner route is stale")
    if role == "executor":
        if value.get("status") != "PLAN_ACCEPTED":
            raise OrdinaryError("ordinary executor route is stale")
        _plan(value)
    if role == "planner":
        if requirements_scope is None:
            requirements_scope = []
        else:
            from devcrew_requirements import normalize_scope_paths

            try:
                requirements_scope = normalize_scope_paths(
                    requirements_scope, require_nonempty=True
                )
            except ValueError as exc:
                raise OrdinaryError(str(exc)) from None
        prior = [
            item
            for item in value.get("issued_dispatches", [])
            if isinstance(item, dict)
            and item.get("role") == "planner"
            and item.get("cycle_id", 0) == _cycle_id(value)
            and isinstance(item.get("requirements_binding"), dict)
            and all(
                key in item["requirements_binding"]
                for key in ("scope_paths", "authority", "cycle_id")
            )
        ]
        if (
            prior
            and requirements_scope is not None
            and requirements_scope != prior[-1].get("requirements_binding", {}).get("scope_paths")
        ):
            raise OrdinaryError("ordinary planner scope is pinned for this cycle")
        if requirements_scope is None and prior:
            requirements_scope = prior[-1].get("requirements_binding", {}).get("scope_paths")
        if requirements_scope is not None:
            if value.get("planner_requirements_scope") not in (None, requirements_scope):
                raise OrdinaryError("ordinary planner scope is pinned for this cycle")
            value["planner_requirements_scope"] = requirements_scope
    elif requirements_scope is not None:
        raise OrdinaryError(
            "ENGINEERING_REQUIREMENTS_SCOPE_OVERRIDE_FORBIDDEN: executor/reviewer scope comes from accepted OWNED_SCOPE"
        )
    if role == "reviewer" and value.get("status") != "REVIEW_READY":
        raise OrdinaryError("ordinary reviewer route requires a current candidate")
    task_class = {"planner": "PLANNER", "executor": "EXECUTOR", "reviewer": "REVIEWER"}[role]
    routing = _routing(value.get("routing"))
    if role == "planner" and value.get("replan_action") == "SYSTEMIC_REPLAN":
        task_class = "SYSTEMIC_REPLAN"
    selection = route(
        role,
        routing["risk"],
        task_class=task_class,
        runtime=runtime,
        decision_complexity=routing["decision_complexity"],
        implementation_complexity=routing["implementation_complexity"],
        review_complexity=routing["review_complexity"],
        escalation_reason=(
            "systemic_replan"
            if task_class == "SYSTEMIC_REPLAN"
            else "repeated_failure"
            if role == "reviewer" and value.get("retry_stage", 0) == 3
            else None
        ),
        retry_stage=value.get("retry_stage", 0),
    )
    if selection.get("RESOLUTION_STATUS") != "RESOLVED":
        raise OrdinaryError("AGENT_RUNTIME_REQUIRED: ordinary role route is unavailable")
    message = "ORDINARY_CURRENT_CHAT: " + json.dumps(
        dispatch(project, task_id, role, value=value), ensure_ascii=False, sort_keys=True
    )
    if role == "planner":
        scope_paths, scope_authority = requirements_scope, "planner_declaration"
    else:
        scope_paths, scope_authority = (
            sorted(set(value["plan"]["report"]["OWNED_SCOPE"])),
            "accepted_plan",
        )
    requirements_binding, serialized = _requirements_delivery(
        project, memory_relative, role, task_class, scope_paths, scope_authority, _cycle_id(value)
    )
    if serialized is not None:
        message += "\nENGINEERING_REQUIREMENTS: " + serialized
    contracts = {
        "planner": "Read the bounded current-chat requirements and return the exact ordinary plan report; do not implement.",
        "executor": "Implement only the accepted ordinary technical plan and return evidence; do not redesign requirements or commit.",
        "reviewer": "Read-only review: call mcp__codex_app__read_thread with the exact HOST_SOURCE_ACCESS thread_id, turn_limit, include_outputs and max_output_chars_per_item; read every listed attachment with mcp__codex_app__read_page_reference. Verify the full current user-message bytes against message_sha256. A summary, truncation, absent thread, or inaccessible attachment is ORDINARY_SOURCE_CONTEXT_REQUIRED, never PASS. Then report independently derived outcomes and omissions.",
    }
    safe_task_id = re.sub(r"[^a-z0-9_]", "_", task_id.lower())
    prepared = _build_routed_spawn(
        f"ordinary_{role}_{safe_task_id}",
        message,
        selection,
        runtime,
        role_contract=contracts[role],
    )
    prepared["spawn"]["task_name"] += "_" + prepared["route"]["ROUTE_ID"]
    issued = value.setdefault("issued_dispatches", [])
    issued.append(
        _bind_cycle(
            value,
            {
                "route_id": prepared["route"]["ROUTE_ID"],
                "role": role,
                "task_class": task_class,
                "prepared": prepared,
                "payload_sha256": _digest(prepared),
                "state_binding": _dispatch_binding(value, source),
                "requirements_binding": requirements_binding,
                "consumed": False,
            },
        )
    )
    _write(path, value)
    return prepared


def record_spawn(
    project: pathlib.Path, task_id: str, report: Any, memory_relative: str = "memory"
) -> dict:
    """Persist an actual host observation after the host performed the spawn."""
    path = _task_path(project, task_id)
    value = _read(path)
    required = {
        "ROUTE_ID",
        "ROLE",
        "AGENT_ID",
        "MODEL",
        "REASONING_EFFORT",
        "FORK_TURNS",
        "SPAWN_PAYLOAD_SHA256",
    }
    if (
        not isinstance(report, dict)
        or set(report) != required
        or report.get("FORK_TURNS") != "none"
    ):
        raise OrdinaryError("ordinary spawn report is invalid")
    matches = [
        item
        for item in value.get("issued_dispatches", [])
        if isinstance(item, dict) and item.get("route_id") == report["ROUTE_ID"]
    ]
    if len(matches) != 1 or matches[0].get("consumed"):
        raise OrdinaryError("ordinary route is missing, ambiguous, or already consumed")
    issued = matches[0]
    prepared = issued.get("prepared", {})
    route = prepared.get("route", {})
    if (
        report.get("ROLE") != issued.get("role")
        or not isinstance(report.get("AGENT_ID"), str)
        or not report["AGENT_ID"].strip()
        or report.get("MODEL") != route.get("RESOLVED_MODEL")
        or report.get("REASONING_EFFORT") != route.get("REASONING_EFFORT")
    ):
        raise OrdinaryError("ordinary spawn observation differs from its routed payload")
    source = _source(value)
    if report.get("SPAWN_PAYLOAD_SHA256") != issued.get("payload_sha256") or issued.get(
        "state_binding"
    ) != _dispatch_binding(value, source):
        raise OrdinaryError("ordinary spawn observation has a stale or altered routed payload")
    task_class = issued.get(
        "task_class",
        {"planner": "PLANNER", "executor": "EXECUTOR", "reviewer": "REVIEWER"}[issued["role"]],
    )
    expected_binding = issued.get("requirements_binding", {})
    from devcrew_requirements import load

    registry, _ = load(project, memory_relative)
    if not all(key in expected_binding for key in ("scope_paths", "authority", "cycle_id")):
        if registry["requirements"]:
            raise OrdinaryError(
                "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY: preserve the existing task history and create a new ordinary task from the original message with an explicit requirements scope"
            )
        expected_binding = {
            "scope_paths": [],
            "authority": "legacy_empty",
            "cycle_id": issued.get("cycle_id", 0),
        }
    trusted_paths, trusted_authority = _trusted_requirements_scope(
        value,
        issued["role"],
        issued.get("cycle_id", 0),
        registry_nonempty=bool(registry["requirements"]),
    )
    if (
        expected_binding.get("scope_paths") != trusted_paths
        or expected_binding.get("authority") != trusted_authority
    ):
        raise OrdinaryError("ordinary engineering requirements scope authority is stale")
    current_requirements, _ = _requirements_delivery(
        project,
        memory_relative,
        issued["role"],
        task_class,
        trusted_paths,
        trusted_authority,
        issued.get("cycle_id", 0),
    )
    if current_requirements != issued.get("requirements_binding"):
        raise OrdinaryError("ordinary engineering requirements changed after routed issuance")
    issued["consumed"] = True
    if issued.get("cycle_id", 0) != _cycle_id(value):
        raise OrdinaryError("ordinary spawn observation belongs to an archived cycle")
    receipt = _bind_cycle(
        value, {"id": _digest({**report, "cycle_id": _cycle_id(value)}), **report}
    )
    value.setdefault("spawn_receipts", []).append(receipt)
    _write(path, value)
    return receipt


def _receipt(value: dict, receipt_id: str, role: str) -> dict:
    matches = [
        item
        for item in value.get("spawn_receipts", [])
        if isinstance(item, dict)
        and item.get("id") == receipt_id
        and item.get("cycle_id", 0) == _cycle_id(value)
    ]
    if len(matches) != 1 or matches[0].get("ROLE") != role:
        raise OrdinaryError("ordinary routed spawn receipt is missing or has the wrong role")
    return matches[0]


def _role_receipt(value: dict, role: str) -> dict:
    receipts = [
        item
        for item in value.get("spawn_receipts", [])
        if isinstance(item, dict)
        and item.get("ROLE") == role
        and item.get("cycle_id", 0) == _cycle_id(value)
    ]
    if not receipts:
        raise OrdinaryError(f"ordinary candidate requires a routed {role} receipt")
    return receipts[-1]


def _verify_candidate_evidence(
    project: pathlib.Path, task_id: str, candidate_value: dict, state: dict, verify
) -> None:
    snapshots = candidate_value.get("evidence")
    if not isinstance(snapshots, list) or not snapshots:
        raise OrdinaryError("ordinary candidate evidence is unavailable")
    for snapshot in snapshots:
        if (
            not isinstance(snapshot, dict)
            or not isinstance(snapshot.get("record"), str)
            or not isinstance(snapshot.get("sha256"), str)
        ):
            raise OrdinaryError("ordinary candidate evidence is malformed")
        record, data, _ = verify(
            snapshot["record"], expected_execution_root=str(project), expected_task_id=task_id
        )
        if hashlib.sha256(record.read_bytes()).hexdigest() != snapshot["sha256"]:
            raise OrdinaryError("ordinary candidate evidence was replaced after verification")
        if (
            data.get("git_head_before") != state["git_head"]
            or data.get("git_head") != state["git_head"]
            or data.get("worktree_fingerprint_before") != state["worktree_fingerprint"]
            or data.get("worktree_fingerprint_after") != state["worktree_fingerprint"]
        ):
            raise OrdinaryError("ordinary evidence does not cover the unchanged candidate state")


def _candidate_evidence_signature(candidate_value: dict) -> str:
    """A retry needs new discriminating state/check output, not a new sentence."""
    evidence = candidate_value.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise OrdinaryError("ordinary candidate evidence is unavailable")
    checks = []
    for item in evidence:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("command"), list)
            or not isinstance(item.get("log_sha256"), str)
        ):
            # v0.18 snapshots did not retain enough information for an
            # automatic repeat correction; preserve them but fail closed.
            raise OrdinaryError("ordinary candidate lacks a loop-guard evidence signature")
        checks.append({"command": item["command"], "log_sha256": item["log_sha256"]})
    return _digest(
        {
            "worktree_fingerprint": candidate_value.get("worktree_fingerprint"),
            "checks": sorted(checks, key=_canonical),
        }
    )


def _changed_candidate_paths(candidate_value: dict, state: dict) -> list[str]:
    """Compare captured trees; a label cannot turn an out-of-scope edit local."""
    before, after = candidate_value.get("files"), state.get("files")
    if not isinstance(before, list) or not isinstance(after, list):
        raise OrdinaryError("ordinary stale candidate lacks a file-state baseline")

    def entries(rows: list) -> dict[str, tuple[str, str]]:
        result = {}
        for row in rows:
            if (
                not isinstance(row, (list, tuple))
                or len(row) != 3
                or not all(isinstance(item, str) for item in row)
                or row[0] in result
            ):
                raise OrdinaryError("ordinary candidate file-state baseline is invalid")
            result[row[0]] = (row[1], row[2])
        return result

    prior, current = entries(before), entries(after)
    return sorted(
        path for path in prior.keys() | current.keys() if prior.get(path) != current.get(path)
    )


def _inside_scope(path: str, scope: list[str]) -> bool:
    return any(path == item or path.startswith(item + "/") for item in scope)


def candidate(
    project: pathlib.Path,
    task_id: str,
    executor_receipt_id: str,
    state: dict,
    evidence: list[str],
    verify,
    memory_relative: str = "memory",
) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    _source(value)
    _validate_current_requirements(project, value, memory_relative)
    if value["status"] != "PLAN_ACCEPTED":
        raise OrdinaryError("ordinary task is not ready for a candidate")
    executor = _receipt(value, executor_receipt_id, "executor")
    plan = _plan(value)
    planner = _planner_receipt(value, plan)
    if not isinstance(evidence, list) or not evidence:
        raise OrdinaryError("ordinary candidate requires verification evidence")
    refs, snapshots = [], []
    for ref in evidence:
        record, data, _ = verify(
            ref, expected_execution_root=str(project), expected_task_id=task_id
        )
        if (
            data.get("git_head_before") != state["git_head"]
            or data.get("git_head") != state["git_head"]
            or data.get("worktree_fingerprint_before") != state["worktree_fingerprint"]
            or data.get("worktree_fingerprint_after") != state["worktree_fingerprint"]
        ):
            raise OrdinaryError("ordinary evidence does not cover the unchanged candidate state")
        refs.append(str(record))
        snapshots.append(
            {
                "record": str(record),
                "sha256": hashlib.sha256(record.read_bytes()).hexdigest(),
                "command": data.get("command"),
                "log_sha256": data.get("log_sha256"),
            }
        )
    if not isinstance(state.get("files"), list):
        raise OrdinaryError("ordinary candidate requires a captured file-state baseline")
    identity = {
        "cycle_id": _cycle_id(value),
        "base_sha": value["base_sha"],
        "git_head": state["git_head"],
        "worktree_fingerprint": state["worktree_fingerprint"],
        "files": state["files"],
        "evidence": snapshots,
        "plan_sha256": plan["sha256"],
    }
    value.update(
        {
            "executor_id": executor["AGENT_ID"],
            "candidate": {
                **identity,
                "id": _digest(identity),
                "evidence_records": refs,
                "planner_receipt_id": planner["id"],
                "executor_receipt_id": executor_receipt_id,
            },
            "review": None,
            "status": "REVIEW_READY",
        }
    )
    _write(path, value)
    return value["candidate"]


def review_start(
    project: pathlib.Path, task_id: str, state: dict, verify, memory_relative: str = "memory"
) -> dict:
    value = _read(_task_path(project, task_id))
    candidate = value.get("candidate")
    source = _source(value)
    _validate_current_requirements(project, value, memory_relative)
    plan = _plan(value)
    if value.get("status") != "REVIEW_READY" or not isinstance(candidate, dict):
        raise OrdinaryError("ordinary task has no reviewable candidate")
    if candidate.get("cycle_id", 0) != _cycle_id(value):
        raise OrdinaryError("ordinary candidate belongs to an archived cycle")
    if state.get("git_head") != candidate.get("git_head") or state.get(
        "worktree_fingerprint"
    ) != candidate.get("worktree_fingerprint"):
        raise OrdinaryError(
            "ordinary candidate is stale; rerun checks and review the current state"
        )
    _verify_candidate_evidence(project, task_id, candidate, state, verify)
    requirements, provenance = _effective_requirements(value)
    return {
        "TASK_ID": task_id,
        "CANDIDATE_ID": candidate["id"],
        "BASE_SHA": candidate["base_sha"],
        "REQUIREMENTS": requirements,
        "OMISSION_PROVENANCE": provenance,
        "ACCEPTANCE": source["acceptance"],
        "ACCEPTED_TECHNICAL_PLAN": plan["report"],
        "PLAN_SHA256": plan["sha256"],
        "HOST_SOURCE_ACCESS": {
            **source["host_source_access"],
            "message_sha256": source["message_sha256"],
        },
        "SOURCE_REVIEW_REQUIRED": "Independently read the host current-user message, derive outcomes, and disclose omissions. If access is unavailable, return ORDINARY_SOURCE_CONTEXT_REQUIRED; a derived requirements matrix is not a substitute.",
    }


def _source_review(source: dict, review: Any, expected_ids: set[str]) -> None:
    if not isinstance(review, dict) or set(review) != {"ACCESS", "OUTCOMES", "OMISSIONS"}:
        raise OrdinaryError("ordinary reviewer source review is invalid")
    access = review["ACCESS"]
    expected_access = {**source["host_source_access"], "message_sha256": source["message_sha256"]}
    if access != expected_access:
        raise OrdinaryError(
            "ORDINARY_SOURCE_CONTEXT_REQUIRED: reviewer cannot prove the required host message read"
        )
    outcomes = review["OUTCOMES"]
    if not isinstance(outcomes, list) or not outcomes:
        raise OrdinaryError("ordinary reviewer must provide independently derived source outcomes")
    covered = set()
    for outcome in outcomes:
        if (
            not isinstance(outcome, dict)
            or set(outcome) != {"OUTCOME", "REQUIREMENT_IDS"}
            or not isinstance(outcome["OUTCOME"], str)
            or not outcome["OUTCOME"].strip()
            or not isinstance(outcome["REQUIREMENT_IDS"], list)
            or not outcome["REQUIREMENT_IDS"]
            or any(not isinstance(item, str) for item in outcome["REQUIREMENT_IDS"])
        ):
            raise OrdinaryError("ordinary reviewer source outcome is invalid")
        covered.update(outcome["REQUIREMENT_IDS"])
    if covered != expected_ids:
        raise OrdinaryError("ordinary reviewer source outcomes omit or alter requirements")
    if not isinstance(review["OMISSIONS"], list) or any(
        not isinstance(item, str) or not item.strip() for item in review["OMISSIONS"]
    ):
        raise OrdinaryError("ordinary reviewer source omissions are invalid")


def submit_review(
    project: pathlib.Path,
    task_id: str,
    report: Any,
    state: dict,
    verify,
    memory_relative: str = "memory",
) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    target = review_start(project, task_id, state, verify, memory_relative)
    source = _source(value)
    required = {
        "TASK_ID",
        "CYCLE_ID",
        "CANDIDATE_ID",
        "VERDICT",
        "FINDINGS",
        "REQUIREMENT_IDS",
        "SPAWN_RECEIPT_ID",
        "SOURCE_REVIEW",
    }
    if (
        not isinstance(report, dict)
        or not required <= report.keys()
        or report.keys() - required - {"CLASSIFICATION", "ADVISORIES"}
        or report.get("TASK_ID") != task_id
        or report.get("CYCLE_ID") != _cycle_id(value)
        or report.get("CANDIDATE_ID") != target["CANDIDATE_ID"]
    ):
        raise OrdinaryError("ordinary review identity is invalid")
    if (
        report.get("VERDICT") not in {"PASS", "FAIL"}
        or not isinstance(report.get("FINDINGS"), list)
        or not isinstance(report.get("REQUIREMENT_IDS"), list)
    ):
        raise OrdinaryError("ordinary review verdict is invalid")
    if report["VERDICT"] == "PASS" and report["FINDINGS"]:
        raise OrdinaryError("ordinary PASS requires no findings")
    if not valid_review_advisories(report.get("ADVISORIES", [])):
        raise OrdinaryError("ordinary ADVISORIES must contain summaries with nonempty evidence")
    if report["VERDICT"] == "FAIL" and not report["FINDINGS"]:
        raise OrdinaryError("ordinary FAIL requires findings")
    if report["VERDICT"] == "PASS" and "CLASSIFICATION" in report:
        raise OrdinaryError("ordinary PASS cannot carry a recovery classification")
    if report["VERDICT"] == "FAIL" and report.get("CLASSIFICATION") not in {
        "LOCAL_FIX",
        "SYSTEMIC_REPLAN",
        "OWNER_DECISION_REQUIRED",
    }:
        raise OrdinaryError("ordinary FAIL requires a recovery classification")
    reviewer = _receipt(value, report.get("SPAWN_RECEIPT_ID"), "reviewer")
    if reviewer["AGENT_ID"] == value.get("executor_id"):
        raise OrdinaryError("ordinary reviewer must be independent from executor")
    expected_ids = {item["id"] for item in _effective_requirements(value)[0]}
    if set(report["REQUIREMENT_IDS"]) != expected_ids:
        raise OrdinaryError("ordinary review omitted an original requirement")
    _source_review(source, report.get("SOURCE_REVIEW"), expected_ids)
    if report["VERDICT"] == "PASS" and report["SOURCE_REVIEW"]["OMISSIONS"]:
        raise OrdinaryError(
            "ordinary PASS cannot leave independently found source outcomes unmapped"
        )
    value["review"] = _bind_cycle(value, {**report, "sha256": _digest(report)})
    value["status"] = "REVIEWED" if report["VERDICT"] == "PASS" else "REJECTED"
    _write(path, value)
    return value["review"]


def _review(value: dict, candidate: dict) -> dict:
    source = _source(value)
    review = value.get("review")
    if not isinstance(review, dict) or not isinstance(review.get("sha256"), str):
        raise OrdinaryError("ordinary review report is unavailable")
    if not _candidate_matches_cycle(value, candidate):
        raise OrdinaryError("ordinary review candidate belongs to a different cycle")
    legacy = _legacy_cycle_zero_review(review, candidate, _cycle_id(value))
    if review.get("cycle_id", 0) != _cycle_id(value) or ("cycle_id" not in review and not legacy):
        raise OrdinaryError("ordinary review report belongs to an archived cycle")
    report = {key: item for key, item in review.items() if key not in {"sha256", "cycle_id"}}
    if (
        _digest(report) != review["sha256"]
        or report.get("TASK_ID") != value.get("task_id")
        or report.get("CANDIDATE_ID") != candidate.get("id")
    ):
        raise OrdinaryError("ordinary review report integrity is invalid")
    if report.get("CYCLE_ID") != _cycle_id(value) and not legacy:
        raise OrdinaryError("ordinary review report belongs to a different cycle")
    expected_ids = {item["id"] for item in _effective_requirements(value)[0]}
    if set(report.get("REQUIREMENT_IDS", [])) != expected_ids:
        raise OrdinaryError("ordinary review report omitted requirements")
    _source_review(source, report.get("SOURCE_REVIEW"), expected_ids)
    reviewer = _receipt(value, report.get("SPAWN_RECEIPT_ID"), "reviewer")
    if reviewer["AGENT_ID"] == value.get("executor_id"):
        raise OrdinaryError("ordinary review report is not independent")
    if report.get("VERDICT") not in {"PASS", "FAIL"} or not isinstance(
        report.get("FINDINGS"), list
    ):
        raise OrdinaryError("ordinary review report verdict is invalid")
    if not valid_review_advisories(report.get("ADVISORIES", [])):
        raise OrdinaryError("ordinary ADVISORIES must contain summaries with nonempty evidence")
    if report["VERDICT"] == "PASS" and (report["FINDINGS"] or report["SOURCE_REVIEW"]["OMISSIONS"]):
        raise OrdinaryError("ordinary review report cannot pass with findings or omissions")
    if report["VERDICT"] == "FAIL" and not report["FINDINGS"]:
        raise OrdinaryError("ordinary review report failure has no findings")
    if (
        report["VERDICT"] == "FAIL"
        and report.get("CLASSIFICATION")
        not in {"LOCAL_FIX", "SYSTEMIC_REPLAN", "OWNER_DECISION_REQUIRED"}
        and not (report.get("CLASSIFICATION") is None and legacy)
    ):
        raise OrdinaryError("ordinary review report recovery classification is invalid")
    return review


def accept(
    project: pathlib.Path, task_id: str, state: dict, verify, memory_relative: str = "memory"
) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    candidate = value.get("candidate")
    review = value.get("review")
    _source(value)
    _plan(value)
    _validate_current_requirements(project, value, memory_relative)
    if (
        value.get("status") != "REVIEWED"
        or not isinstance(candidate, dict)
        or not isinstance(review, dict)
        or review.get("VERDICT") != "PASS"
    ):
        raise OrdinaryError("ordinary completion requires an independent passing review")
    if state.get("git_head") != candidate.get("git_head") or state.get(
        "worktree_fingerprint"
    ) != candidate.get("worktree_fingerprint"):
        raise OrdinaryError("ordinary review is stale; the candidate changed after review")
    _verify_candidate_evidence(project, task_id, candidate, state, verify)
    _review(value, candidate)
    value["status"] = "ACCEPTED"
    _write(path, value)
    return {
        "status": "TASK_DONE",
        "candidate_id": candidate["id"],
        "review_sha256": review["sha256"],
    }


def attach_routing(project: pathlib.Path, task_id: str, routing: Any) -> dict:
    """Explicitly enrich a v0.18 record without replacing its history."""
    path = _task_path(project, task_id)
    value = _read(path)
    _source(value)
    if isinstance(value.get("routing"), dict):
        raise OrdinaryError("ordinary routing assessment is already attached")
    assessment = _routing(routing)
    event = {
        "cycle_id": _cycle_id(value),
        "kind": "routing_attachment",
        "routing": assessment,
        "previous_archive_sha256": value.get("archive_head"),
    }
    event["sha256"] = _digest(event)
    value.setdefault("migration_events", []).append(event)
    value["routing"] = assessment
    # An already accepted v0.18 plan remains verifiable by its original
    # schema; the next submitted plan must carry the richer assessment.
    value["routing_required"] = value.get("plan") is None
    _write(path, value)
    return {
        "status": "ORDINARY_ROUTING_ATTACHED",
        "routing": assessment,
        "event_sha256": event["sha256"],
    }


def _continuation(value: dict, report: Any) -> dict:
    required = {
        "TASK_ID",
        "CYCLE_ID",
        "ACTION",
        "REASON",
        "EVIDENCE_RECORDS",
        "NEXT_ACTION",
        "EXPECTED_RESULT",
        "COST_RATIONALE",
        "PREVIOUS_REVIEW_SHA256",
    }
    expected = required | (
        {"STALE_SCOPE"}
        if isinstance(report, dict) and report.get("ACTION") == "STALE_CANDIDATE"
        else set()
    )
    if not isinstance(report, dict) or set(report) != expected:
        raise OrdinaryError("ordinary continuation is invalid")
    if report.get("TASK_ID") != value.get("task_id") or report.get("CYCLE_ID") != _cycle_id(value):
        raise OrdinaryError("ordinary continuation belongs to another cycle")
    if report.get("ACTION") not in {"LOCAL_FIX", "SYSTEMIC_REPLAN", "STALE_CANDIDATE"}:
        raise OrdinaryError("ordinary continuation action is invalid")
    for key in ("REASON", "NEXT_ACTION", "EXPECTED_RESULT", "COST_RATIONALE"):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise OrdinaryError("ordinary continuation explanation is invalid")
    records = report.get("EVIDENCE_RECORDS")
    if (
        not isinstance(records, list)
        or not records
        or any(not isinstance(item, str) or not item.strip() for item in records)
    ):
        raise OrdinaryError("ordinary continuation requires concrete evidence")
    candidate = value.get("candidate")
    review = value.get("review")
    if (
        not isinstance(candidate, dict)
        or not isinstance(review, dict)
        or report.get("PREVIOUS_REVIEW_SHA256") != review.get("sha256")
    ):
        raise OrdinaryError("ordinary continuation does not bind the failed review")
    if set(records) != set(candidate.get("evidence_records", [])):
        raise OrdinaryError("ordinary continuation must bind the current candidate evidence")
    if (
        report["ACTION"] != "STALE_CANDIDATE"
        and review.get("CLASSIFICATION") is not None
        and review.get("CLASSIFICATION") != report["ACTION"]
    ):
        omission_upgrade = (
            report["ACTION"] == "SYSTEMIC_REPLAN"
            and review.get("CLASSIFICATION") == "LOCAL_FIX"
            and bool((review.get("SOURCE_REVIEW") or {}).get("OMISSIONS"))
        )
        if not omission_upgrade:
            raise OrdinaryError(
                "ordinary continuation contradicts independent review classification"
            )
    if report["ACTION"] == "STALE_CANDIDATE" and report.get("STALE_SCOPE") not in {
        "LOCAL_CHANGE",
        "SYSTEMIC_CHANGE",
        "FOREIGN_CHANGE",
    }:
        raise OrdinaryError("ordinary stale continuation scope is invalid")
    return report


def continue_cycle(
    project: pathlib.Path,
    task_id: str,
    report: Any,
    state: dict,
    verify,
    routing: Any = None,
    memory_relative: str = "memory",
) -> dict:
    """Start a new immutable cycle after a failed or stale candidate.

    This is intentionally a recovery transition, not a reset: it seals every
    old artifact before a new plan/candidate can be issued.
    """
    path = _task_path(project, task_id)
    value = _read(path)
    _source(value)
    _validate_current_requirements(project, value, memory_relative)
    candidate = value.get("candidate")
    review = value.get("review")
    stale = isinstance(candidate, dict) and (
        state.get("git_head") != candidate.get("git_head")
        or state.get("worktree_fingerprint") != candidate.get("worktree_fingerprint")
    )
    if value.get("status") not in {"REJECTED", "REVIEWED", "REVIEW_READY"}:
        raise OrdinaryError("ordinary task has no recoverable candidate")
    if value.get("status") == "REVIEWED" and not stale:
        raise OrdinaryError("ordinary passing review must be accepted, not replaced")
    if value.get("status") == "REVIEW_READY" and not stale:
        raise OrdinaryError("ordinary review-ready candidate is not stale")
    # Validate every recoverable binding before sealing history or changing the cycle.
    if not isinstance(candidate, dict):
        raise OrdinaryError("ordinary recoverable candidate is unavailable")
    plan = _plan(value)
    if (
        not _candidate_matches_cycle(value, candidate)
        or candidate.get("plan_sha256") != plan.get("sha256")
        or not _candidate_identity_valid(value, candidate)
    ):
        raise OrdinaryError("ordinary candidate identity or accepted-plan binding is invalid")
    captured_state = {
        "git_head": candidate.get("git_head"),
        "worktree_fingerprint": candidate.get("worktree_fingerprint"),
    }
    try:
        _verify_candidate_evidence(
            project,
            task_id,
            candidate,
            captured_state,
            lambda ref, **kwargs: verify(ref, current=False, **kwargs),
        )
    except Exception as exc:
        raise OrdinaryError(f"ordinary saved candidate evidence is invalid: {exc}") from None
    if isinstance(review, dict):
        _review(value, candidate)
    if not isinstance(review, dict):
        # A stale candidate can be recovered before review, but its report
        # binding is represented by the candidate identity rather than a PASS.
        report = {**report, "PREVIOUS_REVIEW_SHA256": None}
        required = {
            "TASK_ID",
            "CYCLE_ID",
            "ACTION",
            "REASON",
            "EVIDENCE_RECORDS",
            "NEXT_ACTION",
            "EXPECTED_RESULT",
            "COST_RATIONALE",
            "PREVIOUS_REVIEW_SHA256",
            "STALE_SCOPE",
        }
        if (
            not isinstance(report, dict)
            or set(report) != required
            or report.get("ACTION") != "STALE_CANDIDATE"
            or report.get("TASK_ID") != task_id
            or report.get("CYCLE_ID") != _cycle_id(value)
        ):
            raise OrdinaryError("ordinary stale continuation is invalid")
        if report.get("STALE_SCOPE") not in {"LOCAL_CHANGE", "SYSTEMIC_CHANGE", "FOREIGN_CHANGE"}:
            raise OrdinaryError("ordinary stale continuation scope is invalid")
        if set(report.get("EVIDENCE_RECORDS", [])) != set(
            candidate.get("evidence_records", []) if isinstance(candidate, dict) else []
        ):
            raise OrdinaryError(
                "ordinary stale continuation must bind the current candidate evidence"
            )
        for key in ("REASON", "NEXT_ACTION", "EXPECTED_RESULT", "COST_RATIONALE"):
            if not isinstance(report.get(key), str) or not report[key].strip():
                raise OrdinaryError("ordinary continuation explanation is invalid")
    else:
        report = _continuation(value, report)
    if isinstance(review, dict) and review.get("VERDICT") == "FAIL":
        if review["SOURCE_REVIEW"]["OMISSIONS"] and report["ACTION"] != "SYSTEMIC_REPLAN":
            raise OrdinaryError("ordinary source omissions require a fresh systemic replan")
        if review.get("CLASSIFICATION") == "OWNER_DECISION_REQUIRED":
            raise OrdinaryError("ordinary owner decision cannot be bypassed by continuation")
    if report["ACTION"] == "STALE_CANDIDATE" and not stale:
        raise OrdinaryError("ordinary stale continuation requires a changed candidate state")
    if report["ACTION"] != "STALE_CANDIDATE" and stale:
        raise OrdinaryError(
            "ordinary stale candidate must be explicitly classified before recovery"
        )
    if report["ACTION"] == "STALE_CANDIDATE":
        plan = _plan(value)
        changed_paths = _changed_candidate_paths(candidate, state)
        owned_scope = plan["report"].get("OWNED_SCOPE", [])
        if not changed_paths or any(not _inside_scope(path, owned_scope) for path in changed_paths):
            raise OrdinaryError(
                "ORDINARY_FOREIGN_CHANGE: changed files are outside the accepted plan scope"
            )
        if report.get("STALE_SCOPE") == "FOREIGN_CHANGE":
            raise OrdinaryError(
                "ORDINARY_STALE_SCOPE_MISMATCH: current changes are within the accepted plan scope"
            )
    if isinstance(candidate, dict) and not stale:
        _verify_candidate_evidence(project, task_id, candidate, state, verify)
    current_routing = _routing(value.get("routing"), required=False)
    if routing is not None:
        current_routing = _routing(routing)
    if current_routing is None:
        raise OrdinaryError(
            "ORDINARY_ROUTING_ASSESSMENT_REQUIRED: attach a routing assessment before recovery"
        )
    failed = sum(
        1
        for item in value.get("cycles", [])
        if isinstance(item.get("review"), dict) and item["review"].get("VERDICT") == "FAIL"
    )
    if isinstance(review, dict) and review.get("VERDICT") == "FAIL":
        failed += 1
    if failed >= 2:
        previous_signatures = set()
        for item in value.get("cycles", []):
            archived_candidate = item.get("candidate")
            if isinstance(archived_candidate, dict):
                try:
                    previous_signatures.add(_candidate_evidence_signature(archived_candidate))
                except OrdinaryError:
                    previous_signatures.add(None)
        if (
            not isinstance(candidate, dict)
            or _candidate_evidence_signature(candidate) in previous_signatures
        ):
            raise OrdinaryError(
                "ORDINARY_LOOP_GUARD: repeated failure needs a new candidate and evidence signature"
            )
    origin = None
    preserved_plan = None
    if report["ACTION"] == "LOCAL_FIX":
        receipt_id = candidate.get("executor_receipt_id") if isinstance(candidate, dict) else None
        try:
            origin = _receipt(value, receipt_id, "executor")
        except OrdinaryError:
            origin = None
        accepted = _plan(value)
        planner = _planner_receipt(value, accepted)
        preserved_plan = {
            "report": accepted["report"],
            "sha256": accepted["sha256"],
            "accepted": True,
            "historical_origin": {
                "cycle_id": planner.get("cycle_id", 0),
                "plan_sha256": accepted["sha256"],
                "planner_receipt_id": planner["id"],
            },
        }
    _archive_current_cycle(
        value, reason=report["ACTION"], continuation={**report, "sha256": _digest(report)}
    )
    if preserved_plan is not None:
        preserved_plan = _bind_cycle(value, preserved_plan)
    value.update(
        {
            "routing": current_routing,
            "routing_required": True,
            "plan": preserved_plan,
            "candidate": None,
            "review": None,
            "executor_id": None,
            "status": "PLAN_ACCEPTED" if preserved_plan else "PLANNING",
            "active_continuation": _bind_cycle(value, {**report, "sha256": _digest(report)}),
            "replan_action": "SYSTEMIC_REPLAN"
            if report["ACTION"] == "SYSTEMIC_REPLAN"
            or report.get("STALE_SCOPE") == "SYSTEMIC_CHANGE"
            else None,
            "retry_stage": min(failed, 3),
        }
    )
    if origin is not None:
        value["followup"] = _bind_cycle(
            value,
            {
                "origin_receipt_id": origin["id"],
                "origin_agent_id": origin["AGENT_ID"],
                "used": False,
            },
        )
    else:
        value["followup"] = None
    _write(path, value)
    return {
        "status": "ORDINARY_CYCLE_CONTINUED",
        "cycle_id": value["cycle_id"],
        "action": report["ACTION"],
        "next_status": value["status"],
        "archive_sha256": value["archive_head"],
    }


def executor_followup(project: pathlib.Path, task_id: str) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    source = _source(value)
    plan = _plan(value)
    followup = value.get("followup")
    if (
        value.get("status") != "PLAN_ACCEPTED"
        or not isinstance(followup, dict)
        or followup.get("cycle_id") != _cycle_id(value)
        or followup.get("used")
    ):
        raise OrdinaryError("ordinary executor followup is unavailable")
    payload = {
        "TASK_ID": task_id,
        "CYCLE_ID": _cycle_id(value),
        "ROLE": "executor",
        "ORIGIN_RECEIPT_ID": followup["origin_receipt_id"],
        "AGENT_ID": followup["origin_agent_id"],
        "PLAN_SHA256": plan["sha256"],
        "LIFECYCLE_BINDING": _dispatch_binding(value, source),
    }
    followup.update(
        {
            "payload_sha256": _digest(payload),
            "issued": True,
            "state_binding": _dispatch_binding(value, source),
        }
    )
    _write(path, value)
    return payload


def record_executor_followup(
    project: pathlib.Path, task_id: str, report: Any, memory_relative: str = "memory"
) -> dict:
    path = _task_path(project, task_id)
    value = _read(path)
    followup = value.get("followup")
    source = _source(value)
    _validate_current_requirements(project, value, memory_relative)
    _plan(value)
    required = {
        "TASK_ID",
        "CYCLE_ID",
        "ORIGIN_RECEIPT_ID",
        "AGENT_ID",
        "FOLLOWUP_SUCCESS",
        "FOLLOWUP_PAYLOAD_SHA256",
    }
    if (
        not isinstance(report, dict)
        or set(report) != required
        or report.get("TASK_ID") != task_id
        or report.get("CYCLE_ID") != _cycle_id(value)
        or report.get("FOLLOWUP_SUCCESS") is not True
        or not isinstance(followup, dict)
        or followup.get("cycle_id") != _cycle_id(value)
        or followup.get("used")
        or followup.get("issued") is not True
        or report.get("ORIGIN_RECEIPT_ID") != followup.get("origin_receipt_id")
        or report.get("AGENT_ID") != followup.get("origin_agent_id")
        or not isinstance(followup.get("payload_sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", followup["payload_sha256"])
        or report.get("FOLLOWUP_PAYLOAD_SHA256") != followup.get("payload_sha256")
        or followup.get("state_binding") != _dispatch_binding(value, source)
    ):
        raise OrdinaryError("ordinary executor followup observation is invalid")
    receipt = _bind_cycle(
        value,
        {
            "id": _digest({**report, "cycle_id": _cycle_id(value)}),
            "ROLE": "executor",
            "CONTINUATION": True,
            **report,
        },
    )
    value.setdefault("spawn_receipts", []).append(receipt)
    followup["used"] = True
    _write(path, value)
    return receipt


def resume(
    project: pathlib.Path, task_id: str, state: dict, verify, memory_relative: str = "memory"
) -> dict:
    value = _read(_task_path(project, task_id))
    if value.get("formal_handoff") is not None:
        return {
            "status": "ORDINARY_SEALED_FOR_FORMAL_HANDOFF",
            "task_id": task_id,
            "source_id": value["formal_handoff"]["payload"]["source_id"],
        }
    try:
        source = _source(value)
    except OrdinaryError:
        return {
            "status": "ORDINARY_CONTEXT_REQUIRED",
            "missing": "structured requirements from the original current user message",
        }
    try:
        _validate_current_requirements(project, value, memory_relative)
    except Exception as exc:
        if "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY" in str(exc):
            return {
                "status": "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY",
                "task_id": task_id,
                "recovery": "Preserve this task and its receipts. Create a new ordinary task from the original user message and declare its requirements scope.",
            }
        return {"status": "ORDINARY_REQUIREMENTS_STALE", "task_id": task_id}
    try:
        _effective_requirements(value)
    except OrdinaryError:
        return {"status": "ORDINARY_REVIEW_INVALID", "task_id": task_id}
    try:
        plan = value.get("plan")
        if plan is not None:
            if plan.get("accepted") is True:
                _plan(value)
            else:
                _plan_report(value, plan.get("report"))
    except OrdinaryError:
        return {"status": "ORDINARY_PLAN_INVALID", "task_id": task_id}
    candidate = value.get("candidate")
    if candidate and (
        state.get("git_head") != candidate.get("git_head")
        or state.get("worktree_fingerprint") != candidate.get("worktree_fingerprint")
    ):
        return {"status": "ORDINARY_CANDIDATE_STALE", "task_id": task_id}
    if candidate:
        try:
            _verify_candidate_evidence(project, task_id, candidate, state, verify)
        except Exception:
            return {"status": "ORDINARY_EVIDENCE_INVALID", "task_id": task_id}
    if value.get("review") is not None:
        try:
            _review(value, candidate)
        except OrdinaryError:
            return {"status": "ORDINARY_REVIEW_INVALID", "task_id": task_id}
    return {"status": value["status"], "task_id": task_id, "requirements": source["requirements"]}
