"""Durable task checkout lifecycle; linked worktrees require a user request."""

from __future__ import annotations

import devcrew_fs as fs
import hashlib
import json
import os
import pathlib
import re
import secrets
import subprocess
import tempfile
from contextlib import contextmanager


def _git(project, *args, check=True, read_only=False):
    environment = None
    if read_only:
        environment = dict(os.environ)
        environment["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
        env=environment,
    )
    if check and result.returncode:
        raise ValueError((result.stderr or result.stdout).strip() or "git command failed")
    return result


def _repository(project):
    return _repository_details(project)[:4]


def _repository_details(project):
    # Keep the legacy command rooting when Git environment overrides may
    # reinterpret relative paths between commands.
    if any(name in os.environ for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
        root = pathlib.Path(_git(project, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
        common = pathlib.Path(
            _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        ).resolve()
        git_dir = pathlib.Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    else:
        result = _git(
            project,
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            "--absolute-git-dir",
        )
        fields = result.stdout.splitlines()
        if result.returncode == 0 and len(fields) == 3 and all(fields):
            root, common, git_dir = (pathlib.Path(value) for value in fields)
            root, common = root.resolve(), common.resolve()
        else:
            # Newlines in a path make rev-parse's line-oriented output
            # ambiguous; preserve the old queries and their .strip() rules.
            root = pathlib.Path(
                _git(project, "rev-parse", "--show-toplevel").stdout.strip()
            ).resolve()
            common = pathlib.Path(
                _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            ).resolve()
            git_dir = pathlib.Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    identity = hashlib.sha256((str(root) + "\0" + str(common)).encode()).hexdigest()
    return root, common, identity, identity[:16], git_dir


def _linked_repository_details(project, *, read_only=False):
    def git(*args, check=True):
        return _git(project, *args, check=check, read_only=read_only)

    if not any(name in os.environ for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR")):
        result = git(
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-common-dir",
            "HEAD",
            check=False,
        )
        fields = result.stdout.splitlines()
        if result.returncode == 0 and len(fields) == 3 and all(fields):
            root, common, head = fields
            return (
                pathlib.Path(root).resolve(),
                pathlib.Path(common).resolve(),
                head.strip().lower(),
            )

    # Preserve the old queries and their error/whitespace behavior when paths
    # are ambiguous or Git overrides may affect relative path interpretation.
    root = pathlib.Path(git("rev-parse", "--show-toplevel").stdout.strip()).resolve()
    common = pathlib.Path(
        git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    ).resolve()
    head = git("rev-parse", "HEAD").stdout.strip().lower()
    return root, common, head


def location(project, task_id, *, _repository_metadata=None):
    if (
        not isinstance(task_id, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id)
        or fs.unsafe_parts([task_id])
    ):
        raise ValueError("TASK_ID must be a safe filesystem identifier")
    root, common, identity, key, git_dir = _repository_metadata or _repository_details(project)
    path_key = hashlib.sha256(str(root).encode("utf-8")).hexdigest()[:16]
    namespace = root.parent / ".devcrew-task-worktrees" / f"{root.name}-{path_key}"
    current_state = git_dir / "devcrew-checkouts" / f"{task_id}.json"
    linked_state = namespace / ".devcrew-state" / f"{task_id}.json"
    if current_state.exists() or fs.is_link(current_state):
        if linked_state.exists() or fs.is_link(linked_state):
            raise ValueError("TASK_CHECKOUT_MODE_COLLISION")
        branch = f"codex/tasks/{task_id}"
        if not _has_symlink_component(current_state):
            try:
                record = json.loads(current_state.read_text(encoding="utf-8"))
                saved_branch = record.get("branch") if isinstance(record, dict) else None
                if (
                    isinstance(saved_branch, str)
                    and saved_branch
                    and not _git(
                        root, "check-ref-format", "--branch", saved_branch, check=False
                    ).returncode
                ):
                    branch = saved_branch
            except (OSError, ValueError):
                pass  # resolve validates malformed receipts without changing Git.
        return root, common, identity, root, branch, current_state
    return (
        root,
        common,
        identity,
        namespace / task_id,
        f"codex/tasks/{task_id}",
        namespace / ".devcrew-state" / f"{task_id}.json",
    )


def _current_state(root, task_id):
    git_dir = pathlib.Path(_git(root, "rev-parse", "--absolute-git-dir").stdout.strip())
    return git_dir / "devcrew-checkouts" / f"{task_id}.json"


def primary_branch(project, *, current=None):
    """Resolve local Git evidence without fetching or changing references."""
    heads = set(
        _git(
            project, "for-each-ref", "--format=%(refname:lstrip=2)", "refs/heads/"
        ).stdout.splitlines()
    )
    explicit = _git(project, "config", "--local", "--get", "devcrew.primaryBranch", check=False)
    if explicit.returncode == 0:
        name = explicit.stdout.strip()
        if name not in heads:
            raise ValueError("PRIMARY_BRANCH_REQUIRED: devcrew.primaryBranch is not a local branch")
        return name
    remote_defaults = set()
    for remote in _git(project, "remote").stdout.splitlines():
        prefix = f"refs/remotes/{remote}/"
        ref = _git(project, "symbolic-ref", "--quiet", prefix + "HEAD", check=False)
        if ref.returncode == 0 and ref.stdout.strip().startswith(prefix):
            name = ref.stdout.strip()[len(prefix) :]
            if remote == "origin":
                remote_defaults = {name}
                break
            remote_defaults.add(name)
    if remote_defaults:
        if len(remote_defaults) == 1 and remote_defaults <= heads:
            return next(iter(remote_defaults))
        if current is not None and current not in remote_defaults:
            return None
        raise ValueError(
            "PRIMARY_BRANCH_REQUIRED: remote default is ambiguous or unavailable locally"
        )
    configured = _git(project, "config", "--get", "init.defaultBranch", check=False).stdout.strip()
    if configured in heads:
        return configured
    conventional = heads & {"main", "master"}
    if len(conventional) == 1:
        return next(iter(conventional))
    if len(heads) == 1:
        return next(iter(heads))
    if current is not None:
        # Without an authoritative default, preserve ordinary feature checkouts.
        return current if current in conventional else None
    raise ValueError(
        "PRIMARY_BRANCH_REQUIRED: set git config devcrew.primaryBranch to the local primary branch"
    )


def primary_head(project):
    """Resolve the selected primary branch to its local commit."""
    name = primary_branch(project)
    return (
        _git(project, "rev-parse", "--verify", f"refs/heads/{name}^{{commit}}")
        .stdout.strip()
        .lower()
    )


def _safe_parent(path):
    current = pathlib.Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if fs.is_link(current):
            return False
    parent = path.parent
    return parent.is_dir() and os.access(parent, os.W_OK)


def _has_symlink_component(path):
    current = pathlib.Path(path.anchor)
    for part in pathlib.Path(path).parts[1:]:
        current /= part
        if fs.is_link(current):
            return True
    return False


def _safe_creation_parent(parent):
    parent = pathlib.Path(parent)
    if _has_symlink_component(parent):
        return False
    current = parent
    while not current.exists():
        current = current.parent
    return current.is_dir() and os.access(current, os.W_OK)


@contextmanager
def _receipt_lock(path):
    lock_path = path.with_name(path.name + ".lock")
    if _has_symlink_component(path) or _has_symlink_component(lock_path):
        raise ValueError("TASK_WORKTREE_STATE_INVALID")
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep this inode stable across atomic replacements of the receipt.
    with os.fdopen(fs.open_regular(lock_path, os.O_CREAT | os.O_RDWR), "r+b") as lock:
        fs.lock(lock.fileno())
        try:
            yield
        finally:
            fs.unlock(lock.fileno())


def _receipt_revision(record):
    revision = record.get("receipt_revision", 0) if record is not None else 0
    if type(revision) is not int or revision < 0:
        raise ValueError(
            "TASK_WORKTREE_STATE_INVALID: receipt_revision must be a nonnegative integer"
        )
    return revision


def _check_snapshot(path, expected):
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        current = None
    except (OSError, ValueError) as exc:
        raise ValueError("TASK_WORKTREE_STATE_INVALID") from exc
    if current is not None and not isinstance(current, dict):
        raise ValueError("TASK_WORKTREE_STATE_INVALID")
    if (
        _receipt_revision(current) != _receipt_revision(expected)
        or current != expected
        or (expected is None and (path.exists() or fs.is_link(path)))
    ):
        raise ValueError(
            "TASK_WORKTREE_STATE_CONFLICT: receipt changed; resolve it again before retrying"
        )


def _atomic_json(path, data, *, expected):
    """Compare the complete original snapshot and replace it under one lock."""
    with _phase_manifest_lock(path), _receipt_lock(path):
        _check_snapshot(path, expected)
        _require_consistent_state(path, expected)
        _store_receipt(path, data, expected)


def _phase_manifest_lock(path):
    import devcrew_phase

    return devcrew_phase._manifest_lock(path.with_name(f"{path.stem}.phases.json"))


def _require_no_candidate_transition(path):
    import devcrew_phase

    manifest_path = path.with_name(f"{path.stem}.phases.json")
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ValueError("PHASE_MANIFEST_REQUIRED: cannot check candidate state") from exc
        if not isinstance(manifest, dict):
            raise ValueError("PHASE_MANIFEST_REQUIRED: cannot check candidate state")
        devcrew_phase._require_no_candidate_transition(manifest, path=manifest_path)
        return manifest


def _check_phase_head(manifest, record):
    phase = next(p for p in manifest["phases"] if p["phase_id"] == manifest["current_phase_id"])
    candidate = phase.get("candidate_commit") or {}
    expected = candidate.get("sha") or phase.get("base_sha") or manifest["worktree"]["base_sha"]
    heads = {expected}
    # Preserve the established explicit local-fix/replan reset window.
    if phase.get("local_fix_transition") or phase.get("systemic_replan_transition"):
        heads.add(candidate.get("base"))
    link = manifest["worktree"]
    if (
        record["expected_head"] not in heads
        or record["task_id"] != manifest["task_id"]
        or any(
            record[key] != link[key] for key in ("repository_id", "task_worktree_root", "branch")
        )
    ):
        raise ValueError(
            "PHASE_COMMIT_INCONSISTENT: receipt differs from current phase head or worktree linkage"
        )
    return phase


def _require_consistent_state(path, record):
    """Guard ordinary JSON mutations; explicit Git transitions validate separately."""
    manifest = _require_no_candidate_transition(path)
    if manifest is None:
        return
    try:
        phase = _check_phase_head(manifest, record)
        if not (phase.get("local_fix_transition") or phase.get("systemic_replan_transition")):
            root = pathlib.Path(record["task_worktree_root"])
            if (
                _git(root, "rev-parse", "HEAD").stdout.strip() != record["expected_head"]
                or _git(root, "symbolic-ref", "--short", "HEAD").stdout.strip() != record["branch"]
            ):
                raise ValueError("Git HEAD/branch differs from receipt and manifest")
    except (ValueError, KeyError, TypeError, StopIteration) as exc:
        raise ValueError(
            f"PHASE_COMMIT_INCONSISTENT: receipt={path}: {exc}; "
            "preserve Git, receipt, manifest and evidence for manual reconciliation"
        ) from exc


def _store_receipt(path, data, expected):
    """Persist the next revision; callers hold the receipt lock and check expected."""
    _require_no_candidate_transition(path)
    revision = _receipt_revision(expected) + 1
    stored = dict(data, receipt_revision=revision)
    _write_json(path, stored)
    data["receipt_revision"] = revision


def _write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".task-worktree-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        fs.publish(temporary, path, replace=True)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read(path, identity, task_id, checkout, branch):
    if _has_symlink_component(path):
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    expected = {
        "schema_version": 1,
        "repository_id": identity,
        "task_id": task_id,
        "task_worktree_root": str(checkout),
        "branch": branch,
    }
    fields = set(expected) | {
        "expected_head",
        "last_known_clean",
        "writer_dispatched_since_snapshot",
        "receipt_nonce",
    }
    if isinstance(record, dict) and "receipt_revision" in record:
        fields.add("receipt_revision")
        if type(record["receipt_revision"]) is not int or record["receipt_revision"] < 0:
            return None
    if isinstance(record, dict) and "worktree_request" in record:
        fields.add("worktree_request")
        if (
            not isinstance(record["worktree_request"], str)
            or not record["worktree_request"].strip()
        ):
            return None
    if isinstance(record, dict) and "created_from_branch" in record:
        fields.add("created_from_branch")
        source_branch = record["created_from_branch"]
        if not isinstance(source_branch, str) or not source_branch or "\0" in source_branch:
            return None
        check = subprocess.run(
            ["git", "check-ref-format", f"refs/heads/{source_branch}"], capture_output=True
        )
        if check.returncode:
            return None
    if (
        not isinstance(record, dict)
        or set(record) != fields
        or type(record.get("schema_version")) is not int
        or any(record.get(k) != v for k, v in expected.items())
    ):
        return None
    if (
        not isinstance(record.get("expected_head"), str)
        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", record["expected_head"])
        or type(record.get("last_known_clean")) is not bool
        or type(record.get("writer_dispatched_since_snapshot")) is not bool
        or not isinstance(record.get("receipt_nonce"), str)
    ):
        return None
    return record


def _receipt(record, state, dirty=False):
    receipt = {
        "status": "DIRTY"
        if dirty
        else ("CLEAN" if state == "RESOLVED" else f"TASK_WORKTREE_{state}"),
        "state": state,
        "owner": "DEVCREW",
        "task_id": record["task_id"],
        "repository_id": record["repository_id"],
        "task_worktree_root": record["task_worktree_root"],
        "branch": record["branch"],
        "expected_head": record["expected_head"],
        "head": record["expected_head"],
        "dirty": dirty,
        "receipt_nonce": record["receipt_nonce"],
    }
    if "created_from_branch" in record:
        receipt["created_from_branch"] = record["created_from_branch"]
    return receipt


def _worktrees(project, *, read_only=False):
    raw = _git(project, "worktree", "list", "--porcelain", read_only=read_only).stdout
    entries = []
    for block in raw.strip().split("\n\n"):
        item = dict(line.partition(" ")[::2] for line in block.splitlines())
        if item:
            entries.append(item)
    return entries


def create(project, task_id, base=None, *, worktree_request=None):
    """Prepare the current checkout, or explicitly requested parallel work."""
    if worktree_request is not None:
        if not isinstance(worktree_request, str) or not worktree_request.strip():
            raise ValueError("WORKTREE_REQUEST_REQUIRED: record the actual explicit user request")
        return _create_worktree(
            project, task_id, primary_head(project) if base is None else base, worktree_request
        )
    root, common, identity, path, branch, state_path = location(project, task_id)
    _require_no_candidate_transition(state_path)
    current_state = _current_state(root, task_id)
    if state_path.exists() or fs.is_link(state_path):
        return {
            "status": "TASK_CHECKOUT_ALREADY_EXISTS",
            "task_id": task_id,
            "task_worktree_root": str(path),
            "branch": branch,
        }
    current_branch = _git(
        root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    ).stdout.strip()
    if not current_branch:
        raise ValueError("TASK_CHECKOUT_BRANCH_REQUIRED: detached HEAD has no working branch")
    head = _git(root, "rev-parse", "HEAD").stdout.strip().lower()
    if base is not None and (not isinstance(base, str) or base.lower() != head):
        raise ValueError("CHECKOUT_BASE_MISMATCH: explicit base must match the current HEAD")
    dirty = bool(_git(root, "status", "--porcelain", "--untracked-files=all").stdout)
    if not _safe_creation_parent(current_state.parent):
        raise ValueError("TASK_WORKTREE_PATH_COLLISION")
    created_from_branch = (
        current_branch if current_branch == primary_branch(root, current=current_branch) else None
    )
    if created_from_branch:
        stem, suffix = branch, 2
        while (
            _git(
                root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
            ).returncode
            == 0
        ):
            branch, suffix = f"{stem}-{suffix}", suffix + 1
        _git(root, "switch", "--no-overwrite-ignore", "-c", branch, head)
    else:
        branch = current_branch
    record = {
        "schema_version": 1,
        "repository_id": identity,
        "task_id": task_id,
        "task_worktree_root": str(root),
        "branch": branch,
        "expected_head": head,
        "last_known_clean": not dirty,
        "writer_dispatched_since_snapshot": False,
        "receipt_nonce": secrets.token_hex(24),
    }
    if created_from_branch:
        record["created_from_branch"] = created_from_branch
    _atomic_json(current_state, record, expected=None)
    return _receipt(record, "CREATED", dirty)


def _create_worktree(project, task_id, base, worktree_request):
    root, common, identity, path, branch, state_path = location(project, task_id)
    _require_no_candidate_transition(state_path)
    if _git(root, "check-ref-format", "--branch", branch, check=False).returncode:
        raise ValueError("INVALID_TASK_BRANCH")
    if not isinstance(base, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", base):
        raise ValueError("INVALID_BASE_SHA")
    resolved = _git(root, "rev-parse", "--verify", f"{base}^{{commit}}", check=False)
    if resolved.returncode or resolved.stdout.strip().lower() != base.lower():
        raise ValueError("INVALID_BASE_SHA")
    if base.lower() != primary_head(root):
        raise ValueError(
            "PRIMARY_BASE_MISMATCH: task branch must start at the local primary branch head"
        )
    entries = _worktrees(root)
    registered = next(
        (item for item in entries if pathlib.Path(item.get("worktree", "")) == path), None
    )
    if registered:
        if registered.get("branch") == f"refs/heads/{branch}" and path.is_dir():
            return {
                "status": "TASK_WORKTREE_ALREADY_EXISTS",
                "task_id": task_id,
                "task_worktree_root": str(path),
                "branch": branch,
            }
        raise ValueError("TASK_WORKTREE_PATH_COLLISION")
    if path.exists() or fs.is_link(path):
        raise ValueError("TASK_WORKTREE_PATH_COLLISION")
    if not _safe_creation_parent(path.parent) or not _safe_creation_parent(state_path.parent):
        raise ValueError("TASK_WORKTREE_PATH_COLLISION")
    if (
        _git(
            root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        ).returncode
        == 0
    ):
        return {
            "status": "TASK_WORKTREE_BRANCH_COLLISION",
            "task_id": task_id,
            "task_worktree_root": str(path),
            "branch": branch,
        }
    if any(item.get("branch") == f"refs/heads/{branch}" for item in entries):
        return {
            "status": "TASK_WORKTREE_BRANCH_COLLISION",
            "task_id": task_id,
            "task_worktree_root": str(path),
            "branch": branch,
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "-b", branch, str(path), base)
    dirty = bool(_git(path, "status", "--porcelain", "--untracked-files=all").stdout)
    if dirty or _git(path, "rev-parse", "HEAD").stdout.strip().lower() != base.lower():
        raise ValueError("TASK_WORKTREE_CREATE_VALIDATION_FAILED")
    created_from_branch = primary_branch(root)
    record = {
        "schema_version": 1,
        "repository_id": identity,
        "task_id": task_id,
        "task_worktree_root": str(path),
        "branch": branch,
        "expected_head": base.lower(),
        "last_known_clean": True,
        "writer_dispatched_since_snapshot": False,
        "receipt_nonce": secrets.token_hex(24),
        "worktree_request": worktree_request.strip(),
        "created_from_branch": created_from_branch,
    }
    _atomic_json(state_path, record, expected=None)
    return _receipt(record, "CREATED")


def resolve(project, task_id, *, read_only=False, worktree_request=None, _repository_metadata=None):
    root, common, identity, path, branch, state_path = location(
        project, task_id, _repository_metadata=_repository_metadata
    )

    def git(target, *args, check=True):
        return _git(target, *args, check=check, read_only=read_only)

    if not read_only:
        _require_no_candidate_transition(state_path)
    record = _read(state_path, identity, task_id, path, branch)
    if record is None:
        if not path.exists() and not fs.is_link(path):
            return {
                "status": "UNSAFE_MISSING_WORKTREE",
                "task_id": task_id,
                "task_worktree_root": str(path),
                "branch": branch,
            }
        raise ValueError("TASK_WORKTREE_STATE_INVALID")
    import devcrew_phase

    phase_path = state_path.with_name(f"{state_path.stem}.phases.json")
    if phase_path.exists():
        try:
            manifest = json.loads(phase_path.read_text(encoding="utf-8"))
            if not read_only:
                devcrew_phase._require_no_candidate_transition(manifest, path=phase_path)
            devcrew_phase._verify_pre_candidate_history(pathlib.Path(project), manifest)
            # Pending reads may inspect the existing receipt without replay.
            if "candidate_transition" not in manifest:
                _check_phase_head(manifest, record)
        except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
            raise ValueError(
                f"PHASE_COMMIT_INCONSISTENT: manifest={phase_path}, receipt={state_path}: {exc}; "
                "preserve Git, receipt, manifest and evidence for manual reconciliation"
            ) from exc
    snapshot = dict(record)
    if worktree_request is not None:
        if read_only or not isinstance(worktree_request, str) or not worktree_request.strip():
            raise ValueError("WORKTREE_REQUEST_REQUIRED: record the actual explicit user request")
        if path == root:
            raise ValueError("TASK_CHECKOUT_MODE_COLLISION")
        record["worktree_request"] = worktree_request.strip()
        if not path.exists() and not fs.is_link(path):
            _atomic_json(state_path, record, expected=snapshot)
            snapshot = dict(record)
    if path != root and not record.get("worktree_request") and path.exists():
        raise ValueError(
            "WORKTREE_REQUEST_REQUIRED: existing worktree requires an explicit user request"
        )
    entries = _worktrees(root, read_only=read_only)
    entry = next((item for item in entries if pathlib.Path(item.get("worktree", "")) == path), None)
    if not path.exists() and not fs.is_link(path):
        if read_only or not record.get("worktree_request") or path == root:
            return {
                "status": "UNSAFE_MISSING_WORKTREE",
                "task_id": task_id,
                "task_worktree_root": str(path),
                "branch": branch,
            }
        if (
            entry is None
            and record["last_known_clean"]
            and not record["writer_dispatched_since_snapshot"]
            and _safe_parent(path)
            and not path.exists()
            and not fs.is_link(path)
        ):
            branch_head = git(root, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
            attached_elsewhere = any(
                item.get("branch") == f"refs/heads/{branch}" for item in entries
            )
            if (
                branch_head.returncode == 0
                and branch_head.stdout.strip().lower() == record["expected_head"]
                and not attached_elsewhere
            ):
                _git(root, "worktree", "add", str(path), branch)
                linked_head = _git(path, "rev-parse", "HEAD").stdout.strip().lower()
                linked_branch = _git(path, "symbolic-ref", "--short", "HEAD").stdout.strip()
                current_branch_head = (
                    _git(root, "rev-parse", "--verify", f"refs/heads/{branch}")
                    .stdout.strip()
                    .lower()
                )
                clean = not bool(
                    _git(path, "status", "--porcelain", "--untracked-files=all").stdout
                )
                if (
                    linked_head != record["expected_head"]
                    or current_branch_head != record["expected_head"]
                    or linked_branch != branch
                    or not clean
                ):
                    raise ValueError("TASK_WORKTREE_RECREATE_VALIDATION_FAILED")
                verified = resolve(root, task_id)
                if (
                    verified.get("state") != "RESOLVED"
                    or verified.get("head", "").lower() != record["expected_head"]
                ):
                    raise ValueError("TASK_WORKTREE_RECREATE_VALIDATION_FAILED")
                return _receipt(record, "RECREATED")
        elif (
            entry is not None
            and record["last_known_clean"]
            and not record["writer_dispatched_since_snapshot"]
            and _safe_parent(path)
            and not path.exists()
            and not fs.is_link(path)
        ):
            matches = [item for item in entries if pathlib.Path(item.get("worktree", "")) == path]
            branch_entries = [
                item for item in entries if item.get("branch") == f"refs/heads/{branch}"
            ]
            branch_head = _git(root, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
            if (
                len(matches) == 1
                and len(branch_entries) == 1
                and entry.get("branch") == f"refs/heads/{branch}"
                and entry.get("HEAD", "").lower() == record["expected_head"]
                and branch_head.returncode == 0
                and branch_head.stdout.strip().lower() == record["expected_head"]
            ):
                other_entries = [item for item in entries if item is not entry]
                removed = _git(root, "worktree", "remove", str(path), check=False)
                after_remove = _worktrees(root)
                if (
                    removed.returncode
                    or any(pathlib.Path(item.get("worktree", "")) == path for item in after_remove)
                    or after_remove != other_entries
                ):
                    return {
                        "status": "UNSAFE_MISSING_WORKTREE",
                        "task_id": task_id,
                        "task_worktree_root": str(path),
                        "branch": branch,
                    }
                _git(root, "worktree", "add", str(path), branch)
                linked_head = _git(path, "rev-parse", "HEAD").stdout.strip().lower()
                linked_branch = _git(path, "symbolic-ref", "--short", "HEAD").stdout.strip()
                current_branch_head = (
                    _git(root, "rev-parse", "--verify", f"refs/heads/{branch}")
                    .stdout.strip()
                    .lower()
                )
                clean = not bool(
                    _git(path, "status", "--porcelain", "--untracked-files=all").stdout
                )
                if (
                    linked_head != record["expected_head"]
                    or current_branch_head != record["expected_head"]
                    or linked_branch != branch
                    or not clean
                ):
                    raise ValueError("TASK_WORKTREE_RECREATE_VALIDATION_FAILED")
                verified = resolve(root, task_id)
                if (
                    verified.get("state") != "RESOLVED"
                    or verified.get("head", "").lower() != record["expected_head"]
                ):
                    raise ValueError("TASK_WORKTREE_RECREATE_VALIDATION_FAILED")
                return _receipt(record, "RECREATED")
        return {
            "status": "UNSAFE_MISSING_WORKTREE",
            "task_id": task_id,
            "task_worktree_root": str(path),
            "branch": branch,
        }
    if (
        fs.is_link(path)
        or not path.is_dir()
        or entry is None
        or entry.get("branch") != f"refs/heads/{branch}"
    ):
        raise ValueError("TASK_WORKTREE_PATH_COLLISION")
    linked_root, linked_common, head = _linked_repository_details(path, read_only=read_only)
    branch_head = git(root, "rev-parse", "--verify", f"refs/heads/{branch}", check=False)
    if (
        branch_head.returncode
        or branch_head.stdout.strip().lower() != head
        or linked_root != path.resolve()
        or linked_common != common
        or head != record["expected_head"]
    ):
        raise ValueError(
            f"TASK_WORKTREE_REPOSITORY_MISMATCH: HEAD={head}, receipt expected_head={record['expected_head']}; "
            f"receipt={state_path}; preserve Git, receipt, manifest and evidence for manual reconciliation"
        )
    if record != snapshot:
        _atomic_json(state_path, record, expected=snapshot)
        snapshot = dict(record)
    dirty = bool(git(path, "status", "--porcelain", "--untracked-files=all").stdout)
    # Present clean checkouts may refresh evidence only before any writer was dispatched.
    if not read_only and not dirty and not record["writer_dispatched_since_snapshot"]:
        record["last_known_clean"] = True
        _atomic_json(state_path, record, expected=snapshot)
    elif not read_only and dirty:
        record["last_known_clean"] = False
        _atomic_json(state_path, record, expected=snapshot)
    return _receipt(record, "RESOLVED", dirty)


def validate_dispatch(project, task_id, worktree, base, receipt):
    root, common, identity, path, branch, state_path = location(project, task_id)
    _require_no_candidate_transition(state_path)
    record = _read(state_path, identity, task_id, path, branch)
    if not isinstance(receipt, dict) or record is None:
        raise ValueError("TASK_WORKTREE_INVALID: missing lifecycle receipt")
    if (
        receipt.get("owner") != "DEVCREW"
        or receipt.get("state") not in ("CREATED", "RESOLVED", "RECREATED")
        or receipt.get("receipt_nonce") != record["receipt_nonce"]
        or receipt.get("repository_id") != identity
        or receipt.get("task_id") != task_id
        or receipt.get("task_worktree_root") != str(path)
        or receipt.get("branch") != branch
        or not isinstance(base, str)
        or not isinstance(receipt.get("expected_head"), str)
        or receipt["expected_head"].lower() != base.lower()
        or pathlib.Path(worktree) != path
    ):
        raise ValueError("TASK_WORKTREE_INVALID: stale or mismatched lifecycle receipt")
    fresh = resolve(root, task_id)
    if fresh.get("state") not in ("RESOLVED",) or fresh.get("head", "").lower() != base.lower():
        raise ValueError("TASK_WORKTREE_INVALID: checkout is not resolved")
    # resolve may refresh cleanliness. Reload before modifying its latest state,
    # and reject a receipt invalidated by another dispatch or head transition.
    record = _read(state_path, identity, task_id, path, branch)
    if (
        record is None
        or record["receipt_nonce"] != receipt["receipt_nonce"]
        or record["expected_head"].lower() != base.lower()
    ):
        raise ValueError("TASK_WORKTREE_INVALID: stale or mismatched lifecycle receipt")
    snapshot = dict(record)
    record["writer_dispatched_since_snapshot"] = True
    record["last_known_clean"] = False
    record["receipt_nonce"] = secrets.token_hex(24)
    _atomic_json(state_path, record, expected=snapshot)


def advance_expected_head(project, task_id, expected, candidate, *, before_advance=None):
    """Compare-and-swap the durable lifecycle head after a phase commit."""
    root, _, identity, path, branch, state_path = location(project, task_id)
    with _phase_manifest_lock(state_path), _receipt_lock(state_path):
        _require_no_candidate_transition(state_path)
        record = _read(state_path, identity, task_id, path, branch)
        if (
            record is None
            or record["expected_head"] != expected
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", candidate)
        ):
            raise ValueError("TASK_WORKTREE_STATE_INVALID")
        snapshot = dict(record)
        if before_advance is not None:
            before_advance()
        actual = _git(path, "rev-parse", "HEAD").stdout.strip().lower()
        actual_branch = _git(path, "symbolic-ref", "--short", "HEAD").stdout.strip()
        if (
            actual != candidate
            or actual_branch != branch
            or _git(path, "status", "--porcelain", "--untracked-files=all").stdout
        ):
            raise ValueError("TASK_WORKTREE_COMMIT_VALIDATION_FAILED")
        record["expected_head"] = candidate
        record["last_known_clean"] = True
        record["writer_dispatched_since_snapshot"] = True
        record["receipt_nonce"] = secrets.token_hex(24)
        _check_snapshot(state_path, snapshot)
        _store_receipt(state_path, record, snapshot)
    return record
