import json
import unittest
import test_edge_cases


class Memory(unittest.TestCase):
    run_cli = test_edge_cases.EdgeCases.run_cli
    project = test_edge_cases.EdgeCases.project
    initialized = test_edge_cases.EdgeCases.initialized

    def test_inventory_duplicates_thresholds_and_no_follow(self):
        project = self.initialized()
        root = project / 'memory'
        (root / 'a').write_text('same')
        (root / 'b').write_text('same')
        (root / 'outside').symlink_to(project.parent, target_is_directory=True)
        before = {p: p.read_bytes() for p in root.iterdir() if p.is_file()}
        result = self.run_cli('memory-audit', '--project', project, '--max-files', '1', '--max-bytes', '1')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertIn(['memory/a', 'memory/b'], data['duplicates'])
        self.assertEqual(data['skipped'], ['memory/outside'])
        self.assertEqual(len(data['warnings']), 2)
        self.assertEqual(before, {p: p.read_bytes() for p in root.iterdir() if p.is_file()})
        self.assertEqual(self.run_cli('memory-audit', '--project', project, '--max-files', '0').returncode, 2)

    def test_missing_and_symlink_memory_directory(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result = self.run_cli('memory-audit', '--project', project)
        self.assertEqual(json.loads(result.stdout)['files'], [])
        self.assertFalse((project / 'memory').exists())
        (project / 'memory').symlink_to(project.parent, target_is_directory=True)
        self.assertEqual(self.run_cli('memory-audit', '--project', project).returncode, 2)

    def test_unreadable_subtree_is_not_reported_as_empty_success(self):
        project = self.initialized()
        restricted = project / 'memory/restricted'
        restricted.mkdir()
        (restricted / 'lesson').write_text('important')
        restricted.chmod(0)
        self.addCleanup(restricted.chmod, 0o700)
        result = self.run_cli('memory-audit', '--project', project)
        self.assertEqual(result.returncode, 2, result.stdout)
        self.assertNotIn('Traceback', result.stderr)

    def summary(self, rows):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        ledger = project / 'ledger.jsonl'
        ledger.write_text(''.join(json.dumps(row) + '\n' for row in rows))
        return self.run_cli('estimate-summary', '--records', ledger)

    def row(self, **changes):
        return dict(task='one', unit='hours', coverage='implementation', estimate_scope='a', actual_scope='a', estimate_model='m', actual_model='m', estimate=2, actual=3) | changes

    def test_comparable_groups_and_explicit_exclusions(self):
        result = self.summary([self.row(), self.row(task='two', actual=1), self.row(task='three', actual_scope='changed'), self.row(task='four', actual_model='other'), self.row(task='five', unit='tokens')])
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data['groups'][0]['actual_to_estimate'], 1)
        self.assertEqual(data['groups'][0]['count'], 2)
        self.assertEqual(len(data['groups']), 2)
        self.assertEqual([row['reasons'] for row in data['excluded']], [['scope'], ['model']])
        self.assertEqual(json.loads(self.summary([]).stdout), {'groups': [], 'excluded': []})

    def test_invalid_observations_and_duplicates(self):
        for row in ([], self.row(actual=True), self.row(estimate=0), self.row(actual=-1), self.row(actual=float('nan')), self.row(estimate=float('inf')), self.row(estimate=10**400), self.row(unit='dollars'), self.row(task='')):
            with self.subTest(row=row):
                result = self.summary([row])
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertNotIn('Traceback', result.stderr)
        self.assertEqual(self.summary([self.row(), self.row()]).returncode, 2)
