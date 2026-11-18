"""Bounded, explicit engineering-requirement registry for configured project memory."""
from __future__ import annotations

import devcrew_fs as fs
import hashlib
import json
import os
import pathlib
import re
import stat
import tempfile

REGISTRY_NAME = "engineering-requirements.json"
REGISTRY_LIMIT = 512 * 1024
ENVELOPE_LIMIT = 32 * 1024
SOURCE_LIMIT = 512 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")


class RequirementError(ValueError):
    pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def serialized_context(envelope):
    """Return the canonical marker text and its exact UTF-8 byte count."""
    result = dict(envelope)
    result.pop("context_bytes", None)
    result["context_bytes"] = 0
    for _ in range(4):
        size = len(_json_bytes(result))
        if result["context_bytes"] == size:
            break
        result["context_bytes"] = size
    raw = _json_bytes(result)
    if result["context_bytes"] != len(raw):
        raise RequirementError("could not measure serialized requirements context")
    return raw.decode("utf-8"), len(raw)


def checked_serialized_context(envelope):
    """Serialize the complete envelope and fail with its measured UTF-8 size."""
    serialized, measured = serialized_context(envelope)
    if measured > ENVELOPE_LIMIT:
        raise RequirementError(
            f"selected requirements envelope is {measured} bytes; limit is "
            f"{ENVELOPE_LIMIT} bytes (32 KiB); issuance stopped without truncation"
        )
    return serialized, measured


def normalize_scope_paths(paths, *, require_nonempty=False):
    """Validate and canonicalize a bounded repository-relative declaration."""
    if (not isinstance(paths, list) or len(paths) > 128 or
            (require_nonempty and not paths) or
            any(not isinstance(item, str) or not item for item in paths)):
        raise RequirementError("requirements scope paths must be a bounded declared path list")
    for declared in paths:
        normalized = declared.replace("\\", "/")
        path = pathlib.PurePosixPath(normalized)
        if (fs.unsafe_parts(path.parts) or path.is_absolute() or normalized != path.as_posix() or
                any(part in ("", ".", "..") for part in normalized.split("/")) or
                any(char in normalized for char in "*?[") or "\x00" in normalized):
            raise RequirementError("requirements scope paths must be normalized repository-relative declarations")
    return sorted(set(paths))


def read_bounded_file(path, limit, label):
    """Read a regular, non-symlink file without allocating beyond its ceiling."""
    flags = os.O_RDONLY
    try:
        descriptor = fs.open_regular(path, flags)
    except OSError as exc:
        raise RequirementError(f"{label} is missing or unsafe: {exc}") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RequirementError(f"{label} must be a regular file")
        if info.st_size > limit:
            raise RequirementError(f"{label} exceeds {limit} bytes")
        chunks = []
        remaining = limit + 1
        while remaining:
            block = os.read(descriptor, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        raw = b"".join(chunks)
        if len(raw) > limit:
            raise RequirementError(f"{label} exceeds {limit} bytes")
        return raw
    finally:
        os.close(descriptor)


def _safe_memory(project, relative):
    root = pathlib.Path(project)
    raw = relative.replace("\\", "/") if isinstance(relative, str) else ""
    rel = pathlib.PurePosixPath(raw)
    if not raw or fs.unsafe_parts(rel.parts) or rel.is_absolute() or any(p in ("", ".", "..") for p in raw.split("/")) or any(c in raw for c in "*?["):
        raise RequirementError("configured memory path must stay inside the project")
    path = root
    for part in rel.parts:
        path = path / part
        if fs.is_link(path):
            raise RequirementError("refusing to follow symlink in configured memory path")
    if path.exists() and not path.is_dir():
        raise RequirementError("configured memory path must be a directory")
    return path


def registry_path(project, memory_relative):
    return _safe_memory(project, memory_relative) / REGISTRY_NAME


def load(project, memory_relative):
    path = registry_path(project, memory_relative)
    if fs.is_link(path):
        raise RequirementError("requirements registry must not be a symlink")
    if not path.exists():
        return {"schema_version": 1, "revision": 0, "requirements": []}, 0
    if not path.is_file():
        raise RequirementError("requirements registry must be a regular file")
    raw = read_bounded_file(path, REGISTRY_LIMIT, "requirements registry")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RequirementError(f"invalid requirements registry: {exc}") from None
    if (not isinstance(data, dict) or type(data.get("schema_version")) is not int or data.get("schema_version") != 1 or
            type(data.get("revision")) is not int or data["revision"] < 0 or
            not isinstance(data.get("requirements"), list) or len(data["requirements"]) > 2048):
        raise RequirementError("invalid requirements registry schema")
    identifiers = set()
    for row in data["requirements"]:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str) or
                not ID_RE.fullmatch(row["id"]) or row["id"] in identifiers or
                type(row.get("revision")) is not int or row["revision"] < 1 or
                row.get("status", "active") not in {"active", "inactive"}):
            raise RequirementError("invalid or duplicate requirement registry entry")
        identifiers.add(row["id"])
    return data, len(raw)


