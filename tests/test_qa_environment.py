from __future__ import annotations

import contextlib
import copy
import http.server
import io
import json
import os
import pathlib
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
import uuid
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"
sys.path.insert(0, str(ROOT / "scripts"))

import devcrew_qa_environment  # noqa: E402

PATHS = {"context": "docs/context.md", "tasks": ".codex/tasks", "sessions": ".codex/sessions",
         "memory": "memory", "checks": ".codex/checks"}


class QuietHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready")

    def log_message(self, *_args):
        pass


class CountingHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        history = getattr(self.server, "history", None)
        if history is not None:
            history.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ready")

    def log_message(self, *_args):
        pass


class ErrorHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b"unavailable")

    def log_message(self, *_args):
        pass


class RedirectHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(302)
        self.send_header("Location", self.server.redirect_to)
        self.end_headers()

    def log_message(self, *_args):
        pass


def _http_service_code(port, label, *, delay=0.0, child_marker=None, response=200,
                       redirect_to=None, hang=False, stay_on_bind_failure=False):
    lines = [
        "import http.server, socket, subprocess, sys, time",
        f"open('order.log', 'a').write({label!r} + '\\n')",
    ]
    if child_marker:
        lines += [
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])",
            f"open({child_marker!r}, 'w').write(str(child.pid))",
        ]
    lines += [f"time.sleep({delay})"]
    if stay_on_bind_failure:
        # The port is taken by someone else in this scenario; stay alive so the
        # readiness answer can only come from the foreign listener.
        lines += [
            "probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)",
            "try:",
            f"    probe.bind(('127.0.0.1', {port}))",
            "except OSError:",
            "    time.sleep(120)",
        ]
        return [sys.executable, "-c", "\n".join(lines)]
    if hang:
        lines += [
            "class H(http.server.BaseHTTPRequestHandler):",
            "    def do_GET(self):",
            "        time.sleep(30)",
            "    def log_message(self, *args):",
            "        pass",
            f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()",
        ]
        return [sys.executable, "-c", "\n".join(lines)]
    lines += [
        "class H(http.server.BaseHTTPRequestHandler):",
        "    def do_GET(self):",
        f"        self.send_response({response})",
    ]
    if redirect_to:
        lines.append(f"        self.send_header('Location', {redirect_to!r})")
    lines += [
        "        self.end_headers()",
        "    def log_message(self, *args):",
        "        pass",
        f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()",
    ]
    return [sys.executable, "-c", "\n".join(lines)]


class QaEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = pathlib.Path(self.temp.name) / "project"
        (self.project / ".codex").mkdir(parents=True)
        (self.project / ".codex/devcrew.json").write_text(json.dumps({"schema_version": 1, "paths": PATHS}))
        self.foreign_servers = []

    def tearDown(self):
        # Safety net: never leave test services behind, but only signal verified owned processes.
        for state_path in self.project.glob(".codex/checks/qa-environments/runs/*/state.json"):
            try:
                state = json.loads(state_path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            for service in state.get("services", []):
                pid = service.get("pid")
                if type(pid) is int:
                    self.kill_if_owned(pid, service.get("identity"))
            for entry in state.get("preparation", []) + state.get("cleanup", []):
                pid = entry.get("pid")
                if type(pid) is int:
                    self.kill_if_owned(pid, entry.get("identity"))
        for server in self.foreign_servers:
            server.shutdown()
            server.server_close()
        self.temp.cleanup()

    @staticmethod
    def kill_if_owned(pid, identity):
        current = devcrew_qa_environment._process_identity(pid)
        if current is not None and devcrew_qa_environment._same_process(current, identity):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, "-B", str(CLI), *map(str, args)], cwd=ROOT,
                              capture_output=True, text=True, timeout=60,
                              env={**os.environ, **(env or {})})

    @staticmethod
    def unused_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def service(self, service_id, port, *, depends_on=None, label=None, delay=0.0,
                child_marker=None, response=200, redirect_to=None, hang=False,
                stay_on_bind_failure=False, expected_status=200, readiness_timeout=3):
        return {
            "id": service_id,
            "command": _http_service_code(port, label or service_id, delay=delay,
                                          child_marker=child_marker, response=response,
                                          redirect_to=redirect_to, hang=hang,
                                          stay_on_bind_failure=stay_on_bind_failure),
            "depends_on": list(depends_on or []),
            "readiness": {"url": f"http://127.0.0.1:{port}/", "status": expected_status,
                          "timeout_seconds": readiness_timeout},
        }

    def profile(self, port, *, resource="fixture-db", prepare=None, services=None,
                cleanup=None, data_dir=None, environment=None):
        path = pathlib.Path(self.temp.name) / f"profile-{uuid.uuid4().hex}.json"
        profile = {
            "schema_version": 1, "id": "fixture", "resources": [resource],
            "environment": list(environment or ()), "prepare": prepare or [],
            "services": services or [self.service("api", port)]}
        if cleanup is not None:
            profile["cleanup"] = cleanup
        if data_dir is not None:
            profile["data_dir"] = data_dir
        path.write_text(json.dumps(profile))
        return path

    def run_id_path(self, run_id):
        return self.project / ".codex/checks/qa-environments/runs" / run_id / "state.json"

    def read_state(self, run_id):
        return json.loads(self.run_id_path(run_id).read_text())

    def state_paths(self):
        return sorted(self.project.glob(".codex/checks/qa-environments/runs/*/state.json"))

    @staticmethod
    def reported_run_id(stderr):
        match = re.search(r"run_id=([0-9a-f]{32})", stderr)
        if match is None:
            raise AssertionError(f"no run ID in {stderr!r}")
        return match.group(1)

    def claim_index(self):
        path = self.project / ".codex/checks/qa-environments/resources.json"
        return json.loads(path.read_text()) if path.exists() else {"schema_version": 1, "resources": {}}

    def write_run(self, run_id, state, claims=(), project_root=None):
        path = self.run_id_path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state.setdefault("schema_version", 1)
        state.setdefault("run_id", run_id)
        state.setdefault("profile_id", "fixture")
        state.setdefault("profile_sha256", "0" * 64)
        state.setdefault("project_root", str(project_root or self.project))
        state.setdefault("created_at", time.time())
        state.setdefault("resources", [])
        path.write_text(json.dumps(state))
        index_path = self.project / ".codex/checks/qa-environments/resources.json"
        if claims:
            index = json.loads(index_path.read_text()) if index_path.exists() else {
                "schema_version": 1, "resources": {}}
            for name in claims:
                index["resources"][name] = {"run_id": run_id, "status": "STARTING"}
            index_path.write_text(json.dumps(index))

    def destination(self, root, relative):
        path = root / pathlib.PurePosixPath(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def in_process_start(self, profile):
        args = types.SimpleNamespace(qa_environment_operation="start", profile=str(profile))
        return devcrew_qa_environment._start(args, self.project, PATHS,
                                             self.destination, self.destination)

    def in_process_stop(self, run_id):
        args = types.SimpleNamespace(qa_environment_operation="stop", run_id=run_id)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = devcrew_qa_environment.command(args, self.project, PATHS,
                                                  self.destination, self.destination)
        return code, out.getvalue(), err.getvalue()

    @staticmethod
    def pid_dead(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        fields = subprocess.run(["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True)
        return fields.stdout.strip().startswith("Z")

    @staticmethod
    def wait_dead(pid, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if QaEnvironmentTests.pid_dead(pid):
                return True
            time.sleep(0.05)
        return QaEnvironmentTests.pid_dead(pid)

    @staticmethod
    def wait_for(predicate, timeout=10.0, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(0.02)
        raise AssertionError(f"timed out waiting for {message}")

    def assert_port_bindable(self, port):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", port))

    def start_foreign_server(self, handler=QuietHandler, *, port=0):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
        server.history = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server.thread = thread
        self.foreign_servers.append(server)
        return server

    # 1. Preparation and declared data cleanup share the ownership model.

    def test_preparation_data_is_removed_after_a_confirmed_stop(self):
        port = self.unused_port()
        cleanup_marker = self.project / "cleanup-done"
        profile = self.profile(
            port, resource="data-resource", data_dir="run-data",
            prepare=[[sys.executable, "-c",
                      "import pathlib\n"
                      "pathlib.Path('order.log').write_text('prepare\\n')\n"
                      "pathlib.Path('run-data/fixture-data.json').write_text('{\"isolated\": true}')"]],
            cleanup=[[sys.executable, "-c",
                      "import pathlib\n"
                      "pathlib.Path('cleanup-done').write_text('cleanup')\n"
                      "pathlib.Path('order.log').open('a').write('cleanup\\n')"]],
            services=[self.service("api", port)])

        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        data = self.project / "run-data/fixture-data.json"
        self.assertTrue(data.is_file())
        self.assertTrue((self.project / "run-data/.devcrew-qa-owner.json").is_file())
        state = self.read_state(run_id)
        self.assertEqual(state["cleanup"][0]["status"], "NOT_STARTED")

        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["cleanup"], "COMPLETE")
        self.assertEqual(result["resources_held"], [])
        self.assertEqual(result["cleanup_steps"][0]["status"], "COMPLETE")
        self.assertEqual(result["data"]["status"], "REMOVED")
        self.assertFalse((self.project / "run-data").exists())
        self.assertTrue(cleanup_marker.is_file())
        self.assertEqual((self.project / "order.log").read_text().splitlines(),
                         ["prepare", "api", "cleanup"])
        self.assertEqual(self.claim_index()["resources"], {})

        repeated = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["cleanup"], "COMPLETE")

    def test_preparation_interrupted_after_side_effect_keeps_claim_and_is_not_repeated(self):
        port = self.unused_port()
        profile = self.profile(
            port, resource="interrupted-prep", data_dir="run-data",
            prepare=[[sys.executable, "-c",
                      "import pathlib, time\n"
                      "pathlib.Path('prepare-runs.log').open('a').write('run\\n')\n"
                      "pathlib.Path('run-data/created.json').write_text('data')\n"
                      "deadline = time.monotonic() + 30\n"
                      "while not pathlib.Path('prepare-release').exists():\n"
                      "    if time.monotonic() > deadline:\n"
                      "        raise SystemExit(7)\n"
                      "    time.sleep(0.02)"]])
        started = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "qa", "environment-start", "--project", str(self.project),
             "--profile", str(profile)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        run_id = None
        try:
            def prepared():
                if not self.state_paths():
                    return None
                state = json.loads(self.state_paths()[0].read_text())
                entry = state["preparation"][0]
                if entry.get("pid") and entry.get("identity") and (self.project / "run-data/created.json").exists():
                    return self.state_paths()[0].parent.name
                return None

            run_id = self.wait_for(prepared, message="preparation side effect")
            started.kill()
            started.wait(timeout=10)
            self.assertTrue((self.project / "run-data/created.json").exists())

            stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
            self.assertEqual(stopped.returncode, 2)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["status"], "CLEANUP_FAILED")
            self.assertEqual(result["cleanup"], "INCOMPLETE")
            self.assertEqual(result["preparation"][0]["status"], "EFFECT_UNKNOWN")
            self.assertIn("interrupted-prep", result["resources_held"])
            self.assertIn(f"port:127.0.0.1:{port}", result["resources_held"])
            self.assertFalse((self.project / "run-data").exists(),
                             "the owned data directory should still be removed")
            self.assertEqual(self.read_state(run_id)["preparation"][0]["status"], "EFFECT_UNKNOWN")
            self.assertEqual((self.project / "prepare-runs.log").read_text(), "run\n")

            # A repeated stop must not replay the unknown preparation effect.
            repeated = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
            self.assertEqual(repeated.returncode, 2)
            self.assertEqual(json.loads(repeated.stdout)["cleanup"], "INCOMPLETE")
            self.assertEqual((self.project / "prepare-runs.log").read_text(), "run\n")
            self.assertEqual(self.claim_index()["resources"]["interrupted-prep"]["run_id"], run_id)

            blocked = self.cli("qa", "environment-start", "--project", self.project,
                               "--profile", self.profile(self.unused_port(), resource="interrupted-prep"))
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("resource conflict", blocked.stderr)
        finally:
            (self.project / "prepare-release").write_text("1")
            if started.poll() is None:
                started.kill()
                started.wait(timeout=10)
            started.stdout.close()
            started.stderr.close()

    def test_failed_declared_cleanup_preserves_state_and_claim(self):
        port = self.unused_port()
        profile = self.profile(
            port, resource="cleanup-failure", prepare=[[sys.executable, "-c", "pass"]],
            cleanup=[[sys.executable, "-c",
                      "import pathlib\n"
                      "pathlib.Path('cleanup-attempts.log').open('a').write('attempt\\n')\n"
                      "raise SystemExit(5)"]])
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]

        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["status"], "CLEANUP_FAILED")
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertEqual(result["cleanup_steps"][0]["status"], "FAILED")
        self.assertIn("exited with status 5", result["cleanup_steps"][0]["cleanup_error"])
        self.assertIn("cleanup-failure", result["resources_held"])

        repeated = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(repeated.returncode, 2)
        self.assertEqual((self.project / "cleanup-attempts.log").read_text(), "attempt\n")

    def test_unowned_data_directory_is_never_removed(self):
        port = self.unused_port()
        data_dir = self.project / "foreign-data"
        data_dir.mkdir()
        (data_dir / "owned-by-someone-else.txt").write_text("keep")
        profile = self.profile(port, resource="foreign-data-resource", data_dir="foreign-data")
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 2)
        self.assertIn("not owned", started.stderr)
        self.assertEqual((data_dir / "owned-by-someone-else.txt").read_text(), "keep")
        run_id = self.reported_run_id(started.stderr)
        self.assertEqual(self.read_state(run_id)["data"]["status"], "FAILED")

    # 2. Port ownership: a claim between DevCrew runs plus listener attribution.

    def test_second_devcrew_run_cannot_claim_a_readiness_port(self):
        port = self.unused_port()
        first_profile = self.profile(
            port, resource="first-run",
            prepare=[[sys.executable, "-c",
                      "import pathlib, time\n"
                      "pathlib.Path('gate-entered').write_text('1')\n"
                      "deadline = time.monotonic() + 30\n"
                      "while not pathlib.Path('gate-release').exists():\n"
                      "    if time.monotonic() > deadline:\n"
                      "        raise SystemExit(7)\n"
                      "    time.sleep(0.02)"]])
        first = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "qa", "environment-start", "--project", str(self.project),
             "--profile", str(first_profile)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        run_id = None
        try:
            def claimed():
                if not (self.project / "gate-entered").exists() or not self.state_paths():
                    return None
                run = self.state_paths()[0].parent.name
                return run if f"port:127.0.0.1:{port}" in self.claim_index()["resources"] else None

            run_id = self.wait_for(claimed, message="port claim")
            second = self.cli("qa", "environment-start", "--project", self.project,
                              "--profile", self.profile(port, resource="second-run"))
            self.assertEqual(second.returncode, 2)
            self.assertIn(f"resource conflict: port:127.0.0.1:{port}", second.stderr)

            (self.project / "gate-release").write_text("1")
            stdout, stderr = first.communicate(timeout=30)
            self.assertEqual(first.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["status"], "READY")
            stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertEqual(self.claim_index()["resources"], {})
        finally:
            (self.project / "gate-release").write_text("1")
            if first.poll() is None:
                first.kill()
                first.wait(timeout=10)

    def test_foreign_listener_after_the_port_check_blocks_ready(self):
        port = self.unused_port()
        foreign = self.start_foreign_server(port=port)
        profile = self.profile(port, resource="port-race", services=[
            self.service("api", port, stay_on_bind_failure=True)])

        def race_check(_service):
            # Simulate a foreign listener winning between the check and the launch.
            return None

        with mock.patch.object(devcrew_qa_environment, "_assert_port_free", race_check):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                self.in_process_start(profile)
        self.assertIn("outside its verified process group", str(raised.exception))
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            pass
        state_path = self.state_paths()[0]
        state = json.loads(state_path.read_text())
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["services"][0]["status"], "STOPPED")
        self.assertTrue(self.wait_dead(state["services"][0]["pid"]))
        self.assertEqual(self.claim_index()["resources"], {})

    def test_identity_change_before_ready_is_not_recorded(self):
        service = self.service("api", self.unused_port())
        identity = {"start": "first", "executable": "/bin/service",
                    "process_group": 4242, "session": 4242}
        child = mock.Mock(pid=4242)

        def changed(_pid):
            return {**identity, "start": "second"}

        with mock.patch.object(devcrew_qa_environment, "_readiness_status", return_value=200), \
                mock.patch.object(devcrew_qa_environment, "_listener_check",
                                  return_value=("owned", "")), \
                mock.patch.object(devcrew_qa_environment, "_process_identity", side_effect=changed):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                devcrew_qa_environment._confirm_readiness(service, identity, child)
        self.assertIn("identity changed before readiness", str(raised.exception))

    def test_unverifiable_listener_ownership_is_an_explicit_refusal(self):
        service = self.service("api", self.unused_port())
        identity = {"start": "first", "executable": "/bin/service",
                    "process_group": 4242, "session": 4242}
        with mock.patch.object(devcrew_qa_environment, "_readiness_status", return_value=200), \
                mock.patch.object(devcrew_qa_environment, "_tcp_listener_pids", return_value=None):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                devcrew_qa_environment._confirm_readiness(service, identity, mock.Mock(pid=4242))
        self.assertIn("listener ownership could not be verified", str(raised.exception))

    def test_older_state_with_confirmed_ownership_still_stops(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        identity = devcrew_qa_environment._process_identity(sleeper.pid)
        self.assertIsNotNone(identity)
        try:
            run_id = uuid.uuid4().hex
            # A record written before preparation, data, and port fields existed.
            self.write_run(run_id, {
                "status": "READY", "resources": [],
                "services": [{"id": "api", "status": "READY",
                              "pid": sleeper.pid, "identity": identity}]})
            stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            result = json.loads(stopped.stdout)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["cleanup"], "COMPLETE")
            self.assertEqual(result["data"]["status"], "NOT_CREATED")
            self.assertTrue(self.wait_dead(sleeper.pid))
        finally:
            if sleeper.poll() is None:
                sleeper.kill()
                sleeper.wait(timeout=10)

    # 3. Local HTTP readiness: direct, no proxy, no redirects, original status.

    def test_readiness_compares_the_original_status_without_following_redirects(self):
        target = self.start_foreign_server(CountingHandler)
        redirect_port = self.unused_port()
        redirect = f"http://127.0.0.1:{target.server_port}/final"

        # A 302 response is not the expected 200, even though the target would answer 200.
        first = self.profile(redirect_port, resource="redirect-a", services=[
            self.service("api", redirect_port, response=302, redirect_to=redirect)])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", first)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("did not become ready", failed.stderr)
        self.assertEqual(target.history, [])

        # The same kind of response is READY when the profile expects the original 302.
        redirect_port = self.unused_port()
        second = self.profile(redirect_port, resource="redirect-b", services=[
            self.service("api", redirect_port, response=302, redirect_to=redirect, expected_status=302)])
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", second)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        self.assertEqual(self.read_state(run_id)["services"][0]["status"], "READY")
        self.assertEqual(target.history, [])
        self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)

    def test_readiness_ignores_proxy_environment(self):
        proxy = self.start_foreign_server(CountingHandler)
        hits = self.project / "service-hits.log"
        port = self.unused_port()
        command = [sys.executable, "-c",
                   "import http.server, pathlib\n"
                   "class H(http.server.BaseHTTPRequestHandler):\n"
                   "    def do_GET(self):\n"
                   "        pathlib.Path('service-hits.log').open('a').write('hit\\n')\n"
                   "        self.send_response(200)\n"
                   "        self.end_headers()\n"
                   "    def log_message(self, *args):\n"
                   "        pass\n"
                   f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()"]
        profile = self.profile(port, resource="proxy-check", services=[{
            "id": "api", "command": command, "depends_on": [],
            "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 200,
                          "timeout_seconds": 3}}])
        proxy_url = f"http://127.0.0.1:{proxy.server_port}"
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile,
                           env={"http_proxy": proxy_url, "HTTP_PROXY": proxy_url,
                                "no_proxy": "", "NO_PROXY": ""})
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        self.assertGreaterEqual(len(hits.read_text().splitlines()), 1)
        self.assertEqual(proxy.history, [])
        self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)

    def test_readiness_accepts_an_expected_error_status(self):
        port = self.unused_port()
        profile = self.profile(port, resource="expected-503", services=[
            {"id": "api", "command": _http_service_code(port, "api", response=503),
             "depends_on": [],
             "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 503,
                           "timeout_seconds": 3}}])
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        self.assertEqual(self.read_state(run_id)["services"][0]["status"], "READY")
        self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)

    def test_readiness_timeout_is_bounded(self):
        port = self.unused_port()
        profile = self.profile(port, resource="bounded-timeout", services=[
            {"id": "api", "command": _http_service_code(port, "api", hang=True),
             "depends_on": [],
             "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 200,
                           "timeout_seconds": 0.5}}])
        began = time.monotonic()
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        elapsed = time.monotonic() - began
        self.assertEqual(failed.returncode, 2)
        self.assertIn("did not become ready", failed.stderr)
        self.assertLess(elapsed, 6.0)
        run_id = self.reported_run_id(failed.stderr)
        self.assertEqual(self.read_state(run_id)["status"], "STOPPED")

    # 4. Foreign or incomplete state is refused before any signal or write.

    def test_foreign_or_incomplete_state_is_refused_before_signalling(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            identity = devcrew_qa_environment._process_identity(sleeper.pid)
            self.assertIsNotNone(identity)
            other_project = pathlib.Path(self.temp.name) / "other-project"
            other_project.mkdir()

            def foreign(state):
                state["project_root"] = str(other_project)

            def missing_root(state):
                state.pop("project_root", None)

            def old_schema(state):
                state["schema_version"] = 2

            def wrong_run_id(state):
                state["run_id"] = uuid.uuid4().hex

            for label, mutate in (("foreign project", foreign),
                                  ("missing project root", missing_root),
                                  ("unsupported schema", old_schema),
                                  ("another run id", wrong_run_id)):
                with self.subTest(label):
                    run_id = uuid.uuid4().hex
                    state = {
                        "schema_version": 1, "run_id": run_id,
                        "project_id": "fixture", "profile_id": "fixture",
                        "profile_sha256": "0" * 64, "created_at": time.time(),
                        "project_root": str(self.project), "status": "READY",
                        "resources": ["foreign-state"], "port_claims": [],
                        "preparation": [], "cleanup": [],
                        "data": {"dir": None, "path": None, "status": "NOT_CREATED"},
                        "services": [{"id": "api", "status": "READY",
                                      "pid": sleeper.pid, "identity": identity}],
                    }
                    mutate(state)
                    path = self.run_id_path(run_id)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(state))
                    index_path = self.project / ".codex/checks/qa-environments/resources.json"
                    index_path.write_text(json.dumps({
                        "schema_version": 1,
                        "resources": {"foreign-state": {"run_id": run_id, "status": "READY"}}}))
                    before = path.read_text()
                    index_before = index_path.read_text()

                    stopped = self.cli("qa", "environment-stop", "--project", self.project,
                                       "--run-id", run_id)
                    self.assertEqual(stopped.returncode, 2)
                    self.assertIsNone(sleeper.poll(), "foreign process was signalled")
                    self.assertEqual(path.read_text(), before)
                    self.assertEqual(index_path.read_text(), index_before)
                    status = self.cli("qa", "environment-status", "--project", self.project,
                                      "--run-id", run_id)
                    self.assertEqual(status.returncode, 2)
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=10)

    # 5. Early leader exit with surviving group members is not STOPPED.

    def test_leader_exit_before_identity_keeps_the_group_and_claim(self):
        port = self.unused_port()
        profile = self.profile(port, resource="early-exit", services=[
            {"id": "api",
             "command": [sys.executable, "-c",
                         "import os, subprocess, sys, pathlib\n"
                         "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                         "pathlib.Path('child.pid').write_text(str(child.pid))\n"
                         "os._exit(0)"],
             "depends_on": [],
             "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 200,
                           "timeout_seconds": 2}}])
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 2)
        self.assertIn("before its ownership was confirmed", started.stderr)
        run_id = self.reported_run_id(started.stderr)
        child_pid = self.wait_for(
            lambda: int((self.project / "child.pid").read_text())
            if (self.project / "child.pid").exists() else None)
        self.addCleanup(self.kill_group, child_pid)
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "CLEANUP_FAILED")
        self.assertEqual(state["services"][0]["status"], "EFFECT_UNKNOWN")
        self.assertEqual(self.claim_index()["resources"]["early-exit"]["run_id"], run_id)
        self.assertFalse(self.pid_dead(child_pid), "an unconfirmed group must not be signalled")

        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["status"], "CLEANUP_FAILED")
        self.assertEqual(result["services"][0]["status"], "EFFECT_UNKNOWN")
        self.assertIn("early-exit", result["resources_held"])
        self.assertIn(f"port:127.0.0.1:{port}", result["resources_held"])
        self.assertFalse(self.pid_dead(child_pid), "repeated stop must not signal an unconfirmed owner")
        self.assertEqual(self.claim_index()["resources"]["early-exit"]["run_id"], run_id)

    def test_leader_exit_with_an_empty_group_is_confirmed_stopped(self):
        port = self.unused_port()
        profile = self.profile(port, resource="early-exit-empty", services=[
            {"id": "api",
             "command": [sys.executable, "-c", "import time; time.sleep(0.02); raise SystemExit(0)"],
             "depends_on": [],
             "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 200,
                           "timeout_seconds": 2}}])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2)
        run_id = self.reported_run_id(failed.stderr)
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["services"][0]["status"], "STOPPED")
        self.assertEqual(self.claim_index()["resources"], {})

    @staticmethod
    def kill_group(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass

    # 6. STOPPED/COMPLETE only after cleanup and claim release; repeat reconciles.

    def test_repeated_stop_releases_claims_for_a_saved_stopped_state(self):
        run_id = uuid.uuid4().hex
        self.write_run(run_id, {
            "status": "STOPPED", "resources": ["fixture-db"],
            "services": [{"id": "api", "status": "STOPPED", "pid": None, "identity": None}],
        }, claims=["fixture-db"])
        # Simulate the old crash window: the record says STOPPED, the index still holds the claim.
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["cleanup"], "COMPLETE")
        self.assertEqual(result["resources_held"], [])
        self.assertEqual(self.claim_index()["resources"], {})

    def test_stop_recovers_when_claim_release_write_fails(self):
        port = self.unused_port()
        profile = self.profile(port, resource="release-crash")
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        self.assertIn("release-crash", self.claim_index()["resources"])

        real_write = devcrew_qa_environment._write
        failed_once = {"done": False}

        def flaky(path, value):
            if path.name == "resources.json" and not failed_once["done"]:
                failed_once["done"] = True
                raise OSError("simulated index write failure")
            return real_write(path, value)

        with mock.patch.object(devcrew_qa_environment, "_write", flaky):
            code, out, _err = self.in_process_stop(run_id)
        self.assertEqual(code, 2)
        self.assertTrue(failed_once["done"])
        self.assertNotEqual(self.read_state(run_id)["status"], "STOPPED")
        self.assertIn("release-crash", self.claim_index()["resources"])

        code, out, err = self.in_process_stop(run_id)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["cleanup"], "COMPLETE")
        self.assertEqual(result["resources_held"], [])
        self.assertEqual(self.claim_index()["resources"], {})

    # 7. Child environment is limited to the profile list plus documented system names.

    def test_child_environment_is_limited_to_declared_names(self):
        port = self.unused_port()
        secret = f"sekret-{uuid.uuid4().hex}"
        unlisted = f"unlisted-{uuid.uuid4().hex}"
        presence = (
            "import os, pathlib\n"
            "pathlib.Path({target!r}).write_text(\n"
            "    ('allowed=' + ('yes' if os.environ.get('QA_TEST_ALLOWED') else 'no'))\n"
            "    + '\\n' + ('unlisted=' + ('yes' if os.environ.get('QA_UNLISTED') else 'no'))\n"
            "    + '\\n' + ('path=' + ('yes' if os.environ.get('PATH') else 'no')) + '\\n')"
        )
        profile = self.profile(
            port, resource="env-boundary", environment=["QA_TEST_ALLOWED"],
            prepare=[[sys.executable, "-c", presence.format(target="env-prepare.txt")]],
            cleanup=[[sys.executable, "-c", presence.format(target="env-cleanup.txt")]],
            services=[self.service("api", port)])
        # The service records its own environment as part of its startup.
        profile_data = json.loads(profile.read_text())
        profile_data["services"][0]["command"] = [
            sys.executable, "-c",
            presence.format(target="env-service.txt") + "\n"
            "import http.server\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200)\n"
            "        self.end_headers()\n"
            "    def log_message(self, *args):\n"
            "        pass\n"
            f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()"]
        profile.write_text(json.dumps(profile_data))

        env = {"QA_TEST_ALLOWED": secret, "QA_UNLISTED": unlisted}
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile,
                           env=env)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        for name in ("env-prepare.txt", "env-service.txt"):
            lines = (self.project / name).read_text().splitlines()
            self.assertEqual(lines[0], "allowed=yes", name)
            self.assertEqual(lines[1], "unlisted=no", name)
            self.assertEqual(lines[2], "path=yes", name)

        status = self.cli("qa", "environment-status", "--project", self.project, "--run-id", run_id,
                          env=env)
        self.assertEqual(status.returncode, 0, status.stderr)
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id,
                           env=env)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        cleanup_lines = (self.project / "env-cleanup.txt").read_text().splitlines()
        self.assertEqual(cleanup_lines[0], "allowed=yes")
        self.assertEqual(cleanup_lines[1], "unlisted=no")

        for label, text in (("start stdout", started.stdout), ("start stderr", started.stderr),
                            ("status stdout", status.stdout), ("stop stdout", stopped.stdout),
                            ("state", self.run_id_path(run_id).read_text()),
                            ("index", (self.project /
                                       ".codex/checks/qa-environments/resources.json").read_text())):
            self.assertNotIn(secret, text, f"secret leaked into {label}")
            self.assertNotIn(unlisted, text, f"unlisted value leaked into {label}")

    # Existing ownership and timing guarantees.

    def test_stop_terminates_service_and_its_child_process(self):
        port = self.unused_port()
        profile = self.profile(port, services=[
            self.service("api", port, child_marker="child.pid")])
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        state = self.read_state(run_id)
        parent = state["services"][0]["pid"]
        child = int((self.project / "child.pid").read_text())
        parent_identity, child_identity = state["services"][0]["identity"], \
            devcrew_qa_environment._process_identity(child)
        self.addCleanup(self.kill_if_owned, child, child_identity)
        self.addCleanup(self.kill_if_owned, parent, parent_identity)
        self.assertFalse(self.pid_dead(parent))
        self.assertFalse(self.pid_dead(child))

        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["status"], "STOPPED")
        self.assertTrue(self.wait_dead(parent), "service parent process survived stop")
        self.assertTrue(self.wait_dead(child), "service child process survived stop")

    def test_interrupted_start_intent_stays_unresolved_and_holds_claims(self):
        run_id = uuid.uuid4().hex
        port = self.unused_port()
        self.write_run(run_id, {
            "status": "STARTING", "resources": ["fixture-db"],
            "preparation": [], "services": [{"id": "api", "status": "START_INTENT",
                                             "pid": None, "identity": None}],
        }, claims=["fixture-db"])

        first = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(first.returncode, 2)
        result = json.loads(first.stdout)
        self.assertEqual(result["status"], "CLEANUP_FAILED")
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertEqual(result["services"][0]["status"], "EFFECT_UNKNOWN")
        self.assertIn("interrupted before ownership was confirmed", result["services"][0]["cleanup_error"])
        self.assertEqual(result["resources_held"], ["fixture-db"])

        second = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(second.returncode, 2)
        self.assertEqual(json.loads(second.stdout)["services"][0]["status"], "EFFECT_UNKNOWN")
        self.assertEqual(self.claim_index()["resources"]["fixture-db"]["run_id"], run_id)

        status = self.cli("qa", "environment-status", "--project", self.project, "--run-id", run_id)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertEqual(json.loads(status.stdout)["resources_held"], ["fixture-db"])
        blocked = self.cli("qa", "environment-start", "--project", self.project,
                           "--profile", self.profile(port))
        self.assertEqual(blocked.returncode, 2)
        self.assertIn("resource conflict", blocked.stderr)
        self.assert_port_bindable(port)

    def test_interrupted_start_after_confirmed_pid_recovers_cleanly(self):
        port = self.unused_port()
        profile = self.profile(port, resource="interrupt-resource", services=[
            self.service("api", port, delay=4.0, readiness_timeout=10)])
        process = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "qa", "environment-start", "--project", str(self.project),
             "--profile", str(profile)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pid = run_id = None
        try:
            def started_pid():
                if not self.state_paths():
                    return None
                state = json.loads(self.state_paths()[0].read_text())
                service = state["services"][0]
                if service.get("pid") and service.get("identity"):
                    return service["pid"], self.state_paths()[0].parent.name
                return None

            pid, run_id = self.wait_for(started_pid, message="confirmed PID")
            process.kill()
            process.wait(timeout=10)
            self.assertFalse(self.pid_dead(pid))
            interrupted = self.cli("qa", "environment-status", "--project", self.project, "--run-id", run_id)
            status = json.loads(interrupted.stdout)
            self.assertEqual(status["status"], "STARTING")
            self.assertTrue(status["services"][0]["owned_process_alive"])
            stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
            self.assertEqual(stopped.returncode, 0, stopped.stderr)
            self.assertEqual(json.loads(stopped.stdout)["cleanup"], "COMPLETE")
            self.assertTrue(self.wait_dead(pid))
            self.assertEqual(self.read_state(run_id)["status"], "STOPPED")
            retry = self.cli("qa", "environment-start", "--project", self.project,
                             "--profile", self.profile(self.unused_port(), resource="interrupt-resource"))
            self.assertEqual(retry.returncode, 0, retry.stderr)
            retry_id = json.loads(retry.stdout)["run_id"]
            self.assertEqual(self.cli("qa", "environment-stop", "--project", self.project,
                                      "--run-id", retry_id).returncode, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
            if pid is not None and not self.pid_dead(pid):
                self.kill_if_owned(pid, devcrew_qa_environment._process_identity(pid))

    def test_stale_pid_with_same_executable_is_left_untouched(self):
        sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            identity = devcrew_qa_environment._process_identity(sleeper.pid)
            self.assertIsNotNone(identity)
            stale_identities = {
                "different start time": {**identity, "start": "Thu Jan  1 00:00:00 1970"},
            }
            for label, stale in stale_identities.items():
                with self.subTest(label):
                    run_id = uuid.uuid4().hex
                    self.write_run(run_id, {
                        "status": "READY", "resources": ["fixture-db"],
                        "preparation": [],
                        "services": [{"id": "api", "status": "READY",
                                      "pid": sleeper.pid, "identity": stale}],
                    }, claims=["fixture-db"])
                    stopped = self.cli("qa", "environment-stop", "--project", self.project,
                                       "--run-id", run_id)
                    self.assertEqual(stopped.returncode, 2)
                    result = json.loads(stopped.stdout)
                    self.assertEqual(result["status"], "CLEANUP_FAILED")
                    self.assertIsNone(sleeper.poll(), "foreign process was signalled")
                    self.assertEqual(result["resources_held"], ["fixture-db"])
                    self.assertIn("no longer belongs to this run", result["services"][0]["cleanup_error"])
        finally:
            sleeper.terminate()
            sleeper.wait(timeout=10)

    def test_stop_waits_for_an_in_flight_start_before_releasing_claims(self):
        port = self.unused_port()
        profile = self.profile(port, resource="race-resource", prepare=[[
            sys.executable, "-c",
            "import pathlib, time\n"
            "pathlib.Path('prepare-entered').write_text('1')\n"
            "deadline = time.monotonic() + 30\n"
            "while not pathlib.Path('prepare-release').exists():\n"
            "    if time.monotonic() > deadline:\n"
            "        raise SystemExit(7)\n"
            "    time.sleep(0.02)"]])
        started = subprocess.Popen(
            [sys.executable, "-B", str(CLI), "qa", "environment-start",
             "--project", str(self.project), "--profile", str(profile)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stopped = None
        run_id = None
        try:
            def starting():
                if not self.state_paths() or not (self.project / "prepare-entered").exists():
                    return None
                state = json.loads(self.state_paths()[0].read_text())
                return self.state_paths()[0].parent.name if state.get("status") == "STARTING" else None

            run_id = self.wait_for(starting, message="STARTING")
            stopped = subprocess.Popen(
                [sys.executable, "-B", str(CLI), "qa", "environment-stop",
                 "--project", str(self.project), "--run-id", run_id],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.5)
            self.assertIsNone(stopped.poll(),
                              "stop returned while the start still owned the environment")
            self.assertEqual(self.read_state(run_id)["status"], "STARTING")
            self.assertIn("race-resource", self.claim_index()["resources"])

            (self.project / "prepare-release").write_text("1")
            start_stdout, start_stderr = started.communicate(timeout=30)
            self.assertEqual(started.returncode, 0, start_stderr)
            self.assertEqual(json.loads(start_stdout)["status"], "READY")
            stop_stdout, stop_stderr = stopped.communicate(timeout=30)
            self.assertEqual(stopped.returncode, 0, stop_stderr)
            result = json.loads(stop_stdout)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["cleanup"], "COMPLETE")
            self.assertEqual(result["resources_held"], [])
            state = self.read_state(run_id)
            self.assertEqual(state["status"], "STOPPED")
            self.assertTrue(self.wait_dead(state["services"][0]["pid"]))
        finally:
            if stopped is not None and stopped.poll() is None:
                stopped.kill()
                stopped.wait(timeout=10)
            if stopped is not None:
                stopped.stdout.close()
                stopped.stderr.close()
            if started.poll() is None:
                started.kill()
                started.wait(timeout=10)
            started.stdout.close()
            started.stderr.close()

    def test_environment_state_lock_reports_a_bounded_conflict(self):
        run_id = uuid.uuid4().hex
        read_destination = lambda root, relative: root / relative  # noqa: E731
        held = devcrew_qa_environment._acquire_state_lock(self.project, PATHS,
                                                          read_destination, run_id, wait=0.0)
        try:
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                devcrew_qa_environment._acquire_state_lock(self.project, PATHS,
                                                           read_destination, run_id, wait=0.1)
            self.assertIn("no state was changed", str(raised.exception))
        finally:
            held.__exit__(None, None, None)

    def test_busy_port_does_not_claim_foreign_service_or_hold_resources(self):
        foreign = self.start_foreign_server()
        profile = self.profile(foreign.server_port, resource="foreign-port")
        result = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(result.returncode, 2)
        self.assertIn("is occupied", result.stderr)
        with socket.create_connection(foreign.server_address, timeout=1):
            pass
        retry = self.cli("qa", "environment-start", "--project", self.project,
                         "--profile", self.profile(self.unused_port(), resource="foreign-port"))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        run_id = json.loads(retry.stdout)["run_id"]
        self.assertEqual(self.cli("qa", "environment-stop", "--project", self.project,
                                  "--run-id", run_id).returncode, 0)

    def test_readiness_timeout_cleans_up_owned_process_and_claim(self):
        port = self.unused_port()
        profile = self.profile(port, resource="timeout-resource", services=[
            {"id": "api", "command": [sys.executable, "-c", "import time; time.sleep(30)"],
             "depends_on": [], "readiness": {
                 "url": f"http://127.0.0.1:{port}/", "status": 200, "timeout_seconds": 0.5}}])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("did not become ready", failed.stderr)
        run_id = self.reported_run_id(failed.stderr)
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["services"][0]["status"], "STOPPED")
        self.assertTrue(self.wait_dead(state["services"][0]["pid"]))
        self.assertEqual(self.claim_index()["resources"], {})

    def test_failed_preparation_is_recorded_and_releases_claim(self):
        profile = self.profile(self.unused_port(), resource="prep-resource",
                               prepare=[[sys.executable, "-c", "raise SystemExit(9)"]])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("preparation command failed", failed.stderr)
        run_id = self.reported_run_id(failed.stderr)
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["preparation"][0]["status"], "FAILED")
        # The first failed run remains inspectable; a fresh attempt can own the resource.
        retry = self.cli("qa", "environment-start", "--project", self.project,
                         "--profile", self.profile(self.unused_port(), resource="prep-resource"))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        retry_id = json.loads(retry.stdout)["run_id"]
        self.assertEqual(self.cli("qa", "environment-stop", "--project", self.project,
                                  "--run-id", retry_id).returncode, 0)

    # 5. An owned command is only complete when its process group is empty.

    def test_preparation_that_leaves_a_child_does_not_leave_it_running(self):
        child_file = self.project / "prep-child.pid"
        prepare = [[sys.executable, "-c",
                    "import pathlib, subprocess, sys, time\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                    "pathlib.Path('prep-child.pid').write_text(str(child.pid))\n"
                    "time.sleep(0.3)\n"]]
        profile = self.profile(self.unused_port(), resource="prep-child", prepare=prepare)
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        child_pid = int(child_file.read_text())
        self.assertTrue(self.wait_dead(child_pid), "the preparation descendant must not survive")
        state = self.read_state(run_id)
        self.assertEqual(state["preparation"][0]["status"], "COMPLETE")
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["cleanup"], "COMPLETE")

    def test_declared_cleanup_that_leaves_a_child_does_not_report_complete(self):
        child_file = self.project / "cleanup-child.pid"
        cleanup = [[sys.executable, "-c",
                    "import pathlib, subprocess, sys, time\n"
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
                    "pathlib.Path('cleanup-child.pid').write_text(str(child.pid))\n"
                    "time.sleep(0.3)\n"]]
        profile = self.profile(self.unused_port(), resource="cleanup-child", cleanup=cleanup)
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["cleanup"], "COMPLETE")
        child_pid = int(child_file.read_text())
        self.assertTrue(self.wait_dead(child_pid), "the cleanup descendant must not survive")
        self.assertEqual(self.claim_index()["resources"], {})

    def test_preparation_group_that_cannot_be_cleared_holds_the_claim(self):
        # A group that cannot be proven empty is not reported complete and the
        # claim stays held instead of being released behind a live descendant.
        prepare = [[sys.executable, "-c", "raise SystemExit(0)"]]
        profile = self.profile(self.unused_port(), resource="prep-escape", prepare=prepare)
        with mock.patch.object(devcrew_qa_environment, "_group_members", return_value=[999999]):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                self.in_process_start(profile)
        self.assertIn("process group", str(raised.exception))
        state_path = self.state_paths()[0]
        state = json.loads(state_path.read_text())
        self.assertEqual(state["preparation"][0]["status"], "EFFECT_UNKNOWN")
        self.assertIn("process group", state["preparation"][0]["cleanup_error"])
        self.assertEqual(state["status"], "CLEANUP_FAILED")
        self.assertIn("prep-escape", self.claim_index()["resources"])

    # 6. A refusal before preparation must not run the profile's cleanup.

    def test_start_refusal_before_preparation_does_not_run_declared_cleanup(self):
        data_dir = "var/qa-data"
        foreign = self.project / data_dir
        foreign.mkdir(parents=True)
        (foreign / "foreign.txt").write_text("keep")
        profile = self.profile(
            self.unused_port(), resource="refusal-resource", data_dir=data_dir,
            prepare=[[sys.executable, "-c",
                      "import pathlib; pathlib.Path('prepare-ran').write_text('1')"]],
            cleanup=[[sys.executable, "-c",
                      "import pathlib; pathlib.Path('cleanup-ran').write_text('1')"]])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2)
        self.assertIn("already exists and is not owned", failed.stderr)
        self.assertFalse((self.project / "prepare-ran").exists())
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "declared cleanup must not run before preparation")
        self.assertEqual((foreign / "foreign.txt").read_text(), "keep")
        run_id = self.reported_run_id(failed.stderr)
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["data"]["status"], "FAILED")
        self.assertEqual(state["cleanup"][0]["status"], "NOT_STARTED")
        self.assertEqual(self.claim_index()["resources"], {})

    # 7. Readiness attribution happens before the HTTP answer is trusted.

    def test_readiness_attributes_the_listener_before_the_response(self):
        service = self.service("api", self.unused_port())
        identity = {"start": "first", "executable": "/bin/service",
                    "process_group": 4242, "session": 4242}
        order = []

        def listener(*_args):
            order.append("listener")
            return "owned", ""

        def request(_service):
            order.append("request")
            return 200

        with mock.patch.object(devcrew_qa_environment, "_listener_check", side_effect=listener), \
                mock.patch.object(devcrew_qa_environment, "_readiness_status", side_effect=request), \
                mock.patch.object(devcrew_qa_environment, "_process_identity", return_value=identity):
            self.assertTrue(devcrew_qa_environment._confirm_readiness(
                service, identity, mock.Mock(pid=4242)))
        self.assertEqual(order[0], "listener")
        self.assertEqual(order[1], "request")
        self.assertIn("listener", order[2:], "the listener is re-attributed after the response")

    def test_foreign_answer_released_before_attribution_cannot_become_ready(self):
        port = self.unused_port()

        class AnswerThenRelease(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"ready")
                threading.Thread(target=self.server.release, daemon=True).start()

            def log_message(self, *_args):
                pass

        server = self.start_foreign_server(handler=AnswerThenRelease, port=port)
        server.release = lambda: (server.shutdown(), server.server_close())
        service = [sys.executable, "-c",
                   "import http.server, time\n"
                   "class H(http.server.BaseHTTPRequestHandler):\n"
                   "    def do_GET(self):\n"
                   "        self.send_response(200); self.end_headers(); self.wfile.write(b'ready')\n"
                   "    def log_message(self, *args):\n"
                   "        pass\n"
                   "deadline = time.monotonic() + 30\n"
                   "while True:\n"
                   "    try:\n"
                   f"        server = http.server.HTTPServer(('127.0.0.1', {port}), H)\n"
                   "        break\n"
                   "    except OSError:\n"
                   "        if time.monotonic() > deadline:\n"
                   "            raise SystemExit(5)\n"
                   "        time.sleep(0.01)\n"
                   "server.serve_forever()\n"]
        profile = self.profile(port, resource="answer-race", services=[
            {"id": "api", "command": service, "depends_on": [], "readiness": {
                "url": f"http://127.0.0.1:{port}/", "status": 200, "timeout_seconds": 5}}])
        with mock.patch.object(devcrew_qa_environment, "_assert_port_free", lambda _service: None):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                self.in_process_start(profile)
        self.assertIn("outside its verified process group", str(raised.exception))
        state = json.loads(self.state_paths()[0].read_text())
        self.assertEqual(state["status"], "STOPPED")
        self.assertNotEqual(state["services"][0]["status"], "READY")
        self.assertEqual(self.claim_index()["resources"], {})

    # 8. Stop re-validates the recorded data path before removing anything.

    def test_stop_refuses_a_recorded_data_path_outside_the_project(self):
        run_id = uuid.uuid4().hex
        outside = pathlib.Path(self.temp.name) / "outside"
        data = outside / "qa-data"
        data.mkdir(parents=True)
        (data / ".devcrew-qa-owner.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id, "project_root": str(self.project)}))
        (data / "keep.txt").write_text("keep")
        self.write_run(run_id, {
            "status": "READY", "services": [],
            "data": {"dir": "../outside/qa-data", "path": str(data), "status": "CREATED", "owned": True}})
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertEqual(result["data"]["status"], "EFFECT_UNKNOWN")
        self.assertIn("nothing was removed", result["data"]["cleanup_error"])
        self.assertEqual((data / "keep.txt").read_text(), "keep")

    def test_stop_refuses_a_data_path_whose_parent_became_a_symlink(self):
        run_id = uuid.uuid4().hex
        outside = pathlib.Path(self.temp.name) / "outside"
        data = outside / "qa-data"
        data.mkdir(parents=True)
        (data / ".devcrew-qa-owner.json").write_text(json.dumps({
            "schema_version": 1, "run_id": run_id, "project_root": str(self.project)}))
        (data / "keep.txt").write_text("keep")
        recorded = self.project / "var/qa-data"
        self.write_run(run_id, {
            "status": "READY", "services": [],
            "data": {"dir": "var/qa-data", "path": str(recorded), "status": "CREATED", "owned": True}})
        (self.project / "var").symlink_to(outside, target_is_directory=True)
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2)
        result = json.loads(stopped.stdout)
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertIn("nothing was removed", result["data"]["cleanup_error"])
        self.assertTrue((data / "keep.txt").is_file(), "data behind the symlink must survive")


    # 9. A repeated stop reconciles cleanup steps that already started.

    def test_repeated_stop_does_not_forget_a_failed_cleanup(self):
        port = self.unused_port()
        # A foreign listener makes the launch fail after data creation but before
        # any service record gets a PID, which is the case the saved cleanup
        # failure must survive.
        self.start_foreign_server(port=port)
        profile = self.profile(
            port, resource="cleanup-failure", data_dir="var/qa-data",
            cleanup=[[sys.executable, "-c", "raise SystemExit(9)"]])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2)
        run_id = self.reported_run_id(failed.stderr)
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "CLEANUP_FAILED")
        self.assertEqual(state["cleanup"][0]["status"], "FAILED")
        self.assertEqual(state["data"]["status"], "REMOVED")
        self.assertIn("cleanup-failure", self.claim_index()["resources"])

        repeated = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(repeated.returncode, 2, repeated.stdout)
        result = json.loads(repeated.stdout)
        self.assertEqual(result["status"], "CLEANUP_FAILED")
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertEqual(result["cleanup_steps"][0]["status"], "FAILED")
        self.assertIn("cleanup-failure", self.claim_index()["resources"],
                      "claims stay held while a cleanup step is unresolved")

    # 10. Data ownership is confirmed only after the marker is written.

    def test_data_ownership_is_confirmed_only_after_the_marker(self):
        run_id = uuid.uuid4().hex
        state = {"schema_version": 1, "run_id": run_id, "project_root": str(self.project),
                 "data": {"dir": "var/qa-data", "path": None, "status": "NOT_CREATED"},
                 "preparation": [], "cleanup": [], "services": []}
        snapshots = []
        original = devcrew_qa_environment._write

        def capture(path, value):
            if isinstance(value, dict) and "data" in value:
                snapshots.append(copy.deepcopy(value))
            original(path, value)

        with mock.patch.object(devcrew_qa_environment, "_write", side_effect=capture):
            devcrew_qa_environment._create_data_dir(
                state, self.run_id_path(run_id), self.project.resolve(), PATHS, run_id)
        created = [item for item in snapshots if item["data"]["status"] == "CREATED"]
        self.assertTrue(created)
        self.assertIs(created[0]["data"]["owned"], False,
                      "creation intent must not claim confirmed ownership")
        self.assertIs(created[-1]["data"]["owned"], True)
        self.assertTrue((self.project / "var/qa-data/.devcrew-qa-owner.json").is_file())

    def test_unconfirmed_data_creation_intent_does_not_run_declared_cleanup(self):
        run_id = uuid.uuid4().hex
        data = self.project / "var/qa-data"
        data.mkdir(parents=True)
        (data / "partial.txt").write_text("partial")
        profile = self.profile(
            self.unused_port(), resource="intent-resource", data_dir="var/qa-data",
            cleanup=[[sys.executable, "-c",
                      "import pathlib; pathlib.Path('cleanup-ran').write_text('1')"]])
        profile_value = json.loads(profile.read_text())
        self.write_run(run_id, {
            "status": "STARTING", "services": [],
            "profile_path": str(profile.resolve()),
            "profile_sha256": devcrew_qa_environment._digest(profile_value),
            "cleanup": [{"index": 0, "status": "NOT_STARTED", "pid": None,
                         "identity": None, "exit_code": None}],
            "data": {"dir": "var/qa-data", "path": str(data), "status": "CREATED", "owned": False}},
            claims=("intent-resource",))
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "declared cleanup must not run before ownership is confirmed")
        self.assertEqual((data / "partial.txt").read_text(), "partial")
        self.assertIn("intent-resource", self.claim_index()["resources"])
        result = json.loads(stopped.stdout)
        self.assertEqual(result["data"]["status"], "EFFECT_UNKNOWN")

    # 11. An unverifiable leader identity never signals its group.

    def test_group_clear_refuses_an_unverifiable_leader(self):
        entry = {"pid": 4242, "identity": {"start": "first", "process_group": 4242},
                 "process_group": 4242}
        with mock.patch.object(devcrew_qa_environment, "_group_members", return_value=[4242]), \
                mock.patch.object(devcrew_qa_environment, "_process_state", return_value="unknown"), \
                mock.patch.object(devcrew_qa_environment, "_terminate") as terminate:
            ok, detail = devcrew_qa_environment._command_group_clear(entry, {})
        self.assertFalse(ok)
        self.assertIn("could not be verified", detail)
        terminate.assert_not_called()


    # 12. Profile cleanup never runs against data it no longer owns.

    def _cleanup_gate_profile(self, port):
        return self.profile(
            port, resource="gate-resource", data_dir="var/qa-data",
            cleanup=[[sys.executable, "-c",
                      "import pathlib; pathlib.Path('cleanup-ran').write_text('1')"]])

    def test_declared_cleanup_does_not_run_after_the_data_directory_is_replaced(self):
        port = self.unused_port()
        profile = self._cleanup_gate_profile(port)
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        data = self.project / "var/qa-data"
        shutil.rmtree(data)
        data.mkdir()
        (data / "replacement.txt").write_text("replacement")
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "profile cleanup must not run against a replaced directory")
        self.assertEqual((data / "replacement.txt").read_text(), "replacement")
        result = json.loads(stopped.stdout)
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertIn("gate-resource", self.claim_index()["resources"])

        repeated = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(repeated.returncode, 2, repeated.stdout)
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "a repeated stop must not run cleanup against the replaced directory")
        repeated_result = json.loads(repeated.stdout)
        self.assertEqual(repeated_result["cleanup"], "INCOMPLETE")
        self.assertEqual(repeated_result["cleanup_steps"][0]["status"], "NOT_STARTED")
        self.assertIn("not confirmed as owned", repeated_result["cleanup_steps"][0]["cleanup_error"])
        self.assertIn("gate-resource", self.claim_index()["resources"])

    def test_declared_cleanup_does_not_run_when_the_data_parent_becomes_a_symlink(self):
        port = self.unused_port()
        profile = self._cleanup_gate_profile(port)
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        outside = pathlib.Path(self.temp.name) / "outside-var"
        (self.project / "var").rename(outside)
        (self.project / "var").symlink_to(outside, target_is_directory=True)
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 2, stopped.stdout)
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "profile cleanup must not run through a symlinked parent")
        self.assertTrue((outside / "qa-data").is_dir())
        result = json.loads(stopped.stdout)
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        self.assertIn("gate-resource", self.claim_index()["resources"])

        repeated = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(repeated.returncode, 2, repeated.stdout)
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "a repeated stop must not run cleanup through the symlinked parent")
        self.assertTrue((outside / "qa-data").is_dir())
        repeated_result = json.loads(repeated.stdout)
        self.assertEqual(repeated_result["cleanup"], "INCOMPLETE")
        self.assertEqual(repeated_result["cleanup_steps"][0]["status"], "NOT_STARTED")
        self.assertIn("not run", repeated_result["cleanup_steps"][0]["cleanup_error"])


    # 13. A READY record must carry the identity cleanup would rely on.

    def test_start_intent_validates_any_recorded_process_identity(self):
        run_id = uuid.uuid4().hex
        service = {"id": "api", "status": "START_INTENT", "pid": 4242, "identity": None}
        self.write_run(run_id, {"status": "STARTING", "services": [service]})
        state_path = self.run_id_path(run_id)
        read = devcrew_qa_environment._read_state
        self.assertIsNone(read(state_path, run_id, self.project)["services"][0]["identity"])

        service["identity"] = {"executable": "/bin/service", "process_group": 4242, "session": 4242}
        self.write_run(run_id, {"status": "STARTING", "services": [service]})
        with self.assertRaises(devcrew_qa_environment.EnvironmentError):
            read(state_path, run_id, self.project)

    def test_unavailable_group_inspection_leaves_exited_leaders_group_alone(self):
        identity = {"start": "first", "executable": "/bin/service",
                    "process_group": 4242, "session": 4242}
        entry = {"id": "api", "status": "READY", "pid": 4242, "identity": identity}
        with mock.patch.object(devcrew_qa_environment, "_process_state", return_value="gone"), \
                mock.patch.object(devcrew_qa_environment, "_group_members", return_value=None), \
                mock.patch.object(devcrew_qa_environment, "_terminate", return_value=False) as terminate:
            for cleanup in (devcrew_qa_environment._cleanup_service,
                            devcrew_qa_environment._command_group_clear):
                with self.subTest(cleanup=cleanup.__name__):
                    ok, detail = cleanup(entry, {})
                    self.assertFalse(ok)
                    self.assertIn("could not be inspected", detail)
            terminate.assert_not_called()

    def test_ready_record_without_the_full_identity_is_refused_before_any_change(self):
        variants = {
            "missing pid": lambda pid, identity: (None, identity),
            "missing identity": lambda pid, identity: (pid, None),
            "identity without start time": lambda pid, identity: (
                pid, {key: value for key, value in identity.items() if key != "start"}),
            "identity without process group": lambda pid, identity: (
                pid, {key: value for key, value in identity.items() if key != "process_group"}),
            "identity without session": lambda pid, identity: (
                pid, {key: value for key, value in identity.items() if key != "session"}),
            "start with the wrong type": lambda pid, identity: (pid, {**identity, "start": 123}),
            "executable with the wrong type": lambda pid, identity: (
                pid, {**identity, "executable": 123}),
            "empty executable": lambda pid, identity: (pid, {**identity, "executable": ""}),
        }
        index_path = self.project / ".codex/checks/qa-environments/resources.json"
        for label, build in variants.items():
            with self.subTest(label):
                sleeper = subprocess.Popen(
                    [sys.executable, "-c", "import time; time.sleep(120)"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    identity = devcrew_qa_environment._process_identity(sleeper.pid)
                    self.assertIsNotNone(identity)
                    pid, saved_identity = build(sleeper.pid, identity)
                    run_id = uuid.uuid4().hex
                    data = self.project / "var/qa-data"
                    data.mkdir(parents=True, exist_ok=True)
                    (data / ".devcrew-qa-owner.json").write_text(json.dumps({
                        "schema_version": 1, "run_id": run_id, "project_root": str(self.project)}))
                    (data / "keep.txt").write_text("keep")
                    self.write_run(run_id, {
                        "status": "READY", "resources": ["fixture-db"], "preparation": [],
                        "data": {"dir": "var/qa-data", "path": str(data),
                                 "status": "CREATED", "owned": True},
                        "services": [{"id": "api", "status": "READY",
                                      "pid": pid, "identity": saved_identity}],
                    }, claims=["fixture-db"])
                    before = self.run_id_path(run_id).read_text()
                    index_before = index_path.read_text()

                    stopped = self.cli("qa", "environment-stop", "--project", self.project,
                                       "--run-id", run_id)
                    self.assertEqual(stopped.returncode, 2)
                    self.assertIsNone(sleeper.poll(), "foreign process was signalled")
                    self.assertEqual(self.run_id_path(run_id).read_text(), before,
                                     "an incomplete READY record must not be rewritten")
                    self.assertEqual(index_path.read_text(), index_before,
                                     "an incomplete READY record must not release claims")
                    self.assertEqual((data / "keep.txt").read_text(), "keep",
                                     "the confirmed data directory must survive")
                    self.assertEqual(stopped.stdout, "")
                    self.assertIn("service records are invalid", stopped.stderr)
                    status = self.cli("qa", "environment-status", "--project", self.project,
                                      "--run-id", run_id)
                    self.assertEqual(status.returncode, 2)
                finally:
                    sleeper.terminate()
                    sleeper.wait(timeout=10)

    # 14. A started data removal keeps its stage until the tomb is really gone.

    def test_repeated_stop_retries_a_pending_data_removal(self):
        port = self.unused_port()
        profile = self.profile(port, resource="pending-removal", data_dir="var/qa-data")
        # Start in process so the recorded project root matches the stop call.
        self.assertEqual(self.in_process_start(profile), 0)
        run_id = self.state_paths()[0].parent.name
        data = self.project / "var/qa-data"
        tomb = data.with_name(f"qa-data.deleting-{run_id[:8]}")
        real_rmtree = shutil.rmtree

        def failing_rmtree(path, *args, **kwargs):
            if pathlib.Path(path).name == tomb.name:
                raise OSError("simulated removal failure")
            return real_rmtree(path, *args, **kwargs)

        with mock.patch.object(devcrew_qa_environment.shutil, "rmtree", failing_rmtree):
            code, out, _err = self.in_process_stop(run_id)
        self.assertEqual(code, 2, out)
        result = json.loads(out)
        self.assertEqual(result["status"], "CLEANUP_FAILED")
        self.assertEqual(result["cleanup"], "INCOMPLETE")
        state = self.read_state(run_id)
        self.assertEqual(state["data"]["status"], "REMOVAL_INTENT")
        self.assertEqual(state["data"]["removal_path"], str(tomb))
        self.assertFalse(data.exists(), "the directory was already renamed for removal")
        self.assertTrue(tomb.is_dir(), "the renamed directory still holds the data")
        self.assertIn("pending-removal", self.claim_index()["resources"])

        code, out, err = self.in_process_stop(run_id)
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "STOPPED")
        self.assertEqual(result["cleanup"], "COMPLETE")
        self.assertFalse(tomb.exists(), "the repeated stop must finish the pending removal")
        self.assertEqual(self.read_state(run_id)["data"]["status"], "REMOVED")
        self.assertEqual(self.claim_index()["resources"], {})

    # 15. Readiness uses the same process-group boundary as cleanup.

    def test_readiness_requires_the_listener_in_the_owned_process_group(self):
        with mock.patch.object(devcrew_qa_environment, "_tcp_listener_pids", return_value={5001}), \
                mock.patch.object(devcrew_qa_environment.os, "getpgid", return_value=4242):
            state, _detail = devcrew_qa_environment._listener_check(4242, 4242)
        self.assertEqual(state, "owned")
        with mock.patch.object(devcrew_qa_environment, "_tcp_listener_pids", return_value={5001}), \
                mock.patch.object(devcrew_qa_environment.os, "getpgid", return_value=5001), \
                mock.patch.object(devcrew_qa_environment.os, "getsid", return_value=4242):
            state, detail = devcrew_qa_environment._listener_check(4242, 4242)
        self.assertEqual(state, "foreign",
                         "a listener in another group of the same session is outside the owned group")
        self.assertIn("outside the verified process group", detail)

    def test_readiness_refuses_a_listener_in_another_group_of_the_same_session(self):
        port = self.unused_port()
        worker_code = (
            "import http.server\n"
            "class H(http.server.BaseHTTPRequestHandler):\n"
            "    def do_GET(self):\n"
            "        self.send_response(200); self.end_headers()\n"
            "    def log_message(self, *args):\n"
            "        pass\n"
            f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n")
        command = [
            sys.executable, "-c",
            "import os, pathlib, subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', {worker_code!r}],\n"
            "    preexec_fn=lambda: os.setpgid(0, 0))\n"
            "pathlib.Path('worker.pid').write_text(str(child.pid))\n"
            "time.sleep(120)\n"]
        profile = self.profile(port, resource="session-worker", services=[
            {"id": "api", "command": command, "depends_on": [],
             "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 200,
                           "timeout_seconds": 5}}])
        try:
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                self.in_process_start(profile)
            self.assertIn("outside its verified process group", str(raised.exception))
            state = json.loads(self.state_paths()[0].read_text())
            self.assertNotEqual(state["services"][0]["status"], "READY")
            self.assertEqual(self.claim_index()["resources"], {})
        finally:
            worker_path = self.project / "worker.pid"
            if worker_path.exists():
                try:
                    os.kill(int(worker_path.read_text()), signal.SIGKILL)
                except (OSError, ValueError):
                    pass

    # 16. Dependencies must still be live while later services start.

    def test_dependency_that_exits_during_another_service_start_is_not_ready(self):
        db_port, api_port = self.unused_port(), self.unused_port()
        db_code = [
            "import http.server, threading, time",
            "class H(http.server.BaseHTTPRequestHandler):",
            "    def do_GET(self):",
            "        self.send_response(200); self.end_headers(); self.wfile.write(b'ready')",
            "    def log_message(self, *args):",
            "        pass",
            f"server = http.server.HTTPServer(('127.0.0.1', {db_port}), H)",
            "threading.Thread(target=server.serve_forever, daemon=True).start()",
            "time.sleep(1.5)",
            "server.shutdown()",
        ]
        profile = self.profile(api_port, resource="dependency-exit", services=[
            {"id": "db", "command": [sys.executable, "-c", "\n".join(db_code)],
             "depends_on": [],
             "readiness": {"url": f"http://127.0.0.1:{db_port}/", "status": 200,
                           "timeout_seconds": 5}},
            self.service("api", api_port, depends_on=["db"], delay=3.0, readiness_timeout=10),
        ])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2, failed.stdout)
        self.assertIn("db", failed.stderr)
        run_id = self.reported_run_id(failed.stderr)
        state = self.read_state(run_id)
        self.assertNotEqual(state["status"], "READY")
        self.assertNotEqual(state["services"][1]["status"], "READY")
        self.assertEqual(self.claim_index()["resources"], {})

    def test_publish_refuses_a_dependency_that_stopped_answering(self):
        services = [self.service("db", 4242)]
        identity = {"start": "first", "executable": "/bin/service",
                    "process_group": 4242, "session": 4242}
        states = [{"id": "db", "status": "READY", "pid": 4242, "identity": identity}]
        child = mock.Mock(pid=4242)
        child.poll.return_value = None
        with mock.patch.object(devcrew_qa_environment, "_process_state", return_value="alive"), \
                mock.patch.object(devcrew_qa_environment, "_confirm_readiness", return_value=False):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                devcrew_qa_environment._verify_ready_services(
                    services, states, {4242: child}, reprobe=True)
        self.assertIn("no longer ready", str(raised.exception))

    def test_final_verification_rechecks_services_after_the_last_probe(self):
        services = [self.service("db", 4242), self.service("api", 4243)]
        identity = {"start": "first", "executable": "/bin/service",
                    "process_group": 4242, "session": 4242}
        db, api = mock.Mock(pid=4242), mock.Mock(pid=4243)
        db.poll.return_value = None
        api.poll.return_value = None
        states = [
            {"id": "db", "status": "READY", "pid": 4242, "identity": identity},
            {"id": "api", "status": "READY", "pid": 4243,
             "identity": {**identity, "process_group": 4243, "session": 4243}},
        ]

        def probe(service, _identity, _child):
            if service["id"] == "api":
                # The dependency exits while the final request for the API runs.
                db.poll.return_value = 1
            return True

        with mock.patch.object(devcrew_qa_environment, "_process_state", return_value="alive"), \
                mock.patch.object(devcrew_qa_environment, "_confirm_readiness", side_effect=probe):
            with self.assertRaises(devcrew_qa_environment.EnvironmentError) as raised:
                devcrew_qa_environment._verify_ready_services(
                    services, states, {4242: db, 4243: api}, reprobe=True)
        self.assertIn("db", str(raised.exception))

    # 17. A command that never spawned is not a side effect for declared cleanup.

    def test_spawn_failure_before_preparation_does_not_run_declared_cleanup(self):
        missing = pathlib.Path(self.temp.name) / "missing-command"
        profile = self.profile(
            self.unused_port(), resource="spawn-refusal", prepare=[[str(missing)]],
            cleanup=[[sys.executable, "-c",
                      "import pathlib; pathlib.Path('cleanup-ran').write_text('1')"]])
        failed = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(failed.returncode, 2)
        run_id = self.reported_run_id(failed.stderr)
        self.assertFalse((self.project / "cleanup-ran").exists(),
                         "declared cleanup must not run when no process ever started")
        state = self.read_state(run_id)
        self.assertEqual(state["status"], "STOPPED")
        self.assertEqual(state["preparation"][0]["status"], "FAILED")
        self.assertIsNone(state["preparation"][0]["pid"])
        self.assertEqual(state["cleanup"][0]["status"], "NOT_STARTED")
        self.assertEqual(self.claim_index()["resources"], {})

    # 18. Declared cleanup keeps the profile's order.

    def test_declared_cleanup_runs_in_profile_order(self):
        port = self.unused_port()
        profile = self.profile(
            port, resource="ordered-cleanup",
            prepare=[[sys.executable, "-c", "pass"]],
            cleanup=[
                [sys.executable, "-c",
                 "import pathlib; pathlib.Path('cleanup-order.log').open('a').write('first;')"],
                [sys.executable, "-c",
                 "import pathlib; pathlib.Path('cleanup-order.log').open('a').write('second;')"],
            ])
        started = self.cli("qa", "environment-start", "--project", self.project, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr)
        run_id = json.loads(started.stdout)["run_id"]
        stopped = self.cli("qa", "environment-stop", "--project", self.project, "--run-id", run_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)
        self.assertEqual((self.project / "cleanup-order.log").read_text(), "first;second;")


if __name__ == "__main__":
    unittest.main()
