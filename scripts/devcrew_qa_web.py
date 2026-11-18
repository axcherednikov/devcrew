"""Agent-driven local web QA: bounded scenarios, safe artifacts, fresh readback."""

from __future__ import annotations

import devcrew_fs as fs
import hashlib
import ipaddress
import json
import os
import pathlib
import re
import struct
import urllib.parse
import uuid
from typing import Any, Callable

import devcrew_qa_environment

from devcrew_qa import (
    ID_RE,
    MAX_INPUT_BYTES,
    PATH_RE,
    RUN_ID_RE,
    QaError,
    _atomic_write,
    _digest,
    _field,
    _json_value_matches,
    _read_json,
    _request,
)


SCHEMA_VERSION = 1
MAX_INDEX_BYTES = 8 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 8 * 1024 * 1024
VIEWPORT_ID_RE = ID_RE
OBJECT_ID_RE = re.compile(r"^[A-Za-z0-9._~-]{1,64}$")
DEFECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}:[a-z][a-z0-9-]{0,47}$")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
BLOCKED_REASONS = ("browser_unavailable", "viewport_unavailable", "backend_unavailable",
                   "tooling_unavailable")


def _exact_keys(value: Any, required: set[str], *, optional: set[str] = frozenset()) -> None:
    if not isinstance(value, dict) or not required <= set(value) or set(value) - required - optional:
        raise QaError("input has an unsupported schema or fields")


def _scalar(value: Any) -> bool:
    return (type(value) in (str, int, float, bool, type(None))
            and (not isinstance(value, str) or len(value) <= 500))


def _loopback(base_url: Any) -> dict[str, Any]:
    if not isinstance(base_url, str) or len(base_url) > 256:
        raise QaError("base_url must be a literal loopback HTTP URL")
    parsed = urllib.parse.urlsplit(base_url)
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
    return {"host": address.compressed, "port": port}


def _validate_scenario(value: Any) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    _exact_keys(value, {"schema_version", "profile", "viewports", "scenarios"})
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise QaError("unsupported web scenario schema version")
    profile = value["profile"]
    _exact_keys(profile, {"base_url", "environment_run_id"}, optional={"readback", "timeout_seconds"})
    loopback = _loopback(profile["base_url"])
    timeout = profile.get("timeout_seconds", 5)
    if type(timeout) not in (int, float) or not 0.1 <= timeout <= 10:
        raise QaError("timeout_seconds must be between 0.1 and 10")
    environment_run_id = profile["environment_run_id"]
    if not isinstance(environment_run_id, str) or not RUN_ID_RE.fullmatch(environment_run_id):
        raise QaError("environment_run_id must be a saved managed environment run ID")
    readback = None
    if "readback" in profile:
        readback = profile["readback"]
        _exact_keys(readback, {"path", "expected_status", "expect_json"})
        path = readback["path"]
        if (not isinstance(path, str) or path.count("{id}") != 1 or len(path) > 512
                or not PATH_RE.fullmatch(path.replace("{id}", "x"))
                or "//" in path or any(part in (".", "..") for part in path.split("/"))):
            raise QaError("readback path must be one bounded absolute path with a single {id} placeholder")
        if type(readback["expected_status"]) is not int or not 200 <= readback["expected_status"] < 300:
            raise QaError("readback expected_status must be a successful HTTP status")
        expected = readback["expect_json"]
        if not isinstance(expected, dict) or not 1 <= len(expected) <= 20:
            raise QaError("readback expect_json must contain between 1 and 20 fields")
        for name, expected_value in expected.items():
            if (not isinstance(name, str) or not re.fullmatch(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63})*$", name)
                    or not _scalar(expected_value)):
                raise QaError("readback fields and expected values must be bounded scalars")

    viewports = value["viewports"]
    if not isinstance(viewports, list) or not 2 <= len(viewports) <= 4:
        raise QaError("viewports must contain between 2 and 4 sizes")
    seen_viewports: set[str] = set()
    normalized_viewports = []
    for viewport in viewports:
        _exact_keys(viewport, {"id", "width", "height"})
        identifier = viewport["id"]
        if (not isinstance(identifier, str) or not VIEWPORT_ID_RE.fullmatch(identifier)
                or identifier in seen_viewports):
            raise QaError("viewport IDs must be unique lowercase identifiers")
        seen_viewports.add(identifier)
        width, height = viewport["width"], viewport["height"]
        if type(width) is not int or not 240 <= width <= 3840:
            raise QaError("viewport width must be between 240 and 3840")
        if type(height) is not int or not 320 <= height <= 2160:
            raise QaError("viewport height must be between 320 and 2160")
        normalized_viewports.append({"id": identifier, "width": width, "height": height})
    if not any(viewport["width"] >= 1024 for viewport in normalized_viewports):
        raise QaError("web QA requires a desktop viewport of at least 1024 pixels")
    if not any(viewport["width"] <= 500 for viewport in normalized_viewports):
        raise QaError("web QA requires a mobile-web viewport of at most 500 pixels")

    scenarios = value["scenarios"]
    if not isinstance(scenarios, list) or not 1 <= len(scenarios) <= 12:
        raise QaError("scenarios must contain between 1 and 12 entries")
    seen_scenarios: set[str] = set()
    normalized_scenarios = []
    for scenario in scenarios:
        _exact_keys(scenario, {"id", "kind", "title", "steps", "checks"})
        identifier = scenario["id"]
        if (not isinstance(identifier, str) or not ID_RE.fullmatch(identifier)
                or identifier in seen_scenarios):
            raise QaError("scenario IDs must be unique lowercase identifiers")
        seen_scenarios.add(identifier)
        if scenario["kind"] not in ("e2e", "validation", "ui"):
            raise QaError("scenario kind must be e2e, validation or ui")
        title = scenario["title"]
        if not isinstance(title, str) or not 1 <= len(title) <= 200:
            raise QaError("scenario title must be a bounded string")
        steps = scenario["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20 or any(
                not isinstance(step, str) or not 1 <= len(step) <= 500 for step in steps):
            raise QaError("scenario steps must contain between 1 and 20 bounded strings")
        checks = scenario["checks"]
        if not isinstance(checks, list) or not 1 <= len(checks) <= 20 or any(
                not isinstance(check, str) or not ID_RE.fullmatch(check) for check in checks):
            raise QaError("scenario checks must contain between 1 and 20 check IDs")
        if len(set(checks)) != len(checks):
            raise QaError("scenario check IDs must be unique")
        if scenario["kind"] == "e2e" and readback is None:
            raise QaError("an e2e scenario requires a readback profile")
        normalized_scenarios.append({"id": identifier, "kind": scenario["kind"], "title": title,
                                     "steps": list(steps), "checks": list(checks)})
    if readback is not None and not any(item["kind"] == "e2e" for item in normalized_scenarios):
        raise QaError("a readback profile requires an e2e scenario")

    safe_profile = {"schema_version": SCHEMA_VERSION, "host": loopback["host"], "port": loopback["port"],
                    "timeout_seconds": float(timeout), "environment_run_id": environment_run_id}
    if readback is not None:
        safe_profile["readback"] = readback
    normalized = {"schema_version": SCHEMA_VERSION, "profile": safe_profile,
                  "viewports": normalized_viewports, "scenarios": normalized_scenarios}
    return safe_profile, normalized_viewports, normalized_scenarios