def _text(value, key):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 4096:
        raise RequirementError(f"{key} must be non-empty text no longer than 4096 bytes")
    return value.strip()


def _selectors(value):
    if not isinstance(value, dict) or set(value) != {"paths", "roles", "task_classes"}:
        raise RequirementError("selectors must define paths, roles, and task_classes")
    result = {}
    allowed_roles = {"planner", "executor", "reviewer"}
    for key in ("paths", "roles", "task_classes"):
        items = value[key]
        if not isinstance(items, list) or not items or len(items) > 64:
            raise RequirementError(f"selector {key} must be a non-empty bounded list")
        if any(not isinstance(item, str) or not item.strip() or len(item) > 256 for item in items):
            raise RequirementError(f"selector {key} contains an invalid value")
        normalized = []
        for item in items:
            item = item.strip()
            if key == "paths":
                item = item.replace("\\", "/")
                path = pathlib.PurePosixPath(item)
                if fs.unsafe_parts(path.parts) or path.is_absolute() or any(p in ("", ".", "..") for p in item.rstrip("/").split("/")):
                    raise RequirementError("path selectors must be normalized repository-relative prefixes")
                if item != path.as_posix() + ("/" if item.endswith("/") and item != "." else ""):
                    raise RequirementError("path selectors must be normalized repository-relative prefixes")
            if key == "roles" and item not in allowed_roles:
                raise RequirementError("unsupported requirement role selector")
            normalized.append(item)
        if len(set(normalized)) != len(normalized):
            raise RequirementError(f"selector {key} contains duplicates")
        result[key] = sorted(normalized)
    return result


def _validate_source(project, source):
    if not isinstance(source, dict) or set(source) - {"path", "fragment", "fragment_sha256", "sha256", "complete_file"}:
        raise RequirementError("source reference must contain path, digest, and exact fragment or complete_file")
    rel = source.get("path")
    if not isinstance(rel, str) or not rel or rel.startswith("/") or "\\" in rel or any(p in ("", ".", "..") for p in rel.split("/")):
        raise RequirementError("source path must be normalized repository-relative")
    parts = pathlib.PurePosixPath(rel).parts
    if fs.unsafe_parts(parts):
        raise RequirementError("source path contains a reserved Windows name")
    candidate = pathlib.Path(project)
    for part in parts:
        candidate = candidate / part
        if fs.is_link(candidate):
            raise RequirementError("source reference must not traverse symlinks")
    if not candidate.is_file() or not stat.S_ISREG(candidate.lstat().st_mode):
        raise RequirementError("source reference must name a regular file")
    raw = read_bounded_file(candidate, SOURCE_LIMIT, "selected source")
    digest = hashlib.sha256(raw).hexdigest()
    if digest != source.get("sha256"):
        raise RequirementError("source reference is stale: file digest changed")
    if source.get("complete_file") is True:
        if "fragment" in source or "fragment_sha256" in source:
            raise RequirementError("complete-file source cannot include fragment fields")
    else:
        fragment = source.get("fragment")
        fragment_digest = source.get("fragment_sha256")
        if (not isinstance(fragment, str) or not fragment or
                raw.decode("utf-8", errors="strict").count(fragment) != 1 or
                fragment_digest != hashlib.sha256(fragment.encode("utf-8")).hexdigest()):
            raise RequirementError("source fragment must identify one exact current passage with its digest")
    return source


