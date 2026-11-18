"""Bounded, local-only API QA sessions with immutable run evidence."""

from __future__ import annotations

import devcrew_fs as fs
import hashlib
import ipaddress
import json
import math
import os
import pathlib
import re
import socket
import threading
import time
import urllib.parse
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable


class QaError(ValueError):
    """Invalid QA input or saved evidence."""


SCHEMA_VERSION = 1
MAX_INPUT_BYTES = 1024 * 1024
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_BYTES = 512 * 1024
RUN_ID_RE = re.compile(r"^[0-9a-f]{32}$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63})*$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@/-]*$")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_json(raw: bytes) -> Any:
    def reject_constant(_value: str) -> Any:
        raise ValueError("non-finite JSON constant")

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        try:
            if Decimal(str(parsed)) != Decimal(value):
                raise ValueError("JSON number exceeds exact supported precision")
        except InvalidOperation:
            raise ValueError("invalid JSON number") from None
        return parsed

    return json.loads(raw.decode("utf-8"), parse_constant=reject_constant, parse_float=finite_float)


def _read_json(path: pathlib.Path, limit: int = MAX_INPUT_BYTES) -> Any:
    if fs.is_link(path) or not path.is_file():
        raise QaError("input must be a regular non-symlink file")
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise QaError("input exceeds the size limit")
    try:
        return _strict_json(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        raise QaError("input is not valid UTF-8 JSON") from None


def _exact_keys(value: Any, required: set[str], *, optional: set[str] = frozenset()) -> None:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise QaError("input has an unsupported schema or fields")


def _validate(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]], str, int]:
    _exact_keys(value, {"schema_version", "profile", "scenarios"})
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise QaError("unsupported scenario schema version")
    profile = value["profile"]
    _exact_keys(profile, {"base_url"}, optional={"timeout_seconds"})
    if "timeout_seconds" not in profile:
        timeout = 5
    else:
        timeout = profile["timeout_seconds"]
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 10:
        raise QaError("timeout_seconds must be between 0.1 and 10")
    url = profile["base_url"]
    if not isinstance(url, str) or len(url) > 256:
        raise QaError("base_url must be a literal loopback HTTP URL")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "http" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")):
        raise QaError("base_url must be a literal loopback HTTP URL without credentials or extra components")
    try:
        address = ipaddress.ip_address(parsed.hostname)
        port = parsed.port if parsed.port is not None else 80
    except ValueError:
        raise QaError("base_url host must be a literal loopback address") from None
    if not address.is_loopback or not 1 <= port <= 65535:
        raise QaError("base_url host must be a literal loopback address")
    base = {"host": address.compressed, "port": port, "timeout_seconds": float(timeout),
            "base_url": f"http://{address.compressed}:{port}"}

    scenarios = value["scenarios"]
    if not isinstance(scenarios, list) or not 2 <= len(scenarios) <= 20:
        raise QaError("scenarios must contain between 2 and 20 GET probes")
    seen: set[str] = set()
    normalized = []
    for item in scenarios:
        _exact_keys(item, {"id", "path", "expected_status", "expect_json"})
        identifier, path, status, expected = (item["id"], item["path"], item["expected_status"], item["expect_json"])
        if not isinstance(identifier, str) or not ID_RE.fullmatch(identifier) or identifier in seen:
            raise QaError("scenario IDs must be unique lowercase identifiers")
        seen.add(identifier)
        if (not isinstance(path, str) or len(path) > 512 or not PATH_RE.fullmatch(path)
                or path.startswith("//")
                or any(part in (".", "..") for part in path.split("/"))):
            raise QaError("scenario path must be a bounded absolute path without query or traversal")
        if type(status) is not int or not 100 <= status <= 599:
            raise QaError("expected_status must be an HTTP status code")
        if not isinstance(expected, dict) or not 1 <= len(expected) <= 20:
            raise QaError("expect_json must contain between 1 and 20 fields")
        for field, expected_value in expected.items():
            if not isinstance(field, str) or not FIELD_RE.fullmatch(field):
                raise QaError("expected JSON fields must use simple dotted names")
            if type(expected_value) not in (str, int, float, bool, type(None)) or (isinstance(expected_value, str) and len(expected_value) > 256):
                raise QaError("expected JSON values must be bounded scalar values")
        normalized.append({"id": identifier, "path": path, "expected_status": status, "expect_json": expected})
    if not any(200 <= probe["expected_status"] < 300 for probe in normalized):
        raise QaError("scenarios must include an expected successful response")
    if not any(not 200 <= probe["expected_status"] < 300 for probe in normalized):
        raise QaError("scenarios must include an expected negative response")
    return base, normalized, url, int(timeout * 1000)


