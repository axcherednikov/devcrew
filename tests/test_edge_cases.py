import copy
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest
from unittest import mock
import test_devcrew

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import devcrew


class EdgeCases(unittest.TestCase):
    run_cli = test_devcrew.DevCrewCLITest.run_cli
    project = test_devcrew.DevCrewCLITest.project

    def initialized(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result = self.run_cli('init', '--project', project)
        self.assertEqual(result.returncode, 0, result.stderr)
        return project

    def record(self, project, code="print('ok')", **options):
        args = ['run-check', '--project', project, '--name', 'edge']
        for key, value in options.items():
            args += ['--' + key.replace('_', '-'), str(value)]
        result = self.run_cli(*args, '--', sys.executable, '-c', code)
        record = pathlib.Path(result.stdout.splitlines()[0])
        return result, record, json.loads(record.read_text())

    def test_init_document_directory_is_conflict_without_partial_install(self):
        for relative in ('docs/context.md', '.codex/tasks/README.md'):
            with self.subTest(relative=relative):
                td, project = self.project()
                self.addCleanup(td.cleanup)
                (project / relative).mkdir(parents=True)
                result = self.run_cli('init', '--project', project)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((project / '.codex/devcrew.json').exists())
                self.assertFalse((project / '.codex/agents').exists())

    def test_init_rejects_unsafe_or_overlapping_state_paths(self):
        cases = [
            ('--memory-dir', '../memory'),
            ('--memory-dir', 'C:\\memory'),
            ('--memory-dir', 'memory/*'),
        ]
        for flag, value in cases:
            with self.subTest(flag=flag, value=value):
                td, project = self.project()
                self.addCleanup(td.cleanup)
                result = self.run_cli('init', '--project', project, flag, value)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse((project / '.codex').exists())
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result = self.run_cli('init', '--project', project, '--tasks-dir', 'docs', '--sessions-dir', 'docs/sessions')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertFalse((project / '.codex').exists())

    def test_verify_requires_complete_well_formed_evidence(self):
        project = self.initialized()
        _, record, original = self.record(project)
        changes = [
            ('missing-before', lambda d: d.pop('worktree_fingerprint_before')),
            ('invalid-before', lambda d: d.update(worktree_fingerprint_before={'digest': 'invalid'})),
            ('missing-current', lambda d: d.pop('worktree_fingerprint')),
            ('reversed-time', lambda d: d.update(started_at='2099-01-01T00:00:00+00:00', finished_at='2000-01-01T00:00:00+00:00')),
            ('naive-time', lambda d: d.update(started_at='2000-01-01')),
            ('bool-exit', lambda d: d.update(exit_code=False)),
        ]
        for name, change in changes:
            with self.subTest(name=name):
                data = copy.deepcopy(original)
                change(data)
                record.write_text(json.dumps(data))
                result = self.run_cli('verify-check', '--record', record)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertNotIn('Traceback', result.stderr)
                self.assertFalse(json.loads(result.stderr)['ok'])
        record.write_text(json.dumps(original))
        self.assertEqual(self.run_cli('verify-check', '--record', record).returncode, 0)

    def test_mutating_check_preserves_before_and_after_evidence(self):
        project = self.initialized()
        result, record, payload = self.record(project, "from pathlib import Path; Path('tracked.txt').write_text('changed')")
        self.assertEqual(result.returncode, 0)
        self.assertNotEqual(payload['worktree_fingerprint_before'], payload['worktree_fingerprint_after'])
        self.assertEqual(self.run_cli('verify-check', '--record', record).returncode, 0)

    def test_tracked_paths_batches_tagged_query_and_preserves_arbitrary_names(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        tracked = ['space name', 'tab\tname', 'newline\nname', os.fsdecode(b'bytes-\xff')]
        for name in tracked[:-1]:
            (project / name).write_bytes(b'tracked\n')
        subprocess.run(['git', '-C', str(project), 'add', '--', *tracked[:-1]], check=True)
        blob = subprocess.run(['git', '-C', str(project), 'hash-object', '-w', '--stdin'],
                              input=b'tracked\n', check=True, capture_output=True).stdout.strip()
        index_line = b'100644 ' + blob + b' 0\t' + os.fsencode(tracked[-1]) + b'\0'
        subprocess.run(['git', '-C', str(project), 'update-index', '-z', '--index-info'],
                       input=index_line, check=True, capture_output=True)
        lookalike = 'H 100644 0123456789abcdef0123456789abcdef01234567 0\tclaimed-index-entry'
        (project / lookalike).write_bytes(b'untracked\n')
        expected = sorted(['tracked.txt', *tracked, lookalike])
        original = subprocess.check_output
        calls = []

        def recording_check_output(argv, *args, **kwargs):
            if isinstance(argv, list) and 'ls-files' in argv:
                calls.append(argv)
            return original(argv, *args, **kwargs)

        with mock.patch.object(subprocess, 'check_output', side_effect=recording_check_output):
            actual = devcrew._tracked_paths(project)

        self.assertEqual(actual, expected)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][3:], ['ls-files', '-t', '--stage', '--cached', '--others',
                                        '--exclude-standard', '-z', '--', '.'])

    def test_tracked_paths_falls_back_for_malformed_or_failed_batch(self):
        for failure in ('malformed', 'failed'):
            with self.subTest(failure=failure):
                td, project = self.project()
                self.addCleanup(td.cleanup)
                (project / 'untracked.txt').write_text('untracked\n')
                original = subprocess.check_output
                calls = []

                def failing_or_malformed(argv, *args, **kwargs):
                    if isinstance(argv, list) and 'ls-files' in argv:
                        calls.append(argv)
                        if '-t' in argv:
                            if failure == 'failed':
                                raise subprocess.CalledProcessError(1, argv)
                            return b'! invalid tagged output\0'
                    return original(argv, *args, **kwargs)

                with mock.patch.object(subprocess, 'check_output', side_effect=failing_or_malformed):
                    actual = devcrew._tracked_paths(project)
                self.assertEqual(actual, ['tracked.txt', 'untracked.txt'])
                self.assertEqual(len(calls), 3)

    def test_tracked_paths_reads_fresh_index_and_untracked_state_each_time(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        original = subprocess.check_output
        calls = []

        def recording_check_output(argv, *args, **kwargs):
            if isinstance(argv, list) and 'ls-files' in argv:
                calls.append(argv)
            return original(argv, *args, **kwargs)

        with mock.patch.object(subprocess, 'check_output', side_effect=recording_check_output):
            before = devcrew._tree_entries(project)
            (project / 'indexed.txt').write_text('indexed\n')
            subprocess.run(['git', '-C', str(project), 'add', '--', 'indexed.txt'], check=True)
            indexed = devcrew._tree_entries(project)
            (project / 'untracked.txt').write_text('untracked\n')
            after = devcrew._tree_entries(project)

        self.assertEqual([entry[0] for entry in before], ['tracked.txt'])
        self.assertEqual([entry[0] for entry in indexed], ['indexed.txt', 'tracked.txt'])
        self.assertEqual([entry[0] for entry in after], ['indexed.txt', 'tracked.txt', 'untracked.txt'])
        self.assertEqual(len(calls), 3)

    def test_tracked_paths_parses_conflict_stages_and_rejects_gitlinks(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        stage_ids = []
        for content in (b'base\n', b'ours\n', b'theirs\n'):
            stage_ids.append(subprocess.run(['git', '-C', str(project), 'hash-object', '-w', '--stdin'],
                                            input=content, check=True, capture_output=True).stdout.strip())
        staged_conflict = b''.join(b'100644 ' + blob + b' ' + str(stage).encode() +
                                   b'\tconflict\tname\0'
                                   for stage, blob in zip((1, 2, 3), stage_ids))
        subprocess.run(['git', '-C', str(project), 'update-index', '-z', '--index-info'],
                       input=staged_conflict, check=True, capture_output=True)
        self.assertEqual(devcrew._tracked_paths(project), ['conflict\tname', 'tracked.txt'])

        gitlink = b'160000 ' + stage_ids[0] + b' 2\tmodule\0'
        subprocess.run(['git', '-C', str(project), 'update-index', '-z', '--index-info'],
                       input=gitlink, check=True, capture_output=True)
        with self.assertRaisesRegex(devcrew.ContractError, 'submodules are not supported'):
            devcrew._tracked_paths(project)

    @unittest.skipUnless(os.name == 'posix', 'POSIX executable file mode')
    def test_file_mode_change_invalidates_evidence(self):
        project = self.initialized()
        _, record, _ = self.record(project)
        path = project / 'tracked.txt'
        path.chmod(path.stat().st_mode ^ 0o100)
        self.assertNotEqual(self.run_cli('verify-check', '--record', record).returncode, 0)

    @unittest.skipUnless(os.name == 'posix', 'POSIX process-group timeout')
    def test_timeout_preserves_output_once_and_kills_descendant(self):
        project = self.initialized()
        child = "import signal,time; from pathlib import Path; signal.signal(signal.SIGTERM, signal.SIG_IGN); print('CHILD_READY',flush=True); time.sleep(1); Path('escaped-child').write_text('alive')"
        parent = 'import subprocess,sys,time; print("ONCE",flush=True); subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(10)'
        result, record, payload = self.record(project, parent, timeout=0.4)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(payload['status'], 'timeout')
        log = (project / '.codex/checks' / payload['log']).read_text()
        self.assertEqual(log.count('ONCE'), 1, log)
        self.assertEqual(log.count('CHILD_READY'), 1, log)
        time.sleep(1.1)
        self.assertFalse((project / 'escaped-child').exists())
        self.assertNotEqual(self.run_cli('verify-check', '--record', record).returncode, 0)

    def test_wave_completion_problems_and_invalid_result_identifiers(self):
        project = self.initialized()
        plan = project / 'plan.json'
        results = project / '.codex/checks/results.json'
        plan.write_text(json.dumps({'waves': [{'id': 'one', 'slices': [{'id': 'a', 'files': ['src/a.py']}]}, {'id': 'two', 'slices': [{'id': 'b', 'files': ['src/a.py']}]}]}))
        planned = json.loads(plan.read_text())
        for wave in planned['waves']:
            wave.update(verification=['smoke'], verification_bindings=[
                {'index': 0, 'verification': 'smoke', 'argv': [sys.executable, '-c', "print('ok')"]}])
        plan.write_text(json.dumps(planned))
        results.write_text(json.dumps({'waves': []}))
        approved = self.run_cli('wave-gate', '--project', project, '--plan', plan, '--results', results, '--wave', '0')
        self.assertEqual(approved.returncode, 0, approved.stderr)
        _, record, _ = self.record(project)
        valid = {'waves': [{'id': 'one', 'checks': [str(record)], 'slices': [{'id': 'a', 'done': True, 'problems': []}]}]}
        def gate(data, n='1'):
            results.write_text(json.dumps(data))
            return self.run_cli('wave-gate', '--project', project, '--plan', plan, '--results', results, '--wave', n)
        passed = gate(valid)
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)['status'], 'authorized')
        invalid = []
        problem = copy.deepcopy(valid); problem['waves'][0]['slices'][0]['problems'] = ['not integrated']; invalid.append(problem)
        unknown = copy.deepcopy(valid); unknown['waves'][0]['slices'][0]['id'] = 'missing'; invalid.append(unknown)
        duplicate = copy.deepcopy(valid); duplicate['waves'][0]['slices'] *= 2; invalid.append(duplicate)
        bad_bool = copy.deepcopy(valid); bad_bool['waves'][0]['slices'][0]['done'] = 1; invalid.append(bad_bool)
        for data in invalid:
            with self.subTest(data=data):
                self.assertNotEqual(gate(data).returncode, 0)
        complete = copy.deepcopy(valid)
        complete['waves'].append({'id': 'two', 'checks': [str(record)], 'slices': [{'id': 'b', 'done': True, 'problems': []}]})
        self.assertEqual(json.loads(gate(complete, '2').stdout)['status'], 'complete')
        for path in ('../outside', '/absolute', 'C:\\outside', 'src/*.py'):
            plan.write_text(json.dumps({'waves': [{'id': 'one', 'slices': [{'id': 'a', 'files': [path]}]}]}))
            self.assertNotEqual(gate({'waves': []}, '0').returncode, 0)

    def test_review_approval_is_exact_and_rejects_unresolved_findings(self):
        project = self.initialized()
        report = project / '.codex/checks/review.json'
        snapshot = self.run_cli('capture-state', '--project', project)
        self.assertEqual(snapshot.returncode, 0, snapshot.stderr)
        valid = {'state': json.loads(snapshot.stdout), 'expected_lenses': ['correctness'], 'lenses': [{'id': 'correctness', 'status': 'complete', 'findings': []}], 'approved': True}
        def gate(data):
            report.write_text(json.dumps(data))
            return self.run_cli('review-gate', '--project', project, '--report', report)
        self.assertEqual(gate(valid).returncode, 0)
        for verification in ('confirmed', 'unverified'):
            data = copy.deepcopy(valid)
            data['lenses'][0]['findings'] = [{'severity': 'P1', 'verification': verification, 'summary': 'blocking'}]
            self.assertNotEqual(gate(data).returncode, 0)
        data['lenses'][0]['findings'][0]['verification'] = 'rejected'
        self.assertEqual(gate(data).returncode, 0)
        for data in ({**valid, 'lenses': []}, {**valid, 'approved': 1}, {**valid, 'lenses': valid['lenses'] * 2}):
            self.assertNotEqual(gate(data).returncode, 0)

    def test_nested_repository_is_rejected_before_running_command(self):
        project = self.initialized()
        nested = project / 'nested'
        subprocess.run(['git', 'init', '-q', str(nested)], check=True)
        (nested / 'tested.py').write_text('assert True\n')
        result = self.run_cli('run-check', '--project', project, '--name', 'nested', '--', sys.executable, '-c', "from pathlib import Path; Path('must-not-run').write_text('bad')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('nested Git', result.stderr)
        self.assertFalse((project / 'must-not-run').exists())
        result = self.run_cli('run-check', '--project', nested, '--name', 'standalone', '--', sys.executable, 'tested.py')
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_verify_rejects_directory_replaced_with_nested_repository(self):
        project = self.initialized()
        nested = project / 'nested'
        nested.mkdir()
        (nested / 'tested.py').write_text('assert True\n')
        result, record, _ = self.record(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run(['git', 'init', '-q', str(nested)], check=True)
        (nested / 'tested.py').write_text('assert False\n')
        result = self.run_cli('verify-check', '--record', record)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('nested Git', result.stderr)

    def test_submodule_project_is_rejected_before_running_command(self):
        project = self.initialized()
        td, dependency = self.project()
        self.addCleanup(td.cleanup)
        subprocess.run(['git', '-C', str(project), '-c', 'protocol.file.allow=always', 'submodule', 'add', '-q', str(dependency), 'lib'], check=True)
        result = self.run_cli('run-check', '--project', project, '--name', 'submodule', '--', sys.executable, '-c', "from pathlib import Path; Path('must-not-run').write_text('bad')")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('submodule', result.stderr.lower())
        self.assertFalse((project / 'must-not-run').exists())

    def test_flaky_caught_and_wall_time_bound(self):
        project = self.initialized()
        def hunt(*flags, code):
            return self.run_cli('flaky-hunt', '--project', project, '--name', 'bounded', '--max-runs', '20', '--timeout', '3', *flags, '--', sys.executable, '-c', code)
        caught = hunt(code='raise SystemExit(7)')
        payload = json.loads(caught.stdout)
        self.assertEqual(caught.returncode, 1)
        self.assertEqual(payload['status'], 'caught')
        self.assertEqual(payload['runs'], 1)
        self.assertEqual(json.loads(pathlib.Path(payload['records'][0]).read_text())['exit_code'], 7)
        started = time.monotonic()
        timed = hunt('--max-seconds', '0.2', code='import time; time.sleep(5)')
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(timed.returncode, 2)
        self.assertEqual(json.loads(timed.stdout)['status'], 'environment_error')
        for value in ('0', '-1', 'nan', 'inf'):
            self.assertNotEqual(hunt('--max-seconds', value, code='pass').returncode, 0)


if __name__ == '__main__':
    unittest.main()
