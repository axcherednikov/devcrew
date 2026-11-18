from __future__ import annotations

import hashlib
import http.client
import json
import os
import pathlib
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"
FIXTURE = ROOT / "tests" / "fixtures" / "qa_web_app.py"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

import devcrew_qa_environment  # noqa: E402
import qa_web_app  # noqa: E402
from loopback_http import ThreadingLoopbackHTTPServer  # noqa: E402


def _png(width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xcc\xdd\xee" * width for _ in range(height))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _tree_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class Stand:
    def __init__(self, optimistic: bool = False):
        self.store = qa_web_app.Store(optimistic)
        handler = type("Handler", (qa_web_app.Handler,), {"store": self.store})
        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def create(self, name: str, label: str) -> dict:
        body = json.dumps({"name": name, "label": label})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request("POST", "/api/items", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        if response.status != 201:
            raise AssertionError(f"fixture create failed: {response.status} {result}")
        return result

    def stop(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


class LiveEnvironment:
    """A real backend process recorded as a managed QA environment.

    The saved state looks exactly like an `environment-start` record: readiness
    address plus the verified PID identity that replay must re-check.
    """

    def __init__(self, project: pathlib.Path, *, optimistic: bool = False):
        self.project = project
        self.run_id = uuid.uuid4().hex
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            self.port = probe.getsockname()[1]
        command = [sys.executable, "-B", str(FIXTURE), "--port", str(self.port)]
        if optimistic:
            command.append("--optimistic")
        self.process = subprocess.Popen(command, cwd=project, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, start_new_session=True)

    def wait_until_ready(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"fixture service exited with status {self.process.returncode}")
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.02)
        else:
            raise AssertionError("fixture service did not start")
        identity = devcrew_qa_environment._process_identity(self.process.pid)
        if identity is None:
            raise AssertionError("fixture service identity was not observable")
        self.write_state(status="READY", pid=self.process.pid, identity=identity)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def write_state(self, *, status, pid, identity):
        state = {"schema_version": 1, "run_id": self.run_id,
                 "project_root": str(self.project.resolve()), "status": status,
                 "services": [{"id": "web", "status": "READY" if status == "READY" else "STOPPED",
                               "pid": pid, "identity": identity,
                               "readiness_url": f"{self.base_url}/health"}],
                 "resources": []}
        path = (self.project / ".codex/checks/qa-environments/runs" / self.run_id / "state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def create(self, name: str, label: str) -> dict:
        body = json.dumps({"name": name, "label": label})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request("POST", "/api/items", body, {"Content-Type": "application/json"})
        response = connection.getresponse()
        result = json.loads(response.read())
        connection.close()
        if response.status != 201:
            raise AssertionError(f"fixture create failed: {response.status} {result}")
        return result

    def kill(self):
        """Simulate a crash: the state still claims READY."""
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        self.process.wait(timeout=10)

    def stop(self):
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


class ReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self.temp.name) / "project"
        (self.project / ".codex").mkdir(parents=True)
        (self.project / ".codex/devcrew.json").write_text(json.dumps({"schema_version": 1, "paths": {
            "context": "docs/context.md", "tasks": ".codex/tasks", "sessions": ".codex/sessions",
            "memory": "memory", "checks": ".codex/checks"}}))
        self.shots = pathlib.Path(self.temp.name) / "shots"
        self.shots.mkdir()
        self.stand = None
        self.environments: list[LiveEnvironment] = []
        self.environment_run_id = uuid.uuid4().hex
        self.set_environment(self.environment_run_id, "READY")

    def tearDown(self):
        if self.stand is not None:
            self.stand.stop()
        for environment in self.environments:
            environment.stop()
        self.temp.cleanup()

    def live_environment(self, *, optimistic: bool = False) -> LiveEnvironment:
        environment = LiveEnvironment(self.project, optimistic=optimistic)
        self.environments.append(environment)
        environment.wait_until_ready()
        return environment

    def set_environment(self, run_id: str, status: str, *, cleanup_error=None):
        state = {"schema_version": 1, "run_id": run_id, "status": status,
                 "project_root": str(self.project.resolve()),
                 "services": [{"id": "web", "status": "STOPPED" if status == "STOPPED" else "READY",
                               "pid": None, "identity": None,
                               "readiness_url": "http://127.0.0.1:1/health"}],
                 "resources": []}
        if cleanup_error:
            state["cleanup_error"] = cleanup_error
        path = (self.project / ".codex/checks/qa-environments/runs" / run_id / "state.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60)

    @staticmethod
    def error(result):
        try:
            return json.loads(result.stderr)["error"]
        except (json.JSONDecodeError, KeyError):
            return result.stderr

    def scenario(self, port, *, scenarios=None, readback=None, environment_run_id=None):
        profile = {"base_url": f"http://127.0.0.1:{port}",
                   "environment_run_id": environment_run_id or self.environment_run_id}
        if readback is not None:
            profile["readback"] = readback
        path = pathlib.Path(self.temp.name) / f"scenario-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({
            "schema_version": 1, "profile": profile,
            "viewports": [{"id": "desktop", "width": 1280, "height": 800},
                          {"id": "mobile", "width": 390, "height": 844}],
            "scenarios": scenarios or [{
                "id": "create-item", "kind": "e2e", "title": "Create an item",
                "steps": ["Fill the label", "Save", "Read the item back"],
                "checks": ["saved-message"]}],
        }))
        return path

    def readback(self):
        return {"path": "/api/items/{id}", "expected_status": 200,
                "expect_json": {"label": "{{RUN_TOKEN}}", "name": "note"}}

    def start(self, scenario_path):
        result = self.cli("qa", "web-start", "--project", self.project, "--scenario", scenario_path)
        self.assertEqual(result.returncode, 0, self.error(result))
        return json.loads(result.stdout)

    def screenshot(self, scenario_id, viewport, phase, width=None, height=None):
        path = self.shots / f"{scenario_id}-{viewport['id']}-{phase}-{uuid.uuid4().hex}.png"
        path.write_bytes(_png(width or viewport["width"], height or viewport["height"]))
        return str(path)

    def evidence(self, started, *, actuals, items=None, ui_saved=True, blocked=None):
        items = items or {}
        if blocked is not None:
            return {"schema_version": 1, "run_id": started["run_id"],
                    "scenario_sha256": started["scenario_sha256"],
                    "browser": {"tool": "test-browser"}, "blocked": blocked, "scenarios": []}
        payload = {"schema_version": 1, "run_id": started["run_id"],
                   "scenario_sha256": started["scenario_sha256"],
                   "browser": {"tool": "test-browser"}, "scenarios": []}
        for scenario in started["scenarios"]:
            entry = {"id": scenario["id"], "viewports": {}}
            for viewport in started["viewports"]:
                checks = []
                for check_id in scenario["checks"]:
                    expected, actual = actuals[(scenario["id"], viewport["id"], check_id)]
                    checks.append({"id": check_id, "expected": expected, "actual": actual})
                item = {"observations": ["observed the named state"], "checks": checks,
                        "screenshots": {
                            "before": self.screenshot(scenario["id"], viewport, "before"),
                            "after": self.screenshot(scenario["id"], viewport, "after")}}
                if scenario["kind"] == "e2e":
                    item["ui_saved"] = ui_saved
                    item["object_id"] = items[(scenario["id"], viewport["id"])]
                entry["viewports"][viewport["id"]] = item
            payload["scenarios"].append(entry)
        return payload

    def record(self, started, evidence):
        path = pathlib.Path(self.temp.name) / f"evidence-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps(evidence))
        return self.cli("qa", "web-record", "--project", self.project,
                        "--run-id", started["run_id"], "--evidence", path)

    def make_run(self, *, scenarios=None, actuals=None, optimistic=False):
        environment_run_id = uuid.uuid4().hex
        self.set_environment(environment_run_id, "READY")
        self.stand = Stand(optimistic)
        started = self.start(self.scenario(self.stand.port, scenarios=scenarios,
                                           readback=self.readback() if scenarios is None or any(
                                               item.get("kind") == "e2e" for item in scenarios) else None,
                                           environment_run_id=environment_run_id))
        actuals = actuals or {}
        items = {}
        for scenario in started["scenarios"]:
            for viewport in started["viewports"]:
                for check_id in scenario["checks"]:
                    expected = {"saved-message": "Saved", "error-message": "error",
                                "full-label": "full"}.get(check_id, check_id)
                    actuals.setdefault((scenario["id"], viewport["id"], check_id), (expected, expected))
                if scenario["kind"] == "e2e":
                    items[(scenario["id"], viewport["id"])] = self.stand.create(
                        "note", started["token"])["id"]
        self.set_environment(environment_run_id, "STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(result.returncode, 0, self.error(result))
        return started, json.loads(result.stdout)

    def replay(self, source_run_id, *, environment, scenario=None, defect=None, isolated=False,
               base_url=None):
        args = ["qa", "web-replay-start", "--project", self.project,
                "--source-run-id", source_run_id,
                "--environment-run-id", environment.run_id,
                "--base-url", base_url or environment.base_url]
        if scenario:
            args += ["--scenario", scenario]
        if defect:
            args += ["--defect", defect]
        if isolated:
            args += ["--isolated"]
        return self.cli(*args)

    def find(self, *args):
        return self.cli("qa", "web-find", "--project", self.project, *args)

    def test_find_latest_and_open_report_without_a_running_backend(self):
        started, recorded = self.make_run()
        self.assertEqual(recorded["status"], "PASS")
        self.stand.stop()
        self.stand = None
        self.set_environment(self.environment_run_id, "STOPPED")
        found = self.find()
        self.assertEqual(found.returncode, 0, self.error(found))
        payload = json.loads(found.stdout)
        self.assertEqual(payload["count"], 1)
        self.assertFalse(payload["ambiguous"])
        self.assertEqual(payload["matches"][0]["run_id"], started["run_id"])
        self.assertEqual(payload["matches"][0]["status"], "PASS")
        report = self.cli("qa", "web-report", "--project", self.project,
                          "--run-id", started["run_id"])
        self.assertEqual(report.returncode, 0, self.error(report))
        self.assertIn("Create an item", json.loads(report.stdout)["markdown"])

    def test_find_by_scenario_and_defect_reports_ambiguity(self):
        scenarios = [
            {"id": "create-item", "kind": "e2e", "title": "Create an item",
             "steps": ["Save", "Read back"], "checks": ["saved-message"]},
            {"id": "bad-name", "kind": "validation", "title": "Reject empty name",
             "steps": ["Save empty", "Look for an error"], "checks": ["error-message"]},
        ]
        actuals = {}
        for viewport in ("desktop", "mobile"):
            actuals[("bad-name", viewport, "error-message")] = ("error", "none")
        first, _ = self.make_run(scenarios=scenarios, actuals=actuals)
        self.stand.stop()
        self.stand = None
        second, _ = self.make_run(scenarios=scenarios, actuals=actuals)
        self.stand.stop()
        self.stand = None
        by_defect = self.find("--defect", "bad-name:error-message")
        self.assertEqual(by_defect.returncode, 0, self.error(by_defect))
        payload = json.loads(by_defect.stdout)
        self.assertEqual(payload["count"], 2)
        self.assertTrue(payload["ambiguous"])
        self.assertEqual({item["run_id"] for item in payload["matches"]},
                         {first["run_id"], second["run_id"]})
        by_scenario = json.loads(self.find("--scenario", "bad-name").stdout)
        self.assertEqual(by_scenario["count"], 2)

    def test_replay_scenario_uses_new_id_and_fresh_evidence(self):
        source, source_result = self.make_run()
        source_root = self.project / ".codex/checks/qa-web/runs" / source["run_id"]
        source_snapshot = self.project / ".codex/tasks/qa/snapshots" / source["run_id"]
        before = (_tree_digest(source_root), _tree_digest(source_snapshot))
        before_files = {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                        for path in sorted(source_root.rglob("*")) if path.is_file()}
        self.stand.stop()
        self.stand = None
        environment = self.live_environment()
        replay = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(replay.returncode, 0, self.error(replay))
        started = json.loads(replay.stdout)
        self.assertNotEqual(started["run_id"], source["run_id"])
        self.assertNotEqual(started["token"], source["token"])
        self.assertEqual(started["replay_of"], source["run_id"])
        self.assertEqual(started["selection"], {"kind": "scenario", "id": "create-item",
                                                "scenario_id": "create-item"})
        self.assertEqual([item["id"] for item in started["scenarios"]], ["create-item"])
        # Fresh data on the live stand, not a copy of the historical record.
        item = environment.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        recorded = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(recorded.returncode, 0, self.error(recorded))
        self.assertEqual(json.loads(recorded.stdout)["status"], "PASS")
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertEqual(report["result"]["status"], "PASS")
        self.assertEqual(report["result"]["cleanup"], "COMPLETE")
        self.assertEqual(report["result"]["replay"]["source_status"], "PASS")
        self.assertEqual(report["result"]["replay"]["source_run_id"], source["run_id"])
        self.assertIn("Replay of", report["markdown"])
        observations = report["evidence"]["observations"]
        self.assertEqual(len(observations), 2)
        for observation in observations:
            self.assertEqual(observation["readback"]["status_code"], 200)
            self.assertEqual(observation["readback"]["object_id"], item["id"])
            self.assertTrue(all(field["matched"] for field in observation["readback"]["fields"]))
            self.assertEqual(len(observation["screenshots"]), 2)
        # Recording stopped the bound live environment and confirmed cleanup.
        self.assertIsNotNone(environment.process.poll())
        # The source report, snapshots, screenshots, and evidence are untouched.
        self.assertEqual(before, (_tree_digest(source_root), _tree_digest(source_snapshot)))
        after_files = {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted(source_root.rglob("*")) if path.is_file()}
        self.assertEqual(before_files, after_files)
        old = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                  "--run-id", source["run_id"]).stdout)
        self.assertEqual(old["result"]["status"], source_result["status"])
        self.assertNotIn("replay", old["result"])

    def test_replay_defect_selects_the_failing_scenario_only(self):
        scenarios = [
            {"id": "create-item", "kind": "e2e", "title": "Create an item",
             "steps": ["Save", "Read back"], "checks": ["saved-message"]},
            {"id": "bad-name", "kind": "validation", "title": "Reject empty name",
             "steps": ["Save empty", "Look for an error"], "checks": ["error-message"]},
        ]
        actuals = {("bad-name", viewport, "error-message"): ("error", "none")
                   for viewport in ("desktop", "mobile")}
        source, _ = self.make_run(scenarios=scenarios, actuals=actuals)
        source_root = self.project / ".codex/checks/qa-web/runs" / source["run_id"]
        before = _tree_digest(source_root)
        environment = self.live_environment()
        replay = self.replay(source["run_id"], environment=environment,
                             defect="bad-name:error-message")
        self.assertEqual(replay.returncode, 0, self.error(replay))
        started = json.loads(replay.stdout)
        self.assertEqual([item["id"] for item in started["scenarios"]], ["bad-name"])
        self.assertEqual(started["selection"]["kind"], "defect")
        self.assertEqual(started["selection"]["id"], "bad-name:error-message")
        actuals = {("bad-name", viewport["id"], "error-message"): ("error", "none")
                   for viewport in started["viewports"]}
        recorded = self.record(started, self.evidence(started, actuals=actuals))
        self.assertEqual(recorded.returncode, 0, self.error(recorded))
        self.assertEqual(json.loads(recorded.stdout)["status"], "FAIL")
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertEqual(report["result"]["replay"]["source_defect"]["id"],
                         "bad-name:error-message")
        self.assertEqual(report["result"]["replay"]["source_defect"]["viewports"],
                         ["desktop", "mobile"])
        self.assertEqual(report["result"]["cleanup"], "COMPLETE")
        self.assertEqual(before, _tree_digest(source_root))

    def test_replay_rejects_a_stale_ready_environment(self):
        source, _ = self.make_run()
        environment = self.live_environment()
        environment.kill()  # the saved record still claims READY
        replay = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(replay.returncode, 2)
        self.assertIn("no longer the verified process", self.error(replay))

    def test_replay_rejects_a_base_url_from_another_environment(self):
        source, _ = self.make_run()
        environment = self.live_environment()
        other = self.live_environment()
        replay = self.replay(source["run_id"], environment=environment, scenario="create-item",
                             base_url=other.base_url)
        self.assertEqual(replay.returncode, 2)
        self.assertIn("does not belong to this environment", self.error(replay))

    def test_replay_binds_the_environment_to_the_first_attempt(self):
        source, _ = self.make_run()
        environment = self.live_environment()
        first = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(first.returncode, 0, self.error(first))
        started = json.loads(first.stdout)
        runs = self.project / ".codex/checks/qa-web/runs"
        before = sorted(path.name for path in runs.iterdir())
        # The first attempt is still running, so the environment binding must be
        # visible before the run is indexed.
        second = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(second.returncode, 2)
        self.assertIn(f"already bound to web QA run {started['run_id']}", self.error(second))
        self.assertEqual(before, sorted(path.name for path in runs.iterdir()))
        # The refusal leaves the first attempt's stand and evidence intact.
        self.assertIsNone(environment.process.poll())
        item = environment.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        recorded = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(recorded.returncode, 0, self.error(recorded))
        self.assertEqual(json.loads(recorded.stdout)["status"], "PASS")

    def test_start_refuses_an_environment_bound_to_an_unfinished_replay(self):
        source, _ = self.make_run()
        environment = self.live_environment()
        first = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(first.returncode, 0, self.error(first))
        started = json.loads(first.stdout)
        scenario = self.scenario(self.stand.port, readback=self.readback(),
                                 environment_run_id=environment.run_id)
        blocked = self.cli("qa", "web-start", "--project", self.project,
                           "--scenario", scenario)
        self.assertEqual(blocked.returncode, 2)
        self.assertIn(f"already bound to web QA run {started['run_id']}", self.error(blocked))
        self.assertIsNone(environment.process.poll())

    def test_replay_refuses_an_environment_bound_to_an_unfinished_start(self):
        source, _ = self.make_run()
        environment = self.live_environment()
        scenario = self.scenario(environment.port, readback=self.readback(),
                                 environment_run_id=environment.run_id)
        started = self.start(scenario)
        replay = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(replay.returncode, 2)
        self.assertIn(f"already bound to web QA run {started['run_id']}", self.error(replay))
        self.assertIsNone(environment.process.poll())

    def test_replay_never_reuses_the_source_environment(self):
        environment = self.live_environment()
        scenario = self.scenario(environment.port, readback=self.readback(),
                                 environment_run_id=environment.run_id)
        started = self.start(scenario)
        # The incomplete source still holds the live environment; --isolated does
        # not authorize reusing it.
        refused = self.replay(started["run_id"], environment=environment,
                              scenario="create-item", isolated=True)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("source environment cannot be reused", self.error(refused))
        fresh = self.live_environment()
        allowed = self.replay(started["run_id"], environment=fresh,
                              scenario="create-item", isolated=True)
        self.assertEqual(allowed.returncode, 0, self.error(allowed))
        self.assertTrue(json.loads(allowed.stdout)["isolated"])

    def test_replay_rejects_unknown_defect_and_damaged_source(self):
        scenarios = [{"id": "bad-name", "kind": "validation", "title": "Reject empty name",
                      "steps": ["Save empty"], "checks": ["error-message"]}]
        source, _ = self.make_run(scenarios=scenarios,
                                  actuals={("bad-name", viewport, "error-message"): ("error", "none")
                                           for viewport in ("desktop", "mobile")})
        environment = self.live_environment()
        unknown = self.replay(source["run_id"], environment=environment, defect="bad-name:missing")
        self.assertEqual(unknown.returncode, 2)
        self.assertIn("available", self.error(unknown))
        shot = next((self.project / ".codex/checks/qa-web/runs" / source["run_id"]
                     / "screenshots").glob("*.png"))
        shot.write_bytes(b"broken")
        damaged = self.replay(source["run_id"], environment=environment, scenario="bad-name")
        self.assertEqual(damaged.returncode, 2)
        self.assertIn("integrity", self.error(damaged))

    def test_incomplete_source_requires_isolated_replay(self):
        source_environment = self.live_environment()
        started = self.start(self.scenario(source_environment.port, scenarios=[
            {"id": "create-item", "kind": "e2e", "title": "Create an item",
             "steps": ["Save", "Read back"], "checks": ["saved-message"]}],
            readback=self.readback(), environment_run_id=source_environment.run_id))
        environment = self.live_environment()
        refused = self.replay(started["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(refused.returncode, 2)
        self.assertIn("incomplete", self.error(refused))
        allowed = self.replay(started["run_id"], environment=environment,
                              scenario="create-item", isolated=True)
        self.assertEqual(allowed.returncode, 0, self.error(allowed))
        payload = json.loads(allowed.stdout)
        self.assertIsNone(payload["source_result_sha256"])
        self.assertEqual(payload["source_status"], "RUNNING")
        self.assertTrue(payload["isolated"])

    def test_replay_backend_unavailable_blocks_and_does_not_pass(self):
        source, _ = self.make_run()
        environment = self.live_environment()
        replay = self.replay(source["run_id"], environment=environment, scenario="create-item")
        self.assertEqual(replay.returncode, 0, self.error(replay))
        started = json.loads(replay.stdout)
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): "99" for viewport in started["viewports"]}
        # The stand crashed after replay started: the saved state still says READY.
        environment.kill()
        recorded = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(recorded.returncode, 0, self.error(recorded))
        self.assertEqual(json.loads(recorded.stdout)["status"], "BLOCKED")
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertTrue(all(item["reason"] == "backend_unavailable"
                            for item in report["evidence"]["observations"]))
        self.assertNotEqual(report["result"]["status"], "PASS")

    def test_find_rejects_a_damaged_run_instead_of_returning_stale_evidence(self):
        started, _ = self.make_run()
        result_path = (self.project / ".codex/checks/qa-web/runs" / started["run_id"]
                       / "result.json")
        result = json.loads(result_path.read_text())
        result["status"] = "FAIL"
        result_path.write_text(json.dumps(result))
        broken = self.find("--run-id", started["run_id"])
        self.assertEqual(broken.returncode, 2)
        self.assertIn("integrity", self.error(broken))

    def test_discovery_and_listing_create_nothing_in_an_empty_project(self):
        found = self.find()
        self.assertEqual(found.returncode, 0, self.error(found))
        self.assertEqual(json.loads(found.stdout), {"schema_version": 1, "matches": [],
                                                    "count": 0, "ambiguous": False})
        listed = self.cli("qa", "web-list", "--project", self.project)
        self.assertEqual(listed.returncode, 0, self.error(listed))
        self.assertEqual(json.loads(listed.stdout)["runs"], [])
        # Discovery must not create the web QA root, an index, or a lock file.
        self.assertFalse((self.project / ".codex/checks/qa-web").exists())

    def test_discovery_and_reporting_work_with_read_only_artifacts(self):
        started, _ = self.make_run()
        tree = self.project / ".codex/checks/qa-web"

        def make_read_only(path):
            if path.is_dir():
                path.chmod(0o555)
            else:
                path.chmod(0o444)

        def restore(path):
            for current in [path, *path.rglob("*")]:
                try:
                    current.chmod(0o755 if current.is_dir() else 0o644)
                except OSError:
                    pass

        try:
            # Deepest first so a read-only parent cannot block a child update.
            for path in sorted(tree.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                make_read_only(path)
            tree.chmod(0o555)
            found = self.find("--scenario", "create-item")
            self.assertEqual(found.returncode, 0, self.error(found))
            self.assertEqual(json.loads(found.stdout)["count"], 1)
            report = self.cli("qa", "web-report", "--project", self.project,
                              "--run-id", started["run_id"])
            self.assertEqual(report.returncode, 0, self.error(report))
            self.assertEqual(json.loads(report.stdout)["result"]["status"], "PASS")
            listed = self.cli("qa", "web-list", "--project", self.project)
            self.assertEqual(listed.returncode, 0, self.error(listed))
            self.assertEqual(len(json.loads(listed.stdout)["runs"]), 1)
        finally:
            restore(tree)


if __name__ == "__main__":
    unittest.main()