def _paths(paths: dict[str, str], run_id: str) -> dict[str, str]:
    root = f'{paths["checks"]}/qa-web'
    return {
        "index": f"{root}/index.json",
        "lock": f"{root}/index.lock",
        "run": f"{root}/runs/{run_id}",
        "state": f"{root}/runs/{run_id}/state.json",
        "evidence": f"{root}/runs/{run_id}/evidence.json",
        "result": f"{root}/runs/{run_id}/result.json",
        "report": f"{root}/runs/{run_id}/report.md",
        "snapshot": f'{paths["tasks"]}/qa/snapshots/{run_id}',
    }


def _now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_bytes(payload: Any) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _create_run(project: pathlib.Path, paths: dict[str, str], destination: Callable,
                read_destination: Callable, snapshot: dict[str, Any],
                replay: dict[str, Any] | None = None
                ) -> tuple[str, str, str, dict[str, Any], str, str]:
    """Allocate one immutable run directory and its snapshots.

    Every attempt, including a plain start, is bound to its environment here,
    under the caller's write lock. A second attempt cannot claim an environment
    that an unfinished run already owns.
    """
    profile = snapshot["profile"]
    bound = _bound_run(project, paths, read_destination, profile["environment_run_id"])
    if bound is not None:
        raise QaError(f"this environment is already bound to web QA run {bound}; "
                      "start a fresh environment")
    run_id = uuid.uuid4().hex
    token = f"qa-{run_id}"
    layout = _paths(paths, run_id)
    scenario_hash = _digest(snapshot)
    profile_hash = _digest({"schema_version": SCHEMA_VERSION, "profile": profile})
    state = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "project_root": str(project.resolve()),
             "created_at": _now(), "status": "RUNNING", "token": token,
             "scenario_sha256": scenario_hash, "profile_sha256": profile_hash,
             "environment_run_id": profile["environment_run_id"],
             "viewports": snapshot["viewports"], "scenarios": snapshot["scenarios"]}
    if replay is not None:
        state["replay"] = replay
    # Validate every eventual write path before allocating evidence.
    destination(project, f'{layout["snapshot"]}/scenario.json')
    destination(project, f'{layout["snapshot"]}/profile.json')
    destination(project, layout["state"])
    for path in (f'{layout["snapshot"]}/scenario.json', f'{layout["snapshot"]}/profile.json'):
        target = destination(project, path)
        if target.exists():
            raise QaError("refusing to overwrite immutable QA evidence")
    _atomic_write(destination(project, f'{layout["snapshot"]}/scenario.json'), _json_bytes(snapshot))
    _atomic_write(destination(project, f'{layout["snapshot"]}/profile.json'),
                  _json_bytes({"schema_version": SCHEMA_VERSION, "profile": profile}))
    _atomic_write(destination(project, layout["state"]), _json_bytes(state))
    return run_id, token, layout["run"], state, scenario_hash, profile_hash



def _load_state(path: pathlib.Path, run_id: str) -> dict[str, Any]:
    state = _read_json(path)
    if (not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION
            or state.get("run_id") != run_id or state.get("status") not in ("RUNNING", "COMPLETED")
            or not isinstance(state.get("viewports"), list) or not isinstance(state.get("scenarios"), list)):
        raise QaError("web QA state is invalid or belongs to another run")
    return state


