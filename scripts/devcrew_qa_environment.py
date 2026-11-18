"""Local QA environment lifecycle with conservative process, port, and data ownership."""

from __future__ import annotations

import devcrew_fs as fs
import ctypes
import ctypes.util
import hashlib
import http.client
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.parse
import uuid
from typing import Any, Callable

import devcrew_resources

if os.name == "nt":
    import devcrew_winprocess as winprocess


class EnvironmentError(ValueError):
    """Invalid profile, lifecycle conflict, or incomplete cleanup."""


VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,47}$")
ENV_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
MAX_BYTES = 256 * 1024
MAX_COMMANDS = 16

# Deliberately small: commands do not inherit the CLI environment, only these
# documented system variables plus the names the profile explicitly allows.
SYSTEM_ENV_NAMES = ("HOME", "LANG", "LC_ALL", "LC_CTYPE", "PATH", "TMPDIR", "TZ")

if os.name == "nt":
    SYSTEM_ENV_NAMES += ("SystemRoot", "SystemDrive", "ComSpec", "PATHEXT", "TEMP", "TMP")

OWNER_MARKER = ".devcrew-qa-owner.json"
PREPARE_TIMEOUT_SECONDS = 60.0
STATE_LOCK_WAIT_SECONDS = 10.0
MAX_READINESS_REQUEST_SECONDS = 2.0

STATE_STATUSES = ("STARTING", "READY", "CLEANUP_FAILED", "STOPPED")
SERVICE_STATUSES = (
    "NOT_STARTED",
    "START_INTENT",
    "EFFECT_UNKNOWN",
    "READY",
    "STOPPED",
    "CLEANUP_FAILED",
)
PREPARATION_STATUSES = ("NOT_STARTED", "START_INTENT", "COMPLETE", "FAILED", "EFFECT_UNKNOWN")
CLEANUP_STATUSES = ("NOT_STARTED", "CLEANUP_INTENT", "COMPLETE", "FAILED", "EFFECT_UNKNOWN")
DATA_STATUSES = ("NOT_CREATED", "CREATED", "REMOVAL_INTENT", "REMOVED", "FAILED", "EFFECT_UNKNOWN")
OWNED_STATUSES = ("CREATED", "REMOVAL_INTENT")

