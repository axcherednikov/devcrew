"""Project-local SSH connection profile storage."""

from __future__ import annotations

import devcrew_fs as fs
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile


class ServerProfileError(ValueError):
    """Safe, user-facing profile contract error."""


STORE_RELATIVE = pathlib.Path(".codex/servers/profiles.json")
NAME_LIMIT = 128


def _project(path: str) -> pathlib.Path:
    try:
        root_bytes = subprocess.run(["git", "-C", path, "rev-parse", "--show-toplevel"],
                                    check=True, capture_output=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise ServerProfileError("project must be inside a Git repository") from None
    if root_bytes.endswith(b"\n"):
        root_bytes = root_bytes[:-1]
    if not root_bytes:
        raise ServerProfileError("project must be inside a Git repository")
    return pathlib.Path(os.fsdecode(root_bytes)).resolve()


def _validate_name(name: object) -> str:
    if not isinstance(name, str) or not name or len(name) > NAME_LIMIT or any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ServerProfileError("name must be a non-empty string of at most 128 characters without controls")
    return name


def _text(value: object, field: str, *, allow_space: bool = False) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServerProfileError(f"{field} must be a non-empty string without control characters")
    if not allow_space and any(c.isspace() for c in value):
        raise ServerProfileError(f"{field} cannot contain whitespace")
    if value.startswith("-"):
        raise ServerProfileError(f"{field} cannot begin with an option prefix")
    return value


def _profile(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ServerProfileError("profile input must be a JSON object")
    allowed = {"host", "ssh_alias", "user", "port", "identity_file"}
    unknown = set(value) - allowed
    if unknown:
        raise ServerProfileError("unknown profile fields: " + ", ".join(sorted(map(str, unknown))))
    if ("host" in value) == ("ssh_alias" in value):
        raise ServerProfileError("provide exactly one of host or ssh_alias")
    result: dict[str, object] = {}
    key = "host" if "host" in value else "ssh_alias"
    result[key] = _text(value[key], key)
    for field in ("user", "identity_file"):
        if field in value:
            result[field] = _text(value[field], field, allow_space=(field == "identity_file"))
    if "port" in value:
        port = value["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ServerProfileError("port must be an integer from 1 through 65535")
        result["port"] = port
    return result


def _safe_component(path: pathlib.Path, *, directory: bool) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if fs.is_link(path) or (not stat.S_ISDIR(info.st_mode) if directory else not stat.S_ISREG(info.st_mode)):
        raise ServerProfileError(f"unsafe path type: {path.name}")
    if not fs.has_private_mode(path, 0o700 if directory else 0o600):
        try:
            fs.private(path, 0o700 if directory else 0o600)
        except OSError:
            raise ServerProfileError(f"unsafe permissions on {path.name}") from None
    return True


def _store(root: pathlib.Path, *, create: bool) -> tuple[pathlib.Path, dict[str, object]]:
    codex = root / ".codex"
    servers = codex / "servers"
    for path in (codex, servers):
        exists = _safe_component(path, directory=True)
        if not exists and create:
            path.mkdir(mode=0o700)
            fs.private(path, 0o700)
        elif not exists:
            return servers / "profiles.json", {"schema_version": 1, "profiles": {}}
    file = servers / "profiles.json"
    if not _safe_component(file, directory=False):
        return file, {"schema_version": 1, "profiles": {}}
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ServerProfileError("profile store is unreadable or contains invalid JSON") from None
    schema_version = data.get("schema_version") if isinstance(data, dict) else None
    if (not isinstance(data, dict) or set(data) != {"schema_version", "profiles"}
            or type(schema_version) is not int or schema_version != 1
            or not isinstance(data.get("profiles"), dict)):
        raise ServerProfileError("profile store has an unsupported schema")
    for name, profile in data["profiles"].items():
        _validate_name(name)
        if _profile(profile) != profile:
            raise ServerProfileError("profile store contains invalid data")
    return file, data


def _tracked(root: pathlib.Path) -> list[str]:
    try:
        result = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", ":(top,literal).codex"],
                                check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError):
        raise ServerProfileError("cannot verify Git tracking state") from None
    return [item.decode("utf-8", "replace") for item in result.stdout.split(b"\0") if item]


def _ignore(root: pathlib.Path, file: pathlib.Path) -> None:
    path = root / ".gitignore"
    if _safe_component(path, directory=False):
        before = path.read_bytes()
    else:
        before = b""
    line = b"/.codex/"

    def verify() -> bool:
        for target in (".codex/", ".codex/servers/profiles.json"):
            try:
                result = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", target])
            except OSError:
                raise ServerProfileError("cannot verify Git ignore rules") from None
            if result.returncode:
                return False
        return True

    if verify():
        return
    lines = before.splitlines()
    changed = not lines or lines[-1] != line
    if changed:
        after = before + (b"\n" if before and not before.endswith(b"\n") else b"") + line + b"\n"
        fd, temporary = tempfile.mkstemp(prefix=".gitignore.", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            fs.private(temporary, 0o600)
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    try:
        if not verify():
            raise ServerProfileError("Git does not ignore .codex runtime state")
    except ServerProfileError:
        if changed:
            if before:
                path.write_bytes(before)
            else:
                path.unlink(missing_ok=True)
        raise


def _write(file: pathlib.Path, data: dict[str, object]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".profiles.", dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        fs.private(temporary, 0o600)
        os.replace(temporary, file)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def command(args: object) -> int:
    try:
        name = _validate_name(getattr(args, "name", "")) if hasattr(args, "name") else None
        root = _project(args.project)
        operation = args.servers_operation
        if operation in ("save", "delete"):
            tracked = _tracked(root)
            if tracked:
                raise ServerProfileError("tracked .codex state must be handled separately: " + ", ".join(tracked))
        file, data = _store(root, create=(operation == "save"))
        profiles = data["profiles"]
        if operation == "save":
            try:
                value = json.load(sys.stdin)
            except (json.JSONDecodeError, UnicodeError):
                raise ServerProfileError("stdin must contain one valid JSON object") from None
            profile = _profile(value)
            if name in profiles and not args.replace:
                raise ServerProfileError("profile already exists; pass --replace to update it")
            _ignore(root, file)
            profiles[name] = profile
            _write(file, data)
            result = {"ok": True, "name": name}
        elif operation == "list":
            result = {"ok": True, "profiles": sorted(profiles)}
        elif operation == "get":
            if name not in profiles:
                raise ServerProfileError("profile not found")
            result = {"ok": True, "name": name, "profile": profiles[name]}
        else:
            if name not in profiles:
                raise ServerProfileError("profile not found")
            _ignore(root, file)
            del profiles[name]
            _write(file, data)
            result = {"ok": True, "name": name}
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ServerProfileError, OSError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2
