import json
import os
import pathlib
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
CLI = ROOT / "scripts" / "devcrew.py"


class DevCrewCLITest(unittest.TestCase):
    def run_cli(self, *args, cwd=None):
        return subprocess.run(
            [sys.executable, str(CLI), *map(str, args)],
            cwd=cwd or ROOT,
            text=True,
            capture_output=True,
        )

    def project(self):
        td = tempfile.TemporaryDirectory()
        path = pathlib.Path(td.name) / "project"
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True)
        subprocess.run(
            ["git", "-C", str(path), "config", "core.excludesFile", os.devnull], check=True
        )
        (path / "tracked.txt").write_text("initial\n")
        subprocess.run(["git", "-C", str(path), "add", "--", "tracked.txt"], check=True)
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
                "init",
            ],
            check=True,
        )
        return td, path

    def test_init_default_task_artifacts_live_under_codex(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result = self.run_cli("init", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        config = json.loads((project / ".codex/devcrew.json").read_text())
        self.assertEqual(payload["state_paths"]["tasks"], ".codex/tasks")
        self.assertEqual(config["paths"]["tasks"], ".codex/tasks")
        self.assertTrue((project / ".codex/tasks/README.md").is_file())
        self.assertTrue((project / ".codex/tasks/card.template.md").is_file())
        self.assertFalse((project / "docs/tasks").exists())
        doctor = self.run_cli("doctor", "--project", project)
        self.assertEqual(doctor.returncode, 0, doctor.stdout + doctor.stderr)
        self.assertTrue(json.loads(doctor.stdout)["ok"])

    def test_init_selects_template_language_and_preserves_existing_documents(self):
        assets = ROOT / "skills/devcrew-setup/assets/project"
        files = {
            "docs/context.md": "context.md",
            ".codex/tasks/card.template.md": "task-ledger.template.md",
            ".codex/tasks/README.md": "tasks/README.md",
            ".codex/sessions/README.md": "sessions/README.md",
            "memory/README.md": "memory/README.md",
        }
        for language in ("en", "ru"):
            with self.subTest(language=language), tempfile.TemporaryDirectory() as temp:
                project = pathlib.Path(temp)
                flags = ("--language", language) if language == "ru" else ()
                first = self.run_cli("init", "--project", project, *flags)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                source = assets / "ru" if language == "ru" else assets
                for destination, template in files.items():
                    self.assertEqual(
                        (project / destination).read_bytes(), (source / template).read_bytes()
                    )
                repeated = self.run_cli(
                    "init", "--project", project, "--language", "en" if language == "ru" else "ru"
                )
                self.assertEqual(repeated.returncode, 0, repeated.stdout + repeated.stderr)
                for destination, template in files.items():
                    self.assertEqual(
                        (project / destination).read_bytes(), (source / template).read_bytes()
                    )

    def test_init_sessions_are_ignored_and_durable_knowledge_is_preserved(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        for relative, content in {
            "docs/context.md": "confirmed project context\n",
            "docs/api.md": "public API documentation\n",
            "memory/MEMORY.md": "reusable lesson\n",
            "rules/index.md": "project invariants\n",
        }.items():
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        before = {
            path: path.read_bytes()
            for directory in ("docs", "memory", "rules")
            for path in (project / directory).glob("*.md")
        }
        ignore = project / ".gitignore"
        original = "build/\n.codex/\n!.codex/"
        ignore.write_text(original)
        result = self.run_cli("init", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["state_paths"]["sessions"], ".codex/sessions")
        self.assertTrue((project / ".codex/sessions/README.md").is_file())
        self.assertFalse((project / "docs/sessions").exists())
        self.assertTrue(ignore.read_text().startswith(original + "\n"))
        for relative in (".codex/tasks/T01/plan.md", ".codex/sessions/snapshot.md"):
            path = project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("runtime context\n")
            ignored = subprocess.run(
                ["git", "-C", str(project), "check-ignore", "--", relative],
                text=True,
                capture_output=True,
            )
            self.assertEqual(ignored.returncode, 0, ignored.stderr)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
            visible = subprocess.run(
                ["git", "-C", str(project), "check-ignore", "--", str(path)],
                text=True,
                capture_output=True,
            )
            self.assertEqual(visible.returncode, 1, visible.stderr)
        saved_ignore = ignore.read_bytes()
        repeated = self.run_cli("init", "--project", project)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(ignore.read_bytes(), saved_ignore)

    def test_init_creates_gitignore_for_runtime_state(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result = self.run_cli("init", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((project / ".gitignore").read_text(), "/.codex/\n")

    def test_init_rejects_symlinked_gitignore_before_writing_state(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        outside = pathlib.Path(td.name) / "outside-ignore"
        outside.write_text("user ignore\n")
        (project / ".gitignore").symlink_to(outside)
        result = self.run_cli("init", "--project", project)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((project / ".codex").exists())
        self.assertFalse((project / "docs").exists())
        self.assertEqual(outside.read_text(), "user ignore\n")

    def test_migrate_runtime_state_preserves_data_and_custom_config(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        initialized = self.run_cli(
            "init",
            "--project",
            project,
            "--tasks-dir",
            "docs/tasks",
            "--sessions-dir",
            "docs/sessions",
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        config = project / ".codex/devcrew.json"
        state = json.loads(config.read_text())
        state["agent_context"] = {"enabled": True}
        config.write_text(json.dumps(state))
        card = project / "docs/tasks/T01/card.md"
        card.parent.mkdir()
        card.write_text("done: implementation; next: review\n")
        snapshot = project / "docs/sessions/snapshot.json"
        snapshot.write_bytes(b'{"done": ["implementation"]}\n')
        before = {
            path.relative_to(project): path.read_bytes()
            for path in project.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(project).parts
        }
        dry_run = self.run_cli("migrate-state", "--project", project, "--dry-run")
        self.assertEqual(dry_run.returncode, 0, dry_run.stderr)
        self.assertEqual(
            json.loads(dry_run.stdout)["moves"],
            {
                "tasks": {"from": "docs/tasks", "to": ".codex/tasks"},
                "sessions": {"from": "docs/sessions", "to": ".codex/sessions"},
            },
        )
        self.assertEqual(
            before,
            {
                path.relative_to(project): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(project).parts
            },
        )
        result = self.run_cli("migrate-state", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative, content in before.items():
            destination = str(relative)
            for source, target in (
                ("docs/tasks/", ".codex/tasks/"),
                ("docs/sessions/", ".codex/sessions/"),
            ):
                if destination.startswith(source):
                    destination = target + destination[len(source) :]
                    break
            if relative != pathlib.Path(".codex/devcrew.json"):
                self.assertEqual((project / destination).read_bytes(), content)
        self.assertFalse((project / "docs/tasks").exists())
        self.assertFalse((project / "docs/sessions").exists())
        updated = json.loads(config.read_text())
        self.assertEqual(updated["agent_context"], state["agent_context"])
        self.assertEqual(updated["paths"]["context"], "docs/context.md")
        self.assertEqual(updated["paths"]["memory"], "memory")
        self.assertEqual(updated["paths"]["sessions"], ".codex/sessions")
        self.assertEqual(updated["paths"]["tasks"], ".codex/tasks")
        repeated = self.run_cli("migrate-state", "--project", project)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertFalse(json.loads(repeated.stdout)["changed"])
        self.assertEqual(self.run_cli("doctor", "--project", project).returncode, 0)

    def test_migrate_runtime_state_rejects_collision_or_symlink_without_partial_moves(self):
        for conflict in ("collision", "symlink"):
            with self.subTest(conflict=conflict):
                td, project = self.project()
                self.addCleanup(td.cleanup)
                initialized = self.run_cli(
                    "init",
                    "--project",
                    project,
                    "--tasks-dir",
                    "docs/tasks",
                    "--sessions-dir",
                    "docs/sessions",
                )
                self.assertEqual(initialized.returncode, 0, initialized.stderr)
                config = project / ".codex/devcrew.json"
                before = config.read_bytes()
                if conflict == "collision":
                    (project / ".codex/sessions").mkdir()
                    (project / ".codex/sessions/existing.md").write_text("preserve\n")
                else:
                    (project / "docs/sessions/link").symlink_to(project / "tracked.txt")
                result = self.run_cli("migrate-state", "--project", project)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "migration destination already exists"
                    if conflict == "collision"
                    else "unsupported migration source",
                    result.stderr,
                )
                self.assertEqual(config.read_bytes(), before)
                self.assertTrue((project / "docs/tasks/card.template.md").is_file())
                self.assertTrue((project / "docs/sessions/README.md").is_file())
                self.assertFalse((project / ".codex/tasks").exists())

    def test_migrate_runtime_state_rolls_back_when_config_write_fails(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result = self.run_cli(
            "init",
            "--project",
            project,
            "--tasks-dir",
            "docs/tasks",
            "--sessions-dir",
            "docs/sessions",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        sys.path.insert(0, str(ROOT / "scripts"))
        self.addCleanup(sys.path.remove, str(ROOT / "scripts"))
        import devcrew

        before = {
            path.relative_to(project): path.read_bytes()
            for path in project.rglob("*")
            if path.is_file() and ".git" not in path.relative_to(project).parts
        }
        args = devcrew.build_parser().parse_args(["migrate-state", "--project", str(project)])
        with mock.patch.object(devcrew, "_json_dump", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                devcrew._migrate_state(args)
        self.assertEqual(
            before,
            {
                path.relative_to(project): path.read_bytes()
                for path in project.rglob("*")
                if path.is_file() and ".git" not in path.relative_to(project).parts
            },
        )
        self.assertFalse((project / ".codex/tasks").exists())
        self.assertFalse((project / ".codex/sessions").exists())

    def test_init_idempotent_preserves_user_file_and_conflict_preflight(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        first = self.run_cli("init", "--project", project)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertTrue((project / "docs/context.md").exists())
        self.assertTrue((project / "memory/README.md").exists())
        self.assertFalse((project / "docs/devcrew").exists())
        self.assertEqual(len(list((project / ".codex/agents").glob("*.toml"))), 12)
        expected_efforts = {
            "scout": "low",
            "doc-digest": "low",
            "executor-agent": "low",
            "decision-agent": "medium",
            "product-agent": "high",
            "test-runner": "low",
            "tester": "high",
            "architect-agent": "high",
            "api-analyst": "high",
            "claim-verifier": "high",
            "reviewer-agent": "low",
            "gitlab-commenter": "medium",
        }
        for name, expected in expected_efforts.items():
            with self.subTest(role=name):
                role = tomllib.loads((project / ".codex/agents" / (name + ".toml")).read_text())
                self.assertNotIn("model", role)
                self.assertEqual(role["model_reasoning_effort"], expected)
        context = project / "docs/context.md"
        context.write_text("user context\n")
        second = self.run_cli("init", "--project", project)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(context.read_text(), "user context\n")
        role = project / ".codex/agents/scout.toml"
        role.write_text(role.read_text() + "\n# user edit\n")
        before = {p: p.read_bytes() for p in (project / ".codex/agents").glob("*.toml")}
        conflict = self.run_cli("init", "--project", project)
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("scout.toml", conflict.stderr + conflict.stdout)
        self.assertEqual(
            before, {p: p.read_bytes() for p in (project / ".codex/agents").glob("*.toml")}
        )

    def test_init_rejects_symlinked_state_before_creating_other_files(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        outside = pathlib.Path(td.name) / "outside"
        outside.mkdir()
        (project / ".codex").symlink_to(outside, target_is_directory=True)
        result = self.run_cli("init", "--project", project)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((project / "docs").exists())
        self.assertFalse(any(outside.iterdir()))

    def test_doctor_reports_missing_invalid_and_allows_custom_role(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        self.assertEqual(self.run_cli("init", "--project", project).returncode, 0)
        role = project / ".codex/agents/scout.toml"
        role.write_text(role.read_text().replace('description = "', 'description = "custom '))
        custom = self.run_cli("doctor", "--project", project)
        self.assertEqual(custom.returncode, 0, custom.stderr)
        role.write_text("not = [valid")
        malformed = self.run_cli("doctor", "--project", project)
        self.assertNotEqual(malformed.returncode, 0)
        self.assertIn("invalid role TOML", malformed.stdout)
        role.unlink()
        missing = self.run_cli("doctor", "--project", project)
        self.assertNotEqual(missing.returncode, 0)
        report = json.loads(missing.stdout)
        self.assertFalse(report["ok"])
        self.assertTrue(any("scout.toml" in item for item in report["errors"]))

    def test_run_and_verify_check_observe_argv_output_and_tampering(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        self.assertEqual(self.run_cli("init", "--project", project).returncode, 0)
        result = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "argv",
            "--",
            sys.executable,
            "-c",
            "import sys; print(sys.argv[1]); print('err', file=sys.stderr)",
            "hello; echo BAD",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = pathlib.Path(result.stdout.strip().splitlines()[0])
        payload = json.loads(record.read_text())
        log = project / ".codex/checks" / payload["log"]
        self.assertIn("hello; echo BAD", log.read_text())
        self.assertIn("err", log.read_text())
        self.assertEqual(self.run_cli("verify-check", "--record", record).returncode, 0)
        (project / "tracked.txt").write_text("dirty\n")
        self.assertNotEqual(self.run_cli("verify-check", "--record", record).returncode, 0)
        (project / "tracked.txt").write_text("initial\n")
        log.write_text(log.read_text() + "tampered")
        self.assertNotEqual(self.run_cli("verify-check", "--record", record).returncode, 0)

    def test_verify_rejects_malformed_pass_record_and_head_staleness(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        self.assertEqual(self.run_cli("init", "--project", project).returncode, 0)
        result = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "schema",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        record = pathlib.Path(result.stdout.strip().splitlines()[0])
        payload = json.loads(record.read_text())
        payload["schema_version"] = True
        record.write_text(json.dumps(payload))
        self.assertNotEqual(self.run_cli("verify-check", "--record", record).returncode, 0)
        payload["schema_version"] = 1
        record.write_text(json.dumps(payload))
        (project / "new.txt").write_text("dirty")
        subprocess.run(["git", "-C", str(project), "add", "new.txt"], check=True)
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
                "change",
            ],
            check=True,
        )
        self.assertNotEqual(self.run_cli("verify-check", "--record", record).returncode, 0)

    def test_run_check_failure_timeout_and_missing_executable(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        self.assertEqual(self.run_cli("init", "--project", project).returncode, 0)
        fail = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "fail",
            "--",
            sys.executable,
            "-c",
            "raise SystemExit(3)",
        )
        self.assertNotEqual(fail.returncode, 0)
        timeout = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "timeout",
            "--timeout",
            "0.05",
            "--",
            sys.executable,
            "-c",
            "import time; print('timeout-marker', flush=True); time.sleep(1)",
        )
        self.assertNotEqual(timeout.returncode, 0)
        timeout_record = pathlib.Path(timeout.stdout.strip().splitlines()[0])
        timeout_payload = json.loads(timeout_record.read_text())
        timeout_log = project / ".codex/checks" / timeout_payload["log"]
        self.assertEqual(timeout_log.read_text().count("timeout-marker"), 1)
        missing = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "missing",
            "--",
            "definitely-no-such-executable",
        )
        self.assertNotEqual(missing.returncode, 0)

    def test_wave_review_and_flaky_gates(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        plan = project / "plan.json"
        plan.write_text(
            json.dumps(
                {
                    "waves": [
                        {"id": "one", "slices": [{"id": "a", "files": ["tracked.txt"]}]},
                        {"id": "two", "slices": [{"id": "b", "files": ["tracked.txt"]}]},
                    ]
                }
            )
        )
        planned = json.loads(plan.read_text())
        for wave in planned["waves"]:
            wave.update(
                verification=["smoke"],
                verification_bindings=[
                    {
                        "index": 0,
                        "verification": "smoke",
                        "argv": [sys.executable, "-c", "print('ok')"],
                    }
                ],
            )
        plan.write_text(json.dumps(planned))
        empty = project / "results.json"
        empty.write_text('{"waves": []}')
        self.assertEqual(
            self.run_cli(
                "wave-gate", "--project", project, "--plan", plan, "--results", empty, "--wave", "0"
            ).returncode,
            0,
        )
        blocked = self.run_cli(
            "wave-gate", "--project", project, "--plan", plan, "--results", empty, "--wave", "1"
        )
        self.assertNotEqual(blocked.returncode, 0)
        report = project / "review.json"
        report.write_text(
            json.dumps(
                {
                    "expected_lenses": ["a"],
                    "lenses": [
                        {
                            "id": "a",
                            "status": "complete",
                            "findings": [
                                {"severity": "P1", "verification": "unverified", "summary": "x"}
                            ],
                        }
                    ],
                    "approved": True,
                }
            )
        )
        self.assertNotEqual(
            self.run_cli("review-gate", "--project", project, "--report", report).returncode, 0
        )
        flaky = self.run_cli(
            "flaky-hunt",
            "--project",
            project,
            "--name",
            "f",
            "--max-runs",
            "2",
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "import sys; sys.exit(4)",
        )
        self.assertNotEqual(flaky.returncode, 0)

    def test_init_supports_discovered_project_layout(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        (project / "AGENTS.md").write_text("Use rules/ and durable state in memory/.\n")
        (project / "memory").mkdir()
        (project / "memory/MEMORY.md").write_text("existing project memory\n")
        result = self.run_cli(
            "init",
            "--project",
            project,
            "--context",
            "rules/devcrew-context.md",
            "--tasks-dir",
            "docs/tasks",
            "--sessions-dir",
            "docs/sessions",
            "--memory-dir",
            "memory",
            "--checks-dir",
            ".codex/checks",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["instruction_files"], ["AGENTS.md"])
        self.assertEqual(payload["state_paths"]["context"], "rules/devcrew-context.md")
        self.assertEqual(payload["state_paths"]["tasks"], "docs/tasks")
        self.assertEqual(
            json.loads((project / ".codex/devcrew.json").read_text())["paths"]["tasks"],
            "docs/tasks",
        )
        self.assertTrue((project / "docs/tasks/README.md").is_file())
        self.assertTrue((project / "docs/tasks/card.template.md").is_file())
        self.assertFalse((project / ".codex/tasks").exists())
        repeated = self.run_cli("init", "--project", project)
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout)["state_paths"]["tasks"], "docs/tasks")
        self.assertFalse((project / ".codex/tasks").exists())
        self.assertEqual((project / "memory/MEMORY.md").read_text(), "existing project memory\n")
        self.assertTrue((project / "rules/devcrew-context.md").is_file())
        self.assertFalse((project / "docs/devcrew").exists())
        self.assertEqual(self.run_cli("doctor", "--project", project).returncode, 0)
        check = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "custom-layout",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        record = pathlib.Path(check.stdout.splitlines()[0])
        self.assertEqual(record.parent.resolve(), (project / ".codex/checks").resolve())
        self.assertEqual(self.run_cli("verify-check", "--record", record).returncode, 0)

    def test_wave_alias_duplicate_and_review_missing_lens(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        plan = project / "bad-plan.json"
        plan.write_text(
            json.dumps(
                {
                    "waves": [
                        {
                            "id": "one",
                            "slices": [{"id": "a", "files": ["./tracked.txt", "tracked.txt"]}],
                        }
                    ]
                }
            )
        )
        results = project / "results.json"
        results.write_text('{"waves": []}')
        self.assertNotEqual(
            self.run_cli(
                "wave-gate",
                "--project",
                project,
                "--plan",
                plan,
                "--results",
                results,
                "--wave",
                "0",
            ).returncode,
            0,
        )
        report = project / "missing-review.json"
        report.write_text(
            json.dumps(
                {
                    "expected_lenses": ["a", "b"],
                    "lenses": [{"id": "a", "status": "complete", "findings": []}],
                    "approved": True,
                }
            )
        )
        self.assertNotEqual(
            self.run_cli("review-gate", "--project", project, "--report", report).returncode, 0
        )

    def test_flaky_cap_and_environment_error_are_distinct(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        passed = self.run_cli(
            "flaky-hunt",
            "--project",
            project,
            "--name",
            "cap",
            "--max-runs",
            "2",
            "--timeout",
            "2",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(passed.returncode, 0, passed.stderr)
        self.assertEqual(json.loads(passed.stdout)["runs"], 2)
        missing = self.run_cli(
            "flaky-hunt",
            "--project",
            project,
            "--name",
            "missing",
            "--max-runs",
            "2",
            "--timeout",
            "2",
            "--",
            "no-such-executable",
        )
        self.assertEqual(missing.returncode, 2)
        self.assertEqual(json.loads(missing.stdout)["status"], "environment_error")


if __name__ == "__main__":
    unittest.main()
