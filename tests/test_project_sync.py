import argparse
import contextlib
import hashlib
import io
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"
ROLE_ROOT = ROOT / "skills/devcrew-setup/assets/agents"
PROJECT_STATE = ".codex/devcrew-state.json"
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew


class ProjectSyncTest(unittest.TestCase):
    def run_cli(self, *args, cwd=None, cli=CLI):
        return subprocess.run(
            [sys.executable, str(cli), *map(str, args)],
            cwd=cwd or ROOT,
            text=True,
            capture_output=True,
        )

    def project(self, *, git=True):
        td = tempfile.TemporaryDirectory()
        path = pathlib.Path(td.name) / "project"
        path.mkdir()
        (path / "source.py").write_text("baseline\n")
        if git:
            subprocess.run(["git", "init", "-q", str(path)], check=True)
            subprocess.run(
                ["git", "-C", str(path), "config", "core.excludesFile", devcrew.os.devnull],
                check=True,
            )
            subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True)
            subprocess.run(
                ["git", "-C", str(path), "config", "user.email", "test@example.com"], check=True
            )
            subprocess.run(["git", "-C", str(path), "add", "--", "source.py"], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(path),
                    "-c",
                    "user.name=Test",
                    "-c",
                    "user.email=test@example.com",
                    "commit",
                    "-qm",
                    "baseline",
                ],
                check=True,
            )
        self.addCleanup(td.cleanup)
        return path

    def init_project(self, *, git=True):
        project = self.project(git=git)
        result = self.run_cli("init", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        return project, json.loads(result.stdout)

    def commit_project_setup(self, project, *, tracked_runtime=False):
        subprocess.run(
            ["git", "-C", str(project), "add", "--", ".gitignore", "docs/context.md", "memory"],
            check=True,
        )
        if tracked_runtime:
            # Reproduce projects affected by the old force-add sync behavior.
            subprocess.run(["git", "-C", str(project), "add", "-f", "--", ".codex"], check=True)
        subprocess.run(
            ["git", "-C", str(project), "commit", "--allow-empty", "-qm", "initialize DevCrew"],
            check=True,
        )

    def state(self, project):
        return json.loads((project / PROJECT_STATE).read_text())

    def write_state(self, project, state):
        (project / PROJECT_STATE).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")

    def legacy_project(self):
        project, _ = self.init_project()
        (project / ".codex/agents/decision-agent.toml").unlink()
        (project / PROJECT_STATE).unlink()
        self.commit_project_setup(project)
        return project

    def status(self, project):
        result = self.run_cli("sync-status", "--project", project)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        return json.loads(result.stdout)

    def template_update(self, *, git=False):
        project, _ = self.init_project(git=git)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        plugin = pathlib.Path(td.name) / "plugin"
        for relative in ("scripts", "skills/devcrew-setup/assets", ".codex-plugin"):
            shutil.copytree(ROOT / relative, plugin / relative)
        template = plugin / "skills/devcrew-setup/assets/agents/executor-agent.toml"
        template.write_text(
            template.read_text().replace(
                "Read project instructions,",
                "Apply the updated execution contract. Read project instructions,",
            )
        )
        return project, plugin, template

    def test_template_update_preserves_executor_overrides_and_hashes(self):
        project, plugin, template = self.template_update()
        role = project / ".codex/agents/executor-agent.toml"
        role.write_text(
            ('model = "custom-model"\n' + role.read_text()).replace(
                'model_reasoning_effort = "low"', 'model_reasoning_effort = "high"'
            )
        )
        custom = project / ".codex/agents/custom.toml"
        custom.write_text('name = "custom"\n')
        other = project / ".codex/agents/scout.toml"
        unchanged = other.read_bytes()
        result = self.run_cli("sync", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "RUNTIME_RESTART_RECOMMENDED")
        actual = tomllib.loads(role.read_text())
        current = tomllib.loads(template.read_text())
        self.assertEqual(actual["developer_instructions"], current["developer_instructions"])
        self.assertEqual(
            (actual["model"], actual["model_reasoning_effort"]), ("custom-model", "high")
        )
        state = self.state(project)["managed_roles"]["executor-agent"]
        self.assertEqual(
            state["local_overrides"], {"model": "custom-model", "model_reasoning_effort": "high"}
        )
        self.assertEqual(
            state["template_sha256"], hashlib.sha256(template.read_bytes()).hexdigest()
        )
        self.assertEqual(state["installed_sha256"], hashlib.sha256(role.read_bytes()).hexdigest())
        self.assertEqual(state["managed_fields"], ["name", "description", "developer_instructions"])
        projection = {field: current[field] for field in state["managed_fields"]}
        expected = hashlib.sha256(
            json.dumps(
                projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
        self.assertEqual(state["managed_fields_sha256"], expected)
        for field in ("model", "model_reasoning_effort"):
            self.assertEqual(
                state["override_fields_sha256"][field],
                hashlib.sha256(json.dumps(actual[field]).encode()).hexdigest(),
            )
        before = (project / PROJECT_STATE).read_bytes()
        second = self.run_cli("sync", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertFalse(json.loads(second.stdout)["changed"])
        self.assertEqual((project / PROJECT_STATE).read_bytes(), before)
        self.assertEqual(other.read_bytes(), unchanged)
        self.assertEqual(custom.read_text(), 'name = "custom"\n')
        self.assertEqual((project / "source.py").read_text(), "baseline\n")

    def test_sync_removes_an_unmodified_old_model_pin(self):
        project = self.project(git=False)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        old_plugin = pathlib.Path(td.name) / "old-plugin"
        for relative in ("scripts", "skills/devcrew-setup/assets", ".codex-plugin"):
            shutil.copytree(ROOT / relative, old_plugin / relative)
        template = old_plugin / "skills/devcrew-setup/assets/agents/reviewer-agent.toml"
        template.write_text('model = "old-model"\n' + template.read_text())
        initialized = self.run_cli(
            "init", "--project", project, cli=old_plugin / "scripts/devcrew.py"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        role = project / ".codex/agents/reviewer-agent.toml"
        self.assertEqual(tomllib.loads(role.read_text())["model"], "old-model")
        synced = self.run_cli("sync", "--project", project)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        self.assertEqual(role.read_bytes(), (ROLE_ROOT / "reviewer-agent.toml").read_bytes())
        self.assertNotIn("model", tomllib.loads(role.read_text()))
        self.assertEqual(
            self.state(project)["managed_roles"]["reviewer-agent"]["local_overrides"], {}
        )

    def test_sync_removes_old_model_defaults_and_preserves_unpinned_roles(self):
        project = self.project(git=False)
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        old_plugin = pathlib.Path(td.name) / "old-plugin"
        for relative in ("scripts", "skills/devcrew-setup/assets", ".codex-plugin"):
            shutil.copytree(ROOT / relative, old_plugin / relative)
        templates = old_plugin / "skills/devcrew-setup/assets/agents"
        for template in templates.glob("*.toml"):
            if template.stem != "scout":
                template.write_text('model = "old-model"\n' + template.read_text())
        initialized = self.run_cli(
            "init", "--project", project, cli=old_plugin / "scripts/devcrew.py"
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        roles = project / ".codex/agents"
        scout_before = (roles / "scout.toml").read_bytes()
        synced = self.run_cli("sync", "--project", project)
        self.assertEqual(synced.returncode, 0, synced.stdout + synced.stderr)
        for template in ROLE_ROOT.glob("*.toml"):
            with self.subTest(role=template.stem):
                self.assertEqual((roles / template.name).read_bytes(), template.read_bytes())
        self.assertEqual(scout_before, (roles / "scout.toml").read_bytes())
        again = self.run_cli("sync", "--project", project)
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        self.assertFalse(json.loads(again.stdout)["changed"])

    def test_user_can_change_override_after_sync_without_rewriting_role(self):
        project, _ = self.init_project(git=False)
        role = project / ".codex/agents/executor-agent.toml"
        role.write_text(('model = "custom-model"\n' + role.read_text()))
        first = self.run_cli("sync", "--project", project)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertEqual(
            self.state(project)["managed_roles"]["executor-agent"]["local_overrides"],
            {"model": "custom-model"},
        )
        role.write_text(role.read_text().replace('"custom-model"', '"another-model"'))
        before = role.read_bytes()
        second = self.run_cli("sync", "--project", project)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(role.read_bytes(), before)
        self.assertEqual(
            self.state(project)["managed_roles"]["executor-agent"]["local_overrides"],
            {"model": "another-model"},
        )

    def test_unmodified_default_follows_new_template_model(self):
        project, plugin, template = self.template_update()
        template.write_text('model = "custom-model"\n' + template.read_text())
        result = self.run_cli("sync", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        role = tomllib.loads((project / ".codex/agents/executor-agent.toml").read_text())
        self.assertEqual(role["model"], "custom-model")
        self.assertEqual(
            self.state(project)["managed_roles"]["executor-agent"]["local_overrides"], {}
        )

    def test_managed_edit_or_unknown_field_conflicts_without_writes(self):
        for change in (
            lambda text: text.replace("Read project instructions,", "Custom execution policy."),
            lambda text: text + '\nsandbox_mode = "read-only"\n',
            lambda text: text + "\ncustom_setting = true\n",
        ):
            with self.subTest(change=change):
                project, plugin, _ = self.template_update()
                role = project / ".codex/agents/executor-agent.toml"
                role.write_text(change(role.read_text()))
                missing = project / ".codex/agents/decision-agent.toml"
                missing.unlink()
                before = role.read_bytes(), (project / PROJECT_STATE).read_bytes()
                result = self.run_cli(
                    "sync", "--project", project, cli=plugin / "scripts/devcrew.py"
                )
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertEqual(json.loads(result.stdout)["status"], "SYNC_CONFLICT")
                self.assertEqual(
                    (role.read_bytes(), (project / PROJECT_STATE).read_bytes()), before
                )
                self.assertFalse(missing.exists())

    def test_legacy_role_without_any_state_migrates_known_instructions(self):
        project = self.project(git=False)
        roles = project / ".codex/agents"
        roles.mkdir(parents=True)
        history = json.loads(
            (ROOT / "skills/devcrew-setup/assets/legacy-managed-role-fields.json").read_text()
        )
        old = history["roles"]["executor-agent"][0]["managed_fields"]
        role = roles / "executor-agent.toml"
        fields = {**old, "model": "custom-model", "model_reasoning_effort": "high"}
        role.write_text("".join(f"{key} = {json.dumps(value)}\n" for key, value in fields.items()))
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = tomllib.loads(role.read_text())
        current = tomllib.loads((ROLE_ROOT / "executor-agent.toml").read_text())
        self.assertEqual(actual["developer_instructions"], current["developer_instructions"])
        self.assertEqual(
            (actual["model"], actual["model_reasoning_effort"]), ("custom-model", "high")
        )
        self.assertTrue((roles / "reviewer-agent.toml").exists())
        self.assertTrue((roles / "decision-agent.toml").exists())
        self.assertFalse((project / ".codex/devcrew.json").exists())
        self.assertEqual(self.status(project)["status"], "SYNC_OK")

    def test_unknown_legacy_managed_fields_conflict(self):
        project, _ = self.init_project(git=False)
        (project / PROJECT_STATE).unlink()
        role = project / ".codex/agents/executor-agent.toml"
        role.write_text(
            role.read_text().replace("Read project instructions,", "Unproven custom instructions.")
        )
        before = role.read_bytes()
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(role.read_bytes(), before)
        self.assertFalse((project / PROJECT_STATE).exists())

    def test_shipped_sandbox_permissions_are_managed_and_local_edits_conflict(self):
        project, _ = self.init_project(git=False)
        state = self.state(project)
        for template in ROLE_ROOT.glob("*.toml"):
            fields = tomllib.loads(template.read_text())
            record = state["managed_roles"][template.stem]
            self.assertEqual("sandbox_mode" in record["managed_fields"], "sandbox_mode" in fields)
            projection = {key: fields[key] for key in record["managed_fields"]}
            digest = hashlib.sha256(
                json.dumps(
                    projection, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            self.assertEqual(record["managed_fields_sha256"], digest)
        self.assertEqual(self.status(project)["status"], "SYNC_OK")
        role = project / ".codex/agents/scout.toml"
        role.write_text(
            role.read_text().replace(
                'sandbox_mode = "read-only"', 'sandbox_mode = "workspace-write"'
            )
        )
        before = role.read_bytes(), (project / PROJECT_STATE).read_bytes()
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((role.read_bytes(), (project / PROJECT_STATE).read_bytes()), before)
        self.assertNotIn(
            "sandbox_mode",
            tomllib.loads((project / ".codex/agents/executor-agent.toml").read_text()),
        )

    def test_simultaneous_template_defaults_and_local_reasoning_change(self):
        project, plugin, template = self.template_update()
        role = project / ".codex/agents/executor-agent.toml"
        role.write_text(
            role.read_text().replace(
                'model_reasoning_effort = "low"', 'model_reasoning_effort = "high"'
            )
        )
        template.write_text('model = "another-model"\n' + template.read_text())
        result = self.run_cli("sync", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        actual = tomllib.loads(role.read_text())
        self.assertEqual(
            (actual["model"], actual["model_reasoning_effort"]), ("another-model", "high")
        )
        self.assertEqual(
            self.state(project)["managed_roles"]["executor-agent"]["local_overrides"],
            {"model_reasoning_effort": "high"},
        )

    def test_existing_overrides_survive_another_template_update(self):
        project, plugin, template = self.template_update()
        role = project / ".codex/agents/executor-agent.toml"
        role.write_text(
            ('model = "custom-model"\n' + role.read_text()).replace(
                'model_reasoning_effort = "low"', 'model_reasoning_effort = "high"'
            )
        )
        cli = plugin / "scripts/devcrew.py"
        first = self.run_cli("sync", "--project", project, cli=cli)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        template.write_text(
            'model = "next-default"\n'
            + template.read_text().replace(
                "Apply the updated execution contract.", "Apply the next execution contract."
            )
        )
        second = self.run_cli("sync", "--project", project, cli=cli)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        actual = tomllib.loads(role.read_text())
        self.assertEqual(
            (actual["model"], actual["model_reasoning_effort"]), ("custom-model", "high")
        )
        self.assertIn("Apply the next execution contract.", actual["developer_instructions"])

    def test_legacy_exact_template_defaults_upgrade_instead_of_becoming_overrides(self):
        project, _ = self.init_project(git=False)
        (project / PROJECT_STATE).unlink()
        role = project / ".codex/agents/doc-digest.toml"
        role.write_bytes((ROOT / "tests/fixtures/legacy-doc-digest.toml").read_bytes())
        history = json.loads(
            (ROOT / "skills/devcrew-setup/assets/legacy-managed-role-fields.json").read_text()
        )
        self.assertIn(
            hashlib.sha256(role.read_bytes()).hexdigest(),
            [entry["template_sha256"] for entry in history["roles"]["doc-digest"]],
        )
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(role.read_bytes(), (ROLE_ROOT / "doc-digest.toml").read_bytes())
        self.assertEqual(self.state(project)["managed_roles"]["doc-digest"]["local_overrides"], {})

    def test_invalid_state_hashes_and_invalid_template_preflight_do_not_write(self):
        project, plugin, _ = self.template_update()
        snapshot = {
            path: path.read_bytes() for path in (project / ".codex").rglob("*") if path.is_file()
        }
        invalid = plugin / "skills/devcrew-setup/assets/agents/tester.toml"
        invalid.write_text("invalid = [")
        result = self.run_cli("sync", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(snapshot, {path: path.read_bytes() for path in snapshot})
        state = self.state(project)
        state["managed_roles"]["executor-agent"]["override_fields_sha256"]["model"] = "invalid"
        self.write_state(project, state)
        before = (project / PROJECT_STATE).read_bytes()
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual((project / PROJECT_STATE).read_bytes(), before)

    def test_tracked_local_model_change_can_merge_with_instructions(self):
        project, plugin, _ = self.template_update(git=True)
        self.commit_project_setup(project, tracked_runtime=True)
        role = project / ".codex/agents/executor-agent.toml"
        role.write_text(('model = "custom-model"\n' + role.read_text()))
        result = self.run_cli("sync", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(tomllib.loads(role.read_text())["model"], "custom-model")
        result = self.run_cli("init", "--project", project, cli=plugin / "scripts/devcrew.py")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_state_write_failure_rolls_back_roles_and_state(self):
        for git in (False, True):
            with self.subTest(git=git):
                project, plugin, _ = self.template_update(git=git)
                if git:
                    self.commit_project_setup(project)
                role = project / ".codex/agents/executor-agent.toml"
                missing = project / ".codex/agents/decision-agent.toml"
                missing.unlink()
                if git:
                    self.commit_project_setup(project)
                before = role.read_bytes(), (project / PROJECT_STATE).read_bytes()
                ignore = project / ".gitignore"
                ignore_before = b"user.log" if git else None
                if ignore_before is None:
                    ignore.unlink()
                else:
                    ignore.write_bytes(ignore_before)
                replace = devcrew.os.replace
                failed = False

                def fail_state(source, destination):
                    nonlocal failed
                    if (
                        pathlib.Path(destination).resolve() == (project / PROJECT_STATE).resolve()
                        and not failed
                    ):
                        failed = True
                        raise OSError("injected state write failure")
                    return replace(source, destination)

                with (
                    mock.patch.object(
                        devcrew, "ROLE_ROOT", plugin / "skills/devcrew-setup/assets/agents"
                    ),
                    mock.patch.object(devcrew.os, "replace", side_effect=fail_state),
                    contextlib.redirect_stdout(io.StringIO()),
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    code = devcrew._sync_project(argparse.Namespace(project=str(project)))
                self.assertEqual(code, 1)
                self.assertTrue(failed)
                self.assertEqual(
                    (role.read_bytes(), (project / PROJECT_STATE).read_bytes()), before
                )
                self.assertFalse(missing.exists())
                self.assertEqual(ignore.read_bytes() if ignore.exists() else None, ignore_before)

    def test_new_setup_installs_all_roles_and_current_machine_state(self):
        project, initialized = self.init_project()
        expected_roles = {path.stem for path in ROLE_ROOT.glob("*.toml")}
        self.assertEqual(set(initialized["roles"]), expected_roles)
        self.assertIn("decision-agent", initialized["roles"])
        role = tomllib.loads((project / ".codex/agents/decision-agent.toml").read_text())
        self.assertEqual(role["name"], "decision-agent")
        state = self.state(project)
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text())
        self.assertEqual(state["plugin_version"], manifest["version"].split("+", 1)[0])
        self.assertIsInstance(state["project_schema_version"], int)
        self.assertEqual(self.status(project)["status"], "SYNC_OK")

    def test_uninitialized_project_is_reported_without_mutation(self):
        project = self.project()
        result = self.run_cli("sync-status", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "PROJECT_NOT_INITIALIZED")
        self.assertEqual(sorted(path.name for path in project.iterdir()), [".git", "source.py"])

    def test_plugin_version_change_without_schema_change_is_noop(self):
        project, _ = self.init_project()
        state = self.state(project)
        state["plugin_version"] = "0.1.0"
        self.write_state(project, state)
        before = (project / PROJECT_STATE).read_bytes()
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "SYNC_OK")
        self.assertFalse(payload["changed"])
        self.assertEqual((project / PROJECT_STATE).read_bytes(), before)

    def test_legacy_project_and_old_schema_require_sync(self):
        project = self.legacy_project()
        status = self.status(project)
        self.assertEqual(status["status"], "SYNC_REQUIRED")
        self.assertEqual(status["recorded_project_schema"], 0)
        self.assertIn("decision-agent", status["missing_roles"])

    def test_future_project_schema_is_a_conflict(self):
        project, _ = self.init_project()
        state = self.state(project)
        state["project_schema_version"] += 1
        self.write_state(project, state)
        self.assertEqual(self.status(project)["status"], "SYNC_CONFLICT")

    def test_missing_role_is_installed_and_recommends_runtime_restart(self):
        project = self.legacy_project()
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "RUNTIME_RESTART_RECOMMENDED")
        self.assertIn(".codex/agents/decision-agent.toml", payload["changed_paths"])
        self.assertTrue((project / ".codex/agents/decision-agent.toml").is_file())
        self.assertIn("sync_commit", payload)
        self.assertIsNone(payload["sync_commit"])

    def test_sync_keeps_runtime_ignored_without_changing_history_or_index(self):
        project, _ = self.init_project()
        (project / ".codex/agents/decision-agent.toml").unlink()
        source = project / "source.py"
        source.write_text("staged user change\n")
        subprocess.run(["git", "-C", str(project), "add", "--", "source.py"], check=True)
        source.write_text("unstaged user change\n")
        head = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"])
        index = subprocess.check_output(["git", "-C", str(project), "ls-files", "--stage", "-z"])

        result = self.run_cli("sync", "--project", project)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((project / ".codex/agents/decision-agent.toml").is_file())
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"]), head
        )
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "ls-files", "--stage", "-z"]), index
        )
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "ls-files", "--", ".codex"]), b""
        )
        ignored = subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "check-ignore",
                "--",
                PROJECT_STATE,
                ".codex/agents/decision-agent.toml",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(ignored.returncode, 0, ignored.stderr)
        self.assertEqual(
            set(ignored.stdout.splitlines()), {PROJECT_STATE, ".codex/agents/decision-agent.toml"}
        )
        self.assertEqual(source.read_text(), "unstaged user change\n")
        self.assertIsNone(json.loads(result.stdout)["sync_commit"])
        self.assertIsNone(self.state(project)["sync_commit_id"])

    def test_sync_repairs_missing_runtime_ignore_without_staging(self):
        for existing in (None, "*.log"):
            with self.subTest(existing=existing):
                project, _ = self.init_project()
                ignore = project / ".gitignore"
                if existing is None:
                    ignore.unlink()
                else:
                    ignore.write_text(existing)
                self.assertEqual(self.status(project)["status"], "SYNC_REQUIRED")
                result = self.run_cli("sync", "--project", project)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn(".gitignore", json.loads(result.stdout)["changed_paths"])
                self.assertEqual(
                    ignore.read_text(), (existing + "\n" if existing else "") + "/.codex/\n"
                )
                self.assertEqual(
                    subprocess.check_output(
                        ["git", "-C", str(project), "ls-files", "--", ".codex", ".gitignore"]
                    ),
                    b"",
                )
                self.assertEqual(self.status(project)["status"], "SYNC_OK")

    def test_existing_runtime_ignore_sources_do_not_change_gitignore(self):
        for source in ("root", "unanchored", "pattern", "info", "global"):
            with self.subTest(source=source):
                project = self.project()
                ignore = project / ".gitignore"
                global_config = project.parent / "gitconfig"
                global_config.write_text("")
                if source in ("root", "unanchored", "pattern"):
                    rule = {"root": "/.codex/", "unanchored": ".codex/", "pattern": "/.code[x]/"}[
                        source
                    ]
                    ignore.write_bytes((rule + "\n*.log\n# user rules\n").encode())
                elif source == "info":
                    (project / ".git/info/exclude").write_text("/.codex/\n*.log\n")
                else:
                    global_ignore = project.parent / "global-ignore"
                    global_ignore.write_text(".codex/\n")
                    subprocess.run(
                        [
                            "git",
                            "config",
                            "--file",
                            str(global_config),
                            "core.excludesFile",
                            str(global_ignore),
                        ],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", str(project), "config", "--unset", "core.excludesFile"],
                        check=True,
                    )
                before = ignore.read_bytes() if ignore.exists() else None
                with mock.patch.dict(
                    devcrew.os.environ,
                    {"GIT_CONFIG_GLOBAL": str(global_config), "GIT_CONFIG_NOSYSTEM": "1"},
                ):
                    initialized = self.run_cli("init", "--project", project)
                    self.assertEqual(
                        initialized.returncode, 0, initialized.stdout + initialized.stderr
                    )
                    self.assertEqual(self.status(project)["status"], "SYNC_OK")
                    for command in ("sync", "init", "migrate-state", "sync"):
                        result = self.run_cli(command, "--project", project)
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(ignore.read_bytes() if ignore.exists() else None, before)
                    ignored = subprocess.run(
                        ["git", "-C", str(project), "check-ignore", "-q", "--", PROJECT_STATE]
                    )
                    self.assertEqual(ignored.returncode, 0)

    def test_negated_runtime_ignore_is_repaired_once(self):
        for source in ("root", "global"):
            with self.subTest(source=source):
                project, _ = self.init_project()
                if source == "global":
                    global_ignore = project.parent / "ignore"
                    global_ignore.write_text(".codex/\n")
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            str(project),
                            "config",
                            "core.excludesFile",
                            str(global_ignore),
                        ],
                        check=True,
                    )
                ignore = project / ".gitignore"
                original = ("/.codex/\n" if source == "root" else "") + "!/.codex/\n*.log\n"
                ignore.write_text(original)
                self.assertEqual(self.status(project)["status"], "SYNC_REQUIRED")
                result = self.run_cli("sync", "--project", project)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(ignore.read_text(), original + "/.codex/\n")
                self.assertEqual(self.status(project)["status"], "SYNC_OK")
                repeated = self.run_cli("sync", "--project", project)
                self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                self.assertFalse(json.loads(repeated.stdout)["changed"])
                self.assertEqual(ignore.read_text(), original + "/.codex/\n")

    def test_runtime_ignore_git_failure_does_not_plan_a_repair(self):
        project = self.project()
        ignore = project / ".gitignore"
        before = b"/.codex/\n*.log\n"
        ignore.write_bytes(before)
        failure = subprocess.CompletedProcess([], 128, "", "cannot read Git configuration")
        with mock.patch.object(devcrew, "_git", return_value=failure):
            with self.assertRaisesRegex(devcrew.ContractError, "cannot read Git configuration"):
                devcrew._runtime_ignore_plan(project)
        self.assertEqual(ignore.read_bytes(), before)

    def test_sync_does_not_require_git_author_identity(self):
        project = self.legacy_project()
        for key in ("user.name", "user.email"):
            subprocess.run(["git", "-C", str(project), "config", "--unset", key], check=True)
        with mock.patch.dict(
            devcrew.os.environ, {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
        ):
            result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIsNone(json.loads(result.stdout)["sync_commit"])

    def test_reinitializing_legacy_project_routes_through_local_sync(self):
        project = self.legacy_project()
        result = self.run_cli("init", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "RUNTIME_RESTART_RECOMMENDED")
        self.assertTrue((project / ".codex/agents/decision-agent.toml").is_file())

    def test_unmodified_managed_role_updates_from_previous_template(self):
        project, _ = self.init_project()
        self.commit_project_setup(project)
        role_path = project / ".codex/agents/executor-agent.toml"
        old_template = role_path.read_bytes() + b"\n# previous managed template\n"
        role_path.write_bytes(old_template)
        state = self.state(project)
        old_hash = hashlib.sha256(old_template).hexdigest()
        state["project_schema_version"] = 0
        state["managed_roles"]["executor-agent"] = {
            "template_sha256": old_hash,
            "installed_sha256": old_hash,
            "locally_modified": False,
        }
        self.write_state(project, state)
        self.commit_project_setup(project)

        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "RUNTIME_RESTART_RECOMMENDED")
        self.assertEqual(role_path.read_bytes(), (ROLE_ROOT / "executor-agent.toml").read_bytes())

    def test_locally_modified_managed_role_is_not_silently_overwritten(self):
        project, _ = self.init_project()
        self.commit_project_setup(project)
        role_path = project / ".codex/agents/executor-agent.toml"
        previous_template = role_path.read_bytes() + b"\n# old template\n"
        role_path.write_bytes(previous_template)
        state = self.state(project)
        previous_hash = hashlib.sha256(previous_template).hexdigest()
        state["project_schema_version"] = 0
        state["managed_roles"]["executor-agent"] = {
            "template_sha256": previous_hash,
            "installed_sha256": previous_hash,
            "locally_modified": False,
        }
        self.write_state(project, state)
        self.commit_project_setup(project)
        customized = previous_template + b"\n# user customization\n"
        role_path.write_bytes(customized)

        status = self.status(project)
        self.assertEqual(status["status"], "SYNC_CONFLICT")
        sync = self.run_cli("sync", "--project", project)
        self.assertNotEqual(sync.returncode, 0)
        self.assertEqual(role_path.read_bytes(), customized)

    def test_unrelated_custom_role_is_preserved(self):
        project = self.legacy_project()
        custom = project / ".codex/agents/my-project-agent.toml"
        custom.write_text('name = "my-project-agent"\n')
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(custom.read_text(), 'name = "my-project-agent"\n')

    def test_unrelated_dirty_source_is_neither_staged_nor_committed(self):
        project = self.legacy_project()
        head = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"])
        source = project / "source.py"
        source.write_text("user change\n")
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"]), head
        )
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "diff", "--cached", "--name-only"]),
            b"",
        )
        self.assertEqual(source.read_text(), "user change\n")
        self.assertIn(
            "source.py",
            subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(project),
                    "status",
                    "--short",
                ],
                text=True,
            ),
        )

    def test_preexisting_staged_user_change_is_preserved_by_local_sync(self):
        project = self.legacy_project()
        source = project / "source.py"
        source.write_text("staged user change\n")
        subprocess.run(["git", "-C", str(project), "add", "--", "source.py"], check=True)
        source.write_text("later unstaged user change\n")
        head = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
        ).strip()

        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "RUNTIME_RESTART_RECOMMENDED")
        self.assertTrue((project / ".codex/agents/decision-agent.toml").exists())
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
            ).strip(),
            head,
        )
        self.assertIn(
            "source.py",
            subprocess.check_output(
                ["git", "-C", str(project), "diff", "--cached", "--name-only"], text=True
            ),
        )
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "show", ":source.py"], text=True),
            "staged user change\n",
        )
        self.assertEqual(source.read_text(), "later unstaged user change\n")

    def test_unsafe_git_operation_state_conflicts_before_any_write(self):
        project = self.legacy_project()
        git_dir = pathlib.Path(
            subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "--git-dir"], text=True
            ).strip()
        )
        if not git_dir.is_absolute():
            git_dir = project / git_dir
        (git_dir / "MERGE_HEAD").write_text("simulated merge\n")
        before = (project / ".codex/agents/executor-agent.toml").read_bytes()
        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "SYNC_CONFLICT")
        self.assertEqual((project / ".codex/agents/executor-agent.toml").read_bytes(), before)
        self.assertFalse((project / ".codex/agents/decision-agent.toml").exists())

    def test_staged_managed_state_conflicts_even_when_missing_from_worktree(self):
        project, _ = self.init_project()
        self.commit_project_setup(project, tracked_runtime=True)
        (project / ".codex/agents/decision-agent.toml").unlink()
        state_path = project / PROJECT_STATE
        state = self.state(project)
        state["plugin_version"] = "staged-user-edit"
        self.write_state(project, state)
        subprocess.run(["git", "-C", str(project), "add", "-f", "--", PROJECT_STATE], check=True)
        state_path.unlink()
        result = self.run_cli("sync-status", "--project", project)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "SYNC_CONFLICT")
        self.assertTrue(
            any("managed path has existing Git changes" in item for item in payload["conflicts"])
        )

    def test_unstaged_managed_state_conflicts_before_sync(self):
        project, _ = self.init_project()
        self.commit_project_setup(project, tracked_runtime=True)
        (project / ".codex/agents/decision-agent.toml").unlink()
        state_path = project / PROJECT_STATE
        state = self.state(project)
        state["plugin_version"] = "unstaged-user-edit"
        self.write_state(project, state)
        result = self.run_cli("sync-status", "--project", project)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "SYNC_CONFLICT")
        self.assertTrue(
            any("managed path has existing Git changes" in item for item in payload["conflicts"])
        )
        self.assertIn(
            PROJECT_STATE,
            subprocess.check_output(["git", "-C", str(project), "diff", "--name-only"], text=True),
        )

    def test_sync_does_not_invoke_commit_hooks(self):
        project = self.legacy_project()
        hooks = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "--git-path", "hooks"], text=True
        ).strip()
        hooks_path = pathlib.Path(hooks)
        if not hooks_path.is_absolute():
            hooks_path = project / hooks_path
        hook = hooks_path / "post-commit"
        hook.write_text("#!/bin/sh\ntouch post-commit-ran\nexit 1\n")
        hook.chmod(0o755)
        old_head = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
        ).strip()

        result = self.run_cli("sync", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((project / "post-commit-ran").exists())
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
            ).strip(),
            old_head,
        )
        self.assertTrue((project / ".codex/agents/decision-agent.toml").is_file())
        self.assertEqual(self.status(project)["status"], "SYNC_OK")

    def test_sync_second_run_is_idempotent_and_new_task_preflight_is_ok(self):
        project = self.legacy_project()
        first = self.run_cli("sync", "--project", project)
        self.assertEqual(first.returncode, 0, first.stderr)
        head = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
        ).strip()
        second = self.run_cli("sync", "--project", project)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(json.loads(second.stdout)["status"], "SYNC_OK")
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
            ).strip(),
            head,
        )
        self.assertEqual(self.status(project)["status"], "SYNC_OK")

    def test_non_git_project_syncs_without_commit(self):
        project = self.project(git=False)
        result = self.run_cli("init", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        state = self.state(project)
        state["project_schema_version"] = 0
        (project / ".codex/agents/decision-agent.toml").unlink()
        self.write_state(project, state)
        synced = self.run_cli("sync", "--project", project)
        self.assertEqual(synced.returncode, 0, synced.stderr)
        payload = json.loads(synced.stdout)
        self.assertEqual(payload["status"], "RUNTIME_RESTART_RECOMMENDED")
        self.assertIsNone(payload["sync_commit"])

    def test_sync_command_does_not_stage_with_wildcards(self):
        source = (ROOT / "scripts/devcrew.py").read_text()
        self.assertNotIn('"add", "."', source)
        self.assertNotIn('"add", "-A"', source)

    def test_main_workflows_reference_the_shared_preflight(self):
        for skill in (
            "devcrew",
            "devcrew-task",
            "devcrew-autopilot",
            "devcrew-review",
            "devcrew-diagnose",
            "devcrew-architecture",
            "devcrew-handoff",
        ):
            text = (ROOT / "skills" / skill / "SKILL.md").read_text()
            with self.subTest(skill=skill):
                self.assertIn("project-sync-preflight.md", text)


if __name__ == "__main__":
    unittest.main()