def _field(document: Any, dotted: str) -> Any:
    current = document
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return _MISSING
        current = current[part]
    return current


def _json_value_matches(actual: Any, expected: Any) -> bool:
    # Python treats bool as an int subclass, but JSON booleans and numbers differ.
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is bool and type(expected) is bool and actual is expected
    if type(actual) in (int, float) and type(expected) in (int, float):
        actual_number = Decimal(actual) if type(actual) is int else Decimal(str(actual))
        expected_number = Decimal(expected) if type(expected) is int else Decimal(str(expected))
        return actual_number == expected_number
    return type(actual) is type(expected) and actual == expected


_MISSING = object()


def _normalize_http_ows(value: str) -> str:
    """Trim only HTTP optional whitespace (SP and HTAB)."""
    return value.strip(" \t")


class _ResponseReadError(Exception):
    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status


def _interrupt_socket(active_socket: socket.socket) -> None:
    try:
        active_socket.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _expire_socket(active_socket: socket.socket, expired: threading.Event) -> None:
    expired.set()
    _interrupt_socket(active_socket)


class _HTTPReader:
    """Small bounded byte reader; framing and body limits share one decoder."""

    def __init__(self, sock: socket.socket, deadline: float, expired: threading.Event):
        self.sock = sock
        self.deadline = deadline
        self.expired = expired
        self.buffer = bytearray()
        self.header_bytes = 0
        self.extension_bytes = 0
        self.received_response_bytes = False

    def _recv(self, limit: int = 65536) -> bytes:
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            raise _ResponseReadError("incomplete_response" if self.received_response_bytes else "unavailable")
        try:
            data = self.sock.recv(limit)
        except OSError:
            raise _ResponseReadError("incomplete_response" if self.received_response_bytes else "unavailable") from None
        if data:
            self.received_response_bytes = True
        if self.expired.is_set() or time.monotonic() >= self.deadline:
            raise _ResponseReadError("incomplete_response" if self.received_response_bytes else "unavailable")
        return data

    def line(self, *, header: bool = True) -> bytes:
        while True:
            end = self.buffer.find(b"\r\n")
            if end >= 0:
                if end > 65536:
                    raise _ResponseReadError("invalid_response_framing")
                line = bytes(self.buffer[:end])
                del self.buffer[:end + 2]
                if header:
                    self.header_bytes += len(line) + 2
                    if self.header_bytes > 65536:
                        raise _ResponseReadError("invalid_response_framing")
                return line
            if len(self.buffer) > 65536:
                raise _ResponseReadError("invalid_response_framing")
            data = self._recv()
            if not data:
                raise _ResponseReadError("unavailable" if self.header_bytes == 0 and not self.buffer
                                          else "incomplete_response")
            self.buffer.extend(data)

    def exact(self, count: int) -> bytes:
        if count < 0 or count > MAX_RESPONSE_BYTES:
            raise _ResponseReadError("response_too_large")
        while len(self.buffer) < count:
            data = self._recv(min(65536, count - len(self.buffer)))
            if not data:
                raise _ResponseReadError("incomplete_response")
            self.buffer.extend(data)
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        return result

    def until_eof(self) -> bytes:
        body = bytearray(self.buffer)
        self.buffer.clear()
        if len(body) > MAX_RESPONSE_BYTES:
            raise _ResponseReadError("response_too_large")
        while True:
            data = self._recv(min(65536, MAX_RESPONSE_BYTES + 1 - len(body)))
            if not data:
                if self.expired.is_set() or time.monotonic() >= self.deadline:
                    raise _ResponseReadError("incomplete_response")
                return bytes(body)
            body.extend(data)
            if len(body) > MAX_RESPONSE_BYTES:
                raise _ResponseReadError("response_too_large")


