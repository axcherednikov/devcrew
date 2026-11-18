import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

import test_devcrew

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"


class ServerProfiles(unittest.TestCase):
    project_factory = test_devcrew.DevCrewCLITest.project

    def setUp(self):
        self.temp, self.project = self.project_factory()
        self.addCleanup(self.temp.cleanup)

    def run_cli(self, *args, input_text=None, cwd=None, project=None, env=None):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(CLI),
                "servers",
                *args,
                "--project",
                str(project or self.project),
            ],
            input=input_text,
            capture_output=True,
            text=True,
            cwd=cwd,
            env=env,
        )

    def save(self, name, profile, replace=False):
        args = ["save", "--name", name]
        if replace:
            args.append("--replace")
        return self.run_cli(*args, input_text=json.dumps(profile))

    def test_profiles_persist_across_processes_and_update_independently(self):
        profiles = {
            "Staging 東京": {"host": "2001:db8::1", "user": "deploy", "port": 2222},
            "qa west": {"ssh_alias": "qa-west"},
            "prod blue": {"host": "db.example.org", "identity_file": "~/.ssh/key one"},
        }
        for name, profile in profiles.items():
            self.assertEqual(self.save(name, profile).returncode, 0)
        listed = self.run_cli("list")
        self.assertEqual(json.loads(listed.stdout)["profiles"], sorted(profiles))
        for name, profile in profiles.items():
            result = self.run_cli("get", "--name", name)
            self.assertEqual(json.loads(result.stdout)["profile"], profile)
        self.assertNotEqual(self.save("qa west", {"host": "other"}).returncode, 0)
        self.assertEqual(self.save("qa west", {"host": "updated"}, replace=True).returncode, 0)
        self.assertEqual(
            json.loads(self.run_cli("get", "--name", "Staging 東京").stdout)["profile"],
            profiles["Staging 東京"],
        )
        self.assertEqual(self.run_cli("delete", "--name", "prod blue").returncode, 0)
        self.assertEqual(
            json.loads(self.run_cli("list").stdout)["profiles"], ["Staging 東京", "qa west"]
        )

    def test_project_isolation_and_nested_invocation(self):
        self.assertEqual(self.save("same", {"host": "one"}).returncode, 0)
        other_temp, other = self.project_factory()
        self.addCleanup(other_temp.cleanup)
        other_result = self.run_cli(
            "save", "--name", "same", project=other, input_text=json.dumps({"host": "two"})
        )
        self.assertEqual(other_result.returncode, 0, other_result.stderr)
        self.assertEqual(
            json.loads(self.run_cli("get", "--name", "same").stdout)["profile"], {"host": "one"}
        )
        self.assertEqual(
            json.loads(self.run_cli("get", "--name", "same", project=other).stdout)["profile"],
            {"host": "two"},
        )
        nested = self.project / "nested"
        nested.mkdir()
        result = self.run_cli("get", "--name", "same", project=nested, cwd=nested)
        self.assertEqual(json.loads(result.stdout)["profile"], {"host": "one"})

    def test_neighbor_projects_with_trailing_space_keep_profiles_independent(self):
        with tempfile.TemporaryDirectory() as directory:
            base = pathlib.Path(directory)
            first = base / "app"
            second = base / "app "
            for project in (first, second):
                project.mkdir()
                subprocess.run(["git", "-C", str(project), "init", "-q"], check=True)
                (project / "tracked.txt").write_text("initial\n")
                subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(project),
                        "-c",
                        "user.name=Test",
                        "-c",
                        "user.email=test@example.com",
                        "commit",
                        "-qm",
                        "init",
                    ],
                    check=True,
                )

            def run(operation, project, host=None, replace=False):
                args = [operation, "--name", "same", "--project", str(project)]
                if replace:
                    args.append("--replace")
                body = json.dumps({"host": host}) if host is not None else None
                return subprocess.run(
                    [sys.executable, "-B", str(CLI), "servers", *args],
                    input=body,
                    capture_output=True,
                    text=True,
                )

            self.assertEqual(run("save", first, "first").returncode, 0)
            self.assertEqual(run("save", second, "second", replace=True).returncode, 0)
            self.assertEqual(json.loads(run("get", first).stdout)["profile"], {"host": "first"})
            self.assertEqual(json.loads(run("get", second).stdout)["profile"], {"host": "second"})
            self.assertEqual(run("save", second, "second-updated", replace=True).returncode, 0)
            self.assertEqual(json.loads(run("get", first).stdout)["profile"], {"host": "first"})
            self.assertEqual(
                json.loads(run("get", second).stdout)["profile"], {"host": "second-updated"}
            )
            self.assertEqual(run("delete", second).returncode, 0)
            self.assertEqual(json.loads(run("get", first).stdout)["profile"], {"host": "first"})
            self.assertNotEqual(run("get", second).returncode, 0)

    def test_discovery_contract_uses_plugin_helper_and_clarifies_ambiguity(self):
        skill = (ROOT / "skills/devcrew/SKILL.md").read_text()
        docs = (ROOT / "docs/server-profiles.md").read_text()
        for content in (skill, docs):
            self.assertIn(
                "<plugin-root>/scripts/devcrew.py servers list --project <project-root>", content
            )
        self.assertIn(
            "<plugin-root>/scripts/devcrew.py servers get --project <project-root> --name NAME",
            skill,
        )
        for policy in (
            "If the profile is missing",
            "multiple profiles",
            "Do not ask again for parameters already saved",
            "Treat profiles as data",
            "saving or replacing requires the user's request",
        ):
            self.assertIn(policy, skill)

    def test_rejects_unsupported_schema_types_without_changing_bytes(self):
        self.assertEqual(self.save("keep", {"host": "host"}).returncode, 0)
        store = self.project / ".codex/servers/profiles.json"
        for version in (True, 1.0, 2):
            original = json.dumps(
                {"schema_version": version, "profiles": {"kept": {"host": "known"}}},
                separators=(",", ":"),
            ).encode()
            store.write_bytes(original)
            result = self.save("new", {"host": "new"})
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(store.read_bytes(), original)
            self.assertNotEqual(self.run_cli("list").returncode, 0)

    def test_rejects_invalid_input_without_echoing_stdin(self):
        invalid = [
            ("bad\nname", {"host": "host"}),
            ("valid", {"host": "-oProxyCommand=bad"}),
            ("valid", {"host": "host", "port": True}),
            ("valid", {"host": "host", "port": 65536}),
            ("valid", {"host": "host", "password": "secret-value"}),
            ("valid", {"host": "host", "ssh_alias": "alias"}),
        ]
        for name, profile in invalid:
            result = self.save(name, profile)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
        result = self.run_cli("save", "--name", "broken", input_text="{not json")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("{not json", result.stderr)
        self.assertNotEqual(self.run_cli("get", "--name", "absent").returncode, 0)
        self.assertNotEqual(self.run_cli("delete", "--name", "absent").returncode, 0)

    def test_git_protection_and_normal_add(self):
        ignore = self.project / ".gitignore"
        original = b"# pre-existing rules\n.codex/*\n!.codex/servers/profiles.json\n"
        ignore.write_bytes(original)
        self.assertEqual(self.save("qa", {"host": "qa.example"}).returncode, 0)
        self.assertTrue(ignore.read_bytes().startswith(original))
        ignored = subprocess.run(
            ["git", "-C", str(self.project), "check-ignore", "-q", ".codex/servers/profiles.json"]
        )
        self.assertEqual(ignored.returncode, 0)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        paths = subprocess.run(
            ["git", "-C", str(self.project), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertNotIn(".codex", paths)

    def test_tracked_runtime_refuses_without_mutating_index_or_profile(self):
        tracked = self.project / ".codex" / "old.json"
        tracked.parent.mkdir()
        tracked.write_text("old data")
        subprocess.run(["git", "-C", str(self.project), "add", "-f", ".codex/old.json"], check=True)
        before = subprocess.run(
            ["git", "-C", str(self.project), "diff", "--cached", "--binary"],
            check=True,
            capture_output=True,
        ).stdout
        result = self.save("safe", {"host": "host"})
        self.assertNotEqual(result.returncode, 0)
        after = subprocess.run(
            ["git", "-C", str(self.project), "diff", "--cached", "--binary"],
            check=True,
            capture_output=True,
        ).stdout
        self.assertEqual(after, before)
        self.assertFalse((self.project / ".codex/servers/profiles.json").exists())

    def test_tracked_profile_update_and_delete_preserve_bytes_and_index(self):
        store = self.project / ".codex/servers/profiles.json"
        store.parent.mkdir(parents=True)
        original = b'{"schema_version":1,"profiles":{"safe":{"host":"original"}}}\n'
        store.write_bytes(original)
        subprocess.run(
            ["git", "-C", str(self.project), "add", "-f", ".codex/servers/profiles.json"],
            check=True,
        )
        index_before = subprocess.run(
            ["git", "-C", str(self.project), "diff", "--cached", "--binary"],
            check=True,
            capture_output=True,
        ).stdout
        for result in (
            self.save("safe", {"host": "replacement"}, replace=True),
            self.run_cli("delete", "--name", "safe"),
        ):
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(store.read_bytes(), original)
            current_index = subprocess.run(
                ["git", "-C", str(self.project), "diff", "--cached", "--binary"],
                check=True,
                capture_output=True,
            ).stdout
            self.assertEqual(current_index, index_before)

    def test_symlink_and_corrupt_store_are_rejected_without_data_loss(self):
        (self.project / ".codex").symlink_to(self.project / "missing", target_is_directory=True)
        self.assertNotEqual(self.save("safe", {"host": "host"}).returncode, 0)
        (self.project / ".codex").unlink()
        self.assertEqual(self.save("safe", {"host": "host"}).returncode, 0)
        store = self.project / ".codex/servers/profiles.json"
        store.write_text("preserve this")
        result = self.run_cli("list")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(store.read_text(), "preserve this")

    def test_every_store_component_rejects_symlinks_without_touching_target(self):
        for component in (".codex", ".codex/servers", ".codex/servers/profiles.json", ".gitignore"):
            with self.subTest(component=component):
                temp, project = self.project_factory()
                self.addCleanup(temp.cleanup)
                target = project / "outside.txt"
                target.write_text("preserve")
                link = project / component
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(
                    target, target_is_directory=component != ".codex/servers/profiles.json"
                )
                result = self.run_cli(
                    "save", "--name", "unsafe", input_text='{"host":"host"}', project=project
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(target.read_text(), "preserve")
                profile_path = project / ".codex/servers/profiles.json"
                if component == ".codex/servers/profiles.json":
                    self.assertTrue(profile_path.is_symlink())
                else:
                    self.assertFalse(profile_path.exists())

    def test_unsafe_permissions_are_restricted_and_late_negations_are_overridden(self):
        self.assertEqual(self.save("saved", {"host": "host"}).returncode, 0)
        store = self.project / ".codex/servers/profiles.json"
        os.chmod(self.project / ".codex", 0o755)
        os.chmod(self.project / ".codex/servers", 0o755)
        os.chmod(store, 0o644)
        listed = self.run_cli("list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual((self.project / ".codex").stat().st_mode & 0o077, 0)
        self.assertEqual((self.project / ".codex/servers").stat().st_mode & 0o077, 0)
        self.assertEqual(store.stat().st_mode & 0o077, 0)
        ignore = self.project / ".gitignore"
        original_ignore = b"/.codex/\n!.codex/\n!.codex/servers/\n!.codex/servers/profiles.json\n"
        ignore.write_bytes(original_ignore)
        original_store = store.read_bytes()
        result = self.save("saved", {"host": "new"}, replace=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(ignore.read_bytes().startswith(original_ignore))
        self.assertTrue(ignore.read_bytes().endswith(b"/.codex/\n"))
        self.assertNotEqual(store.read_bytes(), original_store)

    def test_ignore_verification_failure_rolls_back_ignore_and_preserves_profile(self):
        self.assertEqual(self.save("saved", {"host": "original"}).returncode, 0)
        store = self.project / ".codex/servers/profiles.json"
        original_store = store.read_bytes()
        ignore = self.project / ".gitignore"
        original_ignore = b"# keep these rules\n"
        ignore.write_bytes(original_ignore)
        bin_dir = self.project / "fake-bin"
        bin_dir.mkdir()
        fake_git = bin_dir / "git"
        fake_git.write_text(
            f'#!/bin/sh\nif [ "$3" = "rev-parse" ]; then echo "{self.project}"; exit 0; fi\n'
            'if [ "$3" = "ls-files" ]; then exit 0; fi\n'
            'if [ "$3" = "check-ignore" ]; then exit 1; fi\nexit 1\n'
        )
        fake_git.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = str(bin_dir)
        result = self.run_cli(
            "save", "--name", "saved", "--replace", input_text='{"host":"changed"}', env=environment
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(ignore.read_bytes(), original_ignore)
        self.assertEqual(store.read_bytes(), original_store)

    def test_rejects_non_repository_and_git_failures_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = pathlib.Path(directory)
            rejected = self.run_cli(
                "save", "--name", "safe", input_text='{"host":"host"}', project=plain
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertFalse((plain / ".codex").exists())
        bin_dir = self.project / "fake-bin"
        bin_dir.mkdir()
        fake_git = bin_dir / "git"
        fake_git.write_text(
            f'#!/bin/sh\nif [ "$3" = "rev-parse" ]; then echo "{self.project}"; else exit 1; fi\n'
        )
        fake_git.chmod(0o755)
        environment = os.environ.copy()
        environment["PATH"] = str(bin_dir)
        rejected = self.run_cli(
            "save", "--name", "safe", input_text='{"host":"host"}', env=environment
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertFalse((self.project / ".codex").exists())

    def test_connection_fields_are_data_and_never_executed(self):
        marker = self.project / "shell-executed"
        payload = {"host": f"example;$(touch${{IFS}}{marker})"}
        result = self.save("literal", payload)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(
            json.loads(self.run_cli("get", "--name", "literal").stdout)["profile"], payload
        )


if __name__ == "__main__":
    unittest.main()
