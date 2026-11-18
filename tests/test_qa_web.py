from __future__ import annotations

import http.client
import json
import pathlib
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
import zlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "tests" / "fixtures"))

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


class Stand:
    def __init__(self, optimistic: bool = False):
        self.store = qa_web_app.Store(optimistic)
        handler = type("Handler", (qa_web_app.Handler,), {"store": self.store})
        self.server = ThreadingLoopbackHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def create(self, name: str, label: str, secret: str | None = None) -> dict:
        body = json.dumps({"name": name, "label": label,
                           **({"secret": secret} if secret else {})})
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


class QaWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self.temp.name) / "project"
        (self.project / ".codex").mkdir(parents=True)
        (self.project / ".codex/devcrew.json").write_text(json.dumps({"schema_version": 1, "paths": {
            "context": "docs/context.md", "tasks": ".codex/tasks", "sessions": ".codex/sessions",
            "memory": "memory", "checks": ".codex/checks"}}))
        self.environment_run_id = uuid.uuid4().hex
        self.set_environment("READY")
        self.shots = pathlib.Path(self.temp.name) / "shots"
        self.shots.mkdir()
        self.stand = None

    def tearDown(self):
        if self.stand is not None:
            self.stand.stop()
        self.temp.cleanup()

    def set_environment(self, status, *, cleanup_error=None):
        state = {"schema_version": 1, "run_id": self.environment_run_id, "status": status,
                 "services": [{"id": "web", "status": "STOPPED" if status == "STOPPED" else "READY",
                               "pid": None, "identity": None}], "resources": []}
        if cleanup_error:
            state["cleanup_error"] = cleanup_error
        path = (self.project / ".codex/checks/qa-environments/runs" / self.environment_run_id
                / "state.json")
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

    def scenario(self, port, *, scenarios=None, viewports=None, readback=None):
        profile = {"base_url": f"http://127.0.0.1:{port}",
                   "environment_run_id": self.environment_run_id}
        if readback is not None:
            profile["readback"] = readback
        path = pathlib.Path(self.temp.name) / f"scenario-{uuid.uuid4().hex}.json"
        path.write_text(json.dumps({
            "schema_version": 1, "profile": profile,
            "viewports": viewports or [{"id": "desktop", "width": 1280, "height": 800},
                                       {"id": "mobile", "width": 390, "height": 844}],
            "scenarios": scenarios or [{
                "id": "create-item", "kind": "e2e", "title": "Create an item",
                "steps": ["Open the form", "Fill the run label", "Save the item",
                          "Read the saved item from the backend"],
                "checks": ["saved-message"]}],
        }))
        return path

    def start(self, scenario_path):
        result = self.cli("qa", "web-start", "--project", self.project, "--scenario", scenario_path)
        self.assertEqual(result.returncode, 0, self.error(result))
        return json.loads(result.stdout)

    def screenshot(self, scenario_id, viewport, phase, width=None, height=None):
        path = self.shots / f"{scenario_id}-{viewport['id']}-{phase}.png"
        path.write_bytes(_png(width or viewport["width"], height or viewport["height"]))
        return str(path)

    def evidence(self, started, *, actuals, items=None, screenshots=None, ui_saved=True,
                 blocked=None):
        items = items or {}
        screenshots = screenshots or {}
        if blocked is not None:
            return {"schema_version": 1, "run_id": started["run_id"],
                    "scenario_sha256": started["scenario_sha256"],
                    "browser": {"tool": "test-browser", "version": "1.0"},
                    "blocked": blocked, "scenarios": []}
        payload = {"schema_version": 1, "run_id": started["run_id"],
                   "scenario_sha256": started["scenario_sha256"],
                   "browser": {"tool": "test-browser", "version": "1.0"}, "scenarios": []}
        for scenario in started["scenarios"]:
            entry = {"id": scenario["id"], "viewports": {}}
            for viewport in started["viewports"]:
                checks = []
                for check_id in scenario["checks"]:
                    expected, actual = actuals[(scenario["id"], viewport["id"], check_id)]
                    checks.append({"id": check_id, "expected": expected, "actual": actual})
                shot = screenshots.get((scenario["id"], viewport["id"])) or {
                    "before": self.screenshot(scenario["id"], viewport, "before"),
                    "after": self.screenshot(scenario["id"], viewport, "after"),
                }
                item = {"observations": ["observed the named state"],
                        "checks": checks, "screenshots": shot}
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

    def readback(self, expect=None):
        return {"path": "/api/items/{id}", "expected_status": 200,
                "expect_json": expect or {"label": "{{RUN_TOKEN}}", "name": "note"}}

    def passing_actuals(self, started, value="Saved"):
        actuals = {}
        for scenario in started["scenarios"]:
            for viewport in started["viewports"]:
                for check_id in scenario["checks"]:
                    expected = {"saved-message": "Saved", "error-message": "error",
                                "full-label": "full"}.get(check_id, check_id)
                    actuals[(scenario["id"], viewport["id"], check_id)] = (expected, expected)
        return actuals

    def test_real_save_passes_e2e_while_visual_and_validation_defects_fail(self):
        self.stand = Stand()
        scenario = self.scenario(self.stand.port, scenarios=[
            {"id": "create-item", "kind": "e2e", "title": "Create an item",
             "steps": ["Fill the label", "Save", "Read it back from the backend"],
             "checks": ["saved-message"]},
            {"id": "bad-name", "kind": "validation", "title": "Reject an empty name",
             "steps": ["Leave the name empty", "Save", "Look for an error message"],
             "checks": ["error-message"]},
            {"id": "mobile-layout", "kind": "ui", "title": "Show the full label",
             "steps": ["Enter a long label", "Inspect the preview at each viewport"],
             "checks": ["full-label"]},
        ], readback=self.readback())
        started = self.start(scenario)
        items, actuals = {}, {}
        for viewport in started["viewports"]:
            item = self.stand.create("note", started["token"], secret="qa-secret-canary-71")
            items[("create-item", viewport["id"])] = item["id"]
            actuals[("create-item", viewport["id"], "saved-message")] = ("Saved", "Saved")
            actuals[("bad-name", viewport["id"], "error-message")] = ("error", "none")
            actuals[("mobile-layout", viewport["id"], "full-label")] = (
                ("full", "clipped") if viewport["id"] == "mobile" else ("full", "full"))
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(result.returncode, 0, self.error(result))
        recorded = json.loads(result.stdout)
        self.assertEqual(recorded["status"], "FAIL")
        self.assertEqual(recorded["cleanup"], "COMPLETE")
        report = self.cli("qa", "web-report", "--project", self.project, "--run-id", started["run_id"])
        self.assertEqual(report.returncode, 0, self.error(report))
        data = json.loads(report.stdout)
        summaries = {item["id"]: item["status"] for item in data["result"]["scenario_summaries"]}
        self.assertEqual(summaries["create-item"], "PASS")
        self.assertEqual(summaries["bad-name"], "FAIL")
        self.assertEqual(summaries["mobile-layout"], "FAIL")
        e2e = [item for item in data["evidence"]["observations"] if item["scenario_id"] == "create-item"]
        self.assertTrue(all(item["readback"]["outcome"] == "PASS" for item in e2e))
        self.assertTrue(all(item["readback"]["status_code"] == 200 for item in e2e))
        self.assertNotIn("qa-secret-canary-71", json.dumps(data))
        self.assertIn("backend read", data["markdown"])
        self.assertIn("screenshots", json.dumps(data["evidence"]))
        self.assertEqual(len(list((self.project / ".codex/checks/qa-web/runs" / started["run_id"]
                                  / "screenshots").glob("*.png"))), 12)

    def test_optimistic_ui_fails_the_end_to_end_scenario(self):
        self.stand = Stand(optimistic=True)
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(result.returncode, 0, self.error(result))
        self.assertEqual(json.loads(result.stdout)["status"], "FAIL")
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertEqual(report["result"]["scenario_summaries"][0]["status"], "FAIL")
        observations = report["evidence"]["observations"]
        self.assertTrue(all(item["readback"]["status_code"] == 404 for item in observations))
        self.assertTrue(all(item["readback"]["reason"] == "backend_record_missing"
                            for item in observations))

    def test_backend_values_are_not_substituted_from_browser_state(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        foreign = self.stand.create("note", "someone-else")
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): foreign["id"] for viewport in started["viewports"]}
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(result.returncode, 0, self.error(result))
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertEqual(report["result"]["status"], "FAIL")
        self.assertTrue(all(item["readback"]["reason"] == "backend_record_mismatch"
                            for item in report["evidence"]["observations"]))
        self.assertTrue(all(not field["matched"] for item in report["evidence"]["observations"]
                            for field in item["readback"]["fields"] if field["field"] == "label"))

    def test_missing_screenshot_blocks_instead_of_passing(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        screenshots = {}
        mobile = [viewport for viewport in started["viewports"] if viewport["id"] == "mobile"][0]
        screenshots[("create-item", "mobile")] = {
            "before": str(self.shots / "missing.png"),
            "after": self.screenshot("create-item", mobile, "after"),
        }
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items,
                                                    screenshots=screenshots))
        self.assertEqual(result.returncode, 0, self.error(result))
        self.assertEqual(json.loads(result.stdout)["status"], "BLOCKED")
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        blocked = [item for item in report["evidence"]["observations"]
                   if item["reason"] == "missing_screenshot"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["outcome"], "BLOCKED")

    def test_wrong_screenshot_size_blocks(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        screenshots = {("create-item", "mobile"): {
            "before": self.screenshot("create-item", {"id": "mobile"}, "before", 100, 100),
            "after": self.screenshot("create-item", {"id": "mobile"}, "after", 100, 100)}}
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items,
                                                    screenshots=screenshots))
        self.assertEqual(result.returncode, 0, self.error(result))
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertTrue(any(item["reason"] == "viewport_mismatch"
                            for item in report["evidence"]["observations"]))

    def test_blocked_browser_is_reported_honestly(self):
        validation = [{"id": "bad-name", "kind": "validation", "title": "Reject an empty name",
                       "steps": ["Leave the name empty", "Save"], "checks": ["error-message"]}]
        started = self.start(self.scenario(9, scenarios=validation))  # no server needed
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(
            started, actuals={}, blocked={"reason": "browser_unavailable",
                                           "detail": "host browser tool is not available"}))
        self.assertEqual(result.returncode, 0, self.error(result))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "BLOCKED")
        report = json.loads(self.cli("qa", "web-report", "--project", self.project,
                                     "--run-id", started["run_id"]).stdout)
        self.assertEqual(report["result"]["status"], "BLOCKED")
        self.assertIn("browser", json.dumps(report["evidence"]))

    def test_unclosed_environment_blocks_overall_pass(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        # The environment is still READY: product checks pass, but the run cannot claim completion.
        result = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(result.returncode, 0, self.error(result))
        payload = json.loads(result.stdout)
        self.assertEqual(payload["cleanup"], "INCOMPLETE")
        self.assertEqual(payload["status"], "BLOCKED")

    def test_start_requires_a_ready_managed_environment(self):
        self.set_environment("STARTING")
        validation = [{"id": "bad-name", "kind": "validation", "title": "Reject an empty name",
                       "steps": ["Leave the name empty", "Save"], "checks": ["error-message"]}]
        result = self.cli("qa", "web-start", "--project", self.project,
                          "--scenario", self.scenario(9, scenarios=validation))
        self.assertEqual(result.returncode, 2)
        self.assertIn("not READY", self.error(result))

    def test_incomplete_scenario_coverage_is_rejected(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        payload = self.evidence(started, actuals=actuals,
                                items={("create-item", viewport["id"]): item["id"]
                                       for viewport in started["viewports"]})
        payload["scenarios"][0]["viewports"].pop("mobile")
        self.set_environment("STOPPED")
        result = self.record(started, payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cover every viewport", self.error(result))

    def test_scenario_change_after_start_is_rejected(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        payload = self.evidence(started, actuals=actuals,
                                items={("create-item", viewport["id"]): item["id"]
                                       for viewport in started["viewports"]})
        snapshot = (self.project / ".codex/tasks/qa/snapshots" / started["run_id"] / "scenario.json")
        changed = json.loads(snapshot.read_text())
        changed["scenarios"][0]["title"] = "changed after start"
        snapshot.write_text(json.dumps(changed))
        self.set_environment("STOPPED")
        result = self.record(started, payload)
        self.assertEqual(result.returncode, 2)
        self.assertIn("integrity", self.error(result))

    def test_list_and_report_verify_saved_artifacts(self):
        self.stand = Stand()
        started = self.start(self.scenario(self.stand.port, readback=self.readback()))
        item = self.stand.create("note", started["token"])
        actuals = {("create-item", viewport["id"], "saved-message"): ("Saved", "Saved")
                   for viewport in started["viewports"]}
        items = {("create-item", viewport["id"]): item["id"] for viewport in started["viewports"]}
        self.set_environment("STOPPED")
        result = self.record(started, self.evidence(started, actuals=actuals, items=items))
        self.assertEqual(result.returncode, 0, self.error(result))
        self.assertEqual(json.loads(result.stdout)["status"], "PASS")
        listed = self.cli("qa", "web-list", "--project", self.project)
        self.assertEqual(listed.returncode, 0, self.error(listed))
        self.assertEqual(json.loads(listed.stdout)["runs"][0]["run_id"], started["run_id"])
        shot = self.project / ".codex/checks/qa-web/runs" / started["run_id"] / "screenshots"
        next(shot.glob("*.png")).write_bytes(b"broken")
        broken = self.cli("qa", "web-report", "--project", self.project, "--run-id", started["run_id"])
        self.assertEqual(broken.returncode, 2)
        self.assertIn("screenshot integrity", self.error(broken))


if __name__ == "__main__":
    unittest.main()