def validate_record(source_root, record):
    required = {"id", "revision", "selectors", "origin", "assessment"}
    if not isinstance(record, dict) or not required <= set(record):
        raise RequirementError("requirement record is incomplete")
    identifier = record["id"]
    if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier):
        raise RequirementError("requirement id is invalid")
    if type(record["revision"]) is not int or record["revision"] < 1:
        raise RequirementError("requirement revision must be a positive integer")
    selectors = _selectors(record["selectors"])
    origin = record["origin"]
    assessment = record["assessment"]
    if not isinstance(origin, dict) or not isinstance(assessment, dict):
        raise RequirementError("origin and assessment must be objects")
    state = record.get("status", "active")
    if state not in {"active", "inactive"}:
        raise RequirementError("requirement status must be active or inactive")
    if state == "inactive":
        _text(record.get("retirement_reason"), "retirement_reason")
    elif assessment.get("status") != "active" or assessment.get("decision") not in {"distinct", "equivalent"}:
        raise RequirementError("only assessed active distinct/equivalent requirements can be recorded")
    if assessment.get("durable") is not True:
        raise RequirementError("only explicitly assessed durable requirements can be recorded")
    if origin.get("kind") not in {"user_requirement", "project_rule", "confirmed_review", "recurrent_failure"}:
        raise RequirementError("origin must be a supported user, canonical-source, confirmed-review, or recurrence assessment")
    if origin.get("kind") == "project_rule" and "source" not in record:
        raise RequirementError("existing project rules must use a canonical source reference")
    if origin.get("kind") == "confirmed_review" and assessment.get("confirmed_reusable") is not True:
        raise RequirementError("review findings require separate evidence of a confirmed reusable issue")
    for key in ("rationale",):
        _text(assessment.get(key), key)
    evidence = assessment.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 32:
        raise RequirementError("assessment requires bounded source evidence")
    for item in evidence:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or not isinstance(item.get("reference"), str):
            raise RequirementError("assessment evidence entries require kind and reference")
    record = dict(record)
    record["selectors"] = selectors
    if assessment.get("decision") == "equivalent":
        canonical_id = assessment.get("canonical_id")
        if canonical_id != identifier:
            raise RequirementError("equivalent assessment must name the reused canonical ID")
        if any(key in record for key in ("trigger", "action", "check", "source")):
            raise RequirementError("equivalent assessment must not submit a second requirement body")
    elif "source" in record:
        record["source"] = _validate_source(source_root, record["source"])
        if any(key in record for key in ("trigger", "action", "check")):
            raise RequirementError("existing-source references cannot duplicate canonical instruction text")
    else:
        for key in ("trigger", "action", "check"):
            record[key] = _text(record.get(key), key)
    return record


