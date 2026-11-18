import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest
import test_devcrew
import test_edge_cases


class ExecutionControl(unittest.TestCase):
    run_cli = test_edge_cases.EdgeCases.run_cli
    project = test_edge_cases.EdgeCases.project
    initialized = test_edge_cases.EdgeCases.initialized

    def command(self, project, code, *options):
        return [sys.executable, str(test_devcrew.CLI), 'run-check', '--project', str(project),
                '--name', 'control', *map(str, options), '--', sys.executable, '-c', code]

    def test_idle_timeout_and_output_progress(self):
        project = self.initialized()
        silent = subprocess.run(self.command(project, 'import time; time.sleep(4)',
                                            '--idle-timeout', '.3'), capture_output=True, text=True)
        self.assertEqual(silent.returncode, 1)
        record = json.loads(pathlib.Path(silent.stdout.splitlines()[0]).read_text())
        self.assertEqual(record['timeout_kind'], 'idle')
        self.assertIn('check_started', silent.stderr)
        active = subprocess.run(self.command(project,
                    'import time\nfor i in range(5):\n print(i, flush=True); time.sleep(.12)',
                    '--idle-timeout', '.5'), capture_output=True, text=True)
        self.assertEqual(active.returncode, 0, active.stderr)
        for bad in ('0', '-1', 'nan', 'inf'):
            result = subprocess.run(self.command(project, 'pass', '--idle-timeout', bad), capture_output=True)
            self.assertEqual(result.returncode, 2)

    @unittest.skipUnless(os.name == 'posix', 'POSIX resource locks')
    def test_exclusive_resource_and_release_after_interrupt(self):
        project = self.initialized()
        directory = project / '.codex/checks/locks'
        options = ('--resource', 'database', '--resource-dir', directory)
        first = subprocess.Popen(self.command(project, 'import time; time.sleep(20)', *options),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: first.kill() if first.poll() is None else None)
        started = json.loads(first.stderr.readline())
        self.assertEqual(started['event'], 'check_started')
        status = self.run_cli('resource-status', '--resource-dir', directory, '--name', 'database')
        self.assertTrue(json.loads(status.stdout)['busy'])
        rejected = subprocess.run(self.command(project, "raise RuntimeError('must not run')", *options), capture_output=True, text=True)
        self.assertEqual(rejected.returncode, 2)
        self.assertIn('busy', rejected.stderr)
        first.send_signal(signal.SIGTERM)
        out, err = first.communicate(timeout=5)
        self.assertEqual(first.returncode, 1, err)
        record = json.loads(pathlib.Path(out.splitlines()[0]).read_text())
        self.assertEqual(record['status'], 'interrupted')
        self.assertFalse(json.loads(self.run_cli('resource-status', '--resource-dir', directory, '--name', 'database').stdout)['busy'])
        resumed = subprocess.run(self.command(project, 'print("ok")', *options), capture_output=True, text=True)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)

    @unittest.skipUnless(os.name == 'posix', 'POSIX resource locks')
    def test_killed_parent_does_not_release_a_running_childs_resource(self):
        project = self.initialized()
        directory = project / '.codex/checks/locks'
        release = project / '.codex/checks/release'
        code = "from pathlib import Path\nimport time\nwhile not Path('.codex/checks/release').exists(): time.sleep(.05)"
        first = subprocess.Popen(self.command(project, code, '--resource', 'db', '--resource-dir', directory),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        event = json.loads(first.stderr.readline())
        self.addCleanup(lambda: release.touch())
        first.kill()
        first.wait(timeout=3)
        first.stdout.close()
        first.stderr.close()
        status = self.run_cli('resource-status', '--resource-dir', directory, '--name', 'db')
        self.assertTrue(json.loads(status.stdout)['busy'])
        release.touch()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            result = self.run_cli('resource-status', '--resource-dir', directory, '--name', 'db')
            if not json.loads(result.stdout)['busy']:
                break
            time.sleep(.05)
        self.assertFalse(json.loads(result.stdout)['busy'], event)

    @unittest.skipUnless(os.name == 'posix', 'POSIX resource locks')
    def test_status_is_read_only_and_unrelated_lock_data_is_preserved(self):
        project = self.initialized()
        directory = project / '.codex/checks/locks'
        result = self.run_cli('resource-status', '--resource-dir', directory, '--name', 'db')
        self.assertFalse(json.loads(result.stdout)['busy'])
        self.assertFalse(directory.exists())
        directory.mkdir()
        lock = directory / 'db.lock'
        lock.write_text('user data')
        result = subprocess.run(self.command(project, 'pass', '--resource', 'db', '--resource-dir', directory), capture_output=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(lock.read_text(), 'user data')

    @unittest.skipUnless(os.name == 'posix', 'POSIX child process checks')
    def test_broken_progress_pipe_reaps_the_spawned_child(self):
        project = self.initialized()
        process = subprocess.Popen(self.command(project, 'import time; time.sleep(30)', '--timeout', '5'),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        process.stderr.close()
        process.stderr = None
        out, _ = process.communicate(timeout=5)
        self.assertNotEqual(process.returncode, 0)
        record = json.loads(pathlib.Path(out.splitlines()[0]).read_text())
        self.assertEqual(record['status'], 'error')
        self.assertIsNotNone(record['exit_code'])
        with self.assertRaises(ProcessLookupError):
            os.kill(record['pid'], 0)
