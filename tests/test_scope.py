import json
import pathlib
import unittest
import test_edge_cases


class Scope(unittest.TestCase):
    run_cli = test_edge_cases.EdgeCases.run_cli
    project = test_edge_cases.EdgeCases.project
    initialized = test_edge_cases.EdgeCases.initialized

    def capture(self, project, execution_root=None):
        args = ['capture-state', '--project', str(project)]
        if execution_root is not None:
            args += ['--execution-root', str(execution_root)]
        result = self.run_cli(*args, '--include-files')
        self.assertEqual(result.returncode, 0, result.stderr)
        path = project / '.codex/checks/before.json'
        path.write_text(result.stdout)
        return path

    def check(self, project, before, files, execution_root=None):
        owned = project / '.codex/checks/owned.json'
        owned.write_text(json.dumps(files))
        args = ['scope-check', '--project', str(project)]
        if execution_root is not None:
            args += ['--execution-root', str(execution_root)]
        args += ['--before', str(before), '--files', str(owned)]
        return self.run_cli(*args)

    def test_preserves_preexisting_dirty_files_and_detects_actual_edits(self):
        project = self.initialized()
        (project / 'user.txt').write_text('existing work')
        before = self.capture(project)
        (project / 'tracked.txt').write_text('executor work')
        result = self.check(project, before, ['tracked.txt'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['changed'], ['tracked.txt'])
        (project / 'user.txt').write_text('unexpected overwrite')
        result = self.check(project, before, ['tracked.txt'])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['outside_scope'], ['user.txt'])

    def test_detects_deletion_new_file_and_mode_change(self):
        project = self.initialized()
        before = self.capture(project)
        (project / 'tracked.txt').unlink()
        (project / 'new.txt').write_text('new')
        result = self.check(project, before, ['tracked.txt'])
        self.assertEqual(json.loads(result.stdout)['outside_scope'], ['new.txt'])
        self.assertEqual(self.check(project, before, ['tracked.txt', 'new.txt']).returncode, 0)
        before = self.capture(project)
        (project / 'new.txt').chmod(0o755)
        self.assertEqual(json.loads(self.check(project, before, ['new.txt']).stdout)['changed'], ['new.txt'])

    def test_rejects_malformed_snapshot_ownership_and_foreign_project(self):
        project = self.initialized()
        before = self.capture(project)
        for files in ([], {}, ['../escape'], ['tracked.txt', './tracked.txt']):
            with self.subTest(files=files):
                self.assertNotEqual(self.check(project, before, files).returncode, 0)
        other = self.initialized()
        self.assertNotEqual(self.check(other, before, ['tracked.txt']).returncode, 0)
        data = json.loads(before.read_text())
        data['files'] = []
        before.write_text(json.dumps(data))
        result = self.check(project, before, ['tracked.txt'])
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)

    def test_bracket_named_routes_are_literal_paths_not_globs(self):
        project = self.initialized()
        route = project / '[id].tsx'
        route.write_text('existing route')
        (project / 'unrelated*file').write_text('literal filesystem inventory')
        before = self.capture(project)
        (project / 'tracked.txt').write_text('allowed')
        self.assertEqual(self.check(project, before, ['tracked.txt']).returncode, 0)
        before = self.capture(project)
        route.write_text('updated route')
        result = self.check(project, before, ['[id].tsx'])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['changed'], ['[id].tsx'])
