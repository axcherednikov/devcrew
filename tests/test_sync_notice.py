import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'scripts'))
from hooks import project_sync_notice as notice
import devcrew


class SyncNotice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.project = pathlib.Path(self.tmp.name) / 'project'
        self.project.mkdir()
        self.data = pathlib.Path(self.tmp.name) / 'plugin-data'
        self.cli('init')

    def cli(self, command):
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'scripts/devcrew.py'),
                                 command, '--project', str(self.project)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_verified_sync_notifies_once_without_project_writes(self):
        before = {p.relative_to(self.project): p.read_bytes()
                  for p in self.project.rglob('*') if p.is_file()}
        message = notice.notification(self.project, self.data)
        self.assertIsNotNone(message)
        self.assertIn(devcrew._plugin_version(), message)
        self.assertIn('project', message)
        self.assertIsNone(notice.notification(self.project, self.data))
        self.assertEqual(before, {p.relative_to(self.project): p.read_bytes()
                                 for p in self.project.rglob('*') if p.is_file()})

    def test_conflict_and_pending_sync_do_not_confirm_success(self):
        state = self.project / '.codex/devcrew-state.json'
        value = json.loads(state.read_text())
        value['plugin_version'] = '0.0.1'
        state.write_text(json.dumps(value))
        role = self.project / '.codex/agents/scout.toml'
        original = role.read_bytes()
        role.write_text(role.read_text().replace('Find and enumerate', 'Custom instructions: enumerate'))
        self.assertIsNone(notice.notification(self.project, self.data))
        role.write_bytes(original)
        role.unlink()
        self.assertIsNone(notice.notification(self.project, self.data))
        self.cli('sync')
        self.assertIsNotNone(notice.notification(self.project, self.data))

    def test_compatible_older_recorded_version_notifies_without_project_writes(self):
        state = self.project / '.codex/devcrew-state.json'
        value = json.loads(state.read_text())
        value['plugin_version'] = '0.0.1'
        state.write_text(json.dumps(value))
        before = {p.relative_to(self.project): p.read_bytes()
                  for p in self.project.rglob('*') if p.is_file()}

        message = notice.notification(self.project, self.data)

        self.assertIsNotNone(message)
        self.assertIn(devcrew._plugin_version(), message)
        self.assertEqual(before, {p.relative_to(self.project): p.read_bytes()
                                 for p in self.project.rglob('*') if p.is_file()})

    def test_new_plugin_version_notifies_once_when_project_state_is_unchanged(self):
        self.assertIsNotNone(notice.notification(self.project, self.data))
        before = {p.relative_to(self.project): p.read_bytes()
                  for p in self.project.rglob('*') if p.is_file()}
        advanced_version = '99.99.99'

        with patch.object(devcrew, '_plugin_version', return_value=advanced_version):
            message = notice.notification(self.project, self.data)
            self.assertIsNotNone(message)
            self.assertIn(advanced_version, message)
            self.assertIsNone(notice.notification(self.project, self.data))

        self.assertEqual(before, {p.relative_to(self.project): p.read_bytes()
                                 for p in self.project.rglob('*') if p.is_file()})

    def test_legacy_marker_notifies_once_and_is_upgraded_with_version(self):
        self.assertIsNotNone(notice.notification(self.project, self.data))
        directory = notice.runtime_directory(self.data, initialize=True) / 'devcrew-sync-notices'
        marker = next(directory.glob('*.json'))
        legacy = json.loads(marker.read_text())
        legacy.pop('plugin_version', None)
        marker.write_text(json.dumps(legacy))

        message = notice.notification(self.project, self.data)

        self.assertIsNotNone(message)
        upgraded = json.loads(marker.read_text())
        self.assertEqual(upgraded['plugin_version'], devcrew._plugin_version())
        self.assertEqual(upgraded['state_sha256'], legacy['state_sha256'])
        self.assertIsNone(notice.notification(self.project, self.data))

    def test_local_overrides_are_disclosed_and_next_sync_notifies(self):
        self.assertIsNotNone(notice.notification(self.project, self.data))
        role = self.project / '.codex/agents/decision-agent.toml'
        role.write_text('model = "local-model"\n' + role.read_text())
        self.assertIsNone(notice.notification(self.project, self.data))
        self.cli('sync')
        message = notice.notification(self.project, self.data)
        self.assertIn('decision-agent', message)
        self.assertIn('Local overrides preserved', message)

    def test_uninitialized_project_is_silent(self):
        empty = pathlib.Path(self.tmp.name) / 'empty'
        empty.mkdir()
        self.assertIsNone(notice.notification(empty, self.data))

    def test_concurrent_notifications_are_deduplicated(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            messages = list(pool.map(lambda _: notice.notification(self.project, self.data), range(2)))
        self.assertEqual(sum(message is not None for message in messages), 1)

    def test_russian_prompt_selects_russian_notice_with_english_code(self):
        event = {"hook_event_name": "UserPromptSubmit", "cwd": str(self.project),
                 "prompt": "Проверь этот код: `assert record.is_valid()`"}
        result = subprocess.run([sys.executable, "-B", str(ROOT / "hooks/project_sync_notice.py")],
            cwd=self.project, env={**os.environ, "PLUGIN_DATA": str(self.data)},
            input=json.dumps(event), capture_output=True, text=True, check=True)
        self.assertIn("синхронизирован", json.loads(result.stdout)["systemMessage"])

    def test_notice_language_without_a_prompt_uses_process_locale(self):
        with patch.dict(os.environ, {"LC_ALL": "ru_RU.UTF-8"}):
            self.assertEqual(notice.notice_language(None), "ru")
            self.assertEqual(notice.notice_language("Review the changes"), "en")
            self.assertEqual(notice.notice_language("Проверь README.md and CONTRIBUTING.md"), "ru")

    def test_packaged_events_and_cli_output(self):
        hooks = json.loads((ROOT / 'hooks/hooks.json').read_text())['hooks']
        for event in ('UserPromptSubmit', 'Stop'):
            self.assertTrue(any('project_sync_notice.py' in entry['command']
                                for group in hooks[event] for entry in group['hooks']))
            result = subprocess.run([sys.executable, '-B', str(ROOT / 'hooks/project_sync_notice.py')],
                cwd=self.project, env={**os.environ, 'PLUGIN_DATA': str(self.data)},
                input=json.dumps({'hook_event_name': event, 'cwd': str(self.project)}),
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            output = json.loads(result.stdout)
            self.assertEqual(bool(output.get('systemMessage')), event == 'UserPromptSubmit')
            self.assertNotIn('decision', output)

    def test_missing_plugin_data_never_blocks_or_claims_success(self):
        env = {key: value for key, value in os.environ.items() if key != 'PLUGIN_DATA'}
        result = subprocess.run([sys.executable, '-B', str(ROOT / 'hooks/project_sync_notice.py')],
            cwd=self.project, env=env, input=json.dumps({'hook_event_name': 'Stop', 'cwd': str(self.project)}),
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {})
