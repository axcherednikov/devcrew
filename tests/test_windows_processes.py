"""Native Windows regression coverage: real jobs, descendants and TCP listeners."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@unittest.skipUnless(os.name == "nt", "requires native Windows kernel APIs")
class WindowsProcessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(SCRIPTS))
        cls.addClassCleanup(sys.path.remove, str(SCRIPTS))
        import devcrew_winprocess
        cls.win = devcrew_winprocess

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "devcrew.py"), *map(str, args)],
                              capture_output=True, text=True, timeout=35)

    def test_run_check_timeout_stops_nested_processes(self):
        init = self.cli("init", "--project", self.root)
        self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
        marker = self.root / "child.pid"
        command = ("import pathlib,subprocess,sys,time; "
                   "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
                   f"pathlib.Path({str(marker)!r}).write_text(str(child.pid)); time.sleep(60)")
        result = self.cli("run-check", "--project", self.root, "--name", "timeout", "--timeout", "2",
                          "--", sys.executable, "-c", command)
        self.assertNotEqual(result.returncode, 0)
        records = list((self.root / ".codex/checks").glob("*-timeout-*.json"))
        self.assertEqual(len(records), 1, result.stdout + result.stderr)
        record = json.loads(records[0].read_text())
        self.assertEqual((record["status"], record["timeout_kind"]), ("timeout", "wall"))
        self.assertFalse(self.win.alive(int(marker.read_text())))

    def test_qa_service_survives_start_cli_and_stop_reclaims_its_listener(self):
        init = self.cli("init", "--project", self.root)
        self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        script = ("import pathlib,subprocess,sys; "
                  f"p=subprocess.Popen([sys.executable,'-m','http.server','{port}','--bind','127.0.0.1']); "
                  "pathlib.Path('listener.pid').write_text(str(p.pid)); sys.exit(p.wait())")
        profile = self.root / "qa.json"
        profile.write_text(json.dumps({"schema_version": 1, "id": "native", "resources": [],
            "environment": [], "prepare": [], "services": [{"id": "web", "depends_on": [],
                "command": [sys.executable, "-c", script],
                "readiness": {"url": f"http://127.0.0.1:{port}/", "status": 200, "timeout_seconds": 10}}]}))
        started = self.cli("qa", "environment-start", "--project", self.root, "--profile", profile)
        self.assertEqual(started.returncode, 0, started.stderr + started.stdout)
        run_id = json.loads(started.stdout)["run_id"]
        def stop():
            return self.cli("qa", "environment-stop", "--project", self.root, "--run-id", run_id)
        self.addCleanup(stop)
        pid = int((self.root / "listener.pid").read_text())
        state = json.loads(next((self.root / ".codex/checks/qa-environments/runs").glob("*/state.json")).read_text())
        service = state["services"][0]
        identity = self.win.identity(service["pid"])
        group = identity["process_group"]
        self.assertEqual(identity, service["identity"])
        self.assertTrue(self.win.owns_group(service["pid"], identity, group))
        self.assertFalse(self.win.owns_group(service["pid"], dict(identity, start="0"), group))
        self.assertIn(pid, self.win.members(group))
        self.assertIn(pid, self.win.listener_pids(port))
        stopped = stop()
        self.assertEqual(stopped.returncode, 0, stopped.stderr + stopped.stdout)
        self.assertFalse(self.win.alive(pid))
        self.assertNotIn(pid, self.win.listener_pids(port))

    def test_child_keeps_resource_handle_until_job_termination(self):
        import devcrew_resources as resources
        with resources.ResourceLock(self.root, "database", self.root) as lock:
            child = self.win.spawn([sys.executable, "-c", "import time; time.sleep(60)"],
                                   resource_fd=lock.handle.fileno())
        self.addCleanup(self.win.release, child)
        # Only the direct child's duplicated handle still holds the lease.
        self.assertTrue(resources.status(self.root, "database")["busy"])
        with self.assertRaisesRegex(ValueError, "busy"):
            with resources.ResourceLock(self.root, "database", self.root):
                self.fail("a second writer acquired the live child's resource")
        self.win.release(child)
        child.wait(timeout=5)
        self.assertFalse(resources.status(self.root, "database")["busy"])

    def test_force_killed_run_check_releases_resource_after_tree_exit(self):
        init = self.cli("init", "--project", self.root)
        self.assertEqual(init.returncode, 0, init.stderr + init.stdout)
        locks = self.root / ".codex/checks/locks"
        wrapper = subprocess.Popen([sys.executable, "-B", str(SCRIPTS / "devcrew.py"), "run-check",
                                    "--project", str(self.root), "--name", "resource", "--resource",
                                    "database", "--resource-dir", str(locks), "--", sys.executable,
                                    "-c", "import time; time.sleep(60)"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: wrapper.kill() if wrapper.poll() is None else None)
        event = json.loads(wrapper.stderr.readline())
        self.assertEqual(event["event"], "check_started")
        check_status = ("resource-status", "--resource-dir", locks, "--name", "database")
        self.assertTrue(json.loads(self.cli(*check_status).stdout)["busy"])
        wrapper.kill()
        wrapper.communicate(timeout=8)
        deadline = time.monotonic() + 5
        while self.win.alive(event["pid"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.win.alive(event["pid"]))
        self.assertFalse(json.loads(self.cli(*check_status).stdout)["busy"])

    def test_parallel_worker_crash_reclaims_descendants(self):
        source = self.root / "test_worker.py"
        source.write_text(
            "import os, pathlib, subprocess, sys, unittest\n"
            "class Worker(unittest.TestCase):\n"
            "    def test_crash(self):\n"
            "        p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
            "        pathlib.Path('worker-child.pid').write_text(str(p.pid))\n"
            "        os._exit(9)\n")
        result = subprocess.run([sys.executable, str(SCRIPTS / "run_tests.py"), "-j", "2",
                                 "-s", str(self.root), "test_worker.Worker"], cwd=self.root,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
        self.assertIn("exited without a result", result.stderr)
        self.assertFalse(self.win.alive(int((self.root / "worker-child.pid").read_text())))

    def test_startup_job_reclaims_child_if_wrapper_dies_before_assignment(self):
        marker = self.root / "suspended.pid"
        source = """
import os, pathlib, sys
sys.path.insert(0, sys.argv[1])
import devcrew_winprocess as win

def interrupt(pid, **_):
    pathlib.Path(sys.argv[2]).write_text(str(pid))
    os._exit(7)
win.attach = interrupt
win.spawn([sys.executable, '-c', 'raise AssertionError("command ran before ownership")'])
"""
        owner = subprocess.run([sys.executable, "-c", source, str(SCRIPTS), str(marker)], timeout=15)
        self.assertEqual(owner.returncode, 7)
        pid = int(marker.read_text())
        deadline = time.monotonic() + 5
        while self.win.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.win.alive(pid))

    def test_run_check_job_terminates_on_owner_exit(self):
        # An abruptly terminated CLI cannot run a Python finally block.
        marker = self.root / "owned.pid"
        owner = subprocess.run([sys.executable, "-c",
            "import os,pathlib,sys; sys.path.insert(0,sys.argv[1]); import devcrew_winprocess as win; "
            "child=win.spawn([sys.executable,'-c','import time; time.sleep(60)']); "
            "pathlib.Path(sys.argv[2]).write_text(str(child.pid)); os._exit(7)",
            str(SCRIPTS), str(marker)], timeout=15)
        self.assertEqual(owner.returncode, 7)
        pid = int(marker.read_text())
        deadline = time.monotonic() + 5
        while self.win.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(self.win.alive(pid))