def _atomic_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(_json_bytes(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def record(project, memory_relative, candidate, *, expected_registry_revision, source_root=None):
    path = registry_path(project, memory_relative)
    lock = path.with_suffix(path.suffix + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_RDWR
    try:
        descriptor = fs.open_regular(lock, flags, 0o600)
    except OSError as exc:
        raise RequirementError(f"requirements lock is unsafe: {exc}") from None
    with os.fdopen(descriptor, "a+b") as stream:
        fs.lock(stream.fileno())
        data, _ = load(project, memory_relative)
        if data["revision"] != expected_registry_revision:
            raise RequirementError("requirements registry revision conflict; reload and reassess")
        item = validate_record(source_root or project, candidate)
        prior = next((row for row in data["requirements"] if row.get("id") == item["id"]), None)
        if item["assessment"].get("decision") == "equivalent":
            target = item["assessment"].get("canonical_id")
            if prior is None or target != prior["id"]:
                raise RequirementError("equivalent decision must identify an existing canonical requirement")
            if item["selectors"] != prior["selectors"] or item["revision"] != prior["revision"]:
                raise RequirementError("equivalent decision must match the canonical ID, scope, and revision")
            return {"registry_revision": data["revision"], "requirement": prior,
                    "bytes": path.stat().st_size, "equivalent_reused": True}
        if prior:
            replay = dict(item)
            replay.pop("revision", None)
            current = dict(prior)
            current.pop("revision", None)
            current.pop("history", None)
            replay.pop("history", None)
            if replay == current:
                return {"registry_revision": data["revision"], "requirement": prior,
                        "bytes": path.stat().st_size, "replayed": True}
            if item["revision"] != prior["revision"] + 1:
                raise RequirementError("requirement update must increment the existing revision")
            if item.get("status", "active") == "active":
                signature = _canonical_signature(item)
                duplicate = next((row for row in data["requirements"]
                                  if row["id"] != item["id"] and
                                  row.get("status", "active") == "active" and
                                  _canonical_signature(row) == signature), None)
                if duplicate is not None:
                    raise RequirementError(f"duplicate canonical requirement; reuse ID {duplicate['id']}")
            reason = _text(item.get("change_reason"), "change_reason")
            history = list(prior.get("history", []))
            history.append({"revision": prior["revision"], "sha256": hashlib.sha256(_json_bytes(prior)).hexdigest(), "change_reason": reason})
            item["history"] = history[-16:]
            data["requirements"] = [item if row["id"] == item["id"] else row for row in data["requirements"]]
        else:
            if item["revision"] != 1:
                raise RequirementError("new requirement must start at revision 1")
            signature = _canonical_signature(item)
            duplicate = next((row for row in data["requirements"]
                              if row.get("status", "active") == "active" and
                              _canonical_signature(row) == signature), None)
            if duplicate is not None:
                raise RequirementError(f"duplicate canonical requirement; reuse ID {duplicate['id']}")
            data["requirements"].append(item)
        data["requirements"].sort(key=lambda row: row["id"])
        data["revision"] += 1
        if len(_json_bytes(data)) > REGISTRY_LIMIT:
            raise RequirementError("requirements registry would exceed 512 KiB")
        _atomic_write(path, data)
        return {"registry_revision": data["revision"], "requirement": item, "bytes": path.stat().st_size}


def applicable(record, paths, role, task_class):
    selectors = record["selectors"]
    if role not in selectors["roles"] or task_class not in selectors["task_classes"]:
        return False
    declared = [path.replace("\\", "/").rstrip("/") for path in paths]
    prefixes = [prefix.replace("\\", "/").rstrip("/") for prefix in selectors["paths"]]
    return any(path == prefix or path.startswith(prefix + "/") or
               prefix.startswith(path + "/")
               for path in declared for prefix in prefixes)


def _canonical_signature(item):
    source = item.get("source")
    source_identity = ({"path": source["path"],
                        "fragment_sha256": source.get("fragment_sha256"),
                        "sha256": source["sha256"] if source.get("complete_file") is True else None}
                       if source else None)
    body = ({"source": source_identity} if source else
            {key: item.get(key) for key in ("trigger", "action", "check")})
    return _json_bytes({"selectors": item["selectors"], **body})


def context(project, memory_relative, paths, role, task_class, *, source_root=None):
    paths = normalize_scope_paths(paths)
    if role not in {"planner", "executor", "reviewer"} or not isinstance(task_class, str) or not task_class:
        raise RequirementError("requirements role and task class are required")
    data, registry_bytes = load(project, memory_relative)
    selected = []
    for row in data["requirements"]:
        if row.get("status", "active") == "active" and applicable(row, paths, role, task_class):
            if "source" in row:
                _validate_source(source_root or project, row["source"])
            selected.append({key: row[key] for key in ("id", "revision", "selectors", "trigger", "action", "check", "source") if key in row})
    envelope = {"requirements": selected, "registry_revision": data["revision"],
                "registry_bytes": registry_bytes, "entry_count": len(selected)}
    serialized, measured = checked_serialized_context(envelope)
    envelope["context_bytes"] = measured
    return envelope