PROFILE_REQUIRED = {"schema_version", "id", "resources", "environment", "prepare", "services"}
PROFILE_OPTIONAL = {"cleanup", "data_dir"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _read_json(path: pathlib.Path, label: str = "input") -> Any:
    if fs.is_link(path) or not path.is_file():
        raise EnvironmentError(f"{label} must be a regular non-symlink file")
    raw = path.read_bytes()
    if len(raw) > MAX_BYTES:
        raise EnvironmentError(f"{label} exceeds the size limit")
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EnvironmentError(f"{label} is not valid JSON") from None


def _validate_profile(value: Any) -> dict[str, Any]:
    if (
        not isinstance(value, dict)
        or not PROFILE_REQUIRED <= set(value)
        or set(value) - PROFILE_REQUIRED - PROFILE_OPTIONAL
    ):
        raise EnvironmentError("profile has an unsupported schema or fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != VERSION:
        raise EnvironmentError("unsupported environment profile schema")
    if not isinstance(value["id"], str) or not NAME_RE.fullmatch(value["id"]):
        raise EnvironmentError("invalid environment profile id")
    resources = value["resources"]
    if (
        not isinstance(resources, list)
        or len(resources) > MAX_COMMANDS
        or any(not isinstance(x, str) or not NAME_RE.fullmatch(x) for x in resources)
        or len(set(resources)) != len(resources)
    ):
        raise EnvironmentError("resources must be unique bounded names")
    env_names = value["environment"]
    if (
        not isinstance(env_names, list)
        or len(env_names) > 64
        or any(not isinstance(x, str) or not ENV_RE.fullmatch(x) for x in env_names)
        or len(set(env_names)) != len(env_names)
    ):
        raise EnvironmentError("environment must contain only unique variable names")
    commands = value["prepare"]
    if not isinstance(commands, list) or len(commands) > MAX_COMMANDS:
        raise EnvironmentError("prepare must be a bounded command list")
    for command in commands:
        _validate_command(command)
    cleanup = value.get("cleanup", [])
    if not isinstance(cleanup, list) or len(cleanup) > MAX_COMMANDS:
        raise EnvironmentError("cleanup must be a bounded command list")
    for command in cleanup:
        _validate_command(command)
    if "data_dir" in value:
        _validate_data_dir(value["data_dir"])
    services = value["services"]
    if not isinstance(services, list) or not 1 <= len(services) <= MAX_COMMANDS:
        raise EnvironmentError("services must contain between 1 and 16 services")
    ids: set[str] = set()
    for service in services:
        if not isinstance(service, dict) or set(service) != {
            "id",
            "command",
            "depends_on",
            "readiness",
        }:
            raise EnvironmentError("service has an unsupported schema or fields")
        if (
            not isinstance(service["id"], str)
            or not NAME_RE.fullmatch(service["id"])
            or service["id"] in ids
        ):
            raise EnvironmentError("service IDs must be unique bounded names")
        ids.add(service["id"])
        _validate_command(service["command"])
        dependencies = service["depends_on"]
        if not isinstance(dependencies, list) or any(not isinstance(x, str) for x in dependencies):
            raise EnvironmentError("service dependencies must be a list of service IDs")
        readiness = service["readiness"]
        if not isinstance(readiness, dict) or set(readiness) != {
            "url",
            "status",
            "timeout_seconds",
        }:
            raise EnvironmentError("readiness has an unsupported schema")
        url = readiness["url"]
        if (
            not isinstance(url, str)
            or not url.startswith("http://127.0.0.1:")
            or any(x in url for x in ("?", "#", "@"))
        ):
            raise EnvironmentError("readiness URL must be a literal loopback HTTP URL")
        if type(readiness["status"]) is not int or not 100 <= readiness["status"] <= 599:
            raise EnvironmentError("readiness status must be an HTTP status code")
        if (
            type(readiness["timeout_seconds"]) not in (int, float)
            or not 0.1 <= readiness["timeout_seconds"] <= 60
        ):
            raise EnvironmentError("readiness timeout must be between 0.1 and 60 seconds")
        _service_port(service)
    for service in services:
        if any(dep not in ids or dep == service["id"] for dep in service["depends_on"]):
            raise EnvironmentError("service dependency names an unknown service")
    _topological(services)
    return value


def _validate_command(value: Any) -> None:
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 64
        or any(
            not isinstance(part, str) or not part or len(part) > 2048 or "\x00" in part
            for part in value
        )
        or not pathlib.Path(value[0]).is_absolute()
    ):
        raise EnvironmentError("commands must be bounded argv arrays with an absolute executable")


def _validate_data_dir(value: Any) -> None:
    if not isinstance(value, str) or not value or len(value) > 512 or "\x00" in value:
        raise EnvironmentError("data_dir must be a bounded relative path")
    raw = value.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise EnvironmentError("data_dir must be relative to the project")
    if any(part in ("", ".", "..") for part in raw.split("/")):
        raise EnvironmentError("data_dir must not contain empty, current, or parent components")
    if fs.unsafe_parts(pathlib.PurePosixPath(raw).parts):
        raise EnvironmentError("data_dir contains a reserved Windows name")
    if len(pathlib.PurePosixPath(raw).parts) == 0:
        raise EnvironmentError("data_dir must name a directory below the project")


def _topological(services: list[dict[str, Any]]) -> list[dict[str, Any]]:
    remaining = {service["id"]: service for service in services}
    ordered: list[dict[str, Any]] = []
    while remaining:
        ready = [
            key
            for key, item in remaining.items()
            if set(item["depends_on"]) <= {x["id"] for x in ordered}
        ]
        if not ready:
            raise EnvironmentError("service dependencies contain a cycle")
        for key in ready:
            ordered.append(remaining.pop(key))
    return ordered


def _ps_fields(pid: int) -> list[str] | None:
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat=,lstart=,pgid="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    fields = result.stdout.split()
    return fields if len(fields) == 7 else None


def _darwin_executable(pid: int) -> str | None:
    library = ctypes.util.find_library("proc")
    if not library:
        return None
    proc = ctypes.CDLL(library, use_errno=True)
    proc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    proc.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    length = proc.proc_pidpath(pid, buffer, len(buffer))
    return os.fsdecode(buffer.value) if length > 0 else None


def _process_identity(pid: int) -> dict[str, Any] | None:
    """Return a PID-reuse-resistant identity, or None when the process is gone."""
    if type(pid) is not int or pid <= 1:
        return None
    try:
        if os.name == "nt":
            return winprocess.identity(pid)
        os.kill(pid, 0)
        if pathlib.Path("/proc").is_dir():
            stat_text = pathlib.Path(f"/proc/{pid}/stat").read_text()
            end = stat_text.rfind(")")
            fields = stat_text[end + 2 :].split()
            if not fields or fields[0] in ("Z", "X"):
                return None
            executable = os.readlink(f"/proc/{pid}/exe")
            start = fields[19]  # Linux proc starttime, stable across PID reuse.
        else:
            fields = _ps_fields(pid)
            if fields is None or fields[0].startswith("Z"):
                return None
            executable = _darwin_executable(pid)
            start = " ".join(fields[1:6])  # ps lstart; PID plus path alone is not enough.
        if not executable:
            return None
        return {
            "start": start,
            "executable": executable,
            "process_group": os.getpgid(pid),
            "session": os.getsid(pid),
        }
    except (OSError, ValueError, IndexError, PermissionError):
        return None


def _same_process(current: dict[str, Any], identity: Any) -> bool:
    """Compare stable traits; the executable path may change on a legit re-exec."""
    return (
        isinstance(identity, dict)
        and identity.get("start") is not None
        and current.get("start") == identity.get("start")
        and current.get("process_group") == identity.get("process_group")
    )


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        return winprocess.alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    fields = _ps_fields(pid)
    return True if fields is None else not fields[0].startswith("Z")


def _process_state(pid: int, identity: dict[str, Any], child: subprocess.Popen | None) -> str:
    current = None if child is not None and child.poll() is not None else _process_identity(pid)
    if current is not None:
        return "alive" if _same_process(current, identity) else "reused"
    return "unknown" if _process_alive(pid) else "gone"


def _group_members(process_group: int) -> list[int] | None:
    try:
        if os.name == "nt":
            return winprocess.members(process_group)
        result = subprocess.run(
            ["ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    members = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit():
            return None
        if int(parts[1]) == process_group and not parts[2].startswith("Z"):
            members.append(int(parts[0]))
    return members


def _recorded_group(service: dict[str, Any]) -> int | None:
    identity = service.get("identity")
    group = (
        identity.get("process_group")
        if isinstance(identity, dict)
        else service.get("process_group")
    )
    return group if type(group) is int else None


def _owns_group(pid, identity, group):
    if os.name == "nt":
        return winprocess.owns_group(pid, identity, group)
    return group == pid


def _child_group(child):
    return child.devcrew_group if os.name == "nt" else child.pid


def _group_leftovers(process_group: int | None) -> tuple[bool | None, str]:
    if process_group is None:
        return True, ""
    members = _group_members(process_group)
    if members is None:
        return None, f"process group {process_group} could not be inspected"
    if members:
        return (
            False,
            f"process group {process_group} still has processes: {', '.join(map(str, members))}",
        )
    return True, ""


def _send(signal_value: int, pid: int, group: int | None) -> None:
    try:
        if group is None:
            os.kill(pid, signal_value)
        else:
            os.killpg(group, signal_value)
    except (ProcessLookupError, PermissionError):
        pass


def _terminate(
    pid: int, identity: dict[str, Any], group: int | None, child: subprocess.Popen | None
) -> bool:
    if os.name == "nt":
        if not winprocess.owns_group(pid, identity, group):
            return False
        try:
            winprocess.terminate(group)
            return True
        except OSError:
            return False
    for signal_value, grace in ((signal.SIGTERM, 3.0), (signal.SIGKILL, 2.0)):
        _send(signal_value, pid, group)
        deadline = time.monotonic() + grace
        while True:
            state = _process_state(pid, identity, child)
            clear, _ = _group_leftovers(group)
            if state != "alive" and clear:
                return True
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    return _process_state(pid, identity, child) != "alive" and _group_leftovers(group)[0] is True


def _cleanup_service(
    service: dict[str, Any], children: dict[int, subprocess.Popen]
) -> tuple[bool, str]:
    status = service.get("status")
    if status in ("STOPPED", "NOT_STARTED"):
        return True, ""
    pid = service.get("pid")
    identity = service.get("identity")
    if type(pid) is not int or not isinstance(identity, dict):
        candidate = f" (unconfirmed pid {pid})" if type(pid) is int else ""
        return False, (
            "the start was interrupted before ownership was confirmed"
            + candidate
            + "; the process was not signalled and its resources remain claimed"
        )
    recorded_group = _recorded_group(service)
    if recorded_group is None:
        return False, f"pid {pid} has no recorded process group; descendants cannot be ruled out"
    group = recorded_group if _owns_group(pid, identity, recorded_group) else None
    child = children.get(pid)
    state = _process_state(pid, identity, child)
    if state == "unknown":
        return (
            False,
            f"process {pid} is visible but its identity could not be verified; left untouched",
        )
    if state == "reused":
        _, detail = _group_leftovers(recorded_group)
        return False, f"pid {pid} no longer belongs to this run; left untouched" + (
            f"; {detail}" if detail else ""
        )
    if state == "gone":
        # A gone leader cannot be signalled again (its PGID may even be reused),
        # but a surviving group can only contain descendants of the verified leader.
        clear, detail = _group_leftovers(recorded_group)
        if clear is None:
            return False, detail
        if clear:
            return True, f"process {pid} had already exited"
        if group is None:
            return False, (
                f"process {pid} is gone but its recorded process group {recorded_group} "
                "cannot be tied to the confirmed leader"
            )
        if not _terminate(pid, identity, group, child):
            return (
                False,
                f"the owned process group of pid {pid} still has processes after termination",
            )
        leftovers, detail = _group_leftovers(recorded_group)
        if not leftovers:
            return False, detail
        return True, "stopped the remaining owned process group after the leader exited"
    if not _terminate(pid, identity, group, child):
        return (
            False,
            f"process {pid} or its process group is still running after termination signals",
        )
    if child is not None:
        try:
            child.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    leftovers, detail = _group_leftovers(recorded_group)
    if not leftovers:
        return False, detail
    return True, (
        "stopped the owned process group" if group is not None else "stopped the owned process"
    )


def _tcp_listener_pids(port: int) -> set[int] | None:
    """Return PIDs listening on ``port``, or None when it cannot be inspected."""
    if os.name == "nt":
        try:
            return winprocess.listener_pids(port)
        except OSError:
            return None
    proc_tcp = pathlib.Path("/proc/net/tcp")
    if proc_tcp.is_file():
        inodes: set[str] = set()
        try:
            for name in ("/proc/net/tcp", "/proc/net/tcp6"):
                lines = pathlib.Path(name).read_text().splitlines()[1:]
                for line in lines:
                    parts = line.split()
                    if len(parts) < 10 or parts[3] != "0A":
                        continue
                    if not parts[1].upper().endswith(f":{port:04X}"):
                        continue
                    inodes.add(parts[9])
        except OSError:
            return None
        if not inodes:
            return set()
        pids: set[int] = set()
        try:
            entries = list(pathlib.Path("/proc").iterdir())
        except OSError:
            return None
        for entry in entries:
            if not entry.name.isdigit():
                continue
            try:
                descriptors = list((entry / "fd").iterdir())
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    link = os.readlink(descriptor)
                except OSError:
                    continue
                if link.startswith("socket:[") and link[8:-1] in inodes:
                    pids.add(int(entry.name))
                    break
        return pids
    try:
        result = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode not in (0, 1):
        return None
    return {int(line) for line in result.stdout.split() if line.isdigit()}


def _listener_check(port: int, process_group: Any) -> tuple[str, str]:
    """Classify the listeners on ``port`` as owned, absent, foreign, or unknown.

    Cleanup signals exactly one recorded process group, so readiness must use
    that same boundary. A listener in another group of the same session would
    survive stop and cannot count as owned.
    """
    if type(process_group) is not int:
        return "unknown", "the verified process identity has no process group"
    pids = _tcp_listener_pids(port)
    if pids is None:
        return (
            "unknown",
            f"listener ownership on port {port} could not be inspected on this platform",
        )
    if not pids:
        return "none", f"no listener on port {port} could be attributed to a process"
    members = _group_members(process_group) if os.name == "nt" else None
    if os.name == "nt" and members is None:
        return "unknown", "could not inspect the service job"
    for pid in sorted(pids):
        try:
            owned = pid in members if members is not None else os.getpgid(pid) == process_group
            if not owned:
                return (
                    "foreign",
                    f"listener pid {pid} on port {port} is outside the verified process group",
                )
        except OSError:
            return (
                "foreign",
                f"listener pid {pid} on port {port} disappeared before it could be verified",
            )
    return "owned", ""


def _listener_ownership(port: int, process_group: Any) -> tuple[bool | None, str]:
    """Confirm that every listener on ``port`` belongs to the verified process group."""
    state, detail = _listener_check(port, process_group)
    if state == "owned":
        return True, ""
    if state == "unknown":
        return None, detail
    return False, detail


def _service_port(service: dict[str, Any]) -> tuple[str, int]:
    parsed = urllib.parse.urlsplit(service["readiness"]["url"])
    host, port = parsed.hostname, parsed.port
    if host != "127.0.0.1" or port is None:
        raise EnvironmentError("readiness URL must identify a literal IPv4 loopback port")
    return host, port


def _readiness_status(service: dict[str, Any]) -> int | None:
    """Request the recorded loopback address directly, without proxies or redirects."""
    host, port = _service_port(service)
    path = urllib.parse.urlsplit(service["readiness"]["url"]).path or "/"
    timeout = max(
        0.1, min(MAX_READINESS_REQUEST_SECONDS, float(service["readiness"]["timeout_seconds"]))
    )
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request("GET", path, headers={"Connection": "close"})
        return connection.getresponse().status
    except (OSError, http.client.HTTPException, ValueError):
        return None
    finally:
        connection.close()


def _confirm_readiness(
    service: dict[str, Any], identity: dict[str, Any], child: subprocess.Popen
) -> bool:
    """Check that the verified process answers before and after the request.

    The listener is attributed before the HTTP request as well as after it, so a
    foreign server that answers and then releases the port cannot have its
    response accepted once the owned service binds the address.
    """
    host, port = _service_port(service)
    group = identity.get("process_group")
    before, detail = _listener_check(port, group)
    if before == "unknown":
        raise EnvironmentError(
            f"service {service['id']} listener ownership could not be verified: {detail}"
        )
    if before == "foreign":
        raise EnvironmentError(
            f"service {service['id']} readiness on {host}:{port} is answered outside its verified "
            f"process group: {detail}"
        )
    if before == "none":
        return False
    actual = _readiness_status(service)
    if actual is None or actual != service["readiness"]["status"]:
        return False
    after, detail = _listener_check(port, group)
    if after == "unknown":
        raise EnvironmentError(
            f"service {service['id']} listener ownership could not be verified: {detail}"
        )
    if after == "none":
        # The listener disappeared between the response and the attribution; the
        # response is not evidence for the next attempt.
        return False
    if after != "owned":
        raise EnvironmentError(
            f"service {service['id']} readiness on {host}:{port} is answered outside its verified "
            f"process group: {detail}"
        )
    final = _process_identity(child.pid)
    if final is None or not _same_process(final, identity):
        raise EnvironmentError(f"service {service['id']} identity changed before readiness")
    return True


def _verify_ready_services(
    services: list[dict[str, Any]],
    states: list[dict[str, Any]],
    children: dict[int, subprocess.Popen],
    *,
    dependencies: list[str] | None = None,
    reprobe: bool = False,
) -> None:
    """Refuse to continue while a previously ready service is gone or not ready.

    Saved READY status is not evidence by itself: a dependency can exit while a
    later service is still starting. Every transition and the final publication
    re-check the live identity, and the final check repeats the readiness probe.
    """
    specs = {service["id"]: service for service in services}
    selected = [
        current
        for current in states
        if current.get("status") == "READY"
        and (dependencies is None or current.get("id") in dependencies)
    ]

    def check(current: dict[str, Any], probe: bool) -> None:
        service = specs.get(current.get("id"))
        pid, identity = current.get("pid"), current.get("identity")
        if service is None or type(pid) is not int or not isinstance(identity, dict):
            raise EnvironmentError(f"service {current.get('id')} readiness record is incomplete")
        child = children.get(pid)
        if child is not None and child.poll() is not None:
            raise EnvironmentError(f"service {service['id']} exited after readiness")
        if _process_state(pid, identity, child) != "alive":
            raise EnvironmentError(f"service {service['id']} is no longer the verified process")
        if probe:
            if child is None or not _confirm_readiness(service, identity, child):
                raise EnvironmentError(f"service {service['id']} is no longer ready")

    for current in selected:
        check(current, reprobe)
    if reprobe:
        # A service checked early can exit while a later service is probed. The
        # reverse pass runs after every probe, so the last probe cannot hide a
        # dependency that died during it.
        for current in reversed(selected):
            check(current, False)


def _assert_port_free(service: dict[str, Any]) -> None:
    host, port = _service_port(service)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            raise EnvironmentError(f"readiness port {port} is occupied") from None


def _write(path: pathlib.Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(
            temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _paths(paths: dict[str, str], run_id: str) -> tuple[str, str, str]:
    root = f"{paths['checks']}/qa-environments"
    return f"{root}/runs/{run_id}/state.json", f"{root}/resources.json", f"{root}/locks"


def _lock_root(
    project: pathlib.Path, paths: dict[str, str], read_destination: Callable, run_id: str
) -> pathlib.Path:
    """Validate the lock directory without treating it as a state file."""
    _, _, lock_rel = _paths(paths, run_id)
    return read_destination(project, f"{lock_rel}/owner").parent


def _acquire_state_lock(
    project: pathlib.Path,
    paths: dict[str, str],
    read_destination: Callable,
    run_id: str,
    *,
    wait: float,
) -> devcrew_resources.ResourceLock:
    """Take the per-run transition lock shared by start and stop.

    The project-wide resource index keeps its own short lock. This one covers a
    single environment's state writes, so a stop cannot publish STOPPED while a
    start may still launch a service, while unrelated environments stay free.
    """
    lock_root = _lock_root(project, paths, read_destination, run_id)
    lock_name = f"state-{run_id}"
    deadline = time.monotonic() + max(0.0, float(wait))
    while True:
        lock = devcrew_resources.ResourceLock(lock_root, lock_name, project)
        try:
            return lock.__enter__()
        except ValueError as exc:
            # ResourceLock remains a general, immediate lock. Only this
            # per-environment transition lock gets bounded queueing.
            if not isinstance(exc.__cause__, BlockingIOError):
                raise
            if time.monotonic() >= deadline:
                raise EnvironmentError(
                    "another operation still owns this environment's state; no state was changed"
                ) from exc
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


def _validate_saved_service(service: Any) -> None:
    if not isinstance(service, dict):
        raise EnvironmentError("environment state services are invalid")
    identifier, status = service.get("id"), service.get("status")
    pid, identity = service.get("pid"), service.get("identity")
    url = service.get("readiness_url")
    if (
        not isinstance(identifier, str)
        or not identifier
        or status not in SERVICE_STATUSES
        or (pid is not None and not (type(pid) is int and pid > 1))
        or (identity is not None and not isinstance(identity, dict))
        or (url is not None and not isinstance(url, str))
    ):
        raise EnvironmentError("environment state service records are invalid")
    if identity is not None or status in ("READY", "CLEANUP_FAILED"):
        # START_INTENT may precede identity capture. Once an identity is saved,
        # require its complete shape before cleanup can act on any service.
        if (
            type(pid) is not int
            or pid <= 1
            or not isinstance(identity, dict)
            or not isinstance(identity.get("start"), str)
            or not identity.get("start")
            or not isinstance(identity.get("executable"), str)
            or not identity.get("executable")
            or type(identity.get("process_group")) is not int
            or type(identity.get("session")) is not int
        ):
            raise EnvironmentError("environment state service records are invalid")


def _validate_saved_commands(entries: Any, statuses: tuple[str, ...], label: str) -> None:
    if not isinstance(entries, list):
        raise EnvironmentError(f"environment state {label} records are invalid")
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or type(entry.get("index")) is not int
            or entry.get("status") not in statuses
            or (entry.get("pid") is not None and type(entry.get("pid")) is not int)
            or (entry.get("identity") is not None and not isinstance(entry.get("identity"), dict))
        ):
            raise EnvironmentError(f"environment state {label} records are invalid")


def _read_state(path: pathlib.Path, run_id: str, project: pathlib.Path) -> dict[str, Any]:
    """Validate the saved record before the caller signals or writes anything."""
    value = _read_json(path, "environment state")
    if not isinstance(value, dict):
        raise EnvironmentError("environment state is invalid")
    if type(value.get("schema_version")) is not int or value.get("schema_version") != VERSION:
        raise EnvironmentError("unsupported environment state schema; no state was changed")
    if value.get("run_id") != run_id:
        raise EnvironmentError("environment state belongs to another run; no state was changed")
    root = value.get("project_root")
    if not isinstance(root, str) or not root:
        raise EnvironmentError("environment state does not record its project; refusing to act")
    try:
        belongs = pathlib.Path(root).resolve() == project.resolve()
    except (OSError, RuntimeError):
        belongs = False
    if not belongs:
        raise EnvironmentError("environment state belongs to another project; refusing to act")
    if value.get("status") not in STATE_STATUSES:
        raise EnvironmentError("environment state has an unknown status; no state was changed")
    if not isinstance(value.get("resources"), list) or any(
        not isinstance(item, str) for item in value["resources"]
    ):
        raise EnvironmentError("environment state resource claims are invalid")
    services = value.get("services")
    if not isinstance(services, list):
        raise EnvironmentError("environment state services are invalid")
    for service in services:
        _validate_saved_service(service)
    _validate_saved_commands(value.get("preparation", []), PREPARATION_STATUSES, "preparation")
    _validate_saved_commands(value.get("cleanup", []), CLEANUP_STATUSES, "cleanup")
    port_claims = value.get("port_claims", [])
    if not isinstance(port_claims, list) or any(not isinstance(item, str) for item in port_claims):
        raise EnvironmentError("environment state port claims are invalid")
    environment = value.get("environment", [])
    if not isinstance(environment, list) or any(
        not isinstance(item, str) or not ENV_RE.fullmatch(item) for item in environment
    ):
        raise EnvironmentError("environment state environment names are invalid")
    data = value.get("data")
    if data is not None and (
        not isinstance(data, dict)
        or data.get("status") not in DATA_STATUSES
        or (data.get("owned") is not None and type(data.get("owned")) is not bool)
        or (data.get("removal_path") is not None and not isinstance(data.get("removal_path"), str))
    ):
        raise EnvironmentError("environment state data record is invalid")
    value.setdefault("preparation", [])
    value.setdefault("cleanup", [])
    value.setdefault("port_claims", [])
    value.setdefault("environment", [])
    if data is None:
        value["data"] = {"dir": None, "path": None, "status": "NOT_CREATED"}
    return value


def _resource_index(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": VERSION, "resources": {}}
    value = _read_json(path, "environment resource ownership state")
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != VERSION
        or not isinstance(value.get("resources"), dict)
    ):
        raise EnvironmentError("environment resource ownership state is invalid")
    for name, claim in value["resources"].items():
        if (
            not isinstance(name, str)
            or not isinstance(claim, dict)
            or not isinstance(claim.get("run_id"), str)
            or not RUN_ID_RE.fullmatch(claim["run_id"])
            or not isinstance(claim.get("status"), str)
            or claim.get("kind", "resource") not in ("resource", "port")
        ):
            raise EnvironmentError("environment resource ownership state is invalid")
    return value


def _claim_name(name: str, kind: str) -> str:
    return f"port:{name}" if kind == "port" else name


def _ordered_claim_names(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """Resources plus loopback ports, so a second DevCrew run cannot take them."""
    claims = [(name, "resource") for name in profile["resources"]]
    seen = {name for name, _ in claims}
    for service in profile["services"]:
        host, port = _service_port(service)
        key = f"{host}:{port}"
        if key not in seen:
            seen.add(key)
            claims.append((key, "port"))
    return claims


def _held_resources(index_path: pathlib.Path, run_id: str) -> list[str]:
    index = _resource_index(index_path)
    return sorted(
        name
        for name, claim in index["resources"].items()
        if isinstance(claim, dict) and claim.get("run_id") == run_id
    )


def _reserve_claims(
    index_path: pathlib.Path,
    lock_root: pathlib.Path,
    project: pathlib.Path,
    run_id: str,
    claims: list[tuple[str, str]],
    state: dict[str, Any],
    state_path: pathlib.Path,
) -> None:
    """Persist the run intent, then its claims, under the shared index lock.

    The state file is the recovery anchor: if the orchestrator dies before the
    index write, a later stop sees a run without claims instead of claims that
    belong to no inspectable run.
    """
    with devcrew_resources.ResourceLock(lock_root, "qa-environment-state", project):
        index = _resource_index(index_path)
        conflicts = sorted(
            _claim_name(name, kind)
            for name, kind in claims
            if _claim_name(name, kind) in index["resources"]
        )
        if conflicts:
            raise EnvironmentError("resource conflict: " + ", ".join(conflicts))
        _write(state_path, state)
        for name, kind in claims:
            index["resources"][_claim_name(name, kind)] = {
                "run_id": run_id,
                "status": "STARTING",
                "kind": kind,
            }
        _write(index_path, index)


def _mark_claims_ready(
    index_path: pathlib.Path, lock_root: pathlib.Path, project: pathlib.Path, run_id: str
) -> None:
    with devcrew_resources.ResourceLock(lock_root, "qa-environment-state", project):
        index = _resource_index(index_path)
        for claim in index["resources"].values():
            if claim.get("run_id") == run_id:
                claim["status"] = "READY"
        _write(index_path, index)


def _release_claims(
    index_path: pathlib.Path, lock_root: pathlib.Path, project: pathlib.Path, run_id: str
) -> None:
    """Remove only claims owned by this run; a failed write leaves them untouched."""
    with devcrew_resources.ResourceLock(lock_root, "qa-environment-state", project):
        index = _resource_index(index_path)
        remaining = {
            name: claim
            for name, claim in index["resources"].items()
            if claim.get("run_id") != run_id
        }
        if len(remaining) != len(index["resources"]):
            index["resources"] = remaining
            _write(index_path, index)


def _spawn(argv: list[str], project: pathlib.Path, environment: dict[str, str]) -> subprocess.Popen:
    options = dict(
        cwd=project,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    if os.name == "nt":
        return winprocess.spawn(argv, persistent=True, **options)
    return subprocess.Popen(argv, start_new_session=True, **options)


def _stop_fresh_child(child: subprocess.Popen) -> None:
    """Stop a process this call just created and still holds a Popen handle for."""
    if os.name == "nt":
        winprocess.terminate(_child_group(child))
        child.wait(timeout=5)
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=3)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _child_environment(names: list[str]) -> dict[str, str]:
    environment = {name: os.environ[name] for name in SYSTEM_ENV_NAMES if name in os.environ}
    for name in names:
        if name in os.environ:
            environment[name] = os.environ[name]
    return environment


def _await_identity(child: subprocess.Popen) -> dict[str, Any] | None:
    identity = None
    previous: Any = object()
    for _ in range(40):
        if child.poll() is not None:
            break
        current = _process_identity(child.pid)
        if current is not None and current == previous:
            return current
        previous = current
        time.sleep(0.05)
    return identity


def _command_group_clear(
    entry: dict[str, Any], children: dict[int, subprocess.Popen]
) -> tuple[bool, str]:
    """Confirm an owned command left no process behind in its process group.

    A leader that exits does not end its descendants. The group created for the
    command is this run's side effect, so it is terminated when it can still be
    tied to the confirmed leader; otherwise the command stays unresolved.
    """
    pid, identity = entry.get("pid"), entry.get("identity")
    group = _recorded_group(entry)
    if type(pid) is not int or group is None:
        return True, ""
    child = children.get(pid)
    clear, detail = _group_leftovers(group)
    if clear is None:
        return False, detail
    if clear:
        return True, ""
    if not isinstance(identity, dict):
        return False, (
            f"{detail}; the command ownership was not confirmed, so its group was not signalled"
        )
    if not _owns_group(pid, identity, group):
        return False, f"{detail}; the recorded group cannot be tied to the confirmed leader"
    state = _process_state(pid, identity, child)
    if state == "reused":
        return False, f"{detail}; the command leader no longer belongs to this run"
    if state == "unknown":
        # The leader is visible but its current identity cannot be read. A
        # signal could reach an unrelated process group, so it stays untouched.
        return False, (
            f"{detail}; the command leader identity could not be verified, "
            "so its group was not signalled"
        )
    if not _terminate(pid, identity, group, child):
        clear, detail = _group_leftovers(group)
        return False, (detail or f"process group {group} still has processes")
    return True, ""


def _entry(index: int, argv: list[str]) -> dict[str, Any]:
    return {
        "index": index,
        "command_sha256": hashlib.sha256(_canonical(argv)).hexdigest(),
        "status": "NOT_STARTED",
        "pid": None,
        "identity": None,
        "exit_code": None,
    }


def _owned_command_step(
    entry: dict[str, Any],
    argv: list[str],
    project: pathlib.Path,
    environment: dict[str, str],
    state: dict[str, Any],
    state_path: pathlib.Path,
    children: dict[int, subprocess.Popen],
    *,
    intent: str,
    failure: str,
) -> int:
    """Run one owned argv step and persist its intent, identity, and result.

    The intent is written before the side effect. A crash between the effect and
    the result write therefore leaves an unknown outcome that the caller keeps.
    """
    entry["status"] = intent
    _write(state_path, state)
    try:
        child = _spawn(argv, project, environment)
    except OSError:
        entry["status"] = "FAILED"
        _write(state_path, state)
        raise
    children[child.pid] = child
    # The session created for the command is its process group even when the
    # leader exits before a stable identity can be read.
    entry.update({"pid": child.pid, "process_group": _child_group(child)})
    _write(state_path, state)
    identity = _await_identity(child)
    if identity is None and child.poll() is None:
        entry["status"] = "EFFECT_UNKNOWN"
        entry["cleanup_error"] = f"{failure}; the process identity was not confirmed"
        _write(state_path, state)
        _stop_fresh_child(child)
        raise EnvironmentError(f"{failure}; the process identity was not confirmed")
    entry["identity"] = identity
    _write(state_path, state)
    try:
        code = child.wait(timeout=PREPARE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _terminate(child.pid, identity, _child_group(child), child)
        entry["status"] = "EFFECT_UNKNOWN"
        entry["cleanup_error"] = f"{failure}; the command timed out and its effect is unknown"
        _write(state_path, state)
        raise EnvironmentError(f"{failure}; the command timed out")
    entry["exit_code"] = code
    # The leader may exit while a descendant keeps running in the same owned
    # process group. That descendant is still this run's side effect, so the
    # command is not complete until the group is empty.
    clear, detail = _command_group_clear(entry, children)
    if not clear:
        entry["status"] = "EFFECT_UNKNOWN"
        entry["cleanup_error"] = f"{failure}; {detail}"
        _write(state_path, state)
        raise EnvironmentError(f"{failure}; {detail}")
    entry["status"] = "COMPLETE" if code == 0 else "FAILED"
    _write(state_path, state)
    return code


def _new_state(
    run_id: str,
    profile: dict[str, Any],
    project: pathlib.Path,
    profile_path: pathlib.Path,
    services: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": VERSION,
        "run_id": run_id,
        "profile_id": profile["id"],
        "profile_sha256": _digest(profile),
        "profile_path": str(profile_path.resolve()),
        "project_root": str(project),
        "created_at": time.time(),
        "status": "STARTING",
        "environment": list(profile["environment"]),
        "resources": list(profile["resources"]),
        "port_claims": [],
        "data": {"dir": profile.get("data_dir"), "path": None, "status": "NOT_CREATED"},
        "preparation": [_entry(number, argv) for number, argv in enumerate(profile["prepare"])],
        "cleanup": [_entry(number, argv) for number, argv in enumerate(profile.get("cleanup", []))],
        "services": [
            {
                "id": service["id"],
                "status": "NOT_STARTED",
                "pid": None,
                "identity": None,
                "readiness_url": service["readiness"]["url"],
            }
            for service in services
        ],
    }


def _data_root(project: pathlib.Path, paths: dict[str, str], relative: str) -> pathlib.Path:
    """Resolve the declared run data directory without leaving the project."""
    root = project.resolve()
    checks_root = (root / pathlib.PurePosixPath(paths["checks"])).resolve()
    parts = pathlib.PurePosixPath(relative).parts
    current = project
    for part in parts:
        current = current / part
        if fs.is_link(current):
            raise EnvironmentError("data_dir must not traverse symlinks")
    resolved = current.resolve()
    if root not in resolved.parents:
        raise EnvironmentError("data_dir must stay inside the project")
    if resolved == checks_root or checks_root in resolved.parents:
        raise EnvironmentError("data_dir must not overlap the configured checks root")
    return current


def _marker(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        value = _read_json(path / OWNER_MARKER, "data ownership marker")
    except (EnvironmentError, OSError):
        return None
    return value if isinstance(value, dict) else None


def _marker_owned(path: pathlib.Path, run_id: str, project_root: str) -> bool:
    marker = _marker(path)
    return (
        isinstance(marker, dict)
        and marker.get("schema_version") == VERSION
        and marker.get("run_id") == run_id
        and marker.get("project_root") == project_root
    )


def _create_data_dir(
    state: dict[str, Any],
    state_path: pathlib.Path,
    project: pathlib.Path,
    paths: dict[str, str],
    run_id: str,
) -> None:
    relative = state["data"].get("dir")
    if not relative:
        return
    path = _data_root(project, paths, relative)
    # Record the creation intent first. Ownership becomes confirmed only after
    # the marker is written, so a crash in between cannot let a later stop run
    # the profile's cleanup commands against a directory it does not own.
    state["data"].update({"path": str(path), "status": "CREATED", "owned": False})
    _write(state_path, state)
    if path.exists():
        if fs.is_link(path) or not path.is_dir():
            state["data"].update(
                {
                    "status": "FAILED",
                    "owned": False,
                    "cleanup_error": "the declared data directory is not a regular directory",
                }
            )
            _write(state_path, state)
            raise EnvironmentError("the declared data directory is not a regular directory")
        if not _marker_owned(path, run_id, str(project)):
            state["data"].update(
                {
                    "status": "FAILED",
                    "owned": False,
                    "cleanup_error": "the declared data directory already exists and is not owned",
                }
            )
            _write(state_path, state)
            raise EnvironmentError(
                "the declared data directory already exists and is not owned by this run"
            )
    else:
        try:
            path.mkdir(parents=True, mode=0o700)
        except OSError:
            state["data"].update(
                {
                    "status": "FAILED",
                    "owned": False,
                    "cleanup_error": "the declared data directory could not be created",
                }
            )
            _write(state_path, state)
            raise
    _write(
        path / OWNER_MARKER,
        {"schema_version": VERSION, "run_id": run_id, "project_root": str(project)},
    )
    state["data"]["owned"] = True
    _write(state_path, state)


def _remove_data(
    state: dict[str, Any], state_path: pathlib.Path, project: pathlib.Path, paths: dict[str, str]
) -> tuple[bool, str]:
    data = state.get("data") or {}
    status = data.get("status", "NOT_CREATED")
    if status in ("NOT_CREATED", "REMOVED"):
        return True, ""
    if status == "FAILED" and data.get("owned") is False:
        # The start refused a directory it never created; nothing is removed and
        # the foreign data keeps its own owner.
        return True, ""
    if data.get("status") == "FAILED" and not data.get("path"):
        return False, data.get("cleanup_error") or "the declared data directory is not owned"
    if data.get("status") == "FAILED":
        return False, data.get(
            "cleanup_error"
        ) or "the declared data directory could not be verified"
    path_value, run_id = data.get("path"), state.get("run_id")
    if not isinstance(path_value, str) or not path_value:
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "the data directory path was not recorded",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    # Re-validate the recorded path against the current project layout: the
    # state file is not proof that the directory is still inside the project or
    # that its parents did not become symlinks after start.
    relative = data.get("dir")
    if not isinstance(relative, str) or not relative:
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "the data directory name was not recorded; nothing was removed",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    try:
        current = _data_root(project, paths, relative)
    except EnvironmentError as exc:
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": f"the recorded data directory can no longer be confirmed inside "
                f"the project ({exc}); nothing was removed",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    if str(current) != path_value:
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "the recorded data path no longer matches the declared directory; "
                "nothing was removed",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    path = current
    tomb = path.with_name(f"{path.name}.deleting-{str(run_id)[:8]}")
    project_root = state.get("project_root")
    recorded_tomb = data.get("removal_path")
    if recorded_tomb is not None:
        if not isinstance(recorded_tomb, str) or pathlib.Path(recorded_tomb) != tomb:
            data.update(
                {
                    "status": "EFFECT_UNKNOWN",
                    "removal_path": None,
                    "cleanup_error": "the recorded pending removal path does not match this run; "
                    "nothing was removed",
                }
            )
            _write(state_path, state)
            return False, data["cleanup_error"]
    if status in ("REMOVAL_INTENT", "EFFECT_UNKNOWN"):
        # The stage and path of a started removal are kept until the renamed
        # directory is gone. A missing original path alone is not proof that the
        # data was deleted while the tomb still holds it.
        if tomb.exists():
            if not _marker_owned(tomb, run_id, project_root):
                data.update(
                    {
                        "status": "EFFECT_UNKNOWN",
                        "cleanup_error": "the pending data removal cannot be confirmed as owned",
                    }
                )
                _write(state_path, state)
                return False, data["cleanup_error"]
            try:
                shutil.rmtree(tomb)
            except OSError as exc:
                data.update(
                    {
                        "status": "REMOVAL_INTENT",
                        "removal_path": str(tomb),
                        "cleanup_error": f"the pending data removal failed: {exc}",
                    }
                )
                _write(state_path, state)
                return False, data["cleanup_error"]
        if not path.exists():
            data.update({"status": "REMOVED", "cleanup_error": None, "removal_path": None})
            _write(state_path, state)
            return True, ""
    if not path.exists():
        data.update({"status": "REMOVED", "cleanup_error": None, "removal_path": None})
        _write(state_path, state)
        return True, ""
    if fs.is_link(path) or not path.is_dir():
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "the recorded data path is not a regular directory; left untouched",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    if not _marker_owned(path, run_id, project_root):
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "the data directory is not confirmed as owned by this run; "
                "nothing was removed",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    data.update({"status": "REMOVAL_INTENT", "removal_path": str(tomb), "cleanup_error": None})
    _write(state_path, state)
    try:
        os.replace(path, tomb)
    except OSError as exc:
        data.update(
            {
                "status": "EFFECT_UNKNOWN",
                "removal_path": None,
                "cleanup_error": f"the owned data directory could not be moved for removal: {exc}",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    try:
        shutil.rmtree(tomb)
    except OSError as exc:
        # Keep the pending stage so the next stop retries this exact path rather
        # than treating the missing original directory as a finished removal.
        data.update(
            {
                "status": "REMOVAL_INTENT",
                "cleanup_error": f"the owned data directory could not be removed: {exc}",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    if tomb.exists():
        data.update(
            {
                "status": "REMOVAL_INTENT",
                "cleanup_error": "the owned data directory still exists after removal",
            }
        )
        _write(state_path, state)
        return False, data["cleanup_error"]
    data.update({"status": "REMOVED", "cleanup_error": None, "removal_path": None})
    _write(state_path, state)
    return True, ""


def _cleanup_preparation(
    entry: dict[str, Any], children: dict[int, subprocess.Popen]
) -> tuple[bool, str]:
    status = entry.get("status")
    if status == "NOT_STARTED":
        return True, ""
    if status in ("COMPLETE", "FAILED"):
        # A recorded result only stays settled while no descendant survives.
        ok, detail = _command_group_clear(entry, children)
        if not ok:
            return False, f"the recorded preparation result stands, but {detail}"
        return True, ""
    pid, identity = entry.get("pid"), entry.get("identity")
    if status != "START_INTENT":
        return False, entry.get(
            "cleanup_error"
        ) or "the preparation outcome is unknown; it was not repeated"
    if type(pid) is not int or not isinstance(identity, dict):
        entry.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "preparation was interrupted before its result was recorded and no "
                "verified process identity exists; its effect stays unknown",
            }
        )
        return False, entry["cleanup_error"]
    # The command itself is owned, so it may be stopped while it is still the
    # confirmed live leader (a gone leader's PGID could already be reused), but
    # its side effect was never recorded: keep the claim until an operator
    # resolves the outcome.
    child = children.get(pid)
    if _process_state(pid, identity, child) == "alive":
        _terminate(pid, identity, identity.get("process_group"), child)
    entry.update(
        {
            "status": "EFFECT_UNKNOWN",
            "cleanup_error": "preparation was interrupted before its result was recorded; "
            "its effect stays unknown and the command was not repeated",
        }
    )
    return False, entry["cleanup_error"]


def _cleanup_profile(state: dict[str, Any]) -> list[list[str]] | None:
    """Reload the declared cleanup commands only while the profile still matches."""
    entries = state.get("cleanup") or []
    if not entries:
        return []
    profile_path = state.get("profile_path")
    if not isinstance(profile_path, str) or not profile_path:
        return None
    try:
        profile = _validate_profile(_read_json(pathlib.Path(profile_path), "environment profile"))
    except (EnvironmentError, OSError):
        return None
    if _digest(profile) != state.get("profile_sha256"):
        return None
    commands = profile.get("cleanup", [])
    if len(commands) != len(entries):
        return None
    return commands


def _cleanup_declared(
    entry: dict[str, Any],
    commands: list[list[str]] | None,
    state: dict[str, Any],
    state_path: pathlib.Path,
    children: dict[int, subprocess.Popen],
    project: pathlib.Path,
    paths: dict[str, str],
) -> tuple[bool, str]:
    status = entry.get("status", "NOT_STARTED")
    if status == "COMPLETE":
        ok, detail = _command_group_clear(entry, children)
        if not ok:
            return False, f"the recorded cleanup result stands, but {detail}"
        return True, ""
    if status in ("FAILED", "EFFECT_UNKNOWN"):
        ok, detail = _command_group_clear(entry, children)
        message = (
            entry.get("cleanup_error") or "the declared data cleanup did not complete; not repeated"
        )
        return False, message if ok else f"{message}; {detail}"
    if status == "CLEANUP_INTENT":
        identity = entry.get("identity")
        pid = entry.get("pid")
        if type(pid) is int and isinstance(identity, dict):
            child = children.get(pid)
            if _process_state(pid, identity, child) == "alive":
                _terminate(pid, identity, identity.get("process_group"), child)
        entry.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "data cleanup was interrupted before its result was recorded; "
                "its effect stays unknown and it was not repeated",
            }
        )
        return False, entry["cleanup_error"]
    if commands is None or entry.get("index") is None or entry["index"] >= len(commands):
        entry.update(
            {
                "status": "EFFECT_UNKNOWN",
                "cleanup_error": "the profile that declared data cleanup is unavailable or changed; "
                "no cleanup command was run",
            }
        )
        return False, entry["cleanup_error"]
    allowed, detail = _data_cleanup_gate(state, project, paths)
    if not allowed:
        # Re-checked for every step, so a path replaced after an earlier stop
        # cannot let a later cleanup command run.
        entry["cleanup_error"] = detail
        return False, detail
    environment = _child_environment(state.get("environment", []))
    project = pathlib.Path(state["project_root"])
    code = _owned_command_step(
        entry,
        commands[entry["index"]],
        project,
        environment,
        state,
        state_path,
        children,
        intent="CLEANUP_INTENT",
        failure="declared data cleanup failed",
    )
    if code == 0:
        return True, ""
    entry["cleanup_error"] = f"declared data cleanup exited with status {code}"
    return False, entry["cleanup_error"]


def _declared_cleanup_relevant(state: dict[str, Any]) -> bool:
    """Declared cleanup runs while this run may own a side effect to remove.

    A start that fails before data creation, preparation, or launch owns nothing
    for the profile's cleanup commands to remove, so those commands must not run.
    A cleanup step that already started is always reconciled again: its saved
    failure or unknown outcome is not erased by later successful removal.
    """
    if any(
        entry.get("status") not in ("NOT_STARTED", "FAILED") or entry.get("pid") is not None
        for entry in state.get("preparation", [])
    ):
        # A FAILED step without a PID never started a process, so it produced no
        # side effect for the profile's cleanup commands to remove.
        return True
    if any(entry.get("status") != "NOT_STARTED" for entry in state.get("cleanup", [])):
        return True
    data = state.get("data") or {}
    if (
        data.get("status") in ("CREATED", "REMOVAL_INTENT", "EFFECT_UNKNOWN")
        and data.get("owned") is not False
    ):
        return True
    return any(service.get("pid") is not None for service in state.get("services", []))


def _data_cleanup_gate(
    state: dict[str, Any], project: pathlib.Path, paths: dict[str, str]
) -> tuple[bool, str]:
    """Confirm the recorded data directory before running profile cleanup.

    The profile's cleanup commands operate on the run data. If the path was
    replaced, escaped the project, or lost its ownership marker after start,
    those commands must not run even though the built-in removal also refuses.
    """
    data = state.get("data") or {}
    status = data.get("status")
    if status in ("NOT_CREATED", "REMOVED"):
        return True, ""
    if status == "FAILED" and data.get("owned") is False:
        return True, ""
    if data.get("owned") is False:
        return False, (
            "the data directory ownership is not confirmed; profile cleanup commands were not run"
        )
    relative, path_value = data.get("dir"), data.get("path")
    if (
        not isinstance(relative, str)
        or not relative
        or not isinstance(path_value, str)
        or not path_value
    ):
        return False, (
            "the data directory path is not fully recorded; profile cleanup commands were not run"
        )
    try:
        current = _data_root(project, paths, relative)
    except EnvironmentError as exc:
        return False, (
            f"the recorded data directory can no longer be confirmed inside the "
            f"project ({exc}); profile cleanup commands were not run"
        )
    if str(current) != path_value:
        return False, (
            "the recorded data path no longer matches the declared directory; "
            "profile cleanup commands were not run"
        )
    if not current.exists():
        return True, ""
    if fs.is_link(current) or not current.is_dir():
        return False, (
            "the recorded data path is not a regular directory; "
            "profile cleanup commands were not run"
        )
    if not _marker_owned(current, state.get("run_id"), state.get("project_root")):
        return False, (
            "the data directory is not confirmed as owned by this run; "
            "profile cleanup commands were not run"
        )
    return True, ""


def _cleanup_all(
    state: dict[str, Any],
    state_path: pathlib.Path,
    children: dict[int, subprocess.Popen] | None = None,
    cleanup_commands: list[list[str]] | None = None,
    *,
    run_declared: bool | None = None,
    project: pathlib.Path,
    paths: dict[str, str],
) -> bool:
    children = children or {}
    if run_declared is None:
        run_declared = _declared_cleanup_relevant(state)
    failed = False
    for service in reversed(state["services"]):
        ok, detail = _cleanup_service(service, children)
        if ok:
            if service.get("status") not in ("STOPPED", "NOT_STARTED"):
                service["status"] = "STOPPED"
            service.pop("cleanup_error", None)
        else:
            failed = True
            if service.get("status") == "START_INTENT":
                service["status"] = "EFFECT_UNKNOWN"
            elif service.get("status") not in ("EFFECT_UNKNOWN",):
                service["status"] = "CLEANUP_FAILED"
            service["cleanup_error"] = detail
        _write(state_path, state)
    for entry in reversed(state.get("preparation", [])):
        ok, detail = _cleanup_preparation(entry, children)
        if ok:
            entry.pop("cleanup_error", None)
        else:
            failed = True
            entry["cleanup_error"] = detail
        _write(state_path, state)
    if run_declared:
        if cleanup_commands is None:
            cleanup_commands = _cleanup_profile(state)
        for entry in state.get("cleanup", []):
            # The profile declares an ordered list; each step keeps its index and
            # runs in that order so dependent commands see the expected state.
            try:
                ok, detail = _cleanup_declared(
                    entry, cleanup_commands, state, state_path, children, project, paths
                )
            except (EnvironmentError, OSError) as exc:
                ok, detail = False, str(exc)
                entry["status"] = "EFFECT_UNKNOWN"
            if ok:
                entry.pop("cleanup_error", None)
            else:
                failed = True
                entry["cleanup_error"] = detail
                state.setdefault("cleanup_error", detail)
            _write(state_path, state)
    ok, detail = _remove_data(state, state_path, project, paths)
    if not ok:
        failed = True
        state["data"]["cleanup_error"] = detail
        _write(state_path, state)
    return failed


def _ready_by_id(
    services: list[dict[str, Any]], dependencies: list[str], states: list[dict[str, Any]]
) -> bool:
    ready = {state["id"] for state in states if state.get("status") == "READY"}
    return set(dependencies) <= ready


def _service_report(state: dict[str, Any]) -> list[dict[str, Any]]:
    report = []
    for item in state["services"]:
        pid = item.get("pid")
        identity = _process_identity(pid) if type(pid) is int else None
        entry = {
            "id": item.get("id"),
            "status": item.get("status"),
            "owned_process_alive": identity is not None
            and _same_process(identity, item.get("identity")),
        }
        if item.get("cleanup_error"):
            entry["cleanup_error"] = item["cleanup_error"]
        report.append(entry)
    return report


def _command_report(state: dict[str, Any], label: str) -> list[dict[str, Any]]:
    report = []
    for entry in state.get(label, []):
        item = {"index": entry.get("index"), "status": entry.get("status")}
        if entry.get("exit_code") is not None:
            item["exit_code"] = entry["exit_code"]
        if entry.get("cleanup_error"):
            item["cleanup_error"] = entry["cleanup_error"]
        report.append(item)
    return report


def _data_report(state: dict[str, Any]) -> dict[str, Any]:
    data = state.get("data") or {}
    report = {"dir": data.get("dir"), "status": data.get("status")}
    if data.get("owned") is not None:
        report["owned"] = data["owned"]
    if data.get("cleanup_error"):
        report["cleanup_error"] = data["cleanup_error"]
    return report


def command(
    args: Any,
    project: pathlib.Path,
    paths: dict[str, str],
    destination: Callable,
    read_destination: Callable,
) -> int:
    action = args.qa_environment_operation
    try:
        if action == "start":
            return _start(args, project, paths, destination, read_destination)
        if action in ("stop", "status"):
            return _stop_or_status(args, project, paths, read_destination, stop=action == "stop")
        raise EnvironmentError("unknown environment operation")
    except (EnvironmentError, OSError, ValueError, KeyError, TypeError) as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False),
            file=__import__("sys").stderr,
        )
        return 2


def _start(
    args: Any,
    project: pathlib.Path,
    paths: dict[str, str],
    destination: Callable,
    read_destination: Callable,
) -> int:
    profile_path = pathlib.Path(args.profile).expanduser()
    profile = _validate_profile(_read_json(profile_path, "environment profile"))
    run_id = uuid.uuid4().hex
    state_rel, index_rel, _ = _paths(paths, run_id)
    state_path = destination(project, state_rel)
    index_path = destination(project, index_rel)
    lock_root = _lock_root(project, paths, read_destination, run_id)
    services = _topological(profile["services"])
    # Hold the per-run lock for the whole launch: a concurrent stop must not
    # publish STOPPED while this start can still launch a service.
    state_lock = _acquire_state_lock(project, paths, read_destination, run_id, wait=0.0)
    try:
        return _launch(
            project,
            profile,
            profile_path,
            run_id,
            state_path,
            index_path,
            lock_root,
            services,
            paths,
        )
    finally:
        state_lock.__exit__(None, None, None)


def _launch(
    project: pathlib.Path,
    profile: dict[str, Any],
    profile_path: pathlib.Path,
    run_id: str,
    state_path: pathlib.Path,
    index_path: pathlib.Path,
    lock_root: pathlib.Path,
    services: list[dict[str, Any]],
    paths: dict[str, str],
) -> int:
    children: dict[int, subprocess.Popen] = {}
    state = _new_state(run_id, profile, project, profile_path, services)
    claims = _ordered_claim_names(profile)
    state["port_claims"] = [_claim_name(name, kind) for name, kind in claims if kind == "port"]
    try:
        # Persist intent and resource claims before any command or process side effect.
        _reserve_claims(index_path, lock_root, project, run_id, claims, state, state_path)
        environment = _child_environment(profile["environment"])
        _create_data_dir(state, state_path, project, paths, run_id)
        for number, command_argv in enumerate(profile["prepare"]):
            code = _owned_command_step(
                state["preparation"][number],
                command_argv,
                project,
                environment,
                state,
                state_path,
                children,
                intent="START_INTENT",
                failure="dependency/data preparation failed",
            )
            if code != 0:
                raise EnvironmentError("dependency/data preparation command failed")
        for service, current in zip(services, state["services"]):
            if not _ready_by_id(services, service["depends_on"], state["services"]):
                raise EnvironmentError("dependency readiness was lost")
            _verify_ready_services(
                services,
                state["services"],
                children,
                dependencies=service["depends_on"],
                reprobe=True,
            )
            _assert_port_free(service)
            current.update(
                {
                    "status": "START_INTENT",
                    "pid": None,
                    "identity": None,
                    "command_sha256": hashlib.sha256(_canonical(service["command"])).hexdigest(),
                }
            )
            _write(state_path, state)
            try:
                child = _spawn(service["command"], project, environment)
            except OSError:
                current["status"] = "NOT_STARTED"
                _write(state_path, state)
                raise
            children[child.pid] = child
            current["pid"] = child.pid
            _write(state_path, state)
            identity = _await_identity(child)
            if identity is None:
                if child.poll() is not None:
                    leftovers, detail = _group_leftovers(_child_group(child))
                    if leftovers:
                        current["status"] = "STOPPED"
                        current["exit_code"] = child.returncode
                        _write(state_path, state)
                        raise EnvironmentError(f"service {service['id']} exited before readiness")
                    # The leader exited before ownership was confirmed. Without a
                    # verified identity the group must not be signalled; keep the
                    # unknown outcome and the claim until it can be proven empty.
                    current.update(
                        {
                            "status": "EFFECT_UNKNOWN",
                            "process_group": _child_group(child),
                            "cleanup_error": (
                                detail
                                or f"service exited before its identity was confirmed; "
                                f"process group {child.pid} is not proven empty"
                            ),
                        }
                    )
                    _write(state_path, state)
                    raise EnvironmentError(
                        f"service {service['id']} exited before its ownership was confirmed"
                    )
                current["status"] = "EFFECT_UNKNOWN"
                current["cleanup_error"] = "the started process identity could not be verified"
                _write(state_path, state)
                raise EnvironmentError("started process identity could not be verified")
            current["identity"] = identity
            _write(state_path, state)
            deadline = time.monotonic() + float(service["readiness"]["timeout_seconds"])
            while time.monotonic() < deadline:
                if child.poll() is not None:
                    raise EnvironmentError(f"service {service['id']} exited before readiness")
                _verify_ready_services(
                    services, state["services"], children, dependencies=service["depends_on"]
                )
                if _confirm_readiness(service, identity, child):
                    current["status"] = "READY"
                    _write(state_path, state)
                    break
                time.sleep(0.1)
            else:
                raise EnvironmentError(f"service {service['id']} did not become ready")
        # Every service must still be the verified live process and answer its
        # readiness URL before the environment is published as ready.
        _verify_ready_services(services, state["services"], children, reprobe=True)
        _mark_claims_ready(index_path, lock_root, project, run_id)
        state["status"] = "READY"
        _write(state_path, state)
        print(json.dumps({"status": "READY", "run_id": run_id}, ensure_ascii=False))
        return 0
    except BaseException as exc:
        if not state_path.exists():
            raise
        cleanup_failed = _cleanup_all(
            state, state_path, children, profile.get("cleanup", []), project=project, paths=paths
        )
        release_error = None
        if not cleanup_failed:
            try:
                _release_claims(index_path, lock_root, project, run_id)
            except (EnvironmentError, OSError) as error:
                release_error = str(error)
        try:
            held = _held_resources(index_path, run_id)
        except (EnvironmentError, OSError) as error:
            held = []
            release_error = release_error or str(error)
        failed = cleanup_failed or bool(release_error) or bool(held)
        state["status"] = "CLEANUP_FAILED" if failed else "STOPPED"
        if cleanup_failed:
            state["cleanup_error"] = (
                state.get("cleanup_error")
                or "startup failed and owned processes, data, or claims could not be confirmed released"
            )
        elif failed:
            state["cleanup_error"] = (
                release_error
                or "startup failed and resource claims could not be confirmed released"
            )
        else:
            state.pop("cleanup_error", None)
        state["error"] = (
            "startup failed; inspect service state"
            if isinstance(exc, EnvironmentError)
            else "startup interrupted"
        )
        _write(state_path, state)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        raise EnvironmentError(f"{exc}; run_id={run_id}") from exc


def _report(
    state: dict[str, Any], index_path: pathlib.Path, *, cleanup: str | None = None
) -> dict[str, Any]:
    result = {
        "status": state["status"],
        "run_id": state["run_id"],
        "services": _service_report(state),
        "preparation": _command_report(state, "preparation"),
        "cleanup_steps": _command_report(state, "cleanup"),
        "data": _data_report(state),
        "port_claims": list(state.get("port_claims", [])),
    }
    if cleanup is not None:
        result["cleanup"] = cleanup
    if state.get("cleanup_error"):
        result["cleanup_error"] = state["cleanup_error"]
    result["resources_held"] = _held_resources(index_path, state["run_id"])
    return result


def _stop_or_status(
    args: Any,
    project: pathlib.Path,
    paths: dict[str, str],
    read_destination: Callable,
    *,
    stop: bool,
) -> int:
    run_id = args.run_id
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise EnvironmentError("invalid run ID")
    state_rel, index_rel, _ = _paths(paths, run_id)
    state_path = read_destination(project, state_rel)
    index_path = read_destination(project, index_rel)
    if not stop:
        state = _read_state(state_path, run_id, project)
        print(json.dumps(_report(state, index_path), ensure_ascii=False))
        return 0
    # Wait for an in-flight start to finish, then re-read under the same lock.
    state_lock = _acquire_state_lock(
        project, paths, read_destination, run_id, wait=STATE_LOCK_WAIT_SECONDS
    )
    try:
        state = _read_state(state_path, run_id, project)
        # Cleanup is idempotent: a saved STOPPED record is reconciled again so a
        # previous crash cannot leave its claims held behind a successful stop.
        failed = _cleanup_all(state, state_path, project=project, paths=paths)
        release_error = None
        if not failed:
            try:
                _release_claims(
                    index_path,
                    _lock_root(project, paths, read_destination, run_id),
                    project,
                    run_id,
                )
            except (EnvironmentError, OSError) as error:
                release_error = str(error)
                failed = True
        try:
            held = _held_resources(index_path, run_id)
        except (EnvironmentError, OSError) as error:
            held = []
            release_error = release_error or str(error)
            failed = True
        if held and not failed:
            failed = True
            release_error = release_error or "resource claims are still held"
        state["status"] = "CLEANUP_FAILED" if failed else "STOPPED"
        if failed:
            state["cleanup_error"] = (
                release_error
                or state.get("cleanup_error")
                or "owned processes, data, or resources could not be confirmed released"
            )
        else:
            state.pop("cleanup_error", None)
        _write(state_path, state)
        print(
            json.dumps(
                _report(state, index_path, cleanup="INCOMPLETE" if failed else "COMPLETE"),
                ensure_ascii=False,
            )
        )
        return 2 if failed else 0
    finally:
        state_lock.__exit__(None, None, None)