def _load_index(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "runs": []}
    index = _read_json(path, MAX_INDEX_BYTES)
    _exact_keys(index, {"schema_version", "runs"})
    if type(index["schema_version"]) is not int or index["schema_version"] != SCHEMA_VERSION or not isinstance(index["runs"], list):
        raise QaError("unsupported web QA index schema")
    seen: set[str] = set()
    for entry in index["runs"]:
        _exact_keys(entry, {"run_id", "status", "created_at", "result_sha256"},
                    optional={"replay_of"})
        if (not isinstance(entry["run_id"], str) or not RUN_ID_RE.fullmatch(entry["run_id"])
                or entry["run_id"] in seen or entry["status"] not in ("PASS", "FAIL", "BLOCKED")
                or not isinstance(entry["created_at"], str)
                or not isinstance(entry["result_sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", entry["result_sha256"])
                or ("replay_of" in entry and (not isinstance(entry["replay_of"], str)
                                              or not RUN_ID_RE.fullmatch(entry["replay_of"])))):
            raise QaError("invalid web QA index entry")
        seen.add(entry["run_id"])
    return index


def _environment_state(project: pathlib.Path, paths: dict[str, str], read_destination: Callable,
                       environment_run_id: str) -> dict[str, Any] | None:
    relative = f'{paths["checks"]}/qa-environments/runs/{environment_run_id}/state.json'
    path = read_destination(project, relative)
    if not path.exists():
        return None
    try:
        state = _read_json(path)
    except (OSError, QaError):
        return None
    return state if isinstance(state, dict) else None


def _service_endpoint(url: Any) -> tuple[str, int] | None:
    if not isinstance(url, str) or len(url) > 512:
        return None
    parsed = urllib.parse.urlsplit(url)
    try:
        loopback = _loopback(f"http://{parsed.netloc}")
    except QaError:
        return None
    return loopback["host"], loopback["port"]


def _runs_root(project: pathlib.Path, paths: dict[str, str],
               read_destination: Callable) -> pathlib.Path:
    """Validate the web QA runs directory without creating anything."""
    probe = read_destination(project, f'{paths["checks"]}/qa-web/runs/{"0" * 32}/state.json')
    return probe.parent.parent


def _bound_run(project: pathlib.Path, paths: dict[str, str], read_destination: Callable,
               environment_run_id: str) -> str | None:
    """Return the web QA attempt that already owns this environment.

    An attempt is bound to its environment when its run directory is created,
    so an unfinished attempt is visible here even though it is not indexed yet.
    """
    root = _runs_root(project, paths, read_destination)
    if not root.is_dir() or fs.is_link(root):
        return None
    for entry in sorted(root.iterdir()):
        if fs.is_link(entry) or not entry.is_dir() or not RUN_ID_RE.fullmatch(entry.name):
            continue
        state_path = read_destination(
            project, f'{paths["checks"]}/qa-web/runs/{entry.name}/state.json')
        if not state_path.exists():
            continue
        try:
            saved = _read_json(state_path)
        except (OSError, QaError):
            continue
        if isinstance(saved, dict) and saved.get("environment_run_id") == environment_run_id:
            return entry.name
    return None


def _verify_replay_environment(project: pathlib.Path, paths: dict[str, str],
                               read_destination: Callable, environment_run_id: str,
                               base_url: str, source_environment_id: Any) -> dict[str, Any]:
    """Require a live, newly allocated environment before opening a replay.

    A saved READY status is only a claim: the recorded processes must still
    match their saved identity, the replay base URL must belong to this
    environment, and the source environment is never reusable. Environment
    occupancy is enforced where the run is created.
    """
    environment = _environment_state(project, paths, read_destination, environment_run_id)
    if environment is None:
        raise QaError("the new managed environment run is missing; start it first")
    if environment.get("run_id") != environment_run_id:
        raise QaError("the new managed environment state belongs to another run")
    if environment.get("project_root") != str(project.resolve()):
        raise QaError("the new managed environment belongs to another project")
    if environment_run_id == source_environment_id:
        raise QaError("the source environment cannot be reused; start a new environment with separate test data")
    if environment.get("status") != "READY":
        raise QaError("the new managed environment is not READY; replay requires a verified stand")
    services = environment.get("services")
    if not isinstance(services, list) or not services:
        raise QaError("the new managed environment has no recorded services")
    endpoints: list[tuple[str, int]] = []
    for service in services:
        if not isinstance(service, dict):
            raise QaError("the new managed environment service records are invalid")
        identifier = service.get("id")
        if service.get("status") != "READY":
            raise QaError(f"environment service {identifier} is not READY; the saved state is stale")
        pid, identity = service.get("pid"), service.get("identity")
        if type(pid) is not int or not isinstance(identity, dict):
            raise QaError(f"environment service {identifier} has no confirmed process identity")
        current = devcrew_qa_environment._process_identity(pid)
        if current is None or not devcrew_qa_environment._same_process(current, identity):
            raise QaError(f"environment service {identifier} is no longer the verified process; "
                          "start a fresh environment")
        endpoint = _service_endpoint(service.get("readiness_url"))
        if endpoint is not None:
            endpoints.append(endpoint)
    if not endpoints:
        raise QaError("the new managed environment does not record readiness addresses; "
                      "start a fresh environment")
    target = _loopback(base_url)
    if (target["host"], target["port"]) not in endpoints:
        raise QaError("base_url does not belong to this environment; use its recorded service address")
    return environment


def _cleanup_status(project: pathlib.Path, paths: dict[str, str], read_destination: Callable,
                    environment_run_id: str) -> str:
    state = _environment_state(project, paths, read_destination, environment_run_id)
    if state is None:
        return "MISSING"
    if state.get("status") == "STOPPED" and not state.get("cleanup_error"):
        return "COMPLETE"
    return "INCOMPLETE"


def _stop_environment(project: pathlib.Path, paths: dict[str, str], read_destination: Callable,
                      environment_run_id: str) -> None:
    """Stop only the environment bound to this run, keeping record output clean."""
    import contextlib
    import io
    import types

    import devcrew_qa_environment

    args = types.SimpleNamespace(qa_environment_operation="stop", run_id=environment_run_id)
    try:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            devcrew_qa_environment.command(args, project, paths, read_destination, read_destination)
    except (OSError, ValueError, KeyError, TypeError):
        return


def _finalize_cleanup(project: pathlib.Path, paths: dict[str, str], read_destination: Callable,
                      environment_run_id: str) -> str:
    """Read the live backend first, then release the bound managed environment."""
    cleanup = _cleanup_status(project, paths, read_destination, environment_run_id)
    environment = _environment_state(project, paths, read_destination, environment_run_id)
    if environment is not None and environment.get("status") == "READY":
        _stop_environment(project, paths, read_destination, environment_run_id)
        cleanup = _cleanup_status(project, paths, read_destination, environment_run_id)
    return cleanup


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 33 or data[:8] != PNG_SIGNATURE or data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    if not 1 <= width <= 20000 or not 1 <= height <= 20000:
        return None
    return width, height


def _substitute(value: Any, token: str, viewport_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("{{RUN_TOKEN}}", token).replace("{{VIEWPORT_ID}}", viewport_id)
    return value


def _readback(profile: dict[str, Any], scenario_id: str, viewport_id: str, object_id: str,
              token: str) -> dict[str, Any]:
    readback = profile["readback"]
    path = readback["path"].replace("{id}", object_id)
    expected = {name: _substitute(value, token, viewport_id)
                for name, value in readback["expect_json"].items()}
    probe = {"id": "readback", "path": path, "expected_status": readback["expected_status"],
             "expect_json": expected}
    request_profile = {"host": profile["host"], "port": profile["port"],
                       "timeout_seconds": profile["timeout_seconds"]}
    status, document, error = _request(request_profile, probe)
    fields = [{"field": name, "matched": error is None and status == readback["expected_status"]
               and _json_value_matches(_field(document, name), value)}
              for name, value in expected.items()]
    if error == "unavailable":
        outcome, reason = "BLOCKED", "backend_unavailable"
    elif error:
        outcome, reason = "FAIL", f"readback_{error}"
    elif status != readback["expected_status"]:
        outcome, reason = "FAIL", "backend_record_missing"
    elif any(not row["matched"] for row in fields):
        outcome, reason = "FAIL", "backend_record_mismatch"
    else:
        outcome, reason = "PASS", None
    return {"object_id": object_id, "status_code": status, "fields": fields,
            "outcome": outcome, "reason": reason}


def _validate_evidence(value: Any, state: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    _exact_keys(value, {"schema_version", "run_id", "scenario_sha256", "browser", "scenarios"},
                optional={"blocked"})
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise QaError("unsupported web evidence schema version")
    if value["run_id"] != state["run_id"]:
        raise QaError("web evidence belongs to another run")
    if value["scenario_sha256"] != state["scenario_sha256"]:
        raise QaError("web scenario changed after the run started")
    browser = value["browser"]
    _exact_keys(browser, {"tool"}, optional={"version"})
    if not isinstance(browser["tool"], str) or not 1 <= len(browser["tool"]) <= 64:
        raise QaError("browser tool must be a bounded string")
    if "version" in browser and (not isinstance(browser["version"], str) or len(browser["version"]) > 64):
        raise QaError("browser version must be a bounded string")
    viewports = {item["id"]: item for item in state["viewports"]}
    scenarios = {item["id"]: item for item in state["scenarios"]}
    blocked = None
    if "blocked" in value:
        blocked = value["blocked"]
        _exact_keys(blocked, {"reason"}, optional={"detail"})
        if blocked["reason"] not in BLOCKED_REASONS:
            raise QaError("unsupported blocked reason")
        if "detail" in blocked and (not isinstance(blocked["detail"], str) or len(blocked["detail"]) > 500):
            raise QaError("blocked detail must be a bounded string")
        if value["scenarios"]:
            raise QaError("a blocked web run must not report scenario results")
        return blocked, []
    if not isinstance(value["scenarios"], list):
        raise QaError("web evidence scenarios must be a list")
    seen: set[str] = set()
    normalized = []
    for scenario in value["scenarios"]:
        _exact_keys(scenario, {"id", "viewports"})
        identifier = scenario["id"]
        if identifier not in scenarios or identifier in seen:
            raise QaError("web evidence scenario identity is invalid")
        seen.add(identifier)
        if not isinstance(scenario["viewports"], dict) or set(scenario["viewports"]) != set(viewports):
            raise QaError("web evidence must cover every viewport for each scenario")
        per_viewport = {}
        for viewport_id, evidence in scenario["viewports"].items():
            keys = {"observations", "checks", "screenshots"}
            if scenarios[identifier]["kind"] == "e2e":
                keys |= {"ui_saved", "object_id"}
            _exact_keys(evidence, keys)
            observations = evidence["observations"]
            if (not isinstance(observations, list) or len(observations) > 20
                    or any(not isinstance(item, str) or len(item) > 500 for item in observations)):
                raise QaError("observations must be bounded strings")
            checks = evidence["checks"]
            expected_checks = scenarios[identifier]["checks"]
            if not isinstance(checks, list) or len(checks) != len(expected_checks):
                raise QaError("web evidence check coverage is incomplete")
            normalized_checks = []
            seen_checks = set()
            for check in checks:
                _exact_keys(check, {"id", "expected", "actual"})
                if check["id"] not in expected_checks or check["id"] in seen_checks:
                    raise QaError("web evidence check identity is invalid")
                seen_checks.add(check["id"])
                if not _scalar(check["expected"]) or not _scalar(check["actual"]):
                    raise QaError("web evidence check values must be bounded scalars")
                normalized_checks.append({"id": check["id"], "expected": check["expected"],
                                          "actual": check["actual"],
                                          "matched": _json_value_matches(check["actual"], check["expected"])})
            if seen_checks != set(expected_checks):
                raise QaError("web evidence check coverage is incomplete")
            screenshots = evidence["screenshots"]
            _exact_keys(screenshots, {"before", "after"})
            if any(not isinstance(screenshots[phase], str) or not screenshots[phase] for phase in ("before", "after")):
                raise QaError("screenshot paths must be non-empty strings")
            entry = {"observations": list(observations), "checks": normalized_checks,
                     "screenshots": dict(screenshots)}
            if scenarios[identifier]["kind"] == "e2e":
                if type(evidence["ui_saved"]) is not bool:
                    raise QaError("ui_saved must be a boolean")
                if not isinstance(evidence["object_id"], str) or not OBJECT_ID_RE.fullmatch(evidence["object_id"]):
                    raise QaError("object_id must be a bounded identifier")
                entry["ui_saved"] = evidence["ui_saved"]
                entry["object_id"] = evidence["object_id"]
            per_viewport[viewport_id] = entry
        normalized.append({"id": identifier, "viewports": per_viewport})
    if seen != set(scenarios):
        raise QaError("web evidence scenario coverage is incomplete")
    return None, normalized


def _copy_screenshot(project: pathlib.Path, destination: Callable, run_root: str,
                     scenario_id: str, viewport: dict[str, Any], phase: str,
                     source: str) -> tuple[dict[str, Any] | None, str | None]:
    path = pathlib.Path(source).expanduser()
    if fs.is_link(path) or not path.is_file():
        return None, "missing_screenshot"
    try:
        data = path.read_bytes()
    except OSError:
        return None, "missing_screenshot"
    if len(data) > MAX_SCREENSHOT_BYTES:
        return None, "screenshot_too_large"
    dimensions = _png_dimensions(data)
    if dimensions is None:
        return None, "invalid_screenshot"
    if dimensions != (viewport["width"], viewport["height"]):
        return None, "viewport_mismatch"
    relative = f"{run_root}/screenshots/{scenario_id}-{viewport['id']}-{phase}.png"
    target = destination(project, relative)
    if target.exists():
        raise QaError("refusing to overwrite immutable QA evidence")
    _atomic_write(target, data)
    return {"phase": phase, "file": relative, "sha256": hashlib.sha256(data).hexdigest(),
            "width": dimensions[0], "height": dimensions[1]}, None


def _derive(observations: list[dict[str, Any]], blocked: bool, cleanup: str,
            planned: int) -> tuple[str, int]:
    if blocked:
        return "BLOCKED", 0
    if any(item["outcome"] == "FAIL" for item in observations):
        return "FAIL", sum(item["outcome"] == "PASS" for item in observations)
    if (any(item["outcome"] == "BLOCKED" for item in observations)
            or len(observations) != planned or cleanup != "COMPLETE"):
        return "BLOCKED", sum(item["outcome"] == "PASS" for item in observations)
    return "PASS", sum(item["outcome"] == "PASS" for item in observations)


def _scenario_status(observations: list[dict[str, Any]]) -> str:
    if any(item["outcome"] == "FAIL" for item in observations):
        return "FAIL"
    if any(item["outcome"] == "BLOCKED" for item in observations):
        return "BLOCKED"
    return "PASS" if observations else "NOT_RUN"


def _defects(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive stable, repeatable defect identities from saved observations."""
    found: dict[str, dict[str, Any]] = {}

    def record(key: str, scenario_id: str, viewport_id: str, reason: Any,
               check: dict[str, Any] | None = None) -> None:
        row = found.setdefault(key, {"id": key, "scenario_id": scenario_id,
                                     "reason": reason or "failure", "viewports": []})
        if viewport_id not in row["viewports"]:
            row["viewports"].append(viewport_id)
        if check is not None:
            row["check_id"] = check["id"]
            row["expected"] = check["expected"]
            row["actual"] = check["actual"]

    for observation in evidence["observations"]:
        if observation["outcome"] == "PASS":
            continue
        scenario_id = observation["scenario_id"]
        viewport_id = observation["viewport_id"]
        readback = observation.get("readback")
        if readback is not None and readback.get("outcome") != "PASS":
            record(f"{scenario_id}:readback", scenario_id, viewport_id, readback.get("reason"))
            continue
        failed = [check for check in observation["checks"] if not check["matched"]]
        if failed:
            for check in failed:
                record(f"{scenario_id}:{check['id']}", scenario_id, viewport_id,
                       observation.get("reason"), check)
        else:
            record(f"{scenario_id}:{observation.get('reason') or observation['outcome'].lower()}",
                   scenario_id, viewport_id, observation.get("reason"))
    for row in found.values():
        row["viewports"] = sorted(row["viewports"])
    return [found[key] for key in sorted(found)]


def _validate_replay(value: Any, snapshot: dict[str, Any]) -> None:
    _exact_keys(value, {"source_run_id", "source_result_sha256", "source_scenario_sha256",
                        "source_status", "selection", "isolated"}, optional={"source_defect"})
    if (not isinstance(value["source_run_id"], str) or not RUN_ID_RE.fullmatch(value["source_run_id"])
            or not isinstance(value["source_scenario_sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["source_scenario_sha256"])):
        raise QaError("saved replay provenance is invalid")
    if value["source_status"] not in ("PASS", "FAIL", "BLOCKED", "RUNNING"):
        raise QaError("saved replay source status is invalid")
    result_digest = value["source_result_sha256"]
    if value["source_status"] == "RUNNING":
        if result_digest is not None or value["isolated"] is not True:
            raise QaError("an incomplete replay source must be marked isolated")
    elif not isinstance(result_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", result_digest):
        raise QaError("saved replay result digest is invalid")
    if type(value["isolated"]) is not bool:
        raise QaError("saved replay isolation flag is invalid")
    selection = value["selection"]
    _exact_keys(selection, {"kind", "id", "scenario_id"})
    if (selection["kind"] not in ("scenario", "defect")
            or not isinstance(selection["id"], str)
            or not (ID_RE.fullmatch(selection["id"]) if selection["kind"] == "scenario"
                    else DEFECT_ID_RE.fullmatch(selection["id"]))
            or not isinstance(selection["scenario_id"], str)
            or selection["scenario_id"] not in {item["id"] for item in snapshot["scenarios"]}):
        raise QaError("saved replay selection is invalid")
    if "source_defect" in value:
        defect = value["source_defect"]
        _exact_keys(defect, {"id", "scenario_id", "reason", "viewports"},
                    optional={"check_id", "expected", "actual"})
        if (defect["id"] != selection["id"] or defect["scenario_id"] != selection["scenario_id"]
                or selection["kind"] != "defect"
                or not isinstance(defect["reason"], str) or len(defect["reason"]) > 500
                or not isinstance(defect["viewports"], list) or not defect["viewports"]
                or any(not isinstance(item, str) for item in defect["viewports"])
                or not set(defect["viewports"]) <= {item["id"] for item in snapshot["viewports"]}):
            raise QaError("saved replay defect is invalid")
        for key in ("check_id", "expected", "actual"):
            if key in defect and not _scalar(defect[key]):
                raise QaError("saved replay defect value is invalid")
    elif selection["kind"] == "defect":
        raise QaError("saved replay defect detail is missing")


def _markdown(result: dict[str, Any], evidence: dict[str, Any]) -> str:
    lines = [f"# Web QA run {result['run_id']}", "",
             f"- Status: **{result['status']}**",
             f"- Coverage: {result['completed_count']}/{result['planned_count']}",
             f"- Cleanup: **{result['cleanup']}**",
             f"- Managed environment: `{result['environment_run_id']}`"]
    if result.get("replay"):
        replay = result["replay"]
        lines.append(f"- Replay of: `{replay['source_run_id']}` (source **{replay['source_status']}**"
                     + (", isolated attempt" if replay["isolated"] else "") + ")")
        lines.append(f"- Replay selection: **{replay['selection']['kind']}** "
                     f"`{replay['selection']['id']}`")
    if evidence.get("blocked"):
        lines += ["", f"- Blocked: **{evidence['blocked']['reason']}**"]
        if evidence["blocked"].get("detail"):
            lines.append(f"  - {evidence['blocked']['detail']}")
        lines.append("")
        return "\n".join(lines)
    lines += ["", "## Scenarios", ""]
    by_scenario: dict[str, list[dict[str, Any]]] = {}
    for observation in evidence["observations"]:
        by_scenario.setdefault(observation["scenario_id"], []).append(observation)
    for scenario in result["scenario_summaries"]:
        lines.append(f"### `{scenario['id']}` — **{scenario['status']}**")
        lines.append("")
        lines.append(scenario["title"])
        lines.append("")
        lines.append("Steps:")
        for step in scenario["steps"]:
            lines.append(f"1. {step}")
        lines.append("")
        for observation in by_scenario.get(scenario["id"], []):
            lines.append(f"- Viewport `{observation['viewport_id']}` "
                         f"({observation['width']}x{observation['height']}): **{observation['outcome']}**"
                         + (f" — {observation['reason']}" if observation.get("reason") else ""))
            for check in observation["checks"]:
                lines.append(f"  - `{check['id']}`: expected {check['expected']!r}, "
                             f"actual {check['actual']!r} ({'matched' if check['matched'] else 'mismatch'})")
            if observation.get("readback"):
                readback = observation["readback"]
                lines.append(f"  - backend read `{readback['object_id']}`: HTTP "
                             f"{readback['status_code'] if readback['status_code'] is not None else 'unavailable'}")
                for check in readback["fields"]:
                    lines.append(f"    - `{check['field']}`: {'matched' if check['matched'] else 'mismatch'}")
            for shot in observation["screenshots"]:
                lines.append(f"  - screenshot `{shot['phase']}`: `{shot['file']}` "
                             f"({shot['width']}x{shot['height']}, sha256 {shot['sha256'][:12]}…)")
        lines.append("")
        if scenario["status"] in ("FAIL", "BLOCKED"):
            lines.append("Reproduce with the steps above and the saved screenshots.")
            lines.append("")
    return "\n".join(lines)


READ_ONLY_OPERATIONS = ("find", "list", "report")


def _dispatch(operation: str, args: Any, project: pathlib.Path, paths: dict[str, str],
              destination: Callable, read_destination: Callable) -> int:
    if operation == "start":
        return _start(args, project, paths, destination, read_destination)
    if operation == "replay-start":
        return _replay_start(args, project, paths, destination, read_destination)
    if operation == "record":
        return _record(args, project, paths, destination, read_destination)
    if operation == "find":
        return _find(args, project, paths, destination, read_destination)
    if operation == "list":
        return _list(project, paths, destination, read_destination)
    if operation == "report":
        return _report(args, project, paths, destination, read_destination)
    raise QaError("unknown web QA operation")


def command(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
            read_destination: Callable) -> int:


    operation = args.qa_web_operation
    try:
        # Discovery and report reading must never create a lock, directory, or
        # any other file. Saved records are published with atomic renames, so a
        # reader sees a complete record or is told it is not available yet.
        if operation in READ_ONLY_OPERATIONS:
            return _dispatch(operation, args, project, paths, destination, read_destination)
        lock_path = destination(project, _paths(paths, "0" * 32)["lock"])
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(fs.open_regular(lock_path, os.O_CREAT | os.O_RDWR), "r+b") as lock:
            fs.lock(lock.fileno())
            try:
                return _dispatch(operation, args, project, paths, destination, read_destination)
            finally:
                fs.unlock(lock.fileno())
    except (OSError, QaError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False),
              file=__import__("sys").stderr)
        return 2


def _start(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
           read_destination: Callable) -> int:
    raw = _read_json(pathlib.Path(args.scenario).expanduser())
    profile, viewports, scenarios = _validate_scenario(raw)
    environment = _environment_state(project, paths, read_destination, profile["environment_run_id"])
    if environment is None:
        raise QaError("the managed environment run is missing; start it with qa environment-start")
    if environment.get("status") != "READY":
        raise QaError("the managed environment is not READY; a web run cannot start on an unverified stand")
    snapshot = {"schema_version": SCHEMA_VERSION, "profile": profile,
                "viewports": viewports, "scenarios": scenarios}
    run_id, token, _, _, scenario_hash, _ = _create_run(project, paths, destination,
                                                        read_destination, snapshot)
    print(json.dumps({"run_id": run_id, "token": token, "scenario_sha256": scenario_hash,
                      "base_url": profile["base_url"] if "base_url" in profile else f"http://{profile['host']}:{profile['port']}",
                      "viewports": viewports,
                      "scenarios": [{"id": item["id"], "kind": item["kind"], "title": item["title"],
                                     "steps": item["steps"], "checks": item["checks"]}
                                    for item in scenarios],
                      "environment_run_id": profile["environment_run_id"]},
                     ensure_ascii=False))
    return 0


def _find(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
          read_destination: Callable) -> int:
    selectors = [name for name in ("run_id", "scenario", "defect") if getattr(args, name, None)]
    if len(selectors) > 1 or (selectors and getattr(args, "latest", False)):
        raise QaError("choose one of --run-id, --scenario or --defect")
    index = _load_index(read_destination(project, _paths(paths, "0" * 32)["index"]))
    if args.run_id:
        candidates = [args.run_id]
    else:
        candidates = [entry["run_id"] for entry in index["runs"]]
    matches = []
    for run_id in candidates:
        result, evidence = _verify_run(project, paths, destination, read_destination, run_id)
        if args.scenario and not any(item["id"] == args.scenario for item in result["scenario_summaries"]):
            continue
        defects = _defects(evidence)
        if args.defect and not any(item["id"] == args.defect for item in defects):
            continue
        matches.append({"run_id": result["run_id"], "status": result["status"],
                        "created_at": result["created_at"],
                        "scenarios": result["scenario_summaries"],
                        "defects": defects, "replay": result.get("replay"),
                        "report": _paths(paths, result["run_id"])["report"]})
        if not selectors:
            break
    print(json.dumps({"schema_version": SCHEMA_VERSION, "matches": matches,
                      "count": len(matches), "ambiguous": len(matches) > 1},
                     ensure_ascii=False))
    return 0


def _replay_start(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
                  read_destination: Callable) -> int:
    source_id = args.source_run_id
    if not isinstance(source_id, str) or not RUN_ID_RE.fullmatch(source_id):
        raise QaError("invalid source run ID")
    if bool(args.scenario) == bool(args.defect):
        raise QaError("choose exactly one of --scenario or --defect")
    source_layout = _paths(paths, source_id)
    source_state_path = read_destination(project, source_layout["state"])
    if not source_state_path.exists():
        raise QaError("source web QA run is missing")
    source_state = _load_state(source_state_path, source_id)
    if source_state["project_root"] != str(project.resolve()):
        raise QaError("source web QA run belongs to another project")
    source_result = None
    source_evidence = None
    if source_state["status"] == "COMPLETED":
        source_result, source_evidence = _verify_run(project, paths, destination,
                                                     read_destination, source_id)
        source_status = source_result["status"]
    else:
        if not args.isolated:
            raise QaError("source web QA run is incomplete; verify its side effects or replay with --isolated")
        source_status = "RUNNING"
    snapshot_path = read_destination(project, f'{source_layout["snapshot"]}/scenario.json')
    source_snapshot = _read_json(snapshot_path)
    if _digest(source_snapshot) != source_state["scenario_sha256"]:
        raise QaError("source web QA scenario snapshot integrity check failed")
    scenario_id = args.scenario
    defect = None
    if args.defect:
        if source_evidence is None:
            raise QaError("a defect replay requires a completed source run")
        available = _defects(source_evidence)
        defect = next((item for item in available if item["id"] == args.defect), None)
        if defect is None:
            names = ", ".join(item["id"] for item in available) or "none"
            raise QaError(f"source run has no defect {args.defect}; available: {names}")
        scenario_id = defect["scenario_id"]
    selected = [item for item in source_snapshot["scenarios"] if item["id"] == scenario_id]
    if not selected:
        raise QaError("the selected scenario is not part of the source run")

    environment_run_id = args.environment_run_id
    if not isinstance(environment_run_id, str) or not RUN_ID_RE.fullmatch(environment_run_id):
        raise QaError("invalid environment run ID")
    _verify_replay_environment(project, paths, read_destination, environment_run_id,
                               args.base_url, source_state.get("environment_run_id"))

    source_profile = source_snapshot["profile"]
    raw_profile = {"base_url": args.base_url, "environment_run_id": environment_run_id,
                   "timeout_seconds": source_profile.get("timeout_seconds", 5)}
    if "readback" in source_profile and any(item["kind"] == "e2e" for item in selected):
        raw_profile["readback"] = source_profile["readback"]
    profile, viewports, scenarios = _validate_scenario(
        {"schema_version": SCHEMA_VERSION, "profile": raw_profile,
         "viewports": source_snapshot["viewports"], "scenarios": selected})
    selection = {"kind": "defect" if defect is not None else "scenario",
                 "id": args.defect if defect is not None else scenario_id,
                 "scenario_id": scenario_id}
    replay = {"source_run_id": source_id,
              "source_result_sha256": source_result["result_sha256"] if source_result else None,
              "source_scenario_sha256": source_state["scenario_sha256"],
              "source_status": source_status,
              "selection": selection,
              "isolated": bool(args.isolated)}
    if defect is not None:
        replay["source_defect"] = defect
    snapshot = {"schema_version": SCHEMA_VERSION, "profile": profile,
                "viewports": viewports, "scenarios": scenarios}
    run_id, token, _, _, scenario_hash, _ = _create_run(project, paths, destination,
                                                        read_destination, snapshot, replay)
    changed = [name for name in ("host", "port", "environment_run_id")
               if source_profile.get(name) != profile.get(name)]
    print(json.dumps({"run_id": run_id, "token": token, "scenario_sha256": scenario_hash,
                      "base_url": f"http://{profile['host']}:{profile['port']}",
                      "viewports": viewports,
                      "scenarios": [{"id": item["id"], "kind": item["kind"], "title": item["title"],
                                     "steps": item["steps"], "checks": item["checks"]}
                                    for item in scenarios],
                      "environment_run_id": environment_run_id,
                      "replay_of": source_id, "source_status": source_status,
                      "source_result_sha256": replay["source_result_sha256"],
                      "source_scenario_sha256": replay["source_scenario_sha256"],
                      "selection": selection, "isolated": replay["isolated"],
                      "profile_changes": (["base_url"] if "host" in changed or "port" in changed else [])
                                         + (["environment_run_id"] if "environment_run_id" in changed else []),
                      "required_tools": ["browser", "desktop_viewport", "mobile_viewport"] +
                                        (["backend_readback"] if "readback" in profile else [])},
                     ensure_ascii=False))
    return 0


def _record(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
            read_destination: Callable) -> int:
    run_id = args.run_id
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise QaError("invalid run ID")
    layout = _paths(paths, run_id)
    state_path = read_destination(project, layout["state"])
    state = _load_state(state_path, run_id)
    if state["status"] != "RUNNING":
        raise QaError("this web QA run is already complete and cannot be rewritten")
    value = _read_json(pathlib.Path(args.evidence).expanduser())
    blocked, scenarios = _validate_evidence(value, state)
    snapshot_path = read_destination(project, f'{layout["snapshot"]}/scenario.json')
    snapshot = _read_json(snapshot_path)
    if _digest(snapshot) != state["scenario_sha256"]:
        raise QaError("web scenario snapshot integrity check failed")
    if blocked is None and len(scenarios) != len(state["scenarios"]):
        raise QaError("web evidence scenario coverage is incomplete")

    profile = snapshot["profile"]
    viewports = {item["id"]: item for item in state["viewports"]}
    scenario_defs = {item["id"]: item for item in state["scenarios"]}
    observations: list[dict[str, Any]] = []
    if blocked is None:
        for scenario in scenarios:
            definition = scenario_defs[scenario["id"]]
            for viewport_id, evidence in scenario["viewports"].items():
                viewport = viewports[viewport_id]
                screenshots = []
                screenshot_reason = None
                for phase in ("before", "after"):
                    shot, reason = _copy_screenshot(project, destination, layout["run"],
                                                    scenario["id"], viewport, phase,
                                                    evidence["screenshots"][phase])
                    if shot is not None:
                        screenshots.append(shot)
                    elif screenshot_reason is None:
                        screenshot_reason = reason
                checks_ok = all(check["matched"] for check in evidence["checks"])
                readback = None
                reason = screenshot_reason
                if screenshot_reason is not None:
                    outcome = "BLOCKED"
                elif definition["kind"] == "e2e":
                    if not evidence["ui_saved"]:
                        outcome, reason = "FAIL", "ui_did_not_confirm_save"
                    else:
                        readback = _readback(profile, scenario["id"], viewport_id,
                                             evidence["object_id"], state["token"])
                        outcome, reason = readback["outcome"], readback["reason"]
                else:
                    outcome = "PASS" if checks_ok else "FAIL"
                    reason = None if checks_ok else "check_mismatch"
                if outcome == "PASS" and not checks_ok:
                    outcome, reason = "FAIL", "check_mismatch"
                if outcome == "PASS" and readback is not None and readback["outcome"] != "PASS":
                    outcome, reason = readback["outcome"], readback["reason"]
                observations.append({
                    "scenario_id": scenario["id"], "viewport_id": viewport_id,
                    "width": viewport["width"], "height": viewport["height"],
                    "outcome": outcome, "reason": reason,
                    "checks": evidence["checks"], "observations": evidence["observations"],
                    "screenshots": screenshots, "readback": readback,
                })
        planned = len(state["scenarios"]) * len(state["viewports"])
    else:
        planned = len(state["scenarios"]) * len(state["viewports"])
    cleanup = _finalize_cleanup(project, paths, read_destination, state["environment_run_id"])
    if blocked is None:
        status, completed = _derive(observations, False, cleanup, planned)
    else:
        status, completed = "BLOCKED", 0
    summaries = []
    for definition in state["scenarios"]:
        items = [item for item in observations if item["scenario_id"] == definition["id"]]
        summaries.append({"id": definition["id"], "kind": definition["kind"],
                          "title": definition["title"], "steps": definition["steps"],
                          "status": _scenario_status(items)})
    evidence = {"schema_version": SCHEMA_VERSION, "run_id": run_id,
                "scenario_sha256": state["scenario_sha256"], "profile_sha256": state["profile_sha256"],
                "browser": value["browser"], "viewports": state["viewports"],
                "observations": observations, "blocked": blocked}
    if "replay" in state:
        evidence["replay"] = state["replay"]
    evidence_bytes = _json_bytes(evidence)
    if len(evidence_bytes) > MAX_INPUT_BYTES:
        raise QaError("saved QA artifact exceeds the reader size limit")
    evidence_path = destination(project, layout["evidence"])
    if evidence_path.exists():
        raise QaError("refusing to overwrite immutable QA evidence")
    _atomic_write(evidence_path, evidence_bytes)
    result = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "project_root": str(project.resolve()),
              "created_at": _now(), "status": status,
              "scenario_sha256": state["scenario_sha256"], "profile_sha256": state["profile_sha256"],
              "evidence_sha256": hashlib.sha256(evidence_bytes).hexdigest(),
              "planned_count": planned, "completed_count": completed,
              "cleanup": cleanup, "environment_run_id": state["environment_run_id"],
              "scenario_summaries": summaries}
    if "replay" in state:
        result["replay"] = state["replay"]
    result["result_sha256"] = _digest(result)
    _atomic_write(destination(project, layout["result"]), _json_bytes(result))
    _atomic_write(destination(project, layout["report"]), _markdown(result, evidence).encode("utf-8"))
    state["status"] = "COMPLETED"
    state["result_sha256"] = result["result_sha256"]
    _atomic_write(state_path, _json_bytes(state))
    index_path = read_destination(project, layout["index"])
    index = _load_index(index_path)
    entry = {"run_id": run_id, "status": status, "created_at": result["created_at"],
             "result_sha256": result["result_sha256"]}
    if "replay" in state:
        entry["replay_of"] = state["replay"]["source_run_id"]
    index["runs"].insert(0, entry)
    _atomic_write(destination(project, layout["index"]), _json_bytes(index))
    print(json.dumps({"status": status, "run_id": run_id, "cleanup": cleanup,
                      "coverage": f"{completed}/{planned}", "report": layout["report"]},
                     ensure_ascii=False))
    return 0


def _verify_run(project: pathlib.Path, paths: dict[str, str], destination: Callable,
                read_destination: Callable, run_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise QaError("invalid run ID")
    layout = _paths(paths, run_id)
    result_path = read_destination(project, layout["result"])
    evidence_path = read_destination(project, layout["evidence"])
    report_path = read_destination(project, layout["report"])
    if not result_path.exists() or not evidence_path.exists() or not report_path.exists():
        # A writer publishes result, evidence, and report before indexing the
        # run, so this is either an unpublished attempt or missing evidence.
        raise QaError("web QA run is not fully published; its saved evidence is unavailable")
    result, evidence = _read_json(result_path), _read_json(evidence_path)
    _exact_keys(result, {"schema_version", "run_id", "project_root", "created_at", "status",
                         "scenario_sha256", "profile_sha256", "evidence_sha256", "planned_count",
                         "completed_count", "cleanup", "environment_run_id", "scenario_summaries",
                         "result_sha256"}, optional={"replay"})
    _exact_keys(evidence, {"schema_version", "run_id", "scenario_sha256", "profile_sha256",
                           "browser", "viewports", "observations", "blocked"}, optional={"replay"})
    if (type(result["schema_version"]) is not int or type(evidence["schema_version"]) is not int
            or result["schema_version"] != SCHEMA_VERSION or evidence["schema_version"] != SCHEMA_VERSION):
        raise QaError("unsupported saved web QA schema")
    digest = result.pop("result_sha256")
    if digest != _digest(result):
        raise QaError("web QA result integrity check failed")
    result["result_sha256"] = digest
    if (result["run_id"] != run_id or evidence["run_id"] != run_id
            or result["project_root"] != str(project.resolve())
            or result["status"] not in ("PASS", "FAIL", "BLOCKED")
            or result["cleanup"] not in ("COMPLETE", "INCOMPLETE", "MISSING")
            or type(result["planned_count"]) is not int or type(result["completed_count"]) is not int
            or result["completed_count"] < 0 or result["completed_count"] > result["planned_count"]):
        raise QaError("invalid saved web QA result")
    if hashlib.sha256(evidence_path.read_bytes()).hexdigest() != result["evidence_sha256"]:
        raise QaError("web QA evidence integrity check failed")
    snapshot_path = read_destination(project, f'{layout["snapshot"]}/scenario.json')
    snapshot = _read_json(snapshot_path)
    if (_digest(snapshot) != result["scenario_sha256"]
            or _digest({"schema_version": SCHEMA_VERSION, "profile": snapshot["profile"]}) != result["profile_sha256"]):
        raise QaError("web QA input snapshot integrity check failed")
    if (evidence["scenario_sha256"] != result["scenario_sha256"]
            or evidence["profile_sha256"] != result["profile_sha256"]):
        raise QaError("web QA evidence does not match its input snapshots")
    if result.get("replay") != evidence.get("replay"):
        raise QaError("web QA replay provenance does not match its evidence")
    if result.get("replay") is not None:
        _validate_replay(result["replay"], snapshot)
    if evidence["blocked"] is not None:
        if result["status"] != "BLOCKED":
            raise QaError("a blocked web run cannot report a completed status")
    else:
        planned = len(snapshot["scenarios"]) * len(snapshot["viewports"])
        if (len(evidence["observations"]) != planned or result["planned_count"] != planned
                or result["completed_count"] != sum(item["outcome"] == "PASS" for item in evidence["observations"])):
            raise QaError("web QA evidence coverage is incomplete")
        derived, _ = _derive(evidence["observations"], False, result["cleanup"], planned)
        if derived != result["status"]:
            raise QaError("web QA status does not match its observations")
    for observation in evidence["observations"]:
        for shot in observation["screenshots"]:
            path = read_destination(project, shot["file"])
            data = path.read_bytes()
            if (hashlib.sha256(data).hexdigest() != shot["sha256"]
                    or _png_dimensions(data) != (shot["width"], shot["height"])):
                raise QaError("web QA screenshot integrity check failed")
    if evidence["blocked"] is None and result["status"] != "PASS":
        # A failure or block still needs complete screenshots for the recorded observations.
        for observation in evidence["observations"]:
            if len(observation["screenshots"]) != 2 and observation["reason"] not in (
                    "missing_screenshot", "invalid_screenshot", "viewport_mismatch", "screenshot_too_large"):
                raise QaError("web QA screenshot coverage is incomplete")
    if report_path.read_bytes() != _markdown(result, evidence).encode("utf-8"):
        raise QaError("web QA Markdown report does not match verified evidence")
    return result, evidence


def _list(project: pathlib.Path, paths: dict[str, str], destination: Callable,
          read_destination: Callable) -> int:
    layout = _paths(paths, "0" * 32)
    index = _load_index(read_destination(project, layout["index"]))
    entries = []
    for entry in index["runs"]:
        result, _ = _verify_run(project, paths, destination, read_destination, entry["run_id"])
        if entry["result_sha256"] != result["result_sha256"] or entry["status"] != result["status"]:
            raise QaError("web QA index does not match saved result")
        entries.append(entry)
    print(json.dumps({"schema_version": SCHEMA_VERSION, "runs": entries}, ensure_ascii=False))
    return 0


def _report(args: Any, project: pathlib.Path, paths: dict[str, str], destination: Callable,
            read_destination: Callable) -> int:
    result, evidence = _verify_run(project, paths, destination, read_destination, args.run_id)
    layout = _paths(paths, args.run_id)
    index = _load_index(read_destination(project, _paths(paths, "0" * 32)["index"]))
    entry = next((item for item in index["runs"] if item["run_id"] == args.run_id), None)
    if (entry is None or entry["result_sha256"] != result["result_sha256"]
            or entry["status"] != result["status"]):
        raise QaError("web QA run is not present in the project index")
    print(json.dumps({"result": result, "evidence": evidence,
                      "markdown": _markdown(result, evidence)}, ensure_ascii=False))
    return 0
