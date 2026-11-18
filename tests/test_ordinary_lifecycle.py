import hashlib
import json
import os
import pathlib
import subprocess
import sys
import threading
import unittest
from unittest import mock

import test_devcrew


class OrdinaryLifecycleTests(unittest.TestCase):
    run_cli = test_devcrew.DevCrewCLITest.run_cli

    def project(self):
        td, project = test_devcrew.DevCrewCLITest.project(self)
        # Ordinary lifecycle assumes setup has excluded its mutable runtime
        # state. Keep that fixture contract independent of global Git ignores.
        (project / ".gitignore").write_text(".codex/\n")
        return td, project

    def source_gap_fixture(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary
        import devcrew_phase

        git = lambda *args: subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
        with (project / ".gitignore").open("a", encoding="utf-8") as stream:
            stream.write(".codex/\nchat-context.json\n")
        git("add", ".gitignore")
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
                "ignore runtime state",
            ],
            check=True,
        )
        base = git("rev-parse", "HEAD")
        context_path = self.context(project)
        devcrew_ordinary.init(
            project, "T01", json.loads(context_path.read_text(encoding="utf-8")), base
        )
        created = self.run_cli(
            "task-checkout", "create", "--project", project, "--task-id", "T01", "--base-sha", base
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        resolved = self.run_cli(
            "task-checkout", "resolve", "--project", project, "--task-id", "T01"
        )
        self.assertEqual(resolved.returncode, 0, resolved.stderr)
        receipt = json.loads(resolved.stdout)
        return td, project, receipt, devcrew_ordinary, devcrew_phase, git

    def source_gap_contract(self, td, project):
        import devcrew_ordinary

        original = b"full exact attachment bytes\n"
        attachment = pathlib.Path(td.name) / "attachment.txt"
        attachment.write_bytes(original)
        handoff = {
            "source_spec": {
                "mode": "file",
                "source_type": "attachment",
                "path": str(attachment),
                "expected_sha256": hashlib.sha256(original).hexdigest(),
                "origin": "user-authorized local attachment",
            },
            "authorization": {
                "source_id": json.loads((project / ".codex/tasks/T01.ordinary.json").read_text())[
                    "source"
                ]["source_id"],
                "scope": "formal source-gap transition for task T01",
            },
            "host_observation": {
                "reader": "mcp__codex_app__read_thread",
                "thread_id": "thread-1",
                "turn_id": "turn-1",
                "message_sha256": json.loads(
                    (project / ".codex/tasks/T01.ordinary.json").read_text()
                )["source"]["message_sha256"],
                "status": "SOURCE_GAP",
                "format": "local attachment",
                "attachment_reference": str(attachment.resolve()),
                "full_source_readable": False,
                "active_agent_ids": [],
                "active_followup_ids": [],
                "writer_statuses": {},
                "ordinary_state_sha256": devcrew_ordinary._digest(
                    devcrew_ordinary._read(devcrew_ordinary._task_path(project, "T01"))
                ),
            },
        }
        return original, handoff

    def test_ordinary_source_gap_seals_same_task_and_resumes_idempotently(self):
        td, project, receipt, ordinary, phase, git = self.source_gap_fixture()
        captured_branch = ordinary._read(ordinary._task_path(project, "T01"))["checkout_identity"][
            "branch"
        ]
        self.assertEqual(receipt.get("created_from_branch"), captured_branch)
        original, handoff = self.source_gap_contract(td, project)
        before_head = git("rev-parse", "HEAD")
        index = pathlib.Path(git("rev-parse", "--git-path", "index"))
        if not index.is_absolute():
            index = project / index
        before_index_sha = hashlib.sha256(index.read_bytes()).hexdigest()
        handoff_path = pathlib.Path(td.name) / "ordinary-source-gap.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        first = self.run_cli(
            "phase",
            "source-bind",
            "--project",
            project,
            "--task-id",
            "T01",
            "--ordinary-source-gap",
            handoff_path,
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        first = json.loads(first.stdout)
        repeated = self.run_cli(
            "phase",
            "source-bind",
            "--project",
            project,
            "--task-id",
            "T01",
            "--ordinary-source-gap",
            handoff_path,
        )
        self.assertEqual(repeated.returncode, 0, repeated.stderr)
        self.assertEqual(json.loads(repeated.stdout), first)
        self.assertEqual(first["status"], "ORDINARY_SOURCE_GAP_SEALED")
        self.assertEqual((project / first["source"]["snapshot"]).read_bytes(), original)
        self.assertEqual(git("rev-parse", "HEAD"), before_head)
        self.assertEqual(hashlib.sha256(index.read_bytes()).hexdigest(), before_index_sha)
        self.assertEqual(git("status", "--porcelain", "--untracked-files=all"), "")
        self.assertEqual(phase.intent_source_preflight(project, receipt), first["source"])
        with self.assertRaisesRegex(ValueError, "ORDINARY_TASK_SEALED"):
            ordinary.dispatch(project, "T01", "planner")

    def test_legacy_checkout_receipt_without_branch_provenance_remains_readable(self):
        td, project, receipt, _, _, _ = self.source_gap_fixture()
        import devcrew_worktree

        state_path = devcrew_worktree.location(project, "T01")[-1]
        record = json.loads(state_path.read_text(encoding="utf-8"))
        record.pop("created_from_branch")
        state_path.write_text(json.dumps(record), encoding="utf-8")
        legacy = devcrew_worktree.resolve(project, "T01", read_only=True)
        self.assertEqual(legacy["branch"], receipt["branch"])
        self.assertNotIn("created_from_branch", legacy)

    def test_ordinary_source_gap_rejects_active_writer_and_preserves_unsealed_history(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        handoff["host_observation"]["active_agent_ids"] = ["still-running"]
        selection = (
            pathlib.Path(receipt["task_worktree_root"])
            / ".codex/tasks/T01/sources/planning-source.json"
        )
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertIsNone(ordinary._read(ordinary._task_path(project, "T01")).get("formal_handoff"))
        self.assertFalse(phase.manifest_path(project, "T01").exists())
        self.assertFalse(selection.exists())

    def test_source_gap_requires_terminal_observation_for_each_recorded_writer(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        state_path = ordinary._task_path(project, "T01")
        state = ordinary._read(state_path)
        state["spawn_receipts"] = [{"cycle_id": 0, "ROLE": "executor", "AGENT_ID": "executor-1"}]
        ordinary._write_unlocked(state_path, state)
        _, handoff = self.source_gap_contract(td, project)
        handoff["host_observation"]["ordinary_state_sha256"] = ordinary._digest(
            ordinary._read(state_path)
        )
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertFalse(
            (
                pathlib.Path(receipt["task_worktree_root"])
                / ".codex/tasks/T01/sources/planning-source.json"
            ).exists()
        )
        handoff["host_observation"]["writer_statuses"] = {"executor-1": "completed"}
        sealed = phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertEqual(sealed["status"], "ORDINARY_SOURCE_GAP_SEALED")

    def test_source_gap_requires_terminal_observation_for_archived_writer(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        state_path = ordinary._task_path(project, "T01")
        state = ordinary._read(state_path)
        state["spawn_receipts"] = [
            {"cycle_id": 0, "ROLE": "executor", "AGENT_ID": "archived-executor"}
        ]
        ordinary._archive_current_cycle(
            state, reason="replan", continuation={"ACTION": "SYSTEMIC_REPLAN"}
        )
        state["spawn_receipts"] = []
        ordinary._write_unlocked(state_path, state)
        _, handoff = self.source_gap_contract(td, project)
        with self.assertRaisesRegex(phase.PhaseError, "OBSERVATION_INVALID"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        handoff["host_observation"]["writer_statuses"] = {"archived-executor": "completed"}
        sealed = phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertEqual(sealed["status"], "ORDINARY_SOURCE_GAP_SEALED")

    def test_unmatched_archived_issuance_fails_closed(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        state = ordinary._read(ordinary._task_path(project, "T01"))
        state["issued_dispatches"] = [{"cycle_id": 0, "route_id": "unmatched-real-route"}]
        ordinary._archive_current_cycle(
            state, reason="replan", continuation={"ACTION": "SYSTEMIC_REPLAN"}
        )
        state["issued_dispatches"] = []
        ordinary._write_unlocked(ordinary._task_path(project, "T01"), state)
        _, handoff = self.source_gap_contract(td, project)
        with self.assertRaisesRegex(phase.PhaseError, "archived issuance has no matching receipt"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)

    def test_source_gap_rejects_stale_observation_and_mismatched_attachment_before_selection(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        handoff["host_observation"]["ordinary_state_sha256"] = "0" * 64
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertFalse(
            (
                pathlib.Path(receipt["task_worktree_root"])
                / ".codex/tasks/T01/planning-source.json"
            ).exists()
        )
        _, handoff = self.source_gap_contract(td, project)
        handoff["host_observation"]["attachment_reference"] = "another-attachment.txt"
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_OBSERVATION_INVALID"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertIsNone(ordinary._read(ordinary._task_path(project, "T01")).get("formal_handoff"))
        self.assertFalse(
            (
                pathlib.Path(receipt["task_worktree_root"])
                / ".codex/tasks/T01/planning-source.json"
            ).exists()
        )
        replacement = pathlib.Path(td.name) / "replacement.txt"
        replacement_bytes = b"replacement exact attachment bytes\n"
        replacement.write_bytes(replacement_bytes)
        _, corrected = self.source_gap_contract(td, project)
        corrected["source_spec"].update(
            path=str(replacement), expected_sha256=hashlib.sha256(replacement_bytes).hexdigest()
        )
        corrected["host_observation"]["attachment_reference"] = str(replacement.resolve())
        accepted = phase.bind_ordinary_source_gap(project, receipt, corrected)
        self.assertEqual(accepted["status"], "ORDINARY_SOURCE_GAP_SEALED")

    def test_ordinary_init_rejects_same_task_formal_manifest(self):
        td, project, _, ordinary, _, git = self.source_gap_fixture()
        git_dir = pathlib.Path(git("rev-parse", "--absolute-git-dir"))
        state_dir = git_dir / "devcrew-checkouts"
        state_dir.mkdir(exist_ok=True)
        (state_dir / "OTHER.phases.json").write_text("{}\n", encoding="utf-8")
        context = json.loads(self.context(project).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "FORMAL_TASK_OWNERSHIP_CONFLICT"):
            ordinary.init(project, "OTHER", context, git("rev-parse", "HEAD"))

    def test_ordinary_init_serializes_same_id_writers(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary as ordinary

        git = lambda *args: subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
        context = json.loads(self.context(project).read_text(encoding="utf-8"))
        barrier = threading.Barrier(3)
        outcomes = []

        def create():
            barrier.wait()
            try:
                ordinary.init(project, "RACE", context, git("rev-parse", "HEAD"))
                outcomes.append("created")
            except ValueError:
                outcomes.append("rejected")

        threads = [threading.Thread(target=create) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()
        self.assertCountEqual(outcomes, ["created", "rejected"])

    def test_ordinary_init_finds_same_id_in_linked_checkout(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary as ordinary

        git = lambda root, *args: subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
        context = json.loads(self.context(project).read_text(encoding="utf-8"))
        ordinary.init(project, "LINKED", context, git(project, "rev-parse", "HEAD"))
        linked = pathlib.Path(td.name) / "linked"
        subprocess.run(
            [
                "git",
                "-C",
                str(project),
                "worktree",
                "add",
                "-b",
                "codex/tasks/linked",
                str(linked),
                "HEAD",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        linked_context = json.loads(self.context(linked).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "ORDINARY_TASK_OWNERSHIP_CONFLICT"):
            ordinary.init(linked, "LINKED", linked_context, git(linked, "rev-parse", "HEAD"))

    def test_ordinary_init_finds_formal_manifest_in_linked_devcrew_state(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary as ordinary

        git = lambda *args: subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
        key = hashlib.sha256(str(project.resolve()).encode("utf-8")).hexdigest()[:16]
        state = (
            project.parent / ".devcrew-task-worktrees" / f"{project.name}-{key}" / ".devcrew-state"
        )
        state.mkdir(parents=True)
        (state / "LINKED-MANIFEST.json").write_text("{}\n", encoding="utf-8")
        (state / "LINKED-MANIFEST.phases.json").write_text("{}\n", encoding="utf-8")
        context = json.loads(self.context(project).read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "FORMAL_TASK_OWNERSHIP_CONFLICT"):
            ordinary.init(project, "LINKED-MANIFEST", context, git("rev-parse", "HEAD"))

    def test_ordinary_init_from_linked_root_finds_real_formal_manifest_created_from_host(self):
        from test_task_phases import PhaseManifestTests, plan as phase_plan

        td, project = self.project()
        self.addCleanup(td.cleanup)
        (project / ".gitignore").write_text(".codex/\nchat-context.json\n")
        subprocess.run(["git", "-C", str(project), "add", ".gitignore"], check=True)
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
                "ignore runtime state",
            ],
            check=True,
        )
        setup = PhaseManifestTests()
        spawn = setup.planner_receipt(project)
        receipt = setup.receipt
        import devcrew_phase

        manifest = phase_plan()
        manifest["worktree"].update(
            repository_id=receipt["repository_id"],
            task_worktree_root=receipt["task_worktree_root"],
            branch=receipt["branch"],
            base_sha=receipt["expected_head"],
        )
        manifest_path = devcrew_phase.manifest_path(project, "T01")
        devcrew_phase.initialize_manifest(
            manifest_path, manifest, receipt, project, spawn["SPAWN_RECEIPT_ID"]
        )
        self.assertTrue(manifest_path.is_file())
        linked = pathlib.Path(receipt["task_worktree_root"])
        linked_context = json.loads(self.context(linked).read_text(encoding="utf-8"))
        import devcrew_ordinary

        with self.assertRaisesRegex(ValueError, "FORMAL_TASK_OWNERSHIP_CONFLICT"):
            devcrew_ordinary.init(linked, "T01", linked_context, receipt["expected_head"])

    def test_formal_init_rechecks_ownership_inside_shared_namespace_lock(self):
        from test_task_phases import PhaseManifestTests, plan as phase_plan

        td, project = self.project()
        self.addCleanup(td.cleanup)
        (project / ".gitignore").write_text(".codex/\nchat-context.json\n")
        subprocess.run(["git", "-C", str(project), "add", ".gitignore"], check=True)
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
                "ignore runtime state",
            ],
            check=True,
        )
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary as ordinary
        import devcrew_phase as phase

        setup = PhaseManifestTests()
        spawn = setup.planner_receipt(project)
        receipt = setup.receipt
        self.assertTrue(ordinary._namespace_lock_path(project, "T01").is_absolute())
        self.assertEqual(
            ordinary._namespace_lock_path(project, "T01"),
            ordinary._namespace_lock_path(pathlib.Path(receipt["task_worktree_root"]), "T01"),
        )
        manifest = phase_plan()
        manifest["worktree"].update(
            repository_id=receipt["repository_id"],
            task_worktree_root=receipt["task_worktree_root"],
            branch=receipt["branch"],
            base_sha=receipt["expected_head"],
        )
        manifest_path = phase.manifest_path(project, "T01")
        verified = threading.Event()
        continue_formal = threading.Event()
        ordinary_started = threading.Event()
        ordinary_done = threading.Event()
        errors = []
        original = phase._verify_planner_source_binding

        def interleave(*args, **kwargs):
            result = original(*args, **kwargs)
            verified.set()
            if not continue_formal.wait(10):
                raise AssertionError("interleaving test timed out")
            return result

        def formal_create():
            try:
                with mock.patch.object(
                    phase, "_verify_planner_source_binding", side_effect=interleave
                ):
                    phase.initialize_manifest(
                        manifest_path, manifest, receipt, project, spawn["SPAWN_RECEIPT_ID"]
                    )
                errors.append("formal-created")
            except ValueError as exc:
                errors.append(f"formal-rejected:{getattr(exc, 'code', type(exc).__name__)}")

        context = json.loads(self.context(project).read_text(encoding="utf-8"))

        def ordinary_create():
            ordinary_started.set()
            try:
                ordinary.init(project, "T01", context, receipt["expected_head"])
                errors.append("ordinary-created")
            except ValueError as exc:
                errors.append(f"ordinary-rejected:{type(exc).__name__}")
            finally:
                ordinary_done.set()

        worker = threading.Thread(target=formal_create)
        worker.start()
        self.assertTrue(
            verified.wait(10), "formal source verification did not reach interleaving point"
        )
        ordinary_worker = threading.Thread(target=ordinary_create)
        ordinary_worker.start()
        self.assertTrue(ordinary_started.wait(10))
        # Pre-fix, ordinary init completes during the validation/lock gap.
        ordinary_done.wait(0.2)
        continue_formal.set()
        worker.join(10)
        ordinary_worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertFalse(ordinary_worker.is_alive())
        ordinary_path = ordinary._task_path(project, "T01")
        self.assertFalse(ordinary_path.exists() and manifest_path.exists())
        self.assertEqual(len([item for item in errors if item.endswith("created")]), 1, errors)

    def test_primary_prefix_without_checkout_provenance_is_rejected(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary as ordinary
        import devcrew_phase as phase

        git = lambda *args: subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
        (project / ".gitignore").write_text(".codex/\nchat-context.json\n")
        git("add", ".gitignore")
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
                "ignore runtime state",
            ],
            check=True,
        )
        base = git("rev-parse", "HEAD")
        ordinary.init(
            project, "T01", json.loads(self.context(project).read_text(encoding="utf-8")), base
        )
        git("switch", "-c", "codex/tasks/T01-unrelated")
        self.assertEqual(
            self.run_cli(
                "task-checkout",
                "create",
                "--project",
                project,
                "--task-id",
                "T01",
                "--base-sha",
                base,
            ).returncode,
            0,
        )
        receipt = json.loads(
            self.run_cli(
                "task-checkout", "resolve", "--project", project, "--task-id", "T01"
            ).stdout
        )
        _, handoff = self.source_gap_contract(td, project)
        with self.assertRaisesRegex(phase.PhaseError, "CHECKOUT_CHANGED"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)

    def test_non_primary_original_branch_continues_without_created_from_metadata(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary as ordinary
        import devcrew_phase as phase

        git = lambda *args: subprocess.run(
            ["git", "-C", str(project), *args], check=True, capture_output=True, text=True
        ).stdout.strip()
        (project / ".gitignore").write_text(".codex/\nchat-context.json\n")
        git("add", ".gitignore")
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
                "ignore runtime state",
            ],
            check=True,
        )
        git("switch", "-c", "feature/original")
        base = git("rev-parse", "HEAD")
        ordinary.init(
            project, "T01", json.loads(self.context(project).read_text(encoding="utf-8")), base
        )
        created = self.run_cli(
            "task-checkout", "create", "--project", project, "--task-id", "T01", "--base-sha", base
        )
        self.assertEqual(created.returncode, 0, created.stderr)
        receipt = json.loads(
            self.run_cli(
                "task-checkout", "resolve", "--project", project, "--task-id", "T01"
            ).stdout
        )
        self.assertNotIn("created_from_branch", receipt)
        _, handoff = self.source_gap_contract(td, project)
        self.assertEqual(
            phase.bind_ordinary_source_gap(project, receipt, handoff)["status"],
            "ORDINARY_SOURCE_GAP_SEALED",
        )

    def test_archived_executor_receipt_requires_real_terminal_map_after_systemic_replan(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        (project / ".gitignore").write_text(".codex/\nchat-context.json\n")
        subprocess.run(["git", "-C", str(project), "add", ".gitignore"], check=True)
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
                "ignore runtime state",
            ],
            check=True,
        )
        task_id = self.init(project, "T01")
        candidate = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, candidate, "SYSTEMIC_REPLAN")
        continued = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            self.continuation(project, task_id, candidate, failed["sha256"], "SYSTEMIC_REPLAN"),
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        state_path = project / ".codex/tasks/T01.ordinary.json"
        state = json.loads(state_path.read_text())
        archived = state["cycles"][0]
        archived_writer_ids = {
            item["AGENT_ID"]
            for item in archived["spawn_receipts"]
            if isinstance(item.get("AGENT_ID"), str)
        }
        self.assertTrue(archived_writer_ids)
        base = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        self.assertEqual(
            self.run_cli(
                "task-checkout",
                "create",
                "--project",
                project,
                "--task-id",
                task_id,
                "--base-sha",
                base,
            ).returncode,
            0,
        )
        receipt = json.loads(
            self.run_cli(
                "task-checkout", "resolve", "--project", project, "--task-id", task_id
            ).stdout
        )
        _, handoff = self.source_gap_contract(td, project)
        import devcrew_phase

        with self.assertRaisesRegex(devcrew_phase.PhaseError, "OBSERVATION_INVALID"):
            devcrew_phase.bind_ordinary_source_gap(project, receipt, handoff)
        handoff["host_observation"]["writer_statuses"] = {
            agent_id: "completed" for agent_id in archived_writer_ids
        }
        sealed = devcrew_phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertEqual(sealed["status"], "ORDINARY_SOURCE_GAP_SEALED")

    def test_source_gap_rejects_checkout_branch_changed_since_capture(self):
        td, project, receipt, ordinary, phase, git = self.source_gap_fixture()
        state_path = ordinary._task_path(project, "T01")
        state = ordinary._read(state_path)
        state["checkout_identity"]["branch"] = "original-branch"
        ordinary._write_unlocked(state_path, state)
        _, handoff = self.source_gap_contract(td, project)
        with self.assertRaisesRegex(phase.PhaseError, "CHECKOUT_CHANGED"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertFalse(
            (
                pathlib.Path(receipt["task_worktree_root"])
                / ".codex/tasks/T01/planning-source.json"
            ).exists()
        )

    def test_corrupt_handoff_link_rejects_before_source_selection(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        link = phase._ordinary_handoff_path(project, "T01")
        link.parent.mkdir(parents=True, exist_ok=True)
        link.write_text("{broken", encoding="utf-8")
        before = ordinary._read(ordinary._task_path(project, "T01"))
        with self.assertRaisesRegex(phase.PhaseError, "BINDING_INVALID"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertEqual(ordinary._read(ordinary._task_path(project, "T01")), before)
        self.assertFalse(
            (
                pathlib.Path(receipt["task_worktree_root"])
                / ".codex/tasks/T01/planning-source.json"
            ).exists()
        )

    def test_checkout_revalidation_precedes_first_source_selection_write(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        import devcrew_worktree

        selection = (
            pathlib.Path(receipt["task_worktree_root"])
            / ".codex/tasks/T01/sources/planning-source.json"
        )
        with mock.patch.object(devcrew_worktree, "resolve", return_value={"state": "STALE"}):
            with self.assertRaisesRegex(phase.PhaseError, "CHECKOUT_CHANGED"):
                phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertFalse(selection.exists())

    def test_sealed_handoff_retry_uses_saved_private_bytes_after_attachment_disappears(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        original, handoff = self.source_gap_contract(td, project)
        attachment = pathlib.Path(handoff["source_spec"]["path"])
        original_create = phase._atomic_create_json

        def fail_link_once(path, value, **kwargs):
            if pathlib.Path(path).name == "ordinary-handoff.json":
                raise OSError("simulated stop after seal before link")
            return original_create(path, value, **kwargs)

        with mock.patch.object(phase, "_atomic_create_json", side_effect=fail_link_once):
            with self.assertRaisesRegex(OSError, "simulated stop"):
                phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertIsNotNone(
            ordinary._read(ordinary._task_path(project, "T01")).get("formal_handoff")
        )
        attachment.unlink()
        recovered = phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertEqual(recovered["status"], "ORDINARY_SOURCE_GAP_SEALED")
        self.assertEqual(recovered["source"]["sha256"], hashlib.sha256(original).hexdigest())
        selection = (
            pathlib.Path(receipt["task_worktree_root"])
            / ".codex/tasks/T01/sources/planning-source.json"
        )
        self.assertEqual(recovered["source"], json.loads(selection.read_text(encoding="utf-8")))

    def test_partial_seal_rejects_selected_digest_tampering_before_link(self):
        td, project, receipt, ordinary, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        original_create = phase._atomic_create_json

        def fail_link_once(path, value, **kwargs):
            if pathlib.Path(path).name == "ordinary-handoff.json":
                raise OSError("simulated stop after seal before link")
            return original_create(path, value, **kwargs)

        with mock.patch.object(phase, "_atomic_create_json", side_effect=fail_link_once):
            with self.assertRaisesRegex(OSError, "simulated stop"):
                phase.bind_ordinary_source_gap(project, receipt, handoff)
        selection = (
            pathlib.Path(receipt["task_worktree_root"])
            / ".codex/tasks/T01/sources/planning-source.json"
        )
        selected = json.loads(selection.read_text(encoding="utf-8"))
        selected["sha256"] = "0" * 64
        selection.write_text(json.dumps(selected), encoding="utf-8")
        pathlib.Path(handoff["source_spec"]["path"]).unlink()
        with self.assertRaisesRegex(phase.PhaseError, "CONFLICT"):
            phase.bind_ordinary_source_gap(project, receipt, handoff)
        self.assertFalse(phase._ordinary_handoff_path(project, "T01").exists())

    def test_fifo_in_saved_source_snapshot_fails_without_blocking_or_link(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        original_create = phase._atomic_create_json

        def fail_link_once(path, value, **kwargs):
            if pathlib.Path(path).name == "ordinary-handoff.json":
                raise OSError("simulated stop after seal before link")
            return original_create(path, value, **kwargs)

        with mock.patch.object(phase, "_atomic_create_json", side_effect=fail_link_once):
            with self.assertRaisesRegex(OSError, "simulated stop"):
                phase.bind_ordinary_source_gap(project, receipt, handoff)
        pathlib.Path(handoff["source_spec"]["path"]).unlink()
        seal = json.loads((project / ".codex/tasks/T01.ordinary.json").read_text())[
            "formal_handoff"
        ]
        snapshot = (
            pathlib.Path(receipt["task_worktree_root"])
            / ".codex/tasks/T01/sources"
            / f"{seal['payload']['source_id']}.bin"
        )
        snapshot.unlink()
        os.mkfifo(snapshot)
        handoff_path = pathlib.Path(td.name) / "handoff.json"
        handoff_path.write_text(json.dumps(handoff), encoding="utf-8")
        receipt_path = pathlib.Path(td.name) / "receipt.json"
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        code = (
            "import json, pathlib, sys; sys.path.insert(0, sys.argv[1]); "
            "import devcrew_phase; devcrew_phase.bind_ordinary_source_gap("
            "pathlib.Path(sys.argv[2]), json.loads(pathlib.Path(sys.argv[3]).read_text()), "
            "json.loads(pathlib.Path(sys.argv[4]).read_text()))"
        )
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    code,
                    str(pathlib.Path(__file__).resolve().parents[1] / "scripts"),
                    str(project),
                    str(receipt_path),
                    str(handoff_path),
                ],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except subprocess.TimeoutExpired:
            self.fail("retry blocked while reading a FIFO source snapshot")
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(phase._ordinary_handoff_path(project, "T01").exists())

    def test_legacy_planner_requires_registered_handoff_and_uses_original_base(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        payload = {"message": "legacy planner package"}
        issued = {
            "ROUTE_ID": "legacy-route",
            "SPAWN_PAYLOAD": payload,
            "SPAWN_PAYLOAD_SHA256": hashlib.sha256(phase._canonical_json(payload)).hexdigest(),
        }
        store = {"issued_routes": [issued]}
        manifest = {
            "schema_version": 1,
            "task_id": "T01",
            "worktree": {
                "task_worktree_root": str(project),
                "branch": receipt["branch"],
                "base_sha": "f" * 40,
            },
            "phases": [{"base_sha": receipt["expected_head"]}],
        }
        with mock.patch("devcrew_dispatch._read", return_value=store):
            with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED"):
                phase._verify_planner_source_binding(
                    project, manifest, {"ROUTE_ID": "legacy-route"}
                )
            _, handoff = self.source_gap_contract(td, project)
            phase.bind_ordinary_source_gap(project, receipt, handoff)
            with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_SCHEMA_V2_REQUIRED"):
                phase._verify_planner_source_binding(
                    project, manifest, {"ROUTE_ID": "legacy-route"}
                )

    def test_new_v1_formal_init_after_handoff_rejects_valid_source_and_planner_receipt(self):
        from test_task_phases import plan as phase_plan

        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        phase.bind_ordinary_source_gap(project, receipt, handoff)
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_context
        import devcrew_cost
        import devcrew_dispatch

        runtime = {
            "models": [
                {"id": "bounded-test", "capabilities": ["bounded"], "efforts": ["low", "medium"]},
                {
                    "id": "strong-test",
                    "capabilities": ["strong"],
                    "efforts": ["low", "medium", "high"],
                },
            ],
            "roles": {"planner-agent": {"available": True, "supports_overrides": True}},
            "generic_available": True,
            "explicit_model_effort": True,
        }
        prepared = devcrew_context.routed_spawn(
            "planner_T01",
            "Plan the registered task",
            devcrew_cost.route("planner", runtime=runtime),
            runtime,
            project_root=project.resolve(),
            task_id="T01",
            identity={"TASK_ID": "T01", "BASE_SHA": receipt["expected_head"]},
            role_contract="Plan the task.",
        )
        spawn = devcrew_dispatch.record_spawn(
            project,
            {
                "STATUS": "SPAWN_SUCCESS",
                "REQUESTED_ROLE": "planner",
                "TASK_ID": "T01",
                "AGENT_ID": "planner-1",
                "SPAWN_MODEL": prepared["spawn"]["model"],
                "SPAWN_REASONING_EFFORT": prepared["spawn"]["reasoning_effort"],
                "SPAWN_MODE": prepared["dispatch"]["SPAWN_MODE"],
                "BASE_SHA": receipt["expected_head"],
                "PREPARED": {"route": prepared["route"], "dispatch": prepared["dispatch"]},
                "SPAWN_PAYLOAD": prepared["spawn"],
                "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(prepared["spawn"]),
            },
        )
        manifest = phase_plan()
        manifest["worktree"].update(
            repository_id=receipt["repository_id"],
            task_worktree_root=receipt["task_worktree_root"],
            branch=receipt["branch"],
            base_sha=receipt["expected_head"],
        )
        path = phase.manifest_path(project, "T01")
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_SCHEMA_V2_REQUIRED"):
            phase.initialize_manifest(path, manifest, receipt, project, spawn["SPAWN_RECEIPT_ID"])
        self.assertFalse(path.exists())

    def test_source_preflight_uses_registered_base_after_checkout_receipt_advances(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        phase.bind_ordinary_source_gap(project, receipt, handoff)
        advanced = dict(receipt, expected_head="f" * 40)
        source = phase.intent_source_preflight(project, advanced)
        self.assertEqual(source["source_type"], "attachment")

    def test_formal_source_binding_requires_handoff_when_ordinary_history_exists(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        original = b"attachment bytes"
        attachment = pathlib.Path(td.name) / "attachment.txt"
        attachment.write_bytes(original)
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED"):
            phase.bind_intent_source(
                project,
                receipt,
                {
                    "mode": "file",
                    "source_type": "attachment",
                    "path": str(attachment),
                    "expected_sha256": hashlib.sha256(original).hexdigest(),
                    "origin": "authorized attachment",
                },
            )
        self.assertFalse((project / ".codex/tasks/T01/planning-source.json").exists())

    def test_verified_selection_without_seal_fails_planner_preflight_and_pinned_resume_check(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        original = b"verified attachment without ordinary handoff"
        attachment = pathlib.Path(td.name) / "attachment.txt"
        attachment.write_bytes(original)
        record = phase.bind_intent_source(
            project,
            receipt,
            {
                "mode": "file",
                "source_type": "attachment",
                "path": str(attachment),
                "expected_sha256": hashlib.sha256(original).hexdigest(),
                "origin": "test fixture",
            },
            _ordinary_handoff=True,
        )
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED"):
            phase.intent_source_preflight(project, receipt)
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_HANDOFF_REQUIRED"):
            phase.intent_source_preflight(project, receipt, source=record)

    def test_handoff_rejects_rebinding_and_detects_link_tampering(self):
        td, project, receipt, _, phase, _ = self.source_gap_fixture()
        _, handoff = self.source_gap_contract(td, project)
        sealed = phase.bind_ordinary_source_gap(project, receipt, handoff)
        other = pathlib.Path(td.name) / "other-attachment.txt"
        other.write_bytes(b"different bytes")
        conflicting = json.loads(json.dumps(handoff))
        conflicting["source_spec"].update(
            path=str(other), expected_sha256=hashlib.sha256(other.read_bytes()).hexdigest()
        )
        conflicting["host_observation"]["attachment_reference"] = str(other.resolve())
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_CONFLICT"):
            phase.bind_ordinary_source_gap(project, receipt, conflicting)
        self.assertEqual(phase.intent_source_preflight(project, receipt), sealed["source"])
        link_path = project / ".codex/tasks/T01/ordinary-handoff.json"
        link = json.loads(link_path.read_text(encoding="utf-8"))
        link["seal"]["payload"]["source_id"] = "0" * 64
        link_path.write_text(json.dumps(link), encoding="utf-8")
        with self.assertRaisesRegex(phase.PhaseError, "ORDINARY_SOURCE_GAP_BINDING_INVALID"):
            phase.intent_source_preflight(project, receipt)

    def add_path_requirement(self, project, *, path="tracked.txt"):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_requirements

        requirement = {
            "id": "req-lifecycle-scope",
            "revision": 1,
            "selectors": {
                "paths": [path],
                "roles": ["planner", "executor", "reviewer"],
                "task_classes": ["PLANNER", "SYSTEMIC_REPLAN", "EXECUTOR", "REVIEWER"],
            },
            "trigger": "The task touches the owned file.",
            "action": "Apply the path-scoped rule.",
            "check": "Verify the path-scoped rule.",
            "origin": {"kind": "user_requirement", "reference": "test"},
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "No existing rule covers the path.",
                "evidence": [{"kind": "user_requirement", "reference": "test"}],
            },
        }
        devcrew_requirements.record(project, "memory", requirement, expected_registry_revision=0)

    def context(self, project, routing=None):
        path = project / "chat-context.json"
        path.write_text(
            json.dumps(
                {
                    "kind": "current_user_message",
                    "session_id": "session-1",
                    "turn_id": "turn-1",
                    "message_sha256": hashlib.sha256(b"current human request").hexdigest(),
                    "host_source_access": {
                        "tool": "mcp__codex_app__read_thread",
                        "thread_id": "thread-1",
                        "turn_id": "turn-1",
                        "turn_limit": 8,
                        "include_outputs": True,
                        "max_output_chars_per_item": 65536,
                        "attachments": [],
                    },
                    "requirements": [
                        {"id": "R1", "statement": "Keep behavior."},
                        {"id": "R2", "statement": "Run verification."},
                    ],
                    "acceptance": ["Both requirements are checked."],
                    "routing": routing
                    or {
                        "risk": "low",
                        "decision_complexity": "NORMAL",
                        "implementation_complexity": "NORMAL",
                        "review_complexity": "NORMAL",
                    },
                }
            )
        )
        return path

    def init(self, project, task_id="ordinary-1", routing=None):
        result = self.run_cli(
            "ordinary",
            "init",
            "--project",
            project,
            "--task-id",
            task_id,
            "--chat-context",
            self.context(project, routing),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return task_id

    def spawn(self, project, task_id, role, agent_id, *, typed=True, requirements_scope=None):
        runtime = project / ".codex" / "checks" / f"{role}-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        model = "bounded-test" if role == "executor" else "strong-test"
        effort = "medium"
        runtime.write_text(
            json.dumps(
                {
                    "explicit_model_effort": True,
                    "generic_available": True,
                    "models": [
                        {
                            "id": model,
                            "capabilities": ["bounded", "strong", "deep"],
                            "efforts": ["low", effort, "high"],
                            "priority": 1,
                        }
                    ],
                    "roles": {
                        f"{role}-agent" if role != "planner" else "decision-agent": {
                            "available": typed,
                            "model": model,
                            "reasoning_effort": effort,
                        }
                    },
                }
            )
        )
        arguments = [
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            role,
            "--runtime",
            runtime,
        ]
        if requirements_scope is not None:
            scope_file = project / ".codex" / "checks" / f"{role}-scope.json"
            scope_file.write_text(json.dumps({"paths": requirements_scope}))
            arguments.extend(("--requirements-scope", scope_file))
        issued = self.run_cli(*arguments)
        self.assertEqual(issued.returncode, 0, issued.stderr)
        payload = json.loads(issued.stdout)
        route = payload["route"]
        report = project / ".codex" / "checks" / f"{role}-spawn.json"
        report.write_text(
            json.dumps(
                {
                    "ROUTE_ID": route["ROUTE_ID"],
                    "ROLE": role,
                    "AGENT_ID": agent_id,
                    "MODEL": route["RESOLVED_MODEL"],
                    "REASONING_EFFORT": route["REASONING_EFFORT"],
                    "FORK_TURNS": "none",
                    "SPAWN_PAYLOAD_SHA256": hashlib.sha256(
                        json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            )
        )
        recorded = self.run_cli(
            "ordinary",
            "spawn-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            report,
        )
        self.assertEqual(recorded.returncode, 0, recorded.stderr)
        return json.loads(recorded.stdout)["receipt"]

    def candidate(self, project, task_id, *, planner_scope=None):
        check = self.run_cli(
            "run-check",
            "--project",
            project,
            "--task-id",
            task_id,
            "--name",
            "ordinary",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        record = check.stdout.splitlines()[0]
        matrix_result = self.run_cli(
            "ordinary", "dispatch", "--project", project, "--task-id", task_id, "--role", "planner"
        )
        self.assertEqual(matrix_result.returncode, 0, matrix_result.stderr)
        requirement_ids = [item["id"] for item in json.loads(matrix_result.stdout)["REQUIREMENTS"]]
        planner = self.spawn(
            project, task_id, "planner", "planner-a", requirements_scope=planner_scope
        )
        plan_path = project / ".codex" / "checks" / "ordinary-plan.json"
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        plan_path.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "SPAWN_RECEIPT_ID": planner["id"],
                    "REQUIREMENT_IDS": requirement_ids,
                    "STEPS": ["Implement the bounded change."],
                    "OWNED_SCOPE": ["tracked.txt"],
                    "VERIFICATION": ["Run targeted check."],
                    "ROUTING": state["routing"],
                }
            )
        )
        planned = self.run_cli(
            "ordinary",
            "plan-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            plan_path,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)["plan"]
        accepted = self.run_cli(
            "ordinary",
            "plan-accept",
            "--project",
            project,
            "--task-id",
            task_id,
            "--plan-sha256",
            plan["sha256"],
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        receipt = self.spawn(project, task_id, "executor", "executor-a")
        result = self.run_cli(
            "ordinary",
            "candidate",
            "--project",
            project,
            "--task-id",
            task_id,
            "--executor-receipt-id",
            receipt["id"],
            "--evidence-record",
            record,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["candidate"]

    def candidate_after_accepted_plan(self, project, task_id, agent_id="executor-a"):
        check = self.run_cli(
            "run-check",
            "--project",
            project,
            "--task-id",
            task_id,
            "--name",
            "ordinary-followup",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        receipt = self.spawn(project, task_id, "executor", agent_id)
        result = self.run_cli(
            "ordinary",
            "candidate",
            "--project",
            project,
            "--task-id",
            task_id,
            "--executor-receipt-id",
            receipt["id"],
            "--evidence-record",
            check.stdout.splitlines()[0],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["candidate"]

    def report(self, project, task_id, candidate, ids=("R1", "R2"), *, typed=True):
        receipt = self.spawn(project, task_id, "reviewer", "reviewer-b", typed=typed)
        path = project / ".codex" / "checks" / "ordinary-review.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "CANDIDATE_ID": candidate["id"],
                    "CYCLE_ID": candidate["cycle_id"],
                    "VERDICT": "PASS",
                    "FINDINGS": [],
                    "REQUIREMENT_IDS": list(ids),
                    "SPAWN_RECEIPT_ID": receipt["id"],
                    "SOURCE_REVIEW": {
                        "ACCESS": {
                            "tool": "mcp__codex_app__read_thread",
                            "thread_id": "thread-1",
                            "turn_id": "turn-1",
                            "turn_limit": 8,
                            "include_outputs": True,
                            "max_output_chars_per_item": 65536,
                            "attachments": [],
                            "message_sha256": hashlib.sha256(b"current human request").hexdigest(),
                        },
                        "OUTCOMES": [{"OUTCOME": "Keep behavior.", "REQUIREMENT_IDS": list(ids)}],
                        "OMISSIONS": [],
                    },
                }
            )
        )
        return path

    def continuation(self, project, task_id, candidate, review_sha256, action, *, stale_scope=None):
        path = project / ".codex" / "checks" / "ordinary-continuation.json"
        value = {
            "TASK_ID": task_id,
            "CYCLE_ID": candidate["cycle_id"],
            "ACTION": action,
            "REASON": "Independent review found a bounded defect.",
            "EVIDENCE_RECORDS": candidate["evidence_records"],
            "NEXT_ACTION": "Repair the bounded implementation and rerun its check.",
            "EXPECTED_RESULT": "A new candidate has fresh verification and review.",
            "COST_RATIONALE": "The correction is bounded to the accepted scope.",
            "PREVIOUS_REVIEW_SHA256": review_sha256,
        }
        if stale_scope:
            value["STALE_SCOPE"] = stale_scope
        path.write_text(json.dumps(value))
        return path

    def fail_review(self, project, task_id, candidate, classification="LOCAL_FIX"):
        path = self.report(project, task_id, candidate)
        value = json.loads(path.read_text())
        value.update(
            {
                "VERDICT": "FAIL",
                "FINDINGS": ["Targeted check does not cover the defect."],
                "CLASSIFICATION": classification,
            }
        )
        path.write_text(json.dumps(value))
        result = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            path,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["review"]

    @staticmethod
    def digest(value):
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def convert_to_legacy_018(self, project, task_id):
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        state.pop("cycle_id", None)
        state.pop("cycles", None)
        state.pop("archive_head", None)
        state.pop("routing", None)
        state.pop("routing_required", None)
        state["source"].pop("routing", None)
        plan = state["plan"]
        plan.pop("cycle_id", None)
        plan["report"].pop("ROUTING", None)
        receipt_ids = {}
        for receipt in state.get("spawn_receipts", []):
            old_id = receipt["id"]
            payload = {key: item for key, item in receipt.items() if key not in {"id", "cycle_id"}}
            receipt["id"] = self.digest(payload)
            receipt.pop("cycle_id", None)
            receipt_ids[old_id] = receipt["id"]
        plan["report"]["SPAWN_RECEIPT_ID"] = receipt_ids[plan["report"]["SPAWN_RECEIPT_ID"]]
        plan["sha256"] = self.digest(plan["report"])
        candidate = state["candidate"]
        for key in ("cycle_id", "files"):
            candidate.pop(key, None)
        for evidence in candidate["evidence"]:
            evidence.pop("command", None)
            evidence.pop("log_sha256", None)
        candidate["plan_sha256"] = plan["sha256"]
        candidate["planner_receipt_id"] = receipt_ids[candidate["planner_receipt_id"]]
        candidate["executor_receipt_id"] = receipt_ids[candidate["executor_receipt_id"]]
        identity = {
            key: candidate[key]
            for key in ("base_sha", "git_head", "worktree_fingerprint", "evidence", "plan_sha256")
        }
        candidate["id"] = self.digest(identity)
        review = state["review"]
        review["SPAWN_RECEIPT_ID"] = receipt_ids[review["SPAWN_RECEIPT_ID"]]
        review.pop("cycle_id", None)
        review["CANDIDATE_ID"] = candidate["id"]
        review.pop("CYCLE_ID", None)
        review.pop("CLASSIFICATION", None)
        report = {key: item for key, item in review.items() if key != "sha256"}
        review["sha256"] = self.digest(report)
        for receipt in state.get("spawn_receipts", []):
            receipt.pop("cycle_id", None)
        state_path.write_text(json.dumps(state))
        return state_path, candidate, review

    def attach_legacy_routing(self, project, task_id):
        assessment = project / ".codex" / "checks" / "legacy-routing.json"
        assessment.write_text(
            json.dumps(
                {
                    "risk": "low",
                    "decision_complexity": "NORMAL",
                    "implementation_complexity": "NORMAL",
                    "review_complexity": "NORMAL",
                }
            )
        )
        result = self.run_cli(
            "ordinary",
            "attach-routing",
            "--project",
            project,
            "--task-id",
            task_id,
            "--routing-assessment",
            assessment,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_reissued_reviewer_names_are_unique_and_host_compatible(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project, task_id="Repair.Task-1")
        self.candidate(project, task_id)
        started = self.run_cli(
            "ordinary", "review-start", "--project", project, "--task-id", task_id
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.spawn(project, task_id, "reviewer", "reviewer-first")
        runtime = project / ".codex" / "checks" / "reviewer-runtime.json"
        issued = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "reviewer",
            "--runtime",
            runtime,
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        payload = json.loads(issued.stdout)
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        names = [item["prepared"]["spawn"]["task_name"] for item in state["issued_dispatches"]]
        self.assertEqual(len(names), len(set(names)), "each issuance needs a fresh host agent path")
        for name in names:
            self.assertRegex(name, r"^[a-z][a-z0-9_]*$")
        self.assertEqual(names[-1], payload["spawn"]["task_name"])
        self.assertEqual(
            state["issued_dispatches"][-1]["payload_sha256"],
            hashlib.sha256(
                json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest(),
        )

    def test_current_chat_dispatches_and_completes_without_commit(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        head = subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"])
        refs = subprocess.check_output(["git", "-C", str(project), "show-ref"])
        index = (project / ".git" / "index").read_bytes()
        task_id = self.init(project)
        saved = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        self.assertNotIn("prompt", json.dumps(saved))
        result = self.run_cli(
            "ordinary", "dispatch", "--project", project, "--task-id", task_id, "--role", "planner"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("REQUIREMENTS", json.loads(result.stdout))
        candidate = self.candidate(project, task_id)
        review_start = self.run_cli(
            "ordinary", "review-start", "--project", project, "--task-id", task_id
        )
        self.assertEqual(review_start.returncode, 0, review_start.stderr)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                self.report(project, task_id, candidate),
            ).returncode,
            0,
        )
        accepted = self.run_cli("ordinary", "accept", "--project", project, "--task-id", task_id)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["status"], "TASK_DONE")
        self.assertEqual(
            subprocess.check_output(["git", "-C", str(project), "rev-parse", "HEAD"]), head
        )
        self.assertEqual(subprocess.check_output(["git", "-C", str(project), "show-ref"]), refs)
        self.assertEqual((project / ".git" / "index").read_bytes(), index)
        pathlib.Path(candidate["evidence_records"][0]).unlink()
        resumed = self.run_cli("ordinary", "resume", "--project", project, "--task-id", task_id)
        self.assertEqual(json.loads(resumed.stdout)["status"], "ORDINARY_EVIDENCE_INVALID")

    def test_runtime_state_is_ignored_but_source_changes_invalidate_checks(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        check = self.run_cli(
            "run-check",
            "--project",
            project,
            "--task-id",
            task_id,
            "--name",
            "freshness",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        self.assertEqual(check.returncode, 0, check.stderr)
        record = check.stdout.splitlines()[0]
        self.spawn(project, task_id, "planner", "planner-freshness")
        verified = self.run_cli("verify-check", "--record", record)
        self.assertEqual(verified.returncode, 0, verified.stderr)
        for name in ("tracked.txt", "new-source.py"):
            with self.subTest(path=name):
                path = project / name
                before = path.read_bytes() if path.exists() else None
                try:
                    path.write_text("source changed after verification\n")
                    stale = self.run_cli("verify-check", "--record", record)
                    self.assertNotEqual(stale.returncode, 0)
                    self.assertIn("record worktree fingerprint is stale", stale.stderr)
                finally:
                    if before is None:
                        path.unlink()
                    else:
                        path.write_bytes(before)

    def test_planner_requirement_scope_selects_and_pins_current_cycle(self):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_requirements

        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        common = {
            "roles": ["planner", "executor", "reviewer"],
            "task_classes": ["PLANNER", "EXECUTOR", "REVIEWER"],
        }
        for index, path in enumerate(("scripts/", "vendor/")):
            item = {
                "id": f"req-scope-{index}",
                "revision": 1,
                "selectors": {**common, "paths": [path]},
                "trigger": "A matching path is changed.",
                "action": "Apply the required rule.",
                "check": "Verify the rule was applied.",
                "origin": {"kind": "user_requirement", "reference": "test"},
                "assessment": {
                    "status": "active",
                    "durable": True,
                    "decision": "distinct",
                    "rationale": "No existing requirement covers this path.",
                    "evidence": [{"kind": "user_requirement", "reference": "test"}],
                },
            }
            devcrew_requirements.record(project, "memory", item, expected_registry_revision=index)
        runtime = project / ".codex" / "checks" / "planner-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            json.dumps(
                {
                    "explicit_model_effort": True,
                    "generic_available": True,
                    "models": [
                        {
                            "id": "strong-test",
                            "capabilities": ["bounded", "strong"],
                            "efforts": ["medium"],
                            "priority": 1,
                        }
                    ],
                    "roles": {
                        "decision-agent": {
                            "available": True,
                            "model": "strong-test",
                            "reasoning_effort": "medium",
                        }
                    },
                }
            )
        )
        scope = project / "planner-scope.json"
        scope.write_text(json.dumps({"paths": ["scripts/devcrew.py"]}))
        result = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "planner",
            "--runtime",
            runtime,
            "--requirements-scope",
            scope,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        embedded_dispatch = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in payload["spawn"]["message"].splitlines()
                if line.startswith("ORDINARY_CURRENT_CHAT: ")
            )
        )
        issued_state = json.loads(
            (project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text()
        )
        self.assertEqual(
            embedded_dispatch["LIFECYCLE_BINDING"],
            issued_state["issued_dispatches"][-1]["state_binding"],
        )
        envelope = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in payload["spawn"]["message"].splitlines()
                if line.startswith("ENGINEERING_REQUIREMENTS: ")
            )
        )
        self.assertEqual([row["id"] for row in envelope["requirements"]], ["req-scope-0"])
        changed_scope = project / "changed-scope.json"
        changed_scope.write_text(json.dumps({"paths": ["vendor/file.py"]}))
        changed = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "planner",
            "--runtime",
            runtime,
            "--requirements-scope",
            changed_scope,
        )
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("pinned", changed.stderr)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        state.pop("planner_requirements_scope", None)
        legacy_binding = state["issued_dispatches"][-1]["requirements_binding"]
        legacy_binding.pop("authority")
        legacy_binding.pop("cycle_id")
        state_path.write_text(json.dumps(state))
        resumed_declaration = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "planner",
            "--runtime",
            runtime,
            "--requirements-scope",
            changed_scope,
        )
        self.assertEqual(resumed_declaration.returncode, 0, resumed_declaration.stderr)

    def test_spawn_gate_rejects_changed_planner_declaration(self):
        import hashlib

        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        runtime = project / ".codex" / "checks" / "planner-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            json.dumps(
                {
                    "explicit_model_effort": True,
                    "generic_available": True,
                    "models": [
                        {
                            "id": "strong-test",
                            "capabilities": ["bounded", "strong"],
                            "efforts": ["medium"],
                            "priority": 1,
                        }
                    ],
                    "roles": {
                        "decision-agent": {
                            "available": True,
                            "model": "strong-test",
                            "reasoning_effort": "medium",
                        }
                    },
                }
            )
        )
        issued = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "planner",
            "--runtime",
            runtime,
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        payload = json.loads(issued.stdout)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        state["planner_requirements_scope"] = ["tampered/path.py"]
        state_path.write_text(json.dumps(state))
        route = payload["route"]
        report = project / ".codex" / "checks" / "planner-spawn.json"
        report.write_text(
            json.dumps(
                {
                    "ROUTE_ID": route["ROUTE_ID"],
                    "ROLE": "planner",
                    "AGENT_ID": "planner-a",
                    "MODEL": route["RESOLVED_MODEL"],
                    "REASONING_EFFORT": route["REASONING_EFFORT"],
                    "FORK_TURNS": "none",
                    "SPAWN_PAYLOAD_SHA256": hashlib.sha256(
                        json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            )
        )
        recorded = self.run_cli(
            "ordinary",
            "spawn-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            report,
        )
        self.assertNotEqual(recorded.returncode, 0)
        self.assertTrue(
            any(word in recorded.stderr.casefold() for word in ("scope", "stale or altered"))
        )

    def test_legacy_empty_registry_bindings_remain_compatible_and_nonempty_fail_actionably(self):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary
        import devcrew_requirements

        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        empty, _ = devcrew_requirements.load(project, "memory")
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        state["issued_dispatches"] = [
            {
                "role": "planner",
                "task_class": "PLANNER",
                "cycle_id": 0,
                "consumed": True,
                "requirements_binding": {
                    "memory_relative": "memory",
                    "registry_sha256": devcrew_ordinary._digest(empty),
                    "scope_paths": [],
                    "envelope_sha256": None,
                },
            }
        ]
        state_path.write_text(json.dumps(state))
        devcrew_ordinary._validate_current_requirements(
            project, devcrew_ordinary._read(state_path), "memory"
        )
        requirement = {
            "id": "req-legacy",
            "revision": 1,
            "selectors": {"paths": ["scripts/"], "roles": ["planner"], "task_classes": ["PLANNER"]},
            "trigger": "A task matches this path.",
            "action": "Apply the rule.",
            "check": "Verify it.",
            "origin": {"kind": "user_requirement", "reference": "test"},
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "No existing requirement covers this case.",
                "evidence": [{"kind": "user_requirement", "reference": "test"}],
            },
        }
        devcrew_requirements.record(project, "memory", requirement, expected_registry_revision=0)
        with self.assertRaisesRegex(
            devcrew_ordinary.OrdinaryError, "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY"
        ):
            devcrew_ordinary._validate_current_requirements(
                project, devcrew_ordinary._read(state_path), "memory"
            )
        before = json.loads(state_path.read_text())
        resumed = self.run_cli("ordinary", "resume", "--project", project, "--task-id", task_id)
        self.assertEqual(
            json.loads(resumed.stdout)["status"], "ORDINARY_LEGACY_REQUIREMENTS_RECOVERY"
        )
        self.assertEqual(
            json.loads(state_path.read_text())["issued_dispatches"], before["issued_dispatches"]
        )

    def test_legacy_nonempty_binding_does_not_become_compatible_after_registry_removal(self):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary
        import devcrew_requirements

        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        self.add_path_requirement(project)
        expected, _ = devcrew_ordinary._requirements_delivery(
            project, "memory", "planner", "PLANNER", ["tracked.txt"], "planner_declaration", 0
        )
        legacy_binding = {
            key: value for key, value in expected.items() if key not in {"authority", "cycle_id"}
        }
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        state["issued_dispatches"] = [
            {
                "role": "planner",
                "task_class": "PLANNER",
                "cycle_id": 0,
                "consumed": True,
                "requirements_binding": legacy_binding,
            }
        ]
        state_path.write_text(json.dumps(state))
        original_receipts = json.loads(state_path.read_text())["issued_dispatches"]
        registry_path = project / "memory" / devcrew_requirements.REGISTRY_NAME
        registry_path.unlink()
        with self.assertRaises(devcrew_ordinary.OrdinaryError):
            devcrew_ordinary._validate_current_requirements(
                project, devcrew_ordinary._read(state_path), "memory"
            )
        self.assertEqual(json.loads(state_path.read_text())["issued_dispatches"], original_receipts)

    def test_malformed_scope_is_rejected_even_when_registry_is_empty(self):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary

        td, project = self.project()
        self.addCleanup(td.cleanup)
        with self.assertRaisesRegex(
            devcrew_ordinary.OrdinaryError, "normalized repository-relative"
        ):
            devcrew_ordinary._requirements_delivery(
                project, "memory", "planner", "PLANNER", ["../escape"], "planner_declaration", 0
            )

    def test_historical_bindings_use_archived_declaration_and_accepted_plan(self):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_ordinary

        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        value = devcrew_ordinary._read(project / ".codex" / "tasks" / f"{task_id}.ordinary.json")
        value["planner_requirements_scope"] = ["vendor/file.py"]
        value["plan"] = {
            "accepted": True,
            "report": {"OWNED_SCOPE": ["scripts/a.py"]},
            "sha256": "a" * 64,
        }
        value["issued_dispatches"] = [
            {
                "role": "planner",
                "task_class": "PLANNER",
                "cycle_id": 0,
                "consumed": True,
                "requirements_binding": {
                    "memory_relative": "memory",
                    "registry_sha256": "x",
                    "scope_paths": ["scripts/a.py"],
                    "authority": "planner_declaration",
                    "cycle_id": 0,
                },
            },
            {
                "role": "executor",
                "task_class": "EXECUTOR",
                "cycle_id": 0,
                "consumed": True,
                "requirements_binding": {
                    "memory_relative": "memory",
                    "registry_sha256": "x",
                    "scope_paths": ["vendor/file.py"],
                    "authority": "accepted_plan",
                    "cycle_id": 0,
                },
            },
        ]
        devcrew_ordinary._archive_current_cycle(
            value, reason="test", continuation={"ACTION": "SYSTEMIC_REPLAN"}
        )
        self.assertEqual(value["cycle_id"], 1)
        with self.assertRaisesRegex(devcrew_ordinary.OrdinaryError, "scope authority is stale"):
            devcrew_ordinary._validate_current_requirements(project, value, "memory")
        self.assertEqual(
            devcrew_ordinary._trusted_requirements_scope(
                value, "planner", 0, registry_nonempty=False
            ),
            (["vendor/file.py"], "planner_declaration"),
        )
        self.assertEqual(
            devcrew_ordinary._trusted_requirements_scope(
                value, "executor", 0, registry_nonempty=False
            ),
            (["scripts/a.py"], "accepted_plan"),
        )

    def test_executor_and_reviewer_delivery_uses_accepted_owned_scope(self):
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_requirements

        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        items = [
            ("req-planner-path", ["vendor/"], ["planner"], ["PLANNER"]),
            ("req-executor-path", ["tests/"], ["executor"], ["EXECUTOR"]),
            ("req-reviewer-path", ["tests/"], ["reviewer"], ["REVIEWER"]),
        ]
        for index, (rid, paths, roles, classes) in enumerate(items):
            item = {
                "id": rid,
                "revision": 1,
                "selectors": {"paths": paths, "roles": roles, "task_classes": classes},
                "trigger": "A task touches this path.",
                "action": f"Apply {rid}.",
                "check": "Verify the rule.",
                "origin": {"kind": "user_requirement", "reference": "test"},
                "assessment": {
                    "status": "active",
                    "durable": True,
                    "decision": "distinct",
                    "rationale": "No existing rule covers this case.",
                    "evidence": [{"kind": "user_requirement", "reference": "test"}],
                },
            }
            devcrew_requirements.record(project, "memory", item, expected_registry_revision=index)
        planner = self.spawn(
            project, task_id, "planner", "planner-scope", requirements_scope=["vendor/task.py"]
        )
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        plan_path = project / ".codex" / "checks" / "scope-plan.json"
        plan_path.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "SPAWN_RECEIPT_ID": planner["id"],
                    "REQUIREMENT_IDS": ["R1", "R2"],
                    "STEPS": ["Implement the scoped work."],
                    "OWNED_SCOPE": ["tests/test_feature.py"],
                    "VERIFICATION": ["Run targeted tests."],
                    "ROUTING": state["routing"],
                }
            )
        )
        planned = self.run_cli(
            "ordinary",
            "plan-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            plan_path,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)["plan"]
        accepted = self.run_cli(
            "ordinary",
            "plan-accept",
            "--project",
            project,
            "--task-id",
            task_id,
            "--plan-sha256",
            plan["sha256"],
        )
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.spawn(project, task_id, "executor", "executor-scope")
        state = json.loads(state_path.read_text())
        executor_message = state["issued_dispatches"][-1]["prepared"]["spawn"]["message"]
        executor_envelope = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in executor_message.splitlines()
                if line.startswith("ENGINEERING_REQUIREMENTS: ")
            )
        )
        self.assertEqual(
            [row["id"] for row in executor_envelope["requirements"]], ["req-executor-path"]
        )
        candidate = self.candidate_after_accepted_plan(project, task_id)
        started = self.run_cli(
            "ordinary", "review-start", "--project", project, "--task-id", task_id
        )
        self.assertEqual(started.returncode, 0, started.stderr)
        self.spawn(project, task_id, "reviewer", "reviewer-scope")
        state = json.loads(state_path.read_text())
        reviewer_message = state["issued_dispatches"][-1]["prepared"]["spawn"]["message"]
        reviewer_envelope = json.loads(
            next(
                line.split(": ", 1)[1]
                for line in reviewer_message.splitlines()
                if line.startswith("ENGINEERING_REQUIREMENTS: ")
            )
        )
        self.assertEqual(
            [row["id"] for row in reviewer_envelope["requirements"]], ["req-reviewer-path"]
        )
        self.assertNotEqual(candidate["plan_sha256"], "")

    def test_generic_reviewer_gets_full_policy_and_pass_preserves_advisories(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        path = self.report(project, task_id, candidate, typed=False)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        spawn = json.loads(state_path.read_text())["issued_dispatches"][-1]["prepared"]["spawn"]
        self.assertEqual(spawn["agent_type"], "default")
        for instruction in (
            "Are all acceptance criteria met?",
            "Each actionable finding",
            "HOST_SOURCE_ACCESS",
            "ADVISORIES",
        ):
            self.assertIn(instruction, spawn["message"])

        report = json.loads(path.read_text())
        report["ADVISORIES"] = [
            {
                "SUMMARY": "Consider reusing the fixture helper.",
                "EVIDENCE": ["tests/example.py:12 duplicates tests/helpers.py:4"],
            }
        ]
        for invalid, error in (
            (dict(report, ADVISORIES=[{"SUMMARY": "No evidence", "EVIDENCE": []}]), "ADVISORIES"),
            (dict(report, VERDICT="FAIL", CLASSIFICATION="LOCAL_FIX"), "FAIL requires findings"),
            (dict(report, FINDINGS=["Blocking regression"]), "PASS requires no findings"),
        ):
            with self.subTest(error=error):
                path.write_text(json.dumps(invalid))
                rejected = self.run_cli(
                    "ordinary",
                    "review-report",
                    "--project",
                    project,
                    "--task-id",
                    task_id,
                    "--report",
                    path,
                )
                self.assertNotEqual(rejected.returncode, 0)
                self.assertIn(error, rejected.stderr)
        path.write_text(json.dumps(report))
        submitted = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            path,
        )
        self.assertEqual(submitted.returncode, 0, submitted.stderr)
        self.assertEqual(json.loads(submitted.stdout)["review"]["ADVISORIES"], report["ADVISORIES"])
        accepted = self.run_cli("ordinary", "accept", "--project", project, "--task-id", task_id)
        self.assertEqual(accepted.returncode, 0, accepted.stderr)
        self.assertEqual(json.loads(accepted.stdout)["status"], "TASK_DONE")

    def test_stale_review_missing_requirement_lost_context_and_formal_bypass_fail(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        (project / "tracked.txt").write_text("changed\n")
        self.assertNotEqual(
            self.run_cli(
                "ordinary", "review-start", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )
        first_git_dir = pathlib.Path(
            subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "--absolute-git-dir"], text=True
            ).strip()
        )
        first_formal = first_git_dir / "devcrew-checkouts" / "failed.phases.json"
        first_formal.parent.mkdir()
        first_formal.write_text(json.dumps({"phases": [{"status": "REPLAN_REQUIRED"}]}))
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "dispatch",
                "--project",
                project,
                "--task-id",
                task_id,
                "--role",
                "planner",
            ).returncode,
            0,
        )
        td2, second = self.project()
        self.addCleanup(td2.cleanup)
        second_id = self.init(second)
        second_candidate = self.candidate(second, second_id)
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                second,
                "--task-id",
                second_id,
                "--report",
                self.report(second, second_id, second_candidate, ("R1",)),
            ).returncode,
            0,
        )
        state = second / ".codex" / "tasks" / f"{second_id}.ordinary.json"
        value = json.loads(state.read_text())
        value["source"].pop("requirements")
        state.write_text(json.dumps(value))
        resumed = self.run_cli("ordinary", "resume", "--project", second, "--task-id", second_id)
        self.assertEqual(json.loads(resumed.stdout)["status"], "ORDINARY_CONTEXT_REQUIRED")
        git_dir = pathlib.Path(
            subprocess.check_output(
                ["git", "-C", str(second), "rev-parse", "--absolute-git-dir"], text=True
            ).strip()
        )
        formal = git_dir / "devcrew-checkouts" / "failed.phases.json"
        formal.parent.mkdir()
        formal.write_text(json.dumps({"phases": [{"status": "REPLAN_REQUIRED"}]}))
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "init",
                "--project",
                second,
                "--task-id",
                "other",
                "--chat-context",
                self.context(second),
            ).returncode,
            0,
        )

    def test_configured_memory_and_source_outcomes_are_gates(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        config = project / ".codex" / "devcrew.json"
        config.parent.mkdir()
        config.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "paths": {
                        "context": "docs/context.md",
                        "tasks": ".codex/tasks",
                        "sessions": ".codex/sessions",
                        "memory": "custom-memory",
                        "checks": ".codex/checks",
                    },
                }
            )
        )
        task_id = self.init(project)
        runtime = project / ".codex" / "checks" / "planner-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            json.dumps(
                {
                    "explicit_model_effort": True,
                    "generic_available": True,
                    "models": [
                        {
                            "id": "strong-test",
                            "capabilities": ["bounded", "strong"],
                            "efforts": ["medium"],
                            "priority": 1,
                        }
                    ],
                    "roles": {
                        "decision-agent": {
                            "available": True,
                            "model": "strong-test",
                            "reasoning_effort": "medium",
                        }
                    },
                }
            )
        )
        broken = project / "custom-memory" / "engineering-requirements.json"
        broken.parent.mkdir()
        broken.write_text("BROKEN")
        result = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "planner",
            "--runtime",
            runtime,
        )
        self.assertNotEqual(result.returncode, 0)
        broken.unlink()
        issued = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            task_id,
            "--role",
            "planner",
            "--runtime",
            runtime,
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        payload = json.loads(issued.stdout)
        route = payload["route"]
        stale_report = project / ".codex" / "checks" / "stale-planner-spawn.json"
        stale_report.write_text(
            json.dumps(
                {
                    "ROUTE_ID": route["ROUTE_ID"],
                    "ROLE": "planner",
                    "AGENT_ID": "planner-stale",
                    "MODEL": route["RESOLVED_MODEL"],
                    "REASONING_EFFORT": route["REASONING_EFFORT"],
                    "FORK_TURNS": "none",
                    "SPAWN_PAYLOAD_SHA256": hashlib.sha256(
                        json.dumps(
                            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            )
        )
        broken.write_text("BROKEN")
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "spawn-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                stale_report,
            ).returncode,
            0,
        )
        broken.unlink()
        candidate = self.candidate(project, task_id)
        failed = self.report(project, task_id, candidate)
        report = json.loads(failed.read_text())
        report["SOURCE_REVIEW"]["OMISSIONS"] = ["Unmapped source outcome."]
        failed.write_text(json.dumps(report))
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                failed,
            ).returncode,
            0,
        )
        broken.write_text("BROKEN")
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                failed,
            ).returncode,
            0,
        )
        broken.unlink()
        valid = self.report(project, task_id, candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                valid,
            ).returncode,
            0,
        )
        broken.write_text("BROKEN")
        self.assertNotEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )
        broken.unlink()
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )
        broken.write_text(json.dumps({"schema_version": 1, "revision": 1, "requirements": []}))
        resumed = self.run_cli("ordinary", "resume", "--project", project, "--task-id", task_id)
        self.assertEqual(json.loads(resumed.stdout)["status"], "ORDINARY_REQUIREMENTS_STALE")
        broken.unlink()
        state = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        value = json.loads(state.read_text())
        value["review"]["sha256"] = "0" * 64
        state.write_text(json.dumps(value))
        resumed = self.run_cli("ordinary", "resume", "--project", project, "--task-id", task_id)
        self.assertEqual(json.loads(resumed.stdout)["status"], "ORDINARY_REVIEW_INVALID")

    def test_local_fix_archives_failed_cycle_and_requires_current_artifacts(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        first = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, first)
        continuation = self.continuation(project, task_id, first, failed["sha256"], "LOCAL_FIX")
        continued = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        self.assertEqual(json.loads(continued.stdout)["cycle_id"], 1)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        self.assertEqual(len(state["cycles"]), 1)
        self.assertEqual(state["cycles"][0]["review"]["sha256"], failed["sha256"])
        self.assertEqual(state["status"], "PLAN_ACCEPTED")
        self.assertEqual(state["plan"]["sha256"], state["cycles"][0]["plan"]["sha256"])
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "candidate",
                "--project",
                project,
                "--task-id",
                task_id,
                "--executor-receipt-id",
                first["executor_receipt_id"],
                "--evidence-record",
                first["evidence_records"][0],
            ).returncode,
            0,
        )
        (project / "tracked.txt").write_text("local repair\n")
        second = self.candidate_after_accepted_plan(project, task_id)
        self.assertNotEqual(second["id"], first["id"])
        passing = self.report(project, task_id, second)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_repeated_local_fixes_keep_original_planner_ancestry(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        original = self.candidate(project, task_id)
        for cycle in range(2):
            failed = self.fail_review(project, task_id, original)
            continuation = self.continuation(
                project, task_id, original, failed["sha256"], "LOCAL_FIX"
            )
            self.assertEqual(
                self.run_cli(
                    "ordinary",
                    "continue",
                    "--project",
                    project,
                    "--task-id",
                    task_id,
                    "--report",
                    continuation,
                ).returncode,
                0,
            )
            state = json.loads(
                (project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text()
            )
            self.assertEqual(state["plan"]["historical_origin"]["cycle_id"], 0)
            if cycle == 1:
                # Reproduce the bad 0.18.1 origin: current plan points to the
                # intermediate archived cycle whose plan points to cycle zero.
                state["plan"]["historical_origin"]["cycle_id"] = 1
                state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
                state_path.write_text(json.dumps(state))
                resumed = self.run_cli(
                    "ordinary", "resume", "--project", project, "--task-id", task_id
                )
                self.assertEqual(json.loads(resumed.stdout)["status"], "PLAN_ACCEPTED")
            (project / "tracked.txt").write_text(f"repair {cycle}\n")
            original = self.candidate_after_accepted_plan(project, task_id)
        passed = self.report(project, task_id, original)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passed,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_modern_unclassified_fail_is_rejected_on_submit_load_and_archive(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        report_path = self.report(project, task_id, candidate)
        original = state_path.read_bytes()
        report = json.loads(report_path.read_text())
        report.update({"VERDICT": "FAIL", "FINDINGS": ["The modern report has no classification."]})
        report_path.write_text(json.dumps(report))
        rejected = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            report_path,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(state_path.read_bytes(), original)

        report["CLASSIFICATION"] = "LOCAL_FIX"
        report_path.write_text(json.dumps(report))
        saved = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            report_path,
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        review_sha = json.loads(saved.stdout)["review"]["sha256"]
        state = json.loads(state_path.read_text())
        review = state["review"]
        review.pop("CLASSIFICATION")
        review_report = {
            key: item for key, item in review.items() if key not in {"sha256", "cycle_id"}
        }
        review["sha256"] = self.digest(review_report)
        state_path.write_text(json.dumps(state))
        before = state_path.read_bytes()
        continuation = self.continuation(project, task_id, candidate, review["sha256"], "LOCAL_FIX")
        rejected = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertNotEqual(rejected.returncode, 0)
        self.assertEqual(state_path.read_bytes(), before)

        state = json.loads(before)
        state["review"]["CLASSIFICATION"] = "LOCAL_FIX"
        state["review"]["sha256"] = review_sha
        state_path.write_text(json.dumps(state))
        continuation = self.continuation(project, task_id, candidate, review_sha, "LOCAL_FIX")
        continued = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        state = json.loads(state_path.read_text())
        archived = state["cycles"][0]
        archived_review = archived["review"]
        archived_review.pop("CLASSIFICATION")
        archived_report = {
            key: item for key, item in archived_review.items() if key not in {"sha256", "cycle_id"}
        }
        archived_review["sha256"] = self.digest(archived_report)
        payload = {key: item for key, item in archived.items() if key != "sha256"}
        archived["sha256"] = self.digest(payload)
        state["archive_head"] = archived["sha256"]
        state_path.write_text(json.dumps(state))
        resumed = self.run_cli("ordinary", "resume", "--project", project, "--task-id", task_id)
        self.assertEqual(json.loads(resumed.stdout)["status"], "ORDINARY_REVIEW_INVALID")

    def test_uppercase_review_cycle_mismatch_is_rejected_before_archive(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, candidate)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        review = state["review"]
        review["CYCLE_ID"] = 99
        report = {key: item for key, item in review.items() if key not in {"sha256", "cycle_id"}}
        review["sha256"] = self.digest(report)
        state_path.write_text(json.dumps(state))
        before = state_path.read_bytes()
        continuation = self.continuation(project, task_id, candidate, review["sha256"], "LOCAL_FIX")
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state_path.read_bytes(), before)

    def test_saved_local_omission_can_upgrade_to_systemic_replan(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        path = self.report(project, task_id, candidate)
        report = json.loads(path.read_text())
        report.update(
            {
                "VERDICT": "FAIL",
                "FINDINGS": ["Unmapped source outcome."],
                "CLASSIFICATION": "LOCAL_FIX",
            }
        )
        report["SOURCE_REVIEW"]["OMISSIONS"] = ["Honor the omitted source outcome."]
        path.write_text(json.dumps(report))
        saved = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            path,
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        failed = json.loads(saved.stdout)["review"]
        continuation = self.continuation(
            project, task_id, candidate, failed["sha256"], "SYSTEMIC_REPLAN"
        )
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        planner = self.run_cli(
            "ordinary", "dispatch", "--project", project, "--task-id", task_id, "--role", "planner"
        )
        self.assertEqual(planner.returncode, 0, planner.stderr)
        self.assertEqual(json.loads(planner.stdout)["REQUIREMENTS"][-1]["id"], "R3")
        self.assertEqual(
            json.loads(planner.stdout)["OMISSION_PROVENANCE"][0]["review_sha256"], failed["sha256"]
        )
        (project / "tracked.txt").write_text("first recovered plan\n")
        next_candidate = self.candidate(project, task_id)
        duplicate = self.report(project, task_id, next_candidate, ("R1", "R2", "R3"))
        report = json.loads(duplicate.read_text())
        report.update(
            {
                "VERDICT": "FAIL",
                "FINDINGS": ["The same source outcome remains unmapped."],
                "CLASSIFICATION": "SYSTEMIC_REPLAN",
            }
        )
        report["SOURCE_REVIEW"]["OMISSIONS"] = ["Honor the omitted source outcome."]
        duplicate.write_text(json.dumps(report))
        saved_again = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            duplicate,
        )
        self.assertEqual(saved_again.returncode, 0, saved_again.stderr)
        review_again = json.loads(saved_again.stdout)["review"]
        continued = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            self.continuation(
                project, task_id, next_candidate, review_again["sha256"], "SYSTEMIC_REPLAN"
            ),
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        matrix = json.loads(
            self.run_cli(
                "ordinary",
                "dispatch",
                "--project",
                project,
                "--task-id",
                task_id,
                "--role",
                "planner",
            ).stdout
        )
        self.assertEqual([item["id"] for item in matrix["REQUIREMENTS"]], ["R1", "R2", "R3"])
        self.assertEqual(len(matrix["OMISSION_PROVENANCE"]), 1)
        (project / "tracked.txt").write_text("second recovered plan\n")
        final_candidate = self.candidate(project, task_id)
        final_review = self.report(project, task_id, final_candidate, ("R1", "R2", "R3"))
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                final_review,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_owner_decision_omission_cannot_upgrade(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        path = self.report(project, task_id, candidate)
        report = json.loads(path.read_text())
        report.update(
            {
                "VERDICT": "FAIL",
                "FINDINGS": ["Owner choice required."],
                "CLASSIFICATION": "OWNER_DECISION_REQUIRED",
            }
        )
        report["SOURCE_REVIEW"]["OMISSIONS"] = [
            "An additional requirement needs an owner decision."
        ]
        path.write_text(json.dumps(report))
        saved = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            path,
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        failed = json.loads(saved.stdout)["review"]
        continuation = self.continuation(
            project, task_id, candidate, failed["sha256"], "SYSTEMIC_REPLAN"
        )
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        before = state_path.read_bytes()
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state_path.read_bytes(), before)

    def test_missing_evidence_rejects_continue_without_mutation(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, candidate)
        evidence = pathlib.Path(candidate["evidence_records"][0])
        backup = evidence.read_bytes()
        continuation = self.continuation(project, task_id, candidate, failed["sha256"], "LOCAL_FIX")
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        before = state_path.read_bytes()
        evidence.unlink()
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state_path.read_bytes(), before)
        evidence.write_bytes(backup)

    def test_review_ready_systemic_stale_change_gets_fresh_systemic_plan(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        (project / "tracked.txt").write_text("systemic change after review-ready\n")
        continuation = {
            "TASK_ID": task_id,
            "CYCLE_ID": 0,
            "ACTION": "STALE_CANDIDATE",
            "REASON": "The changed owned files invalidate the candidate.",
            "EVIDENCE_RECORDS": candidate["evidence_records"],
            "NEXT_ACTION": "Replan the affected implementation.",
            "EXPECTED_RESULT": "A fresh systemic plan and candidate.",
            "COST_RATIONALE": "The change exceeds local repair assumptions.",
            "PREVIOUS_REVIEW_SHA256": None,
            "STALE_SCOPE": "SYSTEMIC_CHANGE",
        }
        path = project / ".codex" / "checks" / "review-ready-stale.json"
        path.write_text(json.dumps(continuation))
        result = self.run_cli(
            "ordinary", "continue", "--project", project, "--task-id", task_id, "--report", path
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        self.assertEqual(state["replan_action"], "SYSTEMIC_REPLAN")
        next_candidate = self.candidate(project, task_id)
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        planner_route = [
            item["prepared"]["route"]
            for item in state["issued_dispatches"]
            if item["role"] == "planner"
        ][-1]
        self.assertEqual(planner_route["TASK_CLASS"], "SYSTEMIC_REPLAN")
        passing = self.report(project, task_id, next_candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_corrupt_archived_plan_provenance_blocks_recovery_without_mutation(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        first = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, first)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "continue",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                self.continuation(project, task_id, first, failed["sha256"], "LOCAL_FIX"),
            ).returncode,
            0,
        )
        (project / "tracked.txt").write_text("next local candidate\n")
        second = self.candidate_after_accepted_plan(project, task_id)
        failed = self.fail_review(project, task_id, second)
        continued = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            self.continuation(project, task_id, second, failed["sha256"], "LOCAL_FIX"),
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        (project / "tracked.txt").write_text("third local candidate\n")
        third = self.candidate_after_accepted_plan(project, task_id)
        failed = self.fail_review(project, task_id, third)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        state["cycles"][1]["plan"]["historical_origin"]["plan_sha256"] = "f" * 64
        state["plan"]["historical_origin"]["cycle_id"] = 1
        previous = None
        for cycle in state["cycles"]:
            cycle["previous_sha256"] = previous
            archived = {key: item for key, item in cycle.items() if key != "sha256"}
            cycle["sha256"] = self.digest(archived)
            previous = cycle["sha256"]
        state["archive_head"] = previous
        state_path.write_text(json.dumps(state))
        before = state_path.read_bytes()
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            self.continuation(project, task_id, third, failed["sha256"], "LOCAL_FIX"),
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(state_path.read_bytes(), before)

    def test_true_legacy_018_fail_continues_through_fresh_review_and_accept(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, candidate)
        state_path, candidate, review = self.convert_to_legacy_018(project, task_id)
        self.attach_legacy_routing(project, task_id)
        continuation = {
            "TASK_ID": task_id,
            "CYCLE_ID": 0,
            "ACTION": "LOCAL_FIX",
            "REASON": "Apply the explicit local correction.",
            "EVIDENCE_RECORDS": candidate["evidence_records"],
            "NEXT_ACTION": "Run a fresh candidate and independent review.",
            "EXPECTED_RESULT": "The current accepted plan passes.",
            "COST_RATIONALE": "The accepted implementation change is bounded.",
            "PREVIOUS_REVIEW_SHA256": review["sha256"],
        }
        path = project / ".codex" / "checks" / "legacy-continuation.json"
        path.write_text(json.dumps(continuation))
        result = self.run_cli(
            "ordinary", "continue", "--project", project, "--task-id", task_id, "--report", path
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        archived = json.loads(state_path.read_text())["cycles"][0]
        self.assertNotIn("cycle_id", archived["candidate"])
        self.assertNotIn("CYCLE_ID", archived["review"])
        (project / "tracked.txt").write_text("legacy repair\n")
        next_candidate = self.candidate_after_accepted_plan(project, task_id)
        passing = self.report(project, task_id, next_candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_legacy_stale_without_file_baseline_rejects_without_mutation(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        self.fail_review(project, task_id, candidate)
        state_path, candidate, review = self.convert_to_legacy_018(project, task_id)
        self.attach_legacy_routing(project, task_id)
        (project / "tracked.txt").write_text("changed with no baseline\n")
        continuation = {
            "TASK_ID": task_id,
            "CYCLE_ID": 0,
            "ACTION": "STALE_CANDIDATE",
            "REASON": "Classify the changed candidate.",
            "EVIDENCE_RECORDS": candidate["evidence_records"],
            "NEXT_ACTION": "Recheck current files.",
            "EXPECTED_RESULT": "A safely classified change.",
            "COST_RATIONALE": "State identity must remain verifiable.",
            "PREVIOUS_REVIEW_SHA256": review["sha256"],
            "STALE_SCOPE": "LOCAL_CHANGE",
        }
        path = project / ".codex" / "checks" / "legacy-stale.json"
        path.write_text(json.dumps(continuation))
        before = state_path.read_bytes()
        result = self.run_cli(
            "ordinary", "continue", "--project", project, "--task-id", task_id, "--report", path
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("file-state baseline", result.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_source_omission_is_recovered_only_through_systemic_plan(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        original_source = json.loads(state_path.read_text())["source"]
        path = self.report(project, task_id, candidate)
        report = json.loads(path.read_text())
        report.update(
            {
                "VERDICT": "FAIL",
                "FINDINGS": ["Source outcome was not planned."],
                "CLASSIFICATION": "SYSTEMIC_REPLAN",
            }
        )
        report["SOURCE_REVIEW"]["OMISSIONS"] = ["Preserve the source's additional outcome."]
        path.write_text(json.dumps(report))
        saved = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            path,
        )
        self.assertEqual(saved.returncode, 0, saved.stderr)
        failed = json.loads(saved.stdout)["review"]
        continuation = self.continuation(
            project, task_id, candidate, failed["sha256"], "SYSTEMIC_REPLAN"
        )
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = self.run_cli(
            "ordinary", "dispatch", "--project", project, "--task-id", task_id, "--role", "planner"
        )
        self.assertEqual(payload.returncode, 0, payload.stderr)
        planning = json.loads(payload.stdout)
        recovered = planning["REQUIREMENTS"][-1]
        self.assertEqual(
            recovered, {"id": "R3", "statement": "Preserve the source's additional outcome."}
        )
        self.assertEqual(planning["OMISSION_PROVENANCE"][0]["id"], "R3")
        self.assertEqual(
            planning["LIFECYCLE_BINDING"]["effective_requirements_sha256"],
            hashlib.sha256(
                json.dumps(
                    planning["REQUIREMENTS"],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        )
        (project / "tracked.txt").write_text("replanned to include omission\n")
        planner = self.spawn(project, task_id, "planner", "planner-missing-coverage")
        incomplete = project / ".codex" / "checks" / "incomplete-plan.json"
        state = json.loads(state_path.read_text())
        incomplete.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "SPAWN_RECEIPT_ID": planner["id"],
                    "REQUIREMENT_IDS": ["R1", "R2"],
                    "STEPS": ["Implement the plan."],
                    "OWNED_SCOPE": ["tracked.txt"],
                    "VERIFICATION": ["Run checks."],
                    "ROUTING": state["routing"],
                }
            )
        )
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "plan-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                incomplete,
            ).returncode,
            0,
        )
        candidate = self.candidate(project, task_id)
        missing_review = self.report(project, task_id, candidate, ("R1", "R2"))
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                missing_review,
            ).returncode,
            0,
        )
        passing = self.report(project, task_id, candidate, ("R1", "R2", "R3"))
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )
        state = json.loads(state_path.read_text())
        for issued in state["issued_dispatches"]:
            if issued["cycle_id"] == state["cycle_id"] and issued["role"] in {
                "executor",
                "reviewer",
            }:
                self.assertIn('"R3"', issued["prepared"]["spawn"]["message"])
        self.assertEqual(json.loads(state_path.read_text())["source"], original_source)

    def test_plan_uses_its_reported_planner_receipt_not_latest_role_receipt(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        first = self.spawn(project, task_id, "planner", "planner-author-a")
        self.spawn(project, task_id, "planner", "planner-later-b")
        plan_path = project / ".codex" / "checks" / "author-plan.json"
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        plan_path.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "SPAWN_RECEIPT_ID": first["id"],
                    "REQUIREMENT_IDS": ["R1", "R2"],
                    "STEPS": ["Use the bounded change."],
                    "OWNED_SCOPE": ["tracked.txt"],
                    "VERIFICATION": ["Run the targeted check."],
                    "ROUTING": state["routing"],
                }
            )
        )
        planned = self.run_cli(
            "ordinary",
            "plan-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            plan_path,
        )
        self.assertEqual(planned.returncode, 0, planned.stderr)
        plan = json.loads(planned.stdout)["plan"]
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "plan-accept",
                "--project",
                project,
                "--task-id",
                task_id,
                "--plan-sha256",
                plan["sha256"],
            ).returncode,
            0,
        )
        check = self.run_cli(
            "run-check",
            "--project",
            project,
            "--task-id",
            task_id,
            "--name",
            "author-plan",
            "--",
            sys.executable,
            "-c",
            "print('ok')",
        )
        executor = self.spawn(project, task_id, "executor", "executor-a")
        result = self.run_cli(
            "ordinary",
            "candidate",
            "--project",
            project,
            "--task-id",
            task_id,
            "--executor-receipt-id",
            executor["id"],
            "--evidence-record",
            check.stdout.splitlines()[0],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["candidate"]["planner_receipt_id"], first["id"])

    def test_systemic_replan_and_stale_recovery_preserve_history(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        self.add_path_requirement(project)
        first = self.candidate(project, task_id, planner_scope=["tracked.txt"])
        failed = self.fail_review(project, task_id, first, "SYSTEMIC_REPLAN")
        continuation = self.continuation(
            project, task_id, first, failed["sha256"], "SYSTEMIC_REPLAN"
        )
        result = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            continuation,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        self.assertEqual(state["replan_action"], "SYSTEMIC_REPLAN")
        (project / "tracked.txt").write_text("replanned\n")
        second = self.candidate(project, task_id, planner_scope=["tracked.txt"])
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        hard_replan = [
            item["prepared"]["route"]
            for item in state["issued_dispatches"]
            if item["role"] == "planner"
        ][-1]
        self.assertEqual(hard_replan["TASK_CLASS"], "SYSTEMIC_REPLAN")
        self.assertEqual(hard_replan["MODEL_CLASS"], "strong")
        self.assertEqual(hard_replan["REASONING_EFFORT"], "high")
        passing = self.report(project, task_id, second)
        reviewed = self.run_cli(
            "ordinary",
            "review-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            passing,
        )
        self.assertEqual(reviewed.returncode, 0, reviewed.stderr)
        (project / "tracked.txt").write_text("changed after candidate\n")
        resumed = self.run_cli("ordinary", "resume", "--project", project, "--task-id", task_id)
        self.assertEqual(json.loads(resumed.stdout)["status"], "ORDINARY_CANDIDATE_STALE")
        stale = self.continuation(
            project,
            task_id,
            second,
            json.loads(reviewed.stdout)["review"]["sha256"],
            "STALE_CANDIDATE",
            stale_scope="LOCAL_CHANGE",
        )
        result = self.run_cli(
            "ordinary", "continue", "--project", project, "--task-id", task_id, "--report", stale
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        self.assertEqual(len(state["cycles"]), 2)
        fresh_candidate = self.candidate(project, task_id, planner_scope=["tracked.txt"])
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        self.assertEqual(state["status"], "REVIEW_READY")
        self.assertEqual(
            state["candidate"]["planner_receipt_id"], state["plan"]["report"]["SPAWN_RECEIPT_ID"]
        )
        self.assertEqual(state["planner_requirements_scope"], ["tracked.txt"])
        historical_scope = state["issued_dispatches"][0]["requirements_binding"]
        self.assertEqual(historical_scope["authority"], "planner_declaration")
        self.assertEqual(historical_scope["scope_paths"], ["tracked.txt"])
        passing = self.report(project, task_id, fresh_candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_same_executor_followup_is_not_a_spawn_and_foreign_stale_is_blocked(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        self.add_path_requirement(project)
        first = self.candidate(project, task_id, planner_scope=["tracked.txt"])
        failed = self.fail_review(project, task_id, first)
        continuation = self.continuation(project, task_id, first, failed["sha256"], "LOCAL_FIX")
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "continue",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                continuation,
            ).returncode,
            0,
        )
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        missing = project / ".codex" / "checks" / "missing-followup.json"
        missing.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "CYCLE_ID": state["cycle_id"],
                    "ORIGIN_RECEIPT_ID": state["followup"]["origin_receipt_id"],
                    "AGENT_ID": state["followup"]["origin_agent_id"],
                    "FOLLOWUP_SUCCESS": True,
                    "FOLLOWUP_PAYLOAD_SHA256": None,
                }
            )
        )
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "followup-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                missing,
            ).returncode,
            0,
        )
        payload = self.run_cli(
            "ordinary", "executor-followup", "--project", project, "--task-id", task_id
        )
        self.assertEqual(payload.returncode, 0, payload.stderr)
        followup = json.loads(payload.stdout)
        report = project / ".codex" / "checks" / "followup.json"
        report.write_text(
            json.dumps(
                {
                    "TASK_ID": task_id,
                    "CYCLE_ID": followup["CYCLE_ID"],
                    "ORIGIN_RECEIPT_ID": followup["ORIGIN_RECEIPT_ID"],
                    "AGENT_ID": followup["AGENT_ID"],
                    "FOLLOWUP_SUCCESS": True,
                    "FOLLOWUP_PAYLOAD_SHA256": hashlib.sha256(
                        json.dumps(
                            followup, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                }
            )
        )
        observed = self.run_cli(
            "ordinary",
            "followup-report",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            report,
        )
        self.assertEqual(observed.returncode, 0, observed.stderr)
        receipt = json.loads(observed.stdout)["receipt"]
        self.assertTrue(receipt["CONTINUATION"])
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        self.assertEqual(state["cycles"][0]["planner_requirements_scope"], ["tracked.txt"])
        self.assertEqual(state["plan"]["report"]["OWNED_SCOPE"], ["tracked.txt"])
        (project / "tracked.txt").write_text("followup implementation\n")
        next_candidate = self.candidate_after_accepted_plan(project, task_id, "executor-a")
        passing = self.report(project, task_id, next_candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_three_systemic_replans_route_reviewer_retry_and_accept(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        for attempt in range(3):
            failed = self.fail_review(project, task_id, candidate, "SYSTEMIC_REPLAN")
            continuation = self.continuation(
                project, task_id, candidate, failed["sha256"], "SYSTEMIC_REPLAN"
            )
            result = self.run_cli(
                "ordinary",
                "continue",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                continuation,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            (project / "tracked.txt").write_text(f"systemic plan {attempt}\n")
            candidate = self.candidate(project, task_id)
        passing = self.report(project, task_id, candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        reviewer_route = [
            item["prepared"]["route"]
            for item in state["issued_dispatches"]
            if item["role"] == "reviewer"
        ][-1]
        self.assertEqual(reviewer_route["SELECTION_REASON"], "repeated_failure")
        self.assertEqual(reviewer_route["REASONING_EFFORT"], "high")
        for item in state["issued_dispatches"]:
            if item["role"] == "planner" and item["task_class"] == "SYSTEMIC_REPLAN":
                self.assertEqual(item["prepared"]["route"]["SELECTION_REASON"], "systemic_replan")
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_systemic_then_two_local_fixes_preserve_latest_plan_author(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        failed = self.fail_review(project, task_id, candidate, "SYSTEMIC_REPLAN")
        continued = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            self.continuation(project, task_id, candidate, failed["sha256"], "SYSTEMIC_REPLAN"),
        )
        self.assertEqual(continued.returncode, 0, continued.stderr)
        (project / "tracked.txt").write_text("systemic result\n")
        candidate = self.candidate(project, task_id)
        state = json.loads((project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text())
        systemic_planner = [r for r in state["spawn_receipts"] if r["ROLE"] == "planner"][-1]
        for index in range(2):
            failed = self.fail_review(project, task_id, candidate)
            continued = self.run_cli(
                "ordinary",
                "continue",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                self.continuation(project, task_id, candidate, failed["sha256"], "LOCAL_FIX"),
            )
            self.assertEqual(continued.returncode, 0, continued.stderr)
            state = json.loads(
                (project / ".codex" / "tasks" / f"{task_id}.ordinary.json").read_text()
            )
            self.assertEqual(
                state["plan"]["historical_origin"]["planner_receipt_id"], systemic_planner["id"]
            )
            (project / "tracked.txt").write_text(f"local result {index}\n")
            candidate = self.candidate_after_accepted_plan(project, task_id)
        passing = self.report(project, task_id, candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                passing,
            ).returncode,
            0,
        )
        self.assertEqual(
            self.run_cli(
                "ordinary", "accept", "--project", project, "--task-id", task_id
            ).returncode,
            0,
        )

    def test_routing_uses_independent_risk_and_complexity_dimensions(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        routing = {
            "risk": "high",
            "decision_complexity": "NORMAL",
            "implementation_complexity": "COMPLEX",
            "review_complexity": "COMPLEX",
        }
        task_id = self.init(project, routing=routing)
        candidate = self.candidate(project, task_id)
        state_path = project / ".codex" / "tasks" / f"{task_id}.ordinary.json"
        state = json.loads(state_path.read_text())
        planner_route = next(
            item["prepared"]["route"]
            for item in state["issued_dispatches"]
            if item["role"] == "planner"
        )
        executor_route = next(
            item["prepared"]["route"]
            for item in state["issued_dispatches"]
            if item["role"] == "executor"
        )
        self.assertEqual(planner_route["REASONING_EFFORT"], "medium")
        self.assertEqual(executor_route["MODEL_CLASS"], "bounded")
        self.assertEqual(executor_route["REASONING_EFFORT"], "medium")
        review = self.report(project, task_id, candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                review,
            ).returncode,
            0,
        )
        state = json.loads(state_path.read_text())
        reviewer_route = [
            item["prepared"]["route"]
            for item in state["issued_dispatches"]
            if item["role"] == "reviewer"
        ][-1]
        self.assertEqual(reviewer_route["REASONING_EFFORT"], "high")
        self.assertEqual(reviewer_route["SELECTION_REASON"], "high_risk_review")
        td2, second = self.project()
        self.addCleanup(td2.cleanup)
        second_id = self.init(
            second,
            routing={
                "risk": "low",
                "decision_complexity": "DEEP",
                "implementation_complexity": "MECHANICAL",
                "review_complexity": "COMPLEX",
            },
        )
        second_candidate = self.candidate(second, second_id)
        second_review = self.report(second, second_id, second_candidate)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "review-report",
                "--project",
                second,
                "--task-id",
                second_id,
                "--report",
                second_review,
            ).returncode,
            0,
        )
        second_state = json.loads(
            (second / ".codex" / "tasks" / f"{second_id}.ordinary.json").read_text()
        )
        complex_review = [
            item["prepared"]["route"]
            for item in second_state["issued_dispatches"]
            if item["role"] == "reviewer"
        ][-1]
        self.assertEqual(complex_review["SELECTION_REASON"], "complex_review")
        self.assertEqual(complex_review["REASONING_EFFORT"], "high")
        deep_planner = next(
            item["prepared"]["route"]
            for item in second_state["issued_dispatches"]
            if item["role"] == "planner"
        )
        self.assertEqual(deep_planner["MODEL_CLASS"], "deep")
        self.assertEqual(deep_planner["REASONING_EFFORT"], "high")
        td3, unavailable = self.project()
        self.addCleanup(td3.cleanup)
        unavailable_id = self.init(unavailable)
        blocked_runtime = unavailable / ".codex" / "checks" / "blocked-runtime.json"
        blocked_runtime.parent.mkdir(parents=True, exist_ok=True)
        blocked_runtime.write_text(
            json.dumps(
                {
                    "explicit_model_effort": True,
                    "generic_available": True,
                    "models": [
                        {
                            "id": "bounded-test",
                            "capabilities": ["bounded"],
                            "efforts": ["medium"],
                            "priority": 1,
                        }
                    ],
                }
            )
        )
        blocked = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            unavailable,
            "--task-id",
            unavailable_id,
            "--role",
            "planner",
            "--runtime",
            blocked_runtime,
        )
        self.assertNotEqual(blocked.returncode, 0)
        self.assertIn("AGENT_RUNTIME_REQUIRED", blocked.stderr)

    def test_foreign_stale_change_and_missing_routing_are_never_auto_accepted(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        context = json.loads(self.context(project).read_text())
        context.pop("routing")
        missing = project / "missing-routing.json"
        missing.write_text(json.dumps(context))
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "init",
                "--project",
                project,
                "--task-id",
                "legacy",
                "--chat-context",
                missing,
            ).returncode,
            0,
        )
        runtime = project / ".codex" / "checks" / "legacy-runtime.json"
        runtime.parent.mkdir(parents=True, exist_ok=True)
        runtime.write_text(
            json.dumps(
                {
                    "explicit_model_effort": True,
                    "generic_available": True,
                    "models": [
                        {
                            "id": "strong-test",
                            "capabilities": ["strong"],
                            "efforts": ["medium"],
                            "priority": 1,
                        }
                    ],
                }
            )
        )
        self.assertNotEqual(
            self.run_cli(
                "ordinary",
                "issue-dispatch",
                "--project",
                project,
                "--task-id",
                "legacy",
                "--role",
                "planner",
                "--runtime",
                runtime,
            ).returncode,
            0,
        )
        assessment = project / "routing.json"
        assessment.write_text(
            json.dumps(
                {
                    "risk": "low",
                    "decision_complexity": "NORMAL",
                    "implementation_complexity": "NORMAL",
                    "review_complexity": "NORMAL",
                }
            )
        )
        attached = self.run_cli(
            "ordinary",
            "attach-routing",
            "--project",
            project,
            "--task-id",
            "legacy",
            "--routing-assessment",
            assessment,
        )
        self.assertEqual(attached.returncode, 0, attached.stderr)
        issued = self.run_cli(
            "ordinary",
            "issue-dispatch",
            "--project",
            project,
            "--task-id",
            "legacy",
            "--role",
            "planner",
            "--runtime",
            runtime,
        )
        self.assertEqual(issued.returncode, 0, issued.stderr)
        task_id = self.init(project)
        candidate = self.candidate(project, task_id)
        (project / "foreign.txt").write_text("external change\n")
        stale = self.continuation(
            project, task_id, candidate, None, "STALE_CANDIDATE", stale_scope="FOREIGN_CHANGE"
        )
        result = self.run_cli(
            "ordinary", "continue", "--project", project, "--task-id", task_id, "--report", stale
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ORDINARY_FOREIGN_CHANGE", result.stderr)

    def test_loop_guard_rejects_repeat_without_new_candidate_evidence(self):
        td, project = self.project()
        self.addCleanup(td.cleanup)
        task_id = self.init(project)
        first = self.candidate(project, task_id)
        first_fail = self.fail_review(project, task_id, first)
        self.assertEqual(
            self.run_cli(
                "ordinary",
                "continue",
                "--project",
                project,
                "--task-id",
                task_id,
                "--report",
                self.continuation(project, task_id, first, first_fail["sha256"], "LOCAL_FIX"),
            ).returncode,
            0,
        )
        # No file/check-output change: a second failure has no discriminating evidence.
        second = self.candidate_after_accepted_plan(project, task_id)
        second_fail = self.fail_review(project, task_id, second)
        repeated = self.run_cli(
            "ordinary",
            "continue",
            "--project",
            project,
            "--task-id",
            task_id,
            "--report",
            self.continuation(project, task_id, second, second_fail["sha256"], "LOCAL_FIX"),
        )
        self.assertNotEqual(repeated.returncode, 0)
        self.assertIn("ORDINARY_LOOP_GUARD", repeated.stderr)
