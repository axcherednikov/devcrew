import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_fs as fs
import devcrew_resources


class PortableFiles(unittest.TestCase):
    def test_windows_names_reject_device_aliases_and_alternate_data(self):
        for part in ("state:secret", "C:escape", "CON.txt", "prn", "state.", "state ", r"a\..\b", "a/b", "<wild>"):
            self.assertFalse(fs.windows_safe_parts(["safe", part]), part)
        self.assertTrue(fs.windows_safe_parts([".codex", "задачи", "result.json"]))

    def test_owner_is_visible_while_another_process_holds_resource(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = (
                "from devcrew_resources import ResourceLock\n"
                "import sys\n"
                "with ResourceLock(sys.argv[1], 'db', 'child'):\n"
                " print('held', flush=True)\n"
                " sys.stdin.readline()\n"
            )
            with subprocess.Popen([sys.executable, "-B", "-c", script, tmp],
                                  cwd=ROOT / "scripts", stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True) as child:
                try:
                    self.assertEqual(child.stdout.readline().strip(), "held")
                    status = devcrew_resources.status(tmp, "db")
                    self.assertTrue(status["busy"])
                    self.assertEqual(status["owner"]["project"], "child")
                    with self.assertRaisesRegex(ValueError, "busy"):
                        with devcrew_resources.ResourceLock(tmp, "db", "parent"):
                            self.fail("two writers acquired the resource")
                finally:
                    child.stdin.close()
                    child.wait(timeout=10)
            self.assertFalse(devcrew_resources.status(tmp, "db")["busy"])

    def test_stalled_input_stops_at_hook_deadline(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / ".codex/devcrew-state.json"
            marker.parent.mkdir()
            marker.write_text("{}", encoding="utf-8")
            for name in ("user_prompt_submit", "project_sync_notice"):
                with self.subTest(name=name), subprocess.Popen(
                    [sys.executable, "-B", str(ROOT / "hooks" / (name + ".py"))],
                    cwd=tmp, stdin=subprocess.PIPE, stdout=subprocess.PIPE
                ) as hook:
                    try:
                        self.assertEqual(hook.wait(timeout=8), 0)
                        output = json.loads(hook.stdout.read())
                        if name == "user_prompt_submit":
                            self.assertEqual(output["decision"], "block")
                        else:
                            self.assertEqual(output, {})
                    finally:
                        if hook.poll() is None:
                            hook.kill()

    def test_publish_never_overwrites_existing_record_on_create(self):
        with tempfile.TemporaryDirectory() as tmp:
            new, existing = Path(tmp) / "new", Path(tmp) / "state"
            new.write_bytes(b"after")
            existing.write_bytes(b"before")
            with self.assertRaises(FileExistsError):
                fs.publish(new, existing, replace=False)
            self.assertEqual(existing.read_bytes(), b"before")
            fs.publish(new, existing, replace=True)
            self.assertEqual(existing.read_bytes(), b"after")


@unittest.skipUnless(os.name == "nt", "requires Windows filesystems and hook launcher")
class NativeWindowsFiles(unittest.TestCase):
    def test_private_acl_and_shared_writer_are_verified(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "private"
            directory.mkdir()
            fs.private(directory, 0o700)
            file = directory / "snapshot"
            file.write_bytes(b"example")
            fs.private(file, 0o600)
            self.assertTrue(fs.has_private_mode(file, 0o600))
            # Grant the well-known Everyone SID read access (locale independent).
            subprocess.run(["icacls", str(file), "/grant", "*S-1-1-0:(R)"], check=True,
                           capture_output=True)
            self.assertFalse(fs.has_private_mode(file, 0o600))
            self.assertTrue(fs.safe_writer_permissions(file))
            subprocess.run(["icacls", str(file), "/grant", "*S-1-1-0:(M)"], check=True,
                           capture_output=True)
            self.assertFalse(fs.safe_writer_permissions(file))

    def test_reserved_state_paths_rejected_before_init_writes(self):
        for value in ("reports:escape", r"sub\..\CON.txt", "con.txt", "audit."):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as tmp:
                result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
                                         "init", "--project", tmp, "--checks-dir", value], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_junction_blocks_init_write_through_project(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside, project = Path(tmp) / "outside", Path(tmp) / "project"
            outside.mkdir()
            project.mkdir()
            junction = project / ".codex"
            subprocess.run(["cmd", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
                           check=True, capture_output=True)
            self.assertTrue(fs.is_link(junction))
            try:
                result = subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
                                         "init", "--project", str(project)], capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(list(outside.iterdir()), [])
            finally:
                junction.rmdir()

    def test_trusted_hook_commands_in_native_shell_with_unicode_project(self):
        hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "проект with spaces"
            project.mkdir()
            subprocess.run([sys.executable, "-B", str(ROOT / "scripts/devcrew.py"),
                            "init", "--project", str(project)], check=True, capture_output=True)
            env = {**os.environ, "PLUGIN_DATA": str(Path(tmp) / "plugin data"),
                   "PLUGIN_ROOT": str(ROOT)}
            data = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "local",
                               "turn_id": "turn-1", "cwd": str(project), "prompt": "Проверь проект"})
            for hook in hooks["UserPromptSubmit"][0]["hooks"]:
                result = subprocess.run(hook["commandWindows"], shell=True, cwd=project, env=env,
                                        input=data.encode(), capture_output=True, check=True, timeout=10)
                output = json.loads(result.stdout)
                if "user_prompt_submit.py" in hook["commandWindows"]:
                    self.assertIn("ORIGINAL INTENT RECEIPT", output["hookSpecificOutput"]["additionalContext"])
                else:
                    self.assertIn("синхронизирован", output["systemMessage"])
