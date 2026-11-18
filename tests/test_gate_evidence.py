import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

import test_edge_cases


class GateEvidence(unittest.TestCase):
    run_cli = test_edge_cases.EdgeCases.run_cli
    project = test_edge_cases.EdgeCases.project
    initialized = test_edge_cases.EdgeCases.initialized

    def test_run_check_preserves_uncommitted_index_bytes(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        (project / "tracked.txt").write_text("staged content\n")
        subprocess.run(["git", "-C", str(project), "add", "tracked.txt"], check=True)
        index = project / ".git/index"
        before = index.read_bytes()
        head = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"])
        refs = subprocess.check_output(["git", "-C", str(project), "show-ref"])
        result = self.run_cli(
            "run-check",
            "--project",
            project,
            "--name",
            "index-preservation",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(index.read_bytes(), before)
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"]), head
        )
        self.assertEqual(subprocess.check_output(["git", "-C", str(project), "show-ref"]), refs)

    def test_free_check_task_labels_preserve_git_and_non_git_support(self):
        with tempfile.TemporaryDirectory() as directory:
            for git in (False, True):
                project = pathlib.Path(directory) / ("git" if git else "plain")
                project.mkdir()
                if git:
                    subprocess.run(
                        ["git", "-C", str(project), "init", "-q"], check=True, capture_output=True
                    )
                for task_id in ("T01", "free check label"):
                    with self.subTest(git=git, task_id=task_id):
                        result = self.run_cli(
                            "run-check",
                            "--project",
                            project,
                            "--task-id",
                            task_id,
                            "--name",
                            "free-check",
                            "--",
                            sys.executable,
                            "-c",
                            "print('ok')",
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        record = json.loads(pathlib.Path(result.stdout.splitlines()[0]).read_text())
                        self.assertEqual(record["task_id"], task_id)
                        self.assertEqual(record["status"], "passed")
            (project / ".git/config").write_text("[invalid configuration\n")
            before = sorted((project / ".codex/checks").iterdir())
            result = self.run_cli(
                "run-check",
                "--project",
                project,
                "--task-id",
                "T01",
                "--name",
                "corrupt-git",
                "--",
                sys.executable,
                "-c",
                "from pathlib import Path; Path('must-not-run').touch()",
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((project / "must-not-run").exists())
            self.assertEqual(sorted((project / ".codex/checks").iterdir()), before)

    def wave_plan(self, project):
        plan = project / ".codex/checks/plan.json"
        plan.parent.mkdir(parents=True, exist_ok=True)
        if not plan.exists():
            plan.write_text(
                json.dumps(
                    {
                        "waves": [
                            {
                                "id": str(i),
                                "slices": [{"id": str(i), "files": ["tracked.txt"]}],
                                "verification": ["project smoke"],
                                "verification_bindings": [
                                    {
                                        "index": 0,
                                        "verification": "project smoke",
                                        "argv": [sys.executable, "-c", "print('ok')"],
                                    }
                                ],
                            }
                            for i in range(2)
                        ]
                    }
                )
            )
        return plan

    def approve_wave(self, project, execution_root=None, task_id=None):
        plan = self.wave_plan(project)
        empty = plan.parent / "empty.json"
        empty.write_text(json.dumps({"waves": []}))
        args = [
            "wave-gate",
            "--project",
            project,
            "--plan",
            plan,
            "--results",
            empty,
            "--wave",
            "0",
        ]
        if execution_root is not None:
            args += ["--execution-root", execution_root]
        if task_id is not None:
            args += ["--task-id", task_id]
        return self.run_cli(*args)

    def record(self, project, code="print('ok')", **options):
        if not (project / ".codex/checks/plan.json").exists():
            approved = self.approve_wave(
                project, options.get("execution_root"), options.get("task_id")
            )
            self.assertEqual(approved.returncode, 0, approved.stderr)
        return test_edge_cases.EdgeCases.record(self, project, code, **options)

    def snapshot(self, project):
        result = self.run_cli("capture-state", "--project", project)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def review(self, project, state):
        report = project / ".codex/checks/review.json"
        report.write_text(
            json.dumps(
                {
                    "state": state,
                    "expected_lenses": ["correctness"],
                    "lenses": [{"id": "correctness", "status": "complete", "findings": []}],
                    "approved": True,
                }
            )
        )
        return self.run_cli("review-gate", "--project", project, "--report", report)

    def wave(self, project, records, next_wave=None, execution_root=None, task_id=None):
        checks = project / ".codex/checks"
        plan = self.wave_plan(project)
        results = checks / "results.json"
        self.approve_wave(project, execution_root, task_id)
        results.write_text(
            json.dumps(
                {
                    "waves": [
                        {
                            "id": str(i),
                            "checks": paths,
                            "slices": [{"id": str(i), "done": True, "problems": []}],
                        }
                        for i, paths in enumerate(records)
                    ]
                }
            )
        )
        args = ["wave-gate", "--project", str(project)]
        if execution_root is not None:
            args += ["--execution-root", str(execution_root)]
        if task_id is not None:
            args += ["--task-id", task_id]
        args += [
            "--plan",
            str(plan),
            "--results",
            str(results),
            "--wave",
            str(len(records) if next_wave is None else next_wave),
        ]
        return self.run_cli(*args)

    def test_optional_evidence_and_parent_review_need_no_project_setup(self):
        # A direct task may retain evidence without installing roles or task state.
        td, project = self.project()
        self.addCleanup(td.cleanup)
        result, record, payload = self.record(project)
        self.assertEqual(result.returncode, 0, result.stderr)
        log = record.parent / payload["log"]
        self.assertEqual(log.read_text().strip(), "ok")
        verified = self.run_cli("verify-check", "--record", record)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        # Coverage is a question the parent can answer, not a roster of agents.
        reviewed = self.review(project, self.snapshot(project))
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        for relative in ("docs", "memory", ".codex/agents", ".codex/devcrew.json"):
            self.assertFalse((project / relative).exists(), relative)
        (project / "tracked.txt").write_text("later edit\n")
        self.assertNotEqual(self.run_cli("verify-check", "--record", record).returncode, 0)

    def test_split_root_check_stores_host_evidence_for_task_execution(self):
        td, host = self.project()
        self.addCleanup(td.cleanup)
        _, task = self.project()
        self.addCleanup(_.cleanup)
        (task / "task-only.txt").write_text("execution state\n")
        task_id = "task-A"
        result = self.run_cli(
            "run-check",
            "--project",
            host,
            "--execution-root",
            task,
            "--task-id",
            task_id,
            "--name",
            "split",
            "--",
            "python3",
            "-c",
            "import pathlib; print(pathlib.Path.cwd())",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        record = pathlib.Path(result.stdout.strip().splitlines()[0])
        payload = json.loads(record.read_text())
        self.assertEqual(record.parent, (host / ".codex/checks").resolve())
        self.assertEqual(payload["host_project_root"], str(host.resolve()))
        self.assertEqual(payload["execution_root"], str(task.resolve()))
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["execution_head_sha"], payload["git_head"])
        self.assertEqual(
            payload["execution_head_sha"],
            subprocess.run(
                ["git", "-C", str(task), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip(),
        )
        self.assertNotEqual(
            payload["worktree_fingerprint_after"],
            json.loads(self.run_cli("capture-state", "--project", host).stdout)[
                "worktree_fingerprint"
            ],
        )
        self.assertIn(str(task.resolve()), (record.parent / payload["log"]).read_text())
        self.assertNotEqual(payload["cwd"], str(task.resolve()))
        for options in ([], ["--execution-root", str(task), "--task-id", task_id]):
            verified = self.run_cli("verify-check", "--record", record, *options)
            self.assertEqual(verified.returncode, 0, verified.stderr)
        wrong_root = self.run_cli(
            "verify-check", "--record", record, "--execution-root", host, "--task-id", task_id
        )
        wrong_task = self.run_cli(
            "verify-check", "--record", record, "--execution-root", task, "--task-id", "task-B"
        )
        self.assertNotEqual(wrong_root.returncode, 0)
        self.assertNotEqual(wrong_task.returncode, 0)
        reused = self.run_cli(
            "reuse-check",
            "--record",
            record,
            "--execution-root",
            task,
            "--task-id",
            task_id,
            "--",
            "python3",
            "-c",
            "import pathlib; print(pathlib.Path.cwd())",
        )
        self.assertEqual(reused.returncode, 0, reused.stderr)
        cross_task = self.run_cli(
            "reuse-check",
            "--record",
            record,
            "--execution-root",
            task,
            "--task-id",
            "task-B",
            "--",
            "python3",
            "-c",
            "import pathlib; print(pathlib.Path.cwd())",
        )
        self.assertNotEqual(cross_task.returncode, 0)

    def test_split_root_reuse_includes_tracked_task_checks_files(self):
        host_td, host = self.project()
        self.addCleanup(host_td.cleanup)
        task_td, task = self.project()
        self.addCleanup(task_td.cleanup)
        task_checks = task / ".codex/checks"
        task_checks.mkdir(parents=True)
        (task_checks / "tracked.txt").write_text("task-owned check data\n")
        subprocess.run(
            ["git", "-C", str(task), "add", "-f", ".codex/checks/tracked.txt"], check=True
        )
        result, record, payload = self.record(
            host, code='print("ok")', execution_root=task, task_id="task-A"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        verified = self.run_cli(
            "verify-check", "--record", record, "--execution-root", task, "--task-id", "task-A"
        )
        self.assertEqual(verified.returncode, 0, verified.stderr)
        reused = self.run_cli(
            "reuse-check",
            "--record",
            record,
            "--execution-root",
            task,
            "--task-id",
            "task-A",
            "--",
            *payload["command"],
        )
        self.assertEqual(reused.returncode, 0, reused.stderr)
        (task_checks / "tracked.txt").write_text("changed task check data\n")
        stale = self.run_cli(
            "verify-check", "--record", record, "--execution-root", task, "--task-id", "task-A"
        )
        self.assertNotEqual(stale.returncode, 0)

    def test_split_root_review_stales_when_task_changes_but_host_does_not(self):
        td, host = self.project()
        self.addCleanup(td.cleanup)
        self.assertEqual(self.run_cli("init", "--project", host).returncode, 0)
        task_td, task = self.project()
        self.addCleanup(task_td.cleanup)
        (task / "tracked.txt").write_text("before\n")
        captured = self.run_cli("capture-state", "--project", host, "--execution-root", task)
        self.assertEqual(captured.returncode, 0, captured.stderr)
        state = json.loads(captured.stdout)
        self.assertEqual(state["cwd"], str(task.resolve()))
        report = host / ".codex/checks/review.json"
        report.write_text(
            json.dumps(
                {
                    "state": state,
                    "expected_lenses": ["correctness"],
                    "lenses": [{"id": "correctness", "status": "complete", "findings": []}],
                    "approved": True,
                }
            )
        )
        args = ("review-gate", "--project", host, "--execution-root", task, "--report", report)
        self.assertEqual(self.run_cli(*args).returncode, 0)
        (task / "tracked.txt").write_text("after\n")
        stale = self.run_cli(*args)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale", stale.stderr)
        self.assertEqual(
            self.snapshot(host)["worktree_fingerprint"],
            json.loads(self.run_cli("capture-state", "--project", host).stdout)[
                "worktree_fingerprint"
            ],
        )

    def test_split_wave_receipts_are_bound_to_execution_root_and_task_id(self):
        td, host = self.project()
        self.addCleanup(td.cleanup)
        task_td, task_a = self.project()
        self.addCleanup(task_td.cleanup)
        other_td, task_b = self.project()
        self.addCleanup(other_td.cleanup)
        for task in (task_a, task_b):
            (task / "tracked.txt").write_text("same source\n")
        _, record, _ = self.record(host, execution_root=task_a, task_id="task-A")
        self.assertNotEqual(self.wave(host, [[str(record)]]).returncode, 0)
        self.assertEqual(
            self.wave(host, [[str(record)]], execution_root=task_a, task_id="task-A").returncode, 0
        )
        self.assertNotEqual(
            self.wave(host, [[str(record)]], execution_root=task_b, task_id="task-A").returncode, 0
        )
        self.assertNotEqual(
            self.wave(host, [[str(record)]], execution_root=task_a, task_id="task-B").returncode, 0
        )
        _, other_task_record, _ = self.record(host, execution_root=task_a, task_id="task-B")
        mixed = self.wave(host, [[str(record), str(other_task_record)]], execution_root=task_a)
        self.assertNotEqual(mixed.returncode, 0)

    def test_split_fingerprint_includes_task_checks_path_and_scope_uses_host_evidence(self):
        td, host = self.project()
        self.addCleanup(td.cleanup)
        task_td, task = self.project()
        self.addCleanup(task_td.cleanup)
        checks = task / ".codex/checks"
        checks.mkdir(parents=True)
        (checks / "tracked.txt").write_text("task-owned path\n")
        subprocess.run(
            ["git", "-C", str(task), "add", "-f", ".codex/checks/tracked.txt"], check=True
        )
        self.assertEqual(
            self.run_cli(
                "init", "--project", host, "--checks-dir", ".codex/checks/tracked.txt"
            ).returncode,
            0,
        )
        before = self.run_cli(
            "capture-state", "--project", host, "--execution-root", task, "--include-files"
        )
        self.assertEqual(before.returncode, 0, before.stderr)
        snapshot_path = host / ".codex/checks/tracked.txt/before.json"
        snapshot_path.write_text(before.stdout)
        owned = host / ".codex/checks/tracked.txt/owned.json"
        owned.write_text(json.dumps([".codex/checks/tracked.txt"]))
        (checks / "tracked.txt").write_text("updated\n")
        result = self.run_cli(
            "scope-check",
            "--project",
            host,
            "--execution-root",
            task,
            "--before",
            snapshot_path,
            "--files",
            owned,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["changed"], [".codex/checks/tracked.txt"])

    def test_review_requires_snapshot_and_rejects_dirty_tree_and_new_head(self):
        project = self.initialized()
        state = self.snapshot(project)
        self.assertEqual(self.review(project, state).returncode, 0)
        (project / "tracked.txt").write_text("changed\n")
        stale = self.review(project, state)
        self.assertNotEqual(stale.returncode, 0)
        self.assertIn("stale", stale.stderr)
        (project / "tracked.txt").write_text("initial\n")
        self.assertEqual(self.review(project, state).returncode, 0)
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
                "--allow-empty",
                "-qm",
                "new head",
            ],
            check=True,
        )
        self.assertNotEqual(self.review(project, state).returncode, 0)
        fresh = self.snapshot(project)
        self.assertEqual(self.review(project, fresh).returncode, 0)
        for bad in (
            None,
            {},
            {**fresh, "schema_version": True},
            {**fresh, "worktree_fingerprint": {}},
            {**fresh, "cwd": "missing"},
        ):
            with self.subTest(state=bad):
                self.assertNotEqual(self.review(project, bad).returncode, 0)

    def test_evidence_from_another_project_is_rejected(self):
        project = self.initialized()
        other = self.initialized()
        self.assertNotEqual(self.review(project, self.snapshot(other)).returncode, 0)
        _, record, _ = self.record(other)
        result = self.wave(project, [[str(record)]])
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("project", result.stderr)

    def test_wave_requires_successful_unchanged_integrated_tree(self):
        project = self.initialized()
        self.assertNotEqual(self.wave(project, [[]]).returncode, 0)
        _, record, _ = self.record(project)
        relative = record.relative_to(project.resolve()).as_posix()
        self.assertEqual(self.wave(project, [[relative]]).returncode, 0)
        self.assertNotEqual(self.wave(project, [[relative, str(record)]]).returncode, 0)
        (project / "tracked.txt").write_text("changed\n")
        self.assertNotEqual(self.wave(project, [[relative]]).returncode, 0)
        _, failed, _ = self.record(project, code="raise SystemExit(1)")
        self.assertNotEqual(self.wave(project, [[str(failed)]]).returncode, 0)
        _, mutating, _ = self.record(
            project, code="from pathlib import Path; Path('tracked.txt').write_text('mutated')"
        )
        rejected = self.wave(project, [[str(mutating)]])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("changed", rejected.stderr)
        _, fresh, _ = self.record(project)
        self.assertEqual(self.wave(project, [[str(fresh)]]).returncode, 0)

    def test_past_waves_keep_integrity_without_requiring_current_fingerprint(self):
        project = self.initialized()
        _, first, payload = self.record(project)
        self.assertEqual(self.wave(project, [[str(first)]]).returncode, 0)
        (project / "tracked.txt").write_text("second wave\n")
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
                "wave two",
            ],
            check=True,
        )
        _, second, _ = self.record(project)
        self.assertEqual(self.wave(project, [[str(first)], [str(second)]]).returncode, 0)
        self.assertNotEqual(self.wave(project, [[str(first), str(second)]]).returncode, 0)
        log = first.parent / payload["log"]
        log.write_text("tampered")
        self.assertNotEqual(self.wave(project, [[str(first)], [str(second)]]).returncode, 0)

    def test_capture_is_read_only_and_supports_custom_paths_and_non_git(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        self.assertEqual(
            self.run_cli("init", "--project", project, "--checks-dir", "evidence").returncode, 0
        )
        before = {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        state = self.snapshot(project)
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in project.rglob("*") if p.is_file()}
        )
        (project / "evidence/note.txt").write_text("excluded")
        self.assertEqual(state, self.snapshot(project))
        plain = pathlib.Path(td.name) / "plain"
        plain.mkdir()
        (plain / "source.txt").write_text("non-git")
        captured = self.snapshot(plain)
        self.assertIsNone(captured["git_head"])
        self.assertFalse(captured["worktree_fingerprint"]["git"])
        (plain / ".codex/checks").mkdir(parents=True)
        self.assertEqual(self.review(plain, captured).returncode, 0)

    def test_old_records_remain_verifiable_but_cannot_certify_waves(self):
        project = self.initialized()
        _, record, payload = self.record(project)
        del payload["git_head_before"]
        record.write_text(json.dumps(payload))
        self.assertEqual(self.run_cli("verify-check", "--record", record).returncode, 0)
        rejected = self.wave(project, [[str(record)]])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("git_head_before", rejected.stderr)

    def test_check_changing_only_head_does_not_certify_a_wave(self):
        project = self.initialized()
        code = (
            "import subprocess; subprocess.run(['git', '-c', 'user.name=Test', "
            "'-c', 'user.email=test@example.com', 'commit', '--allow-empty', "
            "'-qm', 'changed during check'], check=True)"
        )
        result, record, payload = self.record(project, code=code)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            payload["worktree_fingerprint_before"], payload["worktree_fingerprint_after"]
        )
        self.assertNotEqual(payload["git_head_before"], payload["git_head"])
        rejected = self.wave(project, [[str(record)]])
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("changed", rejected.stderr)

    def test_init_adds_template_without_overwriting_existing_task_state(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        args = ("init", "--project", project, "--tasks-dir", "work/tasks")
        self.assertEqual(self.run_cli(*args).returncode, 0)
        template = project / "work/tasks/card.template.md"
        self.assertTrue(template.is_file())
        template.write_text("custom template\n")
        card = project / "work/tasks/current.md"
        card.write_text("existing decisions and unfinished work\n")
        self.assertEqual(self.run_cli(*args).returncode, 0)
        self.assertEqual(template.read_text(), "custom template\n")
        self.assertEqual(card.read_text(), "existing decisions and unfinished work\n")
        template.unlink()
        self.assertEqual(self.run_cli(*args).returncode, 0)
        self.assertTrue(template.is_file())
        self.assertEqual(card.read_text(), "existing decisions and unfinished work\n")


if __name__ == "__main__":
    unittest.main()
