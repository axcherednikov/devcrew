"""Synthetic projects exercise the class-grouped unittest runner."""

import json
import importlib.util
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts/run_tests.py"
RUNNER_SPEC = importlib.util.spec_from_file_location("devcrew_parallel_runner", RUNNER)
parallel_runner = importlib.util.module_from_spec(RUNNER_SPEC)
RUNNER_SPEC.loader.exec_module(parallel_runner)


class ParallelRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = pathlib.Path(self.temp.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()

    def write_module(self, name, source):
        path = self.tests / name
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return path

    def run_runner(self, *args, timeout=30):
        return subprocess.run(
            [sys.executable, "-B", str(RUNNER), "-s", str(self.tests), *args],
            cwd=self.root,
            env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_process_table_parses_short_commands_and_excludes_zombies(self):
        result = subprocess.CompletedProcess(
            args=["ps"],
            returncode=0,
            stdout=(
                "101 1 101 S Sat Oct 24 12:34:56 2026 x\n"
                "102 101 101 Z Sat Oct 24 12:34:56 2026 child\n"
            ),
        )
        with patch.object(parallel_runner.subprocess, "run", return_value=result) as ps_run:
            table = parallel_runner._ps_table()
        ps_run.assert_called_once()
        self.assertIn("stat=", ps_run.call_args.args[0][2])
        self.assertEqual(table[101]["ppid"], 1)
        self.assertEqual(table[101]["pgid"], 101)
        self.assertEqual(table[101]["state"], "S")
        self.assertEqual(table[101]["identity"], ("Sat Oct 24 12:34:56 2026", "x"))
        self.assertEqual(table[102]["state"], "Z")
        with patch.object(parallel_runner, "_ps_table", return_value=table):
            alive = parallel_runner._owned_alive(
                {pid: process["identity"] for pid, process in table.items()}
            )
        self.assertEqual(alive, {101: table[101]["identity"]})

    def test_serial_reenters_reviewer_class_after_another_class(self):
        selected = "test_agent_context.AgentContext.test_public_reviewer_route_rejects_stale_caller_selection"
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(RUNNER),
                "--jobs",
                "1",
                selected,
                "test_parallel_runner.ParallelRunnerTests.test_process_table_parses_short_commands_and_excludes_zombies",
                selected,
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("Ran 3 tests", result.stderr)

    def test_registered_orphan_snapshot_excludes_unrelated_processes(self):
        table = {
            102: {"ppid": 1, "pgid": 102, "state": "S", "identity": ("born", "child")},
            103: {"ppid": 102, "pgid": 102, "state": "S", "identity": ("born", "grandchild")},
            104: {"ppid": 1, "pgid": 104, "state": "S", "identity": ("born", "unrelated")},
        }
        with patch.object(parallel_runner, "_ps_table", return_value=table):
            owned, warning = parallel_runner._snapshot_worker_tree(101, registered_pid=102)
        self.assertIsNone(warning)
        self.assertEqual(set(owned), {102, 103})

    def test_fast_worker_crash_cleans_up_child_without_polling_delay(self):
        for detached in (False, True):
            with self.subTest(detached=detached):
                child_pid_file = self.root / f"fast-child-{detached}.pid"
                self.write_module(
                    "test_fast_crash.py",
                    f"""
                    import os, pathlib, subprocess, sys, unittest
                    class FastCrash(unittest.TestCase):
                        def test_crash(self):
                            child = subprocess.Popen(
                                [sys.executable, '-c', 'import time; time.sleep(60)'],
                                start_new_session={detached!r},
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                            pathlib.Path({str(child_pid_file)!r}).write_text(str(child.pid))
                            os._exit(7)
                """,
                )
                child_pid = None
                try:
                    result = self.run_runner("-j", "2", "test_fast_crash.FastCrash")
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("exited without a result", result.stderr)
                    self.assertTrue(child_pid_file.exists(), result.stderr)
                    child_pid = int(child_pid_file.read_text())
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline and _pid_exists(child_pid):
                        time.sleep(0.05)
                    self.assertFalse(_pid_exists(child_pid), "fast-crash child survived runner")
                finally:
                    if child_pid is None and child_pid_file.exists():
                        child_pid = int(child_pid_file.read_text())
                    if child_pid is not None and _pid_exists(child_pid):
                        os.kill(child_pid, signal.SIGKILL)

    def test_worker_death_during_registration_ack_still_cleans_up(self):
        worker_file, child_file = self.root / "worker.pid", self.root / "ack-child.pid"
        child_source = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(child_file)!r}).write_text(str(os.getpid())); time.sleep(60)"
        )
        self.write_module(
            "test_ack_crash.py",
            f"""
            import os, pathlib, subprocess, sys, unittest
            class AckCrash(unittest.TestCase):
                def test_child(self):
                    pathlib.Path({str(worker_file)!r}).write_text(str(os.getpid()))
                    subprocess.Popen([sys.executable, '-c', {child_source!r}],
                                     start_new_session=True,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        """,
        )
        launcher = self.root / "ack_launcher.py"
        launcher.write_text(
            textwrap.dedent(f"""
            import os, pathlib, signal, sys, time
            sys.path.insert(0, {str(RUNNER.parent)!r})
            import run_tests
            from multiprocessing.connection import Connection
            if __name__ == '__main__':
                original_send = Connection.send
                def fail_registration_ack(connection, value):
                    if value is True:
                        deadline = time.monotonic() + 3
                        while not pathlib.Path({str(child_file)!r}).exists() and time.monotonic() < deadline:
                            time.sleep(.01)
                        os.kill(int(pathlib.Path({str(worker_file)!r}).read_text()), signal.SIGKILL)
                        raise BrokenPipeError('worker died before registration acknowledgement')
                    return original_send(connection, value)
                Connection.send = fail_registration_ack
                raise SystemExit(run_tests.main())
        """),
            encoding="utf-8",
        )
        try:
            result = subprocess.run(
                [sys.executable, "-B", str(launcher), "-s", str(self.tests), "-j", "2"],
                cwd=self.root,
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 1, result.stderr + result.stdout)
            self.assertIn("exited without a result", result.stderr)
            self.assertNotIn("Traceback", result.stderr)
            self.assertTrue(child_file.exists())
            self.assertFalse(
                _pid_exists(int(child_file.read_text())), "registration child survived"
            )
        finally:
            for pid_file in (worker_file, child_file):
                if pid_file.exists():
                    pid = int(pid_file.read_text())
                    if _pid_exists(pid):
                        os.kill(pid, signal.SIGKILL)

    def test_list_selectors_duplicates_empty_and_invalid_jobs(self):
        self.write_module(
            "test_alpha.py",
            """
            import unittest
            class Alpha(unittest.TestCase):
                def test_one(self): pass
                def test_two(self): pass
        """,
        )
        listed = self.run_runner("--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("test_alpha.Alpha.test_one", listed.stdout)
        self.assertIn("2 test occurrence(s)", listed.stdout)

        duplicate = self.run_runner(
            "--list", "test_alpha.Alpha.test_one", "test_alpha.Alpha.test_one"
        )
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(duplicate.stdout.count("test_alpha.Alpha.test_one"), 2)
        self.assertIn("2 test occurrence(s)", duplicate.stdout)

        missing = self.run_runner("test_alpha.Absent")
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("attribute", missing.stderr)
        empty = self.run_runner("-p", "missing_*.py")
        self.assertNotEqual(empty.returncode, 0)
        self.assertIn("no tests selected", empty.stderr)
        invalid_jobs = self.run_runner("-j", "0")
        self.assertNotEqual(invalid_jobs.returncode, 0)
        self.assertIn("positive integer", invalid_jobs.stderr)

        self.write_module("test_unselected_broken.py", "syntax error !\n")
        selected_only = self.run_runner("-j", "1", "test_alpha.Alpha.test_one")
        self.assertEqual(selected_only.returncode, 0, selected_only.stderr + selected_only.stdout)
        selected_list = self.run_runner("--list", "test_alpha.Alpha.test_one")
        self.assertEqual(selected_list.returncode, 0, selected_list.stderr)
        self.assertIn("1 test occurrence(s)", selected_list.stdout)
        full_discovery = self.run_runner()
        self.assertNotEqual(full_discovery.returncode, 0)
        self.assertIn("import/selection errors", full_discovery.stderr)

    def test_runner_and_worker_import_the_callers_working_directory(self):
        (self.root / "root_helper.py").write_text("VALUE = 17\n", encoding="utf-8")
        self.write_module(
            "test_root_import.py",
            """
            import unittest
            import root_helper
            class RootImport(unittest.TestCase):
                def test_value(self): self.assertEqual(root_helper.VALUE, 17)
        """,
        )
        listed = self.run_runner("--list", "test_root_import.RootImport")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        executed = self.run_runner("-j", "2", "test_root_import.RootImport")
        self.assertEqual(executed.returncode, 0, executed.stderr + executed.stdout)

    def test_selected_serial_load_tests_runs_once_without_unrelated_discovery(self):
        marker = self.root / "load-count.txt"
        self.write_module(
            "test_load_once.py",
            f"""
            import pathlib, unittest
            MARKER = pathlib.Path({str(marker)!r})
            class Selected(unittest.TestCase):
                def test_one(self): pass
            def load_tests(loader, tests, pattern):
                count = int(MARKER.read_text() if MARKER.exists() else '0')
                MARKER.write_text(str(count + 1))
                return loader.loadTestsFromTestCase(Selected)
        """,
        )
        self.write_module("test_unselected_broken.py", "syntax error !\n")
        result = self.run_runner("-j", "1", "test_load_once")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(marker.read_text(encoding="utf-8"), "1")

    def test_result_with_live_child_is_reported_and_child_is_stopped(self):
        for mode in ("success", "protocol"):
            with self.subTest(mode=mode):
                child_pid_file = self.root / f"{mode}-child.pid"
                self.write_module(
                    f"test_live_{mode}.py",
                    f"""
                    import multiprocessing, pathlib, time, unittest
                    CHILD_PID = pathlib.Path({str(child_pid_file)!r})
                    def sleeper(): time.sleep(60)
                    class LiveChild(unittest.TestCase):
                        def _start_child(self):
                            child = multiprocessing.Process(target=sleeper)
                            child.start()
                            CHILD_PID.write_text(str(child.pid))
                        def test_live_child(self):
                            self._start_child()
                        if {mode!r} == 'protocol':
                            def run(self, result=None):
                                self._start_child()
                                raise RuntimeError('synthetic worker protocol error')
                """,
                )
                result = self.run_runner("-j", "2", f"test_live_{mode}.LiveChild", timeout=20)
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("child processes were still live", result.stderr)
                self.assertTrue(child_pid_file.exists(), result.stderr + result.stdout)
                child_pid = int(child_pid_file.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    try:
                        os.kill(child_pid, 0)
                    except ProcessLookupError:
                        break
                    time.sleep(0.05)
                else:
                    self.fail(f"runner left owned child process {child_pid} alive")

    def test_class_groups_are_atomic_and_worker_limit_is_respected(self):
        state = self.root / "state.json"
        lock = self.root / "state.lock"
        self.write_module(
            "test_groups.py",
            f"""
            import fcntl, json, os, pathlib, time, unittest
            STATE = pathlib.Path({str(state)!r})
            LOCK = pathlib.Path({str(lock)!r})
            def update(name, delta):
                with LOCK.open('a+') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    data = json.loads(STATE.read_text() if STATE.exists() else
                                      '{{"active":0,"maximum":0,"ready":0,"go":false,"events":[]}}')
                    data['active'] += delta
                    data['maximum'] = max(data['maximum'], data['active'])
                    if data['active'] == 0:
                        data['ready'] = 0; data['go'] = False
                    data['events'].append([name, os.getpid(), data['active']])
                    STATE.write_text(json.dumps(data))
                    fcntl.flock(handle, fcntl.LOCK_UN)
            def record(name):
                with LOCK.open('a+') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    data = json.loads(STATE.read_text() if STATE.exists() else
                                      '{{"active":0,"maximum":0,"ready":0,"go":false,"events":[]}}')
                    data['events'].append([name, os.getpid(), data['active']])
                    STATE.write_text(json.dumps(data))
                    fcntl.flock(handle, fcntl.LOCK_UN)
            def wait_for_two():
                with LOCK.open('a+') as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    data = json.loads(STATE.read_text())
                    data['ready'] += 1
                    if data['ready'] >= 2: data['go'] = True
                    STATE.write_text(json.dumps(data))
                    fcntl.flock(handle, fcntl.LOCK_UN)
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    with LOCK.open('r') as handle:
                        fcntl.flock(handle, fcntl.LOCK_SH)
                        data = json.loads(STATE.read_text())
                    if data['go']: return
                    time.sleep(.01)
                raise AssertionError('two class fixtures did not overlap')
            class GroupBase:
                @classmethod
                def setUpClass(cls):
                    super().setUpClass(); update(cls.__name__ + ':setup', 1); wait_for_two()
                @classmethod
                def tearDownClass(cls):
                    update(cls.__name__ + ':teardown', -1); super().tearDownClass()
                def test_a(self): record(type(self).__name__ + ':a')
                def test_b(self): record(type(self).__name__ + ':b')
            class GroupA(GroupBase, unittest.TestCase): pass
            class GroupB(GroupBase, unittest.TestCase): pass
            class GroupC(GroupBase, unittest.TestCase): pass
            class GroupD(GroupBase, unittest.TestCase): pass
        """,
        )
        result = self.run_runner("-j", "2")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        data = json.loads(state.read_text(encoding="utf-8"))
        self.assertEqual(data["maximum"], 2)
        for cls in ("GroupA", "GroupB", "GroupC", "GroupD"):
            events = [event for event in data["events"] if event[0].startswith(cls + ":")]
            self.assertEqual(
                [event[0] for event in events],
                [cls + ":setup", cls + ":a", cls + ":b", cls + ":teardown"],
            )
            self.assertEqual(len({event[1] for event in events}), 1)

    def test_unittest_result_categories_and_class_fixture_errors_are_aggregated(self):
        self.write_module(
            "test_results.py",
            """
            import unittest
            class Outcomes(unittest.TestCase):
                def test_failure(self): self.fail('expected failure')
                def test_error(self): raise RuntimeError('expected error')
                @unittest.skip('skip reason')
                def test_skip(self): pass
                @unittest.expectedFailure
                def test_expected(self): self.fail('expected failure')
                @unittest.expectedFailure
                def test_unexpected_success(self): pass
        """,
        )
        result = self.run_runner("-j", "2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Ran 5 tests", result.stdout)
        self.assertIn("failures=1", result.stdout)
        self.assertIn("errors=1", result.stdout)
        self.assertIn("skipped=1", result.stdout)
        self.assertIn("expected failures=1", result.stdout)
        self.assertIn("unexpected successes=1", result.stdout)

        self.write_module(
            "test_fixture_error.py",
            """
            import unittest
            class BrokenFixture(unittest.TestCase):
                @classmethod
                def setUpClass(cls): raise RuntimeError('setup failed')
                def test_one(self): pass
                def test_two(self): pass
        """,
        )
        fixture_result = self.run_runner("-j", "2", "test_fixture_error.BrokenFixture")
        self.assertEqual(fixture_result.returncode, 1)
        self.assertIn("errors=1", fixture_result.stdout)
        self.assertIn("Ran 0 tests", fixture_result.stdout)

        self.write_module(
            "test_class_cleanup.py",
            """
            import unittest
            def broken_cleanup(): raise RuntimeError('class cleanup failed')
            class BrokenCleanup(unittest.TestCase):
                @classmethod
                def setUpClass(cls): cls.addClassCleanup(broken_cleanup)
                def test_one(self): pass
        """,
        )
        cleanup_result = self.run_runner("-j", "2", "test_class_cleanup.BrokenCleanup")
        self.assertEqual(cleanup_result.returncode, 1)
        self.assertIn("Ran 1 tests", cleanup_result.stdout)
        self.assertIn("errors=1", cleanup_result.stdout)

    def test_module_hooks_are_rejected_in_parallel_and_work_serially(self):
        marker = self.root / "module-fixture-ran"
        self.write_module(
            "test_module_hook.py",
            f"""
            import pathlib, unittest
            def setUpModule(): pathlib.Path({str(marker)!r}).write_text('yes')
            class Hooked(unittest.TestCase):
                def test_one(self): self.assertTrue(True)
        """,
        )
        parallel = self.run_runner("-j", "2", "test_module_hook.Hooked")
        self.assertNotEqual(parallel.returncode, 0)
        self.assertIn("setUpModule", parallel.stderr)
        self.assertIn("--jobs 1", parallel.stderr)
        self.assertFalse(marker.exists())
        serial = self.run_runner("-j", "1", "test_module_hook.Hooked")
        self.assertEqual(serial.returncode, 0, serial.stderr + serial.stdout)
        self.assertTrue(marker.exists())

        self.write_module(
            "test_hook_source.py",
            """
            import unittest
            class Source(unittest.TestCase):
                def test_one(self): pass
        """,
        )
        self.write_module(
            "test_load_hook.py",
            """
            import unittest
            from test_hook_source import Source
            def load_tests(loader, tests, pattern):
                return loader.loadTestsFromTestCase(Source)
        """,
        )
        load_hook = self.run_runner("-j", "2", "test_load_hook")
        self.assertNotEqual(load_hook.returncode, 0)
        self.assertIn("load_tests", load_hook.stderr)

        custom_marker = self.root / "custom-suite-ran"
        self.write_module(
            "test_custom_suite.py",
            f"""
            import pathlib, unittest
            class Hooked(unittest.TestCase):
                def test_one(self): pass
            class CustomSuite(unittest.TestSuite):
                def run(self, result, debug=False):
                    pathlib.Path({str(custom_marker)!r}).write_text('ran')
                    return super().run(result, debug)
            def load_tests(loader, tests, pattern): return CustomSuite(tests)
        """,
        )
        custom = self.run_runner("-j", "2", "test_custom_suite")
        self.assertNotEqual(custom.returncode, 0)
        self.assertIn("custom suite", custom.stderr)
        custom_serial = self.run_runner("-j", "1", "test_custom_suite")
        self.assertEqual(custom_serial.returncode, 0, custom_serial.stderr + custom_serial.stdout)
        self.assertTrue(custom_marker.exists())

        self.write_module(
            "test_module_cleanup.py",
            """
            import unittest
            def cleanup(): pass
            unittest.addModuleCleanup(cleanup)
            class Hooked(unittest.TestCase):
                def test_one(self): pass
        """,
        )
        module_cleanup = self.run_runner("-j", "2", "test_module_cleanup.Hooked")
        self.assertNotEqual(module_cleanup.returncode, 0)
        self.assertIn("registered module cleanup", module_cleanup.stderr)

    def test_import_error_worker_crash_and_buffered_streams_are_visible(self):
        self.write_module(
            "test_output.py",
            """
            import os, sys, unittest
            class Output(unittest.TestCase):
                def test_streams(self):
                    print('python stdout')
                    print('python stderr', file=sys.stderr)
                    os.write(1, b'fd stdout\\n')
                    os.write(2, b'fd stderr\\n')
        """,
        )
        output = self.run_runner("-j", "2", "test_output.Output")
        self.assertEqual(output.returncode, 0, output.stderr + output.stdout)
        self.assertIn("python stdout", output.stdout)
        self.assertIn("fd stdout", output.stdout)
        self.assertIn("python stderr", output.stderr)
        self.assertIn("fd stderr", output.stderr)

        child_pid = self.root / "crash-child.pid"
        self.write_module(
            "test_crash.py",
            f"""
            import os, pathlib, subprocess, sys, time, unittest
            CHILD = pathlib.Path({str(child_pid)!r})
            class Crash(unittest.TestCase):
                def test_crash(self):
                    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                             start_new_session=True)
                    CHILD.write_text(str(child.pid))
                    time.sleep(.8)  # allow the parent tree monitor to record this descendant
                    os._exit(7)
        """,
        )
        crash = self.run_runner("-j", "2", "test_crash.Crash")
        self.assertNotEqual(crash.returncode, 0)
        self.assertIn("exited without a result", crash.stderr)
        self.assertTrue(child_pid.exists())
        crashed_descendant = int(child_pid.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _pid_exists(crashed_descendant):
            time.sleep(0.05)
        self.assertFalse(
            _pid_exists(crashed_descendant),
            f"crashed worker descendant {crashed_descendant} survived",
        )

        self.write_module("test_broken_import.py", "syntax error !\n")
        broken = self.run_runner("-j", "2")
        self.assertNotEqual(broken.returncode, 0)
        self.assertIn("import/selection errors", broken.stderr)

    def test_interrupt_stops_queue_and_terminates_new_session_descendant(self):
        child_pids = [self.root / "child-a.pid", self.root / "child-b.pid"]
        later_marker = self.root / "later-ran"
        self.write_module(
            "test_interrupt.py",
            f"""
            import pathlib, subprocess, sys, time, unittest
            CHILD_A = pathlib.Path({str(child_pids[0])!r})
            CHILD_B = pathlib.Path({str(child_pids[1])!r})
            class LongRunningA(unittest.TestCase):
                def test_a(self):
                    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                             start_new_session=True)
                    CHILD_A.write_text(str(child.pid))
                    while True: time.sleep(.05)
            class LongRunningB(unittest.TestCase):
                def test_a(self):
                    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],
                                             start_new_session=True)
                    CHILD_B.write_text(str(child.pid))
                    while True: time.sleep(.05)
            class Later(unittest.TestCase):
                def test_not_dispatched(self): pathlib.Path({str(later_marker)!r}).write_text('ran')
        """,
        )
        process = subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(RUNNER),
                "-s",
                str(self.tests),
                "-j",
                "2",
                "test_interrupt.LongRunningA",
                "test_interrupt.LongRunningB",
                "test_interrupt.Later",
            ],
            cwd=self.root,
            env=os.environ | {"PYTHONDONTWRITEBYTECODE": "1"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        descendants = []
        try:
            deadline = time.monotonic() + 10
            while (
                time.monotonic() < deadline
                and not all(path.exists() for path in child_pids)
                and process.poll() is None
            ):
                time.sleep(0.05)
            self.assertTrue(
                all(path.exists() for path in child_pids), "workers did not launch both children"
            )
            descendants = [int(path.read_text(encoding="utf-8")) for path in child_pids]
            time.sleep(0.7)  # allow the runner's process-tree monitor to observe the new session
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                running = process.poll() is None
                if running:
                    process.kill()
                process.wait(timeout=5)
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                self.fail(
                    f"runner did not stop after interrupt (still running={running}); "
                    "inspect task-owned process tree before retrying"
                )
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired as exc:
                process_table = subprocess.run(
                    ["ps", "-axo", "pid=,ppid=,pgid=,command="],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
                relevant = "\n".join(
                    line
                    for line in process_table.splitlines()
                    if str(self.root) in line or any(str(pid) in line for pid in descendants)
                )
                if process.stdout:
                    process.stdout.close()
                if process.stderr:
                    process.stderr.close()
                self.fail(
                    f"runner exited but a descendant kept output pipes open: "
                    f"stdout={exc.output!r}, stderr={exc.stderr!r}; processes={relevant}"
                )
            self.assertEqual(process.returncode, 130, stderr + stdout)
            self.assertFalse(later_marker.exists())
            self.assertIn("Interrupted", stderr)
            for descendant in descendants:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and _pid_exists(descendant):
                    time.sleep(0.05)
                self.assertFalse(
                    _pid_exists(descendant), f"worker descendant {descendant} survived interrupt"
                )
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            for descendant in descendants:
                if _pid_exists(descendant):
                    os.kill(descendant, signal.SIGKILL)

    def test_make_default_is_help_and_selected_targets_do_not_expand_to_full_suite(self):
        make = subprocess.run(["make"], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertIn("make test", make.stdout)
        self.assertNotIn("Ran ", make.stdout)
        dry_run = subprocess.run(
            [
                "make",
                "-n",
                "test",
                "TESTS=test_parallel_runner.ParallelRunnerTests.test_list_selectors_duplicates_empty_and_invalid_jobs",
                "JOBS=1",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("scripts/run_tests.py", dry_run.stdout)
        self.assertIn("--jobs 1", dry_run.stdout)
        self.assertIn(
            "test_parallel_runner.ParallelRunnerTests.test_list_selectors_duplicates_empty_and_invalid_jobs",
            dry_run.stdout,
        )
        forwarded = subprocess.run(
            [
                "make",
                "test",
                "JOBS=1",
                "TESTS=test_parallel_runner.ParallelRunnerTests.test_list_selectors_duplicates_empty_and_invalid_jobs",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
        )
        self.assertEqual(forwarded.returncode, 0, forwarded.stderr + forwarded.stdout)
        self.assertIn("Ran 1 test", forwarded.stderr + forwarded.stdout)


def _pid_exists(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


if __name__ == "__main__":
    unittest.main()