_STATUS_LINE = re.compile(rb"^(HTTP/1\.[01]) ([0-9]{3}) ([\t\x20-\x7e\x80-\xff]*)$")
_HEADER_NAME = re.compile(rb"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_CHUNK_TOKEN = rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
_CHUNK_QUOTED = rb'"(?:[\t \x21\x23-\x5b\x5d-\x7e\x80-\xff]|\\[\t \x21-\x7e\x80-\xff])*"'
_CHUNK_SIZE = re.compile(
    rb"^([0-9A-Fa-f]+)((?:[ \t]*;[ \t]*" + _CHUNK_TOKEN
    + rb"(?:[ \t]*=[ \t]*(?:" + _CHUNK_TOKEN + rb"|" + _CHUNK_QUOTED + rb"))?)*)$"
)


def _significant_digits(value: bytes) -> bytes:
    return value.lstrip(b"0") or b"0"


def _read_status(reader: _HTTPReader) -> tuple[str, int, list[tuple[bytes, bytes]]]:
    line = reader.line()
    match = _STATUS_LINE.fullmatch(line)
    if not match:
        raise _ResponseReadError("invalid_status")
    status = int(match.group(2))
    if not 100 <= status <= 599:
        raise _ResponseReadError("invalid_status", status)
    headers: list[tuple[bytes, bytes]] = []
    while True:
        line = reader.line()
        if not line:
            return match.group(1).decode("ascii"), status, headers
        if line[:1] in (b" ", b"\t") or b":" not in line:
            raise _ResponseReadError("invalid_response_framing", status)
        name, value = line.split(b":", 1)
        if not _HEADER_NAME.fullmatch(name) or any(byte not in (9,) and not 32 <= byte <= 126 and byte < 128
                                                   for byte in value):
            raise _ResponseReadError("invalid_response_framing", status)
        headers.append((name.lower(), value.strip(b" \t")))


def _body(reader: _HTTPReader, version: str, status: int, headers: list[tuple[bytes, bytes]],
          *, consume: bool = True) -> bytes | None:
    lengths = [value for name, value in headers if name == b"content-length"]
    transfers = [value for name, value in headers if name == b"transfer-encoding"]
    if lengths and (any(not value or not value.isascii() or not value.isdigit() for value in lengths)
                    or len(set(lengths)) != 1):
        raise _ResponseReadError("invalid_response_framing", status)
    if transfers and (lengths or len(transfers) != 1 or version != "HTTP/1.1"
                       or transfers[0].lower() != b"chunked"):
        raise _ResponseReadError("invalid_response_framing", status)
    if lengths and (any(not value or not value.isascii() or not value.isdigit() for value in lengths)
                    or len(set(lengths)) != 1):
        raise _ResponseReadError("invalid_response_framing", status)
    if status in range(100, 200) or status in (204, 304, 205):
        if status < 200 or status == 204:
            if lengths or transfers:
                raise _ResponseReadError("invalid_response_framing", status)
        return b"" if consume else None
    if not consume:
        if lengths and (len(_significant_digits(lengths[0])) > 9
                        or int(_significant_digits(lengths[0])) > MAX_RESPONSE_BYTES):
            raise _ResponseReadError("response_too_large", status)
        return None
    if lengths:
        # Avoid converting attacker-sized decimal strings.
        length = _significant_digits(lengths[0])
        if len(length) > 9 or int(length) > MAX_RESPONSE_BYTES:
            raise _ResponseReadError("response_too_large", status)
        return reader.exact(int(length))
    if transfers:
        body = bytearray()
        while True:
            line = reader.line(header=False)
            match = _CHUNK_SIZE.fullmatch(line)
            if not match:
                raise _ResponseReadError("invalid_response_framing", status)
            reader.extension_bytes += len(match.group(2))
            if reader.extension_bytes > 65536:
                raise _ResponseReadError("invalid_response_framing", status)
            size_digits = _significant_digits(match.group(1))
            if len(size_digits) > 8:
                raise _ResponseReadError("response_too_large", status)
            size = int(size_digits, 16)
            if size > MAX_RESPONSE_BYTES - len(body):
                raise _ResponseReadError("response_too_large", status)
            if not size:
                while True:
                    trailer = reader.line()
                    if not trailer:
                        return bytes(body)
                    if trailer[:1] in (b" ", b"\t") or b":" not in trailer:
                        raise _ResponseReadError("invalid_response_framing", status)
                    name, value = trailer.split(b":", 1)
                    if not _HEADER_NAME.fullmatch(name) or name.lower() in (b"content-length", b"transfer-encoding"):
                        raise _ResponseReadError("invalid_response_framing", status)
                    if any(byte not in (9,) and not 32 <= byte <= 126 and byte < 128 for byte in value):
                        raise _ResponseReadError("invalid_response_framing", status)
            body.extend(reader.exact(size))
            if reader.exact(2) != b"\r\n":
                raise _ResponseReadError("invalid_response_framing", status)
    return reader.until_eof()


def _request(profile: dict[str, Any], probe: dict[str, Any]) -> tuple[int | None, dict[str, Any] | None, str | None]:
    active_socket: socket.socket | None = None
    timer: threading.Timer | None = None
    deadline_expired = threading.Event()
    try:
        deadline = time.monotonic() + profile["timeout_seconds"]
        active_socket = socket.create_connection((profile["host"], profile["port"]), profile["timeout_seconds"])
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise socket.timeout("request deadline exceeded")
        active_socket.settimeout(remaining)
        # Interrupt any blocking read at the absolute request deadline.
        timer = threading.Timer(remaining, _expire_socket, args=(active_socket, deadline_expired))
        timer.daemon = True
        timer.start()
        host = f"[{profile['host']}]" if ":" in profile["host"] else profile["host"]
        request = (f"GET {probe['path']} HTTP/1.1\r\nHost: {host}:{profile['port']}\r\n"
                   "Accept: application/json\r\nConnection: close\r\n\r\n").encode("ascii")
        active_socket.sendall(request)
        reader = _HTTPReader(active_socket, deadline, deadline_expired)
        status = None
        while True:
            version, status, headers = _read_status(reader)
            if status == 101:
                raise _ResponseReadError("invalid_response_framing", status)
            # Framing syntax on informational messages is checked before skipping them.
            interim_length = any(name == b"content-length" for name, _ in headers)
            interim_transfer = any(name == b"transfer-encoding" for name, _ in headers)
            if 100 <= status < 200:
                if interim_length or interim_transfer:
                    raise _ResponseReadError("invalid_response_framing", status)
                continue
            break
        if status != probe["expected_status"]:
            # Validate framing headers, but do not consume a mismatched response body.
            _body(reader, version, status, headers, consume=False)
            if deadline_expired.is_set() or time.monotonic() >= deadline:
                raise _ResponseReadError("incomplete_response", status)
            return status, None, None
        if not probe["expect_json"]:
            _body(reader, version, status, headers, consume=False)
            if deadline_expired.is_set() or time.monotonic() >= deadline:
                raise _ResponseReadError("incomplete_response", status)
            return status, None, None
        body = _body(reader, version, status, headers)
        if deadline_expired.is_set() or time.monotonic() >= deadline:
            return status, None, "incomplete_response"
        if status in (204, 304, 205):
            return status, None, "invalid_json"
        try:
            parsed = _strict_json(body)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
            return status, None, "invalid_json"
        if deadline_expired.is_set() or time.monotonic() >= deadline:
            raise _ResponseReadError("incomplete_response", status)
        return status, parsed, None
    except _ResponseReadError as error:
        if error.code == "invalid_status":
            return error.status, None, error.code
        return error.status if error.status is not None else locals().get("status"), None, error.code
    except (OSError, socket.timeout, ValueError):
        return None, None, "unavailable"
    finally:
        if timer is not None:
            timer.cancel()
        if active_socket is not None:
            active_socket.close()


def _write(destination: Callable[[pathlib.Path, str], pathlib.Path], root: pathlib.Path,
           relative: str, payload: Any, *, immutable: bool = True) -> pathlib.Path:
    target = destination(root, relative)
    if immutable and target.exists():
        raise QaError("refusing to overwrite immutable QA evidence")
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    encoded = data.encode("utf-8")
    limit = MAX_INDEX_BYTES if relative.endswith("/index.json") else MAX_INPUT_BYTES
    if len(encoded) > limit:
        raise QaError("saved QA artifact exceeds the reader size limit")
    _atomic_write(target, encoded)
    return target


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass


def _load_index(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "runs": []}
    index = _read_json(path, MAX_INDEX_BYTES)
    _exact_keys(index, {"schema_version", "runs"})
    if type(index["schema_version"]) is not int or index["schema_version"] != SCHEMA_VERSION or not isinstance(index["runs"], list):
        raise QaError("unsupported QA index schema")
    ids: set[str] = set()
    for run in index["runs"]:
        _exact_keys(run, {"run_id", "status", "created_at", "result_sha256"})
        if (not isinstance(run["run_id"], str) or not RUN_ID_RE.fullmatch(run["run_id"])
                or run["run_id"] in ids or run["status"] not in ("PASS", "FAIL", "BLOCKED")
                or not isinstance(run["created_at"], str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT[^\s]+Z", run["created_at"])
                or not isinstance(run["result_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", run["result_sha256"])):
            raise QaError("invalid QA index entry")
        ids.add(run["run_id"])
    return index


def _verify_run(project: pathlib.Path, paths: dict[str, str], destination: Callable,
                read_destination: Callable, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not RUN_ID_RE.fullmatch(run_id):
        raise QaError("invalid run ID")
    root = project / paths["checks"] / "qa" / "runs" / run_id
    result_path = read_destination(project, f'{paths["checks"]}/qa/runs/{run_id}/result.json')
    evidence_path = read_destination(project, f'{paths["checks"]}/qa/runs/{run_id}/evidence.json')
    result, evidence = _read_json(result_path), _read_json(evidence_path)
    _exact_keys(result, {"schema_version", "run_id", "project_root", "created_at", "status", "scenario_sha256", "profile_sha256", "evidence_sha256", "result_sha256", "probe_count", "completed_count"})
    _exact_keys(evidence, {"schema_version", "run_id", "scenario_sha256", "profile_sha256", "observations"})
    if type(result["schema_version"]) is not int or type(evidence["schema_version"]) is not int or result["schema_version"] != SCHEMA_VERSION or evidence["schema_version"] != SCHEMA_VERSION:
        raise QaError("unsupported saved QA schema")
    if (result["status"] not in ("PASS", "FAIL", "BLOCKED")
            or not isinstance(result["created_at"], str)
            or not re.fullmatch(r"\d{4}-\d\d-\d\dT[^\s]+Z", result["created_at"])
            or type(result["probe_count"]) is not int or result["probe_count"] < 1
            or type(result["completed_count"]) is not int or result["completed_count"] < 0
            or any(not isinstance(result[key], str) or not re.fullmatch(r"[0-9a-f]{64}", result[key])
                   for key in ("scenario_sha256", "profile_sha256", "evidence_sha256", "result_sha256"))):
        raise QaError("invalid saved QA result fields")
    digest = result.pop("result_sha256")
    if digest != _digest(result):
        raise QaError("QA result integrity check failed")
    result["result_sha256"] = digest
    if result["run_id"] != run_id or evidence["run_id"] != run_id or result["project_root"] != str(project.resolve()):
        raise QaError("QA run belongs to a different project or ID")
    if hashlib.sha256(evidence_path.read_bytes()).hexdigest() != result["evidence_sha256"]:
        raise QaError("QA evidence integrity check failed")
    snapshots = project / paths["tasks"] / "qa" / "snapshots" / run_id
    scenario_path = read_destination(project, f'{paths["tasks"]}/qa/snapshots/{run_id}/scenario.json')
    profile_path = read_destination(project, f'{paths["tasks"]}/qa/snapshots/{run_id}/profile.json')
    scenario, profile = _read_json(scenario_path), _read_json(profile_path)
    _exact_keys(scenario, {"schema_version", "scenarios"})
    _exact_keys(profile, {"schema_version", "host", "port", "timeout_seconds"})
    if (type(scenario["schema_version"]) is not int or scenario["schema_version"] != SCHEMA_VERSION
            or type(profile["schema_version"]) is not int or profile["schema_version"] != SCHEMA_VERSION
            or type(profile["port"]) is not int or not 1 <= profile["port"] <= 65535
            or type(profile["timeout_seconds"]) not in (int, float)
            or not 0.1 <= profile["timeout_seconds"] <= 10):
        raise QaError("invalid saved QA snapshot schema")
    if _digest(scenario) != result["scenario_sha256"] or _digest(profile) != result["profile_sha256"]:
        raise QaError("QA input snapshot integrity check failed")
    if evidence["scenario_sha256"] != result["scenario_sha256"] or evidence["profile_sha256"] != result["profile_sha256"]:
        raise QaError("QA evidence does not match its input snapshots")
    if (not isinstance(scenario, dict) or not isinstance(scenario.get("scenarios"), list)
            or not isinstance(evidence["observations"], list) or len(evidence["observations"]) != result["probe_count"]
            or result["completed_count"] > result["probe_count"]):
        raise QaError("QA evidence coverage is incomplete")
    probes = scenario["scenarios"]
    observations = evidence["observations"]
    if len(probes) != len(observations) or len(probes) != result["probe_count"]:
        raise QaError("QA evidence does not cover the saved scenario")
    derived_status = "PASS"
    derived_completed = 0
    for probe, observation in zip(probes, observations):
        _exact_keys(probe, {"id", "path", "expected_status", "expect_json"})
        _exact_keys(observation, {"scenario_id", "status_code", "outcome", "field_checks", "error"})
        if observation["scenario_id"] != probe["id"] or observation["outcome"] not in ("PASS", "FAIL", "BLOCKED"):
            raise QaError("QA evidence scenario identity is invalid")
        if observation["error"] not in (None, "unavailable", "invalid_json", "response_too_large",
                                         "incomplete_response", "invalid_response_framing", "invalid_status"):
            raise QaError("QA evidence contains an unsupported error")
        code = observation["status_code"]
        if code is not None:
            valid_http_status = type(code) is int and 100 <= code <= 599
            invalid_status_observation = (observation["error"] == "invalid_status"
                                          and type(code) is int and 0 <= code <= 999
                                          and not 100 <= code <= 599)
            if not (valid_http_status or invalid_status_observation):
                raise QaError("QA evidence has an invalid HTTP status")
        if (observation["error"] == "invalid_status" and code is not None
                and not (type(code) is int and 0 <= code <= 999 and not 100 <= code <= 599)):
            raise QaError("QA evidence has an inconsistent invalid HTTP status")
        fields = observation["field_checks"]
        if not isinstance(fields, list):
            raise QaError("QA evidence fields are invalid")
        if observation["error"] == "unavailable":
            expected_outcome = "BLOCKED"
            derived_status = "BLOCKED"
        elif observation["error"]:
            expected_outcome = "FAIL"
            if derived_status != "BLOCKED":
                derived_status = "FAIL"
        elif code != probe["expected_status"]:
            expected_outcome = "FAIL"
            if derived_status != "BLOCKED":
                derived_status = "FAIL"
        else:
            derived_completed += 1
            if len(fields) != len(probe["expect_json"]):
                raise QaError("QA evidence field coverage is incomplete")
            projected_fields: set[str] = set()
            for field_check in fields:
                _exact_keys(field_check, {"field", "matched"})
                field = field_check["field"]
                if (not isinstance(field, str) or field not in probe["expect_json"]
                        or field in projected_fields or type(field_check["matched"]) is not bool):
                    raise QaError("QA evidence field projection is invalid")
                projected_fields.add(field)
            if projected_fields != set(probe["expect_json"]):
                raise QaError("QA evidence field coverage is incomplete")
            expected_outcome = "PASS" if all(row["matched"] for row in fields) else "FAIL"
            if expected_outcome == "FAIL" and derived_status != "BLOCKED":
                derived_status = "FAIL"
        if observation["outcome"] != expected_outcome:
            raise QaError("QA evidence outcome does not match its observation")
    if result["status"] != derived_status or result["completed_count"] != derived_completed:
        raise QaError("QA result status or coverage does not match evidence")
    if derived_status == "PASS" and derived_completed != len(probes):
        raise QaError("incomplete QA evidence cannot report PASS")
    if (not any(200 <= probe["expected_status"] < 300 for probe in probes)
            or not any(not 200 <= probe["expected_status"] < 300 for probe in probes)):
        raise QaError("saved QA scenario lacks required positive or negative coverage")
    report_path = read_destination(project, f'{paths["checks"]}/qa/runs/{run_id}/report.md')
    if report_path.read_bytes() != _markdown(result, evidence).encode("utf-8"):
        raise QaError("QA Markdown report does not match verified evidence")
    return result, evidence


def command(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
            read_destination: Callable) -> int:
    if getattr(args, "qa_operation", "").startswith("web-"):
        import devcrew_qa_web

        return devcrew_qa_web.command(args, project, paths, destination, read_destination)
    if args.qa_operation != "run-api":
        return _command_unlocked(args, project, paths, destination, read_destination)


    lock_path = destination(project, f'{paths["checks"]}/qa/index.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(fs.open_regular(lock_path, os.O_CREAT | os.O_RDWR), "r+b") as lock:
        fs.lock(lock.fileno())
        try:
            return _command_unlocked(args, project, paths, destination, read_destination)
        finally:
            fs.unlock(lock.fileno())


def _command_unlocked(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
                      read_destination: Callable) -> int:
    import datetime

    try:
        qa_root = f'{paths["checks"]}/qa'
        index_path = (read_destination if args.qa_operation != "run-api" else destination)(project, f"{qa_root}/index.json")
        index = _load_index(index_path)
        if args.qa_operation == "run-api":
            scenario_input = _read_json(pathlib.Path(args.scenario).expanduser())
            profile, probes, _base_url, _timeout = _validate(scenario_input)
            scenario_snapshot = {"schema_version": SCHEMA_VERSION, "scenarios": probes}
            safe_profile = {"schema_version": SCHEMA_VERSION, "host": profile["host"], "port": profile["port"],
                            "timeout_seconds": profile["timeout_seconds"]}
            for snapshot in (scenario_snapshot, safe_profile):
                encoded = (json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
                if len(encoded) > MAX_INPUT_BYTES:
                    raise QaError("saved QA artifact exceeds the reader size limit")
            # Bound the largest possible evidence representation from validated inputs.
            evidence_candidate = {"schema_version": SCHEMA_VERSION, "run_id": "0" * 32,
                                  "scenario_sha256": "0" * 64, "profile_sha256": "0" * 64,
                                  "observations": [
                                      {"scenario_id": probe["id"], "status_code": 999,
                                       "outcome": "FAIL", "field_checks": [
                                           {"field": key, "matched": False}
                                           for key in probe["expect_json"]], "error": "incomplete_response"}
                                      for probe in probes]}
            if len((json.dumps(evidence_candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")) > MAX_INPUT_BYTES:
                raise QaError("saved QA artifact exceeds the reader size limit")
            # Ensure the next index entry fits before the first network side effect.
            index_candidate = dict(index)
            index_candidate["runs"] = list(index["runs"]) + [{
                "run_id": "0" * 32, "status": "BLOCKED", "created_at": "2000-01-01T00:00:00Z",
                "result_sha256": "0" * 64,
            }]
            encoded_index = (json.dumps(index_candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if len(encoded_index) > MAX_INDEX_BYTES:
                raise QaError("saved QA artifact exceeds the reader size limit")
            run_id = uuid.uuid4().hex
            # Validate every eventual write path before sending any request.
            destination(project, f'{paths["tasks"]}/qa/snapshots/{run_id}/scenario.json')
            destination(project, f'{paths["tasks"]}/qa/snapshots/{run_id}/profile.json')
            destination(project, f'{qa_root}/runs/{run_id}/evidence.json')
            destination(project, f'{qa_root}/runs/{run_id}/result.json')
            scenario_hash, profile_hash = _digest(scenario_snapshot), _digest(safe_profile)
            observations: list[dict[str, Any]] = []
            status = "PASS"
            completed = 0
            for probe in probes:
                actual_status, document, transport_error = _request(profile, probe)
                if transport_error == "unavailable":
                    outcome, matches = "BLOCKED", []
                    status = "BLOCKED"
                elif transport_error:
                    outcome, matches = "FAIL", []
                    if status != "BLOCKED":
                        status = "FAIL"
                elif actual_status != probe["expected_status"]:
                    outcome, matches = "FAIL", []
                    if status != "BLOCKED":
                        status = "FAIL"
                else:
                    completed += 1
                    matches = [{"field": key, "matched": _json_value_matches(_field(document, key), value)}
                               for key, value in probe["expect_json"].items()]
                    outcome = "PASS" if all(row["matched"] for row in matches) else "FAIL"
                    if outcome == "FAIL" and status != "BLOCKED":
                        status = "FAIL"
                observations.append({"scenario_id": probe["id"], "status_code": actual_status,
                                    "outcome": outcome, "field_checks": matches,
                                    "error": transport_error})
            created = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            snapshot_base = f'{paths["tasks"]}/qa/snapshots/{run_id}'
            _write(destination, project, f"{snapshot_base}/scenario.json", scenario_snapshot)
            _write(destination, project, f"{snapshot_base}/profile.json", safe_profile)
            run_base = f"{qa_root}/runs/{run_id}"
            evidence = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
                        "scenario_sha256": scenario_hash, "profile_sha256": profile_hash,
                        "observations": observations}
            evidence_path = _write(destination, project, f"{run_base}/evidence.json", evidence)
            result = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "project_root": str(project.resolve()),
                      "created_at": created, "status": status, "scenario_sha256": scenario_hash,
                      "profile_sha256": profile_hash, "evidence_sha256": hashlib.sha256(evidence_path.read_bytes()).hexdigest(),
                      "probe_count": len(probes), "completed_count": completed}
            result["result_sha256"] = _digest(result)
            _write(destination, project, f"{run_base}/result.json", result)
            markdown = _markdown(result, evidence)
            _atomic_write(destination(project, f"{run_base}/report.md"), markdown.encode("utf-8"))
            # Admission lock is held from preflight through publication.
            index = _load_index(index_path)
            index["runs"].insert(0, {"run_id": run_id, "status": status, "created_at": created,
                                     "result_sha256": result["result_sha256"]})
            _write(destination, project, f"{qa_root}/index.json", index, immutable=False)
            print(json.dumps({"status": status, "run_id": run_id, "report": f"{run_base}/report.md"}))
            return 0
        if args.qa_operation == "list":
            # Verify every indexed result before exposing its status.
            entries = []
            for entry in index["runs"]:
                result, _ = _verify_run(project, paths, destination, read_destination, entry["run_id"])
                if entry["result_sha256"] != result["result_sha256"] or entry["status"] != result["status"]:
                    raise QaError("QA index does not match saved result")
                entries.append(entry)
            print(json.dumps({"schema_version": SCHEMA_VERSION, "runs": entries}, ensure_ascii=False))
            return 0
        result, evidence = _verify_run(project, paths, destination, read_destination, args.run_id)
        indexed = next((entry for entry in index["runs"] if entry["run_id"] == args.run_id), None)
        if (indexed is None or indexed["result_sha256"] != result["result_sha256"]
                or indexed["status"] != result["status"]):
            raise QaError("QA run is not present in the project index")
        print(json.dumps({"result": result, "evidence": evidence,
                          "markdown": _markdown(result, evidence)}, ensure_ascii=False))
        return 0
    except (OSError, QaError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=__import__("sys").stderr)
        return 2


def _markdown(result: dict[str, Any], evidence: dict[str, Any]) -> str:
    lines = [f"# QA run {result['run_id']}", "", f"- Status: **{result['status']}**",
             f"- Probes: {result['completed_count']}/{result['probe_count']}", "", "## Observations", ""]
    for observation in evidence["observations"]:
        lines.append(f"- `{observation['scenario_id']}`: **{observation['outcome']}** (HTTP {observation['status_code'] if observation['status_code'] is not None else 'unavailable'})")
        for check in observation["field_checks"]:
            lines.append(f"  - `{check['field']}`: {'matched' if check['matched'] else 'mismatch'}")
    lines.append("")
    return "\n".join(lines)
