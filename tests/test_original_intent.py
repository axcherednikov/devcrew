import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from hooks import user_prompt_submit


class PromptCaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.plugin_data = self.root / "plugin-data"
        self.plugin_data.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.event = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": str(self.project),
            "prompt": "  Recommend a card when СБП is blocked.\n",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_capture_identity_and_claim(self):
        identity = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=100
        )
        record = user_prompt_submit.claim_prompt(
            self.plugin_data, identity["source_id"], expected_sha256=identity["sha256"], now=101
        )
        prompt_bytes = self.event["prompt"].encode("utf-8")
        self.assertNotIn("prompt", record)
        self.assertEqual(record["sha256"], hashlib.sha256(prompt_bytes).hexdigest())
        self.assertEqual(
            record["provenance"],
            {
                "kind": "user_prompt_submit",
                "session_id": "session-1",
                "turn_id": "turn-1",
                "project": str(self.project.resolve()),
            },
        )
        self.assertEqual(identity["source_type"], "user_prompt_digest")

    def test_identical_replay_is_idempotent_but_conflicting_replay_is_rejected(self):
        first = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=100
        )
        replay = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=200
        )
        self.assertEqual(first, replay)
        changed = dict(self.event, prompt="Different source")
        with self.assertRaisesRegex(ValueError, "conflicting replay"):
            user_prompt_submit.capture_prompt(self.plugin_data, changed, self.project, now=200)
        record = user_prompt_submit.claim_prompt(self.plugin_data, first["source_id"], now=201)
        self.assertNotIn("prompt", record)

    def test_claim_expires_and_digest_mismatch_fails_closed(self):
        identity = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=100
        )
        with self.assertRaisesRegex(ValueError, "digest"):
            user_prompt_submit.claim_prompt(
                self.plugin_data, identity["source_id"], expected_sha256="0" * 64, now=101
            )
        self.assertIsNone(
            user_prompt_submit.claim_prompt(
                self.plugin_data, identity["source_id"], now=100 + user_prompt_submit.TTL_SECONDS
            )
        )
        store_path = self.plugin_data / "devcrew-original-intent" / user_prompt_submit.STORE_NAME
        self.assertNotIn(identity["source_id"], json.loads(store_path.read_text())["entries"])

    def test_claim_recomputes_prompt_digest_and_provenance_identity(self):
        identity = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=100
        )
        store_path = self.plugin_data / "devcrew-original-intent" / user_prompt_submit.STORE_NAME
        original = json.loads(store_path.read_text())
        tampered = json.loads(json.dumps(original))
        tampered["entries"][identity["source_id"]]["prompt"] = "tampered"
        store_path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "receipt schema"):
            user_prompt_submit.claim_prompt(self.plugin_data, identity["source_id"], now=101)

        tampered = json.loads(json.dumps(original))
        tampered["entries"][identity["source_id"]]["provenance"]["project"] = "/different/project"
        store_path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "source identity"):
            user_prompt_submit.claim_prompt(self.plugin_data, identity["source_id"], now=101)

        tampered = json.loads(json.dumps(original))
        tampered["entries"][identity["source_id"]]["source_id"] = "0" * 64
        store_path.write_text(json.dumps(tampered))
        with self.assertRaisesRegex(ValueError, "source identity"):
            user_prompt_submit.claim_prompt(self.plugin_data, identity["source_id"], now=101)

    def test_concurrent_identical_capture_is_idempotent(self):
        def capture(_):
            return user_prompt_submit.capture_prompt(
                self.plugin_data, self.event, self.project, now=100
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            identities = list(pool.map(capture, range(16)))
        self.assertTrue(all(item == identities[0] for item in identities))
        record = user_prompt_submit.claim_prompt(
            self.plugin_data, identities[0]["source_id"], now=101
        )
        self.assertNotIn("prompt", record)

    def test_capture_queue_cleans_expired_entries_and_enforces_count(self):
        old_limit = user_prompt_submit.MAX_ENTRIES
        user_prompt_submit.MAX_ENTRIES = 2
        try:
            expired = user_prompt_submit.capture_prompt(
                self.plugin_data, self.event, self.project, now=100
            )
            for index in range(3):
                event = dict(self.event, turn_id=f"turn-{index + 2}", prompt=f"task {index}")
                user_prompt_submit.capture_prompt(
                    self.plugin_data, event, self.project, now=100 + index + 1
                )
            store_path = (
                self.plugin_data / "devcrew-original-intent" / user_prompt_submit.STORE_NAME
            )
            entries = json.loads(store_path.read_text())["entries"]
            self.assertEqual(len(entries), 2)
            self.assertNotIn(expired["source_id"], entries)
            self.assertLessEqual(store_path.stat().st_size, user_prompt_submit.MAX_STORE_BYTES)
        finally:
            user_prompt_submit.MAX_ENTRIES = old_limit

    def test_capture_queue_enforces_total_byte_limit_by_evicting_oldest(self):
        old_limit = user_prompt_submit.MAX_STORE_BYTES
        user_prompt_submit.MAX_STORE_BYTES = 900
        try:
            first = dict(self.event, prompt="a" * 700)
            first_id = user_prompt_submit.capture_prompt(
                self.plugin_data, first, self.project, now=100
            )
            second = dict(self.event, turn_id="turn-2", prompt="b" * 700)
            second_id = user_prompt_submit.capture_prompt(
                self.plugin_data, second, self.project, now=101
            )
            store_path = (
                self.plugin_data / "devcrew-original-intent" / user_prompt_submit.STORE_NAME
            )
            entries = json.loads(store_path.read_text())["entries"]
            self.assertNotIn(first_id["source_id"], entries)
            self.assertIn(second_id["source_id"], entries)
            self.assertLessEqual(store_path.stat().st_size, old_limit)
        finally:
            user_prompt_submit.MAX_STORE_BYTES = old_limit

    def test_main_emits_capture_metadata_and_preserves_learning_context(self):
        plugin_data = self.root / "main-plugin-data"
        plugin_data.mkdir()
        env = dict(os.environ, PLUGIN_DATA=str(plugin_data))
        cli = pathlib.Path(__file__).resolve().parents[1] / "scripts/devcrew.py"
        project = self.project
        subprocess.run(
            [sys.executable, "-B", str(cli), "init", "--project", str(project)],
            capture_output=True,
            check=True,
        )
        event = dict(self.event, cwd=str(project))
        proc = subprocess.run(
            [sys.executable, str(pathlib.Path(user_prompt_submit.__file__))],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            cwd=project,
            env=env,
            check=True,
        )
        result = json.loads(proc.stdout)
        additional = result["hookSpecificOutput"]["additionalContext"]
        self.assertIn("DEV CREW PROJECT LEARNING", additional)
        self.assertIn("If the user writes in Russian, reply in Russian", additional)
        self.assertIn("ORIGINAL INTENT RECEIPT (metadata only", additional)
        self.assertIn(hashlib.sha256(event["prompt"].encode("utf-8")).hexdigest(), additional)
        metadata = json.loads(
            additional.split("TRUSTED_EVENT_METADATA (data): ", 1)[1].splitlines()[0]
        )
        self.assertEqual(metadata["python"], sys.executable)
        read = subprocess.run(
            [
                metadata["python"],
                "-X",
                "utf8",
                "-B",
                metadata["cli"],
                "requirements-context",
                "--project",
                metadata["project"],
                "--role",
                "executor",
                "--task-class",
                "EXECUTOR",
                "--paths",
                "src",
            ],
            capture_output=True,
            check=True,
            encoding="utf-8",
        )
        self.assertIn("requirements", json.loads(read.stdout))

    def test_main_respects_learning_opt_out(self):
        project = self.root / "opted-out-project"
        codex = project / ".codex"
        codex.mkdir(parents=True)
        (codex / "devcrew.json").write_text("{}")
        (codex / "devcrew-learning.json").write_text('{"enabled": false}')
        env = dict(os.environ)
        env.pop("PLUGIN_DATA", None)
        proc = subprocess.run(
            [sys.executable, str(pathlib.Path(user_prompt_submit.__file__))],
            input="not even parsed when opted out",
            text=True,
            capture_output=True,
            cwd=project,
            env=env,
            check=True,
        )
        self.assertEqual(proc.stdout, "")
        self.assertFalse((project / "devcrew-original-intent").exists())

    def test_capture_is_private_bounded_and_rejects_oversize_prompt(self):
        identity = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=100
        )
        directory = self.plugin_data / "devcrew-original-intent"
        store = directory / user_prompt_submit.STORE_NAME
        self.assertTrue(user_prompt_submit.fs.has_private_mode(directory, 0o700))
        self.assertTrue(user_prompt_submit.fs.has_private_mode(store, 0o600))
        self.assertLessEqual(store.stat().st_size, user_prompt_submit.MAX_STORE_BYTES)
        too_large = dict(
            self.event, turn_id="turn-2", prompt="x" * (user_prompt_submit.INPUT_LIMIT + 1)
        )
        with self.assertRaisesRegex(ValueError, "1 MiB"):
            user_prompt_submit.capture_prompt(self.plugin_data, too_large, self.project, now=100)
        self.assertIsNotNone(
            user_prompt_submit.claim_prompt(self.plugin_data, identity["source_id"], now=101)
        )

    def test_store_schema_contains_only_capture_records(self):
        identity = user_prompt_submit.capture_prompt(
            self.plugin_data, self.event, self.project, now=100
        )
        path = self.plugin_data / "devcrew-original-intent" / user_prompt_submit.STORE_NAME
        store = json.loads(path.read_text())
        self.assertEqual(store["schema_version"], user_prompt_submit.STORE_SCHEMA)
        record = store["entries"][identity["source_id"]]
        self.assertEqual(
            set(record),
            {
                "source_id",
                "source_type",
                "sha256",
                "confirmation",
                "provenance",
                "captured_at",
                "expires_at",
            },
        )
        self.assertNotIn("transcript_path", record)


class IntentSourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.task_root = self.root / "task"
        self.task_root.mkdir()
        self.receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "task_id": "T01",
            "task_worktree_root": str(self.task_root),
        }
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_phase

        self.phase = devcrew_phase

    def tearDown(self):
        self.temp.cleanup()

    def test_file_source_is_snapshotted_and_replay_is_stable_after_input_changes(self):
        source = self.root / "attachment.bin"
        original = b"exact\x00bytes\r\n"
        source.write_bytes(original)
        spec = {
            "mode": "file",
            "source_type": "attachment",
            "path": str(source),
            "expected_sha256": hashlib.sha256(original).hexdigest(),
            "origin": "provided attachment",
        }
        record = self.phase.bind_intent_source(self.project, self.receipt, spec)
        snapshot = self.task_root / record["snapshot"]
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertTrue(user_prompt_submit.fs.has_private_mode(snapshot, 0o600))
        source.write_bytes(b"changed later")
        with self.assertRaisesRegex(self.phase.PhaseError, "INTENT_SOURCE_DIGEST_MISMATCH"):
            self.phase.bind_intent_source(self.project, self.receipt, spec)
        self.assertEqual(snapshot.read_bytes(), original)

    def test_unavailable_source_is_explicit_and_has_stable_identity(self):
        spec = {
            "mode": "unavailable",
            "source_type": "user_prompt",
            "origin": "original request",
            "reason": "prompt capture had expired",
        }
        first = self.phase.bind_intent_source(self.project, self.receipt, spec)
        second = self.phase.bind_intent_source(self.project, self.receipt, spec)
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "UNVERIFIED")
        self.assertIsNone(first["snapshot"])
        self.assertIsNone(first["sha256"])

    def test_full_prompt_capture_binding_is_forbidden(self):
        with self.assertRaisesRegex(self.phase.PhaseError, "INTENT_FULL_MESSAGE_FORBIDDEN"):
            self.phase.bind_intent_source(self.project, self.receipt, {"mode": "capture"})
        self.assertFalse((self.task_root / ".codex/tasks/T01/sources").exists())

    def test_source_bind_rejects_digest_mismatch_and_symlink_input(self):
        source = self.root / "source.txt"
        source.write_text("original", encoding="utf-8")
        spec = {
            "mode": "file",
            "source_type": "document",
            "path": str(source),
            "expected_sha256": "0" * 64,
            "origin": "ticket T-1",
        }
        with self.assertRaisesRegex(self.phase.PhaseError, "INTENT_SOURCE_DIGEST_MISMATCH"):
            self.phase.bind_intent_source(self.project, self.receipt, spec)
        link = self.root / "linked.txt"
        link.symlink_to(source)
        spec.update(path=str(link), expected_sha256=hashlib.sha256(source.read_bytes()).hexdigest())
        with self.assertRaisesRegex(self.phase.PhaseError, "UNSAFE_INTENT_SOURCE_PATH"):
            self.phase.bind_intent_source(self.project, self.receipt, spec)

    def test_source_bind_cli_runs_before_manifest_init_and_returns_metadata_only(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.project), *args], check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "-q")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        (self.project / "README").write_text("registered task\n")
        git("add", "README")
        git("commit", "-qm", "fixture")
        base = git("rev-parse", "HEAD")
        import devcrew_worktree

        devcrew_worktree.create(self.project, "T01", base, worktree_request="Use a task worktree")
        task_root = pathlib.Path(
            devcrew_worktree.resolve(self.project, "T01")["task_worktree_root"]
        )
        self.receipt["task_worktree_root"] = str(task_root)
        original = b"private exact attachment bytes"
        attachment = self.root / "input.bin"
        attachment.write_bytes(original)
        spec = {
            "mode": "file",
            "source_type": "attachment",
            "path": str(attachment),
            "expected_sha256": hashlib.sha256(original).hexdigest(),
            "origin": "provided attachment",
        }
        spec_path = self.root / "spec.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        cli = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "devcrew.py"
        process = subprocess.run(
            [
                sys.executable,
                str(cli),
                "phase",
                "source-bind",
                "--project",
                str(self.project),
                "--task-id",
                "T01",
                "--source-spec",
                str(spec_path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result["status"], "INTENT_SOURCE_BOUND")
        self.assertNotIn(original.decode(), process.stdout)
        self.assertFalse(self.phase.manifest_path(self.project, "T01").exists())

    def test_source_bind_after_init_uses_private_storage_in_normal_primary_checkout(self):
        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.project), *args], check=True, capture_output=True, text=True
            ).stdout.strip()

        git("init", "-q", "-b", "main")
        git("config", "user.name", "Test")
        git("config", "user.email", "test@example.com")
        cli = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "devcrew.py"
        initialized = subprocess.run(
            [sys.executable, str(cli), "init", "--project", str(self.project)],
            capture_output=True,
            text=True,
        )
        self.assertEqual(initialized.returncode, 0, initialized.stderr)
        tasks = self.project / ".codex/tasks"
        self.assertTrue(tasks.is_dir())
        tasks.chmod(0o755)
        task = tasks / "T01"
        task.mkdir()
        task.chmod(0o755)
        parent_modes = (tasks.stat().st_mode, task.stat().st_mode)
        git("add", ".")
        git("commit", "-qm", "initialized project fixture")
        import devcrew_worktree

        receipt = devcrew_worktree.create(self.project, "T01", git("rev-parse", "HEAD"))
        self.assertEqual(pathlib.Path(receipt["task_worktree_root"]), self.project.resolve())
        original = b"private attachment in initialized primary checkout"
        attachment = self.root / "input.bin"
        attachment.write_bytes(original)
        spec = self.root / "source.json"
        spec.write_text(
            json.dumps(
                {
                    "mode": "file",
                    "source_type": "attachment",
                    "path": str(attachment),
                    "expected_sha256": hashlib.sha256(original).hexdigest(),
                    "origin": "provided attachment",
                }
            )
        )
        bound = subprocess.run(
            [
                sys.executable,
                str(cli),
                "phase",
                "source-bind",
                "--project",
                str(self.project),
                "--task-id",
                "T01",
                "--source-spec",
                str(spec),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(bound.returncode, 0, bound.stderr)
        result = json.loads(bound.stdout)
        snapshot = self.project / result["source"]["snapshot"]
        self.assertEqual(snapshot.read_bytes(), original)
        self.assertTrue(user_prompt_submit.fs.has_private_mode(snapshot, 0o600))
        self.assertTrue(user_prompt_submit.fs.has_private_mode(snapshot.parent, 0o700))
        self.assertEqual((tasks.stat().st_mode, task.stat().st_mode), parent_modes)
        self.assertNotIn(original.decode(), bound.stdout)


class IntentTransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.task_root = self.root / "task"
        self.task_root.mkdir()
        self.project = self.task_root
        self.receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": str(self.task_root),
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
        import devcrew_phase

        self.phase = devcrew_phase

    def tearDown(self):
        self.temp.cleanup()

    def pending_manifest(self):
        phase = {
            "phase_id": "P1",
            "goal": "Support checkout",
            "depends_on": [],
            "owned_scope": ["src/checkout.py"],
            "acceptance": ["Checkout shows the requested option"],
            "verification": ["checkout test passes"],
            "verification_bindings": [
                {
                    "index": 0,
                    "verification": "checkout test passes",
                    "argv": ["python", "-m", "unittest"],
                }
            ],
            "commit_boundary": "Checkout support",
            "commit_subject": "type(T01): support checkout",
            "status": "PLANNED",
            "base_sha": "a" * 40,
        }
        source = self.phase.bind_intent_source(
            self.task_root,
            self.receipt,
            {
                "mode": "unavailable",
                "source_type": "user_prompt",
                "origin": "original request",
                "reason": "capture was not available",
            },
        )
        return {
            "schema_version": 2,
            "task_id": "T01",
            "task_mode": "SINGLE_PHASE",
            "semantic_contract": {"capabilities": ["checkout"]},
            "worktree": {
                "repository_id": "repo",
                "task_worktree_root": str(self.task_root),
                "branch": "codex/tasks/T01",
                "base_sha": "a" * 40,
            },
            "current_phase_id": "P1",
            "phases": [phase],
            "manifest_revision": 0,
            "original_intent": {
                "sources": [source],
                "requirements": [
                    {
                        "requirement_id": "F1",
                        "kind": "established_fact",
                        "statement": "Checkout runs in the task worktree.",
                        "source_fragments": [],
                        "acceptance_refs": [],
                        "evidence_records": [],
                    }
                ],
                "decisions": [],
                "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
            },
        }

    @unittest.skipIf(
        os.name == "nt", "POSIX mode bits; Windows ACL tampering is exercised in test_native_files"
    )
    def test_verified_sources_allow_readable_parents_but_keep_private_source_storage(self):
        manifest = self.pending_manifest()
        tasks = self.task_root / ".codex/tasks"
        task = tasks / "T01"
        task.mkdir(parents=True, exist_ok=True)
        tasks.chmod(0o755)
        task.chmod(0o755)
        attachment = self.root / "attachment.txt"
        attachment.write_bytes(b"exact attachment")
        spec = {
            "mode": "file",
            "source_type": "attachment",
            "path": str(attachment),
            "expected_sha256": hashlib.sha256(attachment.read_bytes()).hexdigest(),
            "origin": "provided attachment",
        }
        source = self.phase.bind_intent_source(self.project, self.receipt, spec)
        manifest["original_intent"]["sources"] = [source]
        self.phase.validate_manifest(manifest, self.receipt)
        snapshot = self.task_root / source["snapshot"]
        for directory, mode in ((snapshot.parent, 0o755), (task, 0o775), (tasks, 0o777)):
            with self.subTest(directory=directory, mode=mode):
                original_mode = directory.stat().st_mode & 0o777
                directory.chmod(mode)
                try:
                    with self.assertRaisesRegex(self.phase.PhaseError, "UNSAFE_INTENT_SOURCE_PATH"):
                        self.phase.bind_intent_source(self.project, self.receipt, spec)
                    with self.assertRaisesRegex(self.phase.PhaseError, "UNSAFE_INTENT_SOURCE_PATH"):
                        self.phase.validate_manifest(manifest, self.receipt)
                finally:
                    directory.chmod(original_mode)
        snapshot.chmod(0o644)
        with self.assertRaisesRegex(self.phase.PhaseError, "UNSAFE_INTENT_SOURCE_PATH"):
            self.phase.bind_intent_source(self.project, self.receipt, spec)
        with self.assertRaisesRegex(self.phase.PhaseError, "INTENT_SOURCE_SNAPSHOT_MISSING"):
            self.phase.validate_manifest(manifest, self.receipt)

    def answered_manifest(self):
        plugin_data = self.root / "plugin-data"
        plugin_data.mkdir(exist_ok=True)

        def bind(turn, prompt):
            contract = {
                "task_id": "T01",
                "goal": prompt,
                "requirements": [prompt],
                "acceptance": ["Observable card recommendation"],
                "non_goals": [],
            }
            data = json.dumps(contract).encode()
            path = self.root / (turn + ".json")
            path.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            event = {
                "session_id": "session-1",
                "turn_id": turn,
                "cwd": str(self.task_root),
                "prompt": user_prompt_submit.confirmation_text("T01", digest),
            }
            identity = user_prompt_submit.capture_prompt(plugin_data, event, self.task_root)
            return self.phase.bind_intent_source(
                self.task_root,
                self.receipt,
                {
                    "mode": "contract",
                    "plugin_data": str(plugin_data),
                    "path": str(path),
                    "confirmation_source_id": identity["source_id"],
                    "expected_sha256": digest,
                },
            )

        original = bind("turn-1", "Recommend a bank card if SBP is blocked.")
        answer = bind("turn-2", "I no longer need a bank card recommendation.")
        manifest = self.pending_manifest()
        acceptance = manifest["phases"][0]["acceptance"][0]
        original_bytes = (self.task_root / original["snapshot"]).read_bytes()
        answer_bytes = (self.task_root / answer["snapshot"]).read_bytes()

        def fragment(source, data):
            return {
                "source_id": source["source_id"],
                "start_byte": 0,
                "end_byte": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        original_fragment, answer_fragment = (
            fragment(original, original_bytes),
            fragment(answer, answer_bytes),
        )
        requirement = manifest["original_intent"]["requirements"][0]
        requirement.update(
            {
                "requirement_id": "R1",
                "kind": "explicit_user_obligation",
                "statement": "Recommend a bank card when SBP is blocked.",
                "source_fragments": [original_fragment],
                "acceptance_refs": [
                    {
                        "phase_id": "P1",
                        "acceptance_index": 0,
                        "sha256": hashlib.sha256(acceptance.encode()).hexdigest(),
                    }
                ],
            }
        )
        manifest["original_intent"]["sources"] = [original, answer]
        manifest["original_intent"]["decisions"] = [
            {
                "decision_id": "D1",
                "source_fragments": [answer_fragment],
                "changes": [
                    {
                        "requirement_id": "R1",
                        "before": {
                            "kind": requirement["kind"],
                            "statement": requirement["statement"],
                            "source_fragments": [original_fragment],
                        },
                        "after": {
                            "kind": "explicit_user_obligation",
                            "statement": "Recommend a different payment card when SBP is blocked.",
                            "source_fragments": [answer_fragment],
                        },
                    }
                ],
                "human_confirmation": {
                    "actor": "user",
                    "channel": "codex_user_message",
                    "interpretation": "User explicitly removed R1.",
                    "authority_rationale": "Answer came directly from the user.",
                },
            }
        ]
        return manifest, original_fragment, answer_fragment

    def complete_assessment(self, manifest, fragment, *, result="COMPLETE", disposition="COVERED"):
        intent = manifest["original_intent"]
        sources = intent["sources"]
        decision = intent["decisions"][0]
        digest = self.phase.intent_plan_digest(manifest)
        assessment = {
            "task_id": "T01",
            "plan_sha256": digest,
            "previous_plan_sha256": None,
            "mode": "IMPLEMENTATION",
            "result": result,
            "source_reads": [
                {
                    "source_id": source["source_id"],
                    "sha256": source["sha256"],
                    "findings": "Source contains the referenced request or answer.",
                }
                for source in sources
            ],
            "unavailable_sources": [],
            "source_outcomes": [
                {
                    "source_fragments": [fragment],
                    "expected_outcome": "Card recommendation when SBP is blocked.",
                    "requirement_ids": ["R1"],
                    "disposition": disposition,
                    "rationale": "Outcome compared with the active requirement.",
                }
            ],
            "obligation_checks": [
                {
                    "requirement_id": "R1",
                    "change": "USER_AMENDED",
                    "preserved_outcome": "The user changed the requested card recommendation.",
                    "safety_rationale": "The explicit recommendation requirement remains active.",
                    "acceptance_rationale": "The amended requirement remains mapped to acceptance.",
                    "decision_id": decision["decision_id"],
                }
            ],
            "technical_change_rationale": "Coordinator read both captured sources and assessed the plan.",
        }
        intent["planning_assessment"] = {
            "status": "ASSESSED",
            "assessment": assessment,
            "history": [],
        }
        return assessment

    def test_effective_requirements_and_digest_projection(self):
        requirement = {
            "requirement_id": "R1",
            "kind": "explicit_user_obligation",
            "statement": "Recommend a card",
            "source_fragments": [],
            "acceptance_refs": [],
            "evidence_records": [".codex/tasks/T01/evidence.json"],
        }
        manifest = {
            "task_id": "T01",
            "task_mode": "SINGLE_PHASE",
            "semantic_contract": {"capabilities": ["recommendation"]},
            "original_intent": {"sources": [], "requirements": [requirement], "decisions": []},
            "phases": [
                {
                    "phase_id": "P1",
                    "depends_on": [],
                    "goal": "G",
                    "owned_scope": ["x.py"],
                    "acceptance": ["A"],
                    "verification": ["V"],
                }
            ],
        }
        self.assertEqual(
            self.phase.effective_intent_requirements(manifest),
            {
                "R1": {
                    "kind": "explicit_user_obligation",
                    "statement": "Recommend a card",
                    "source_fragments": [],
                }
            },
        )
        digest = self.phase.intent_plan_digest(manifest)
        manifest["original_intent"]["requirements"][0]["evidence_records"].append("other.json")
        self.assertEqual(self.phase.intent_plan_digest(manifest), digest)
        manifest["phases"][0]["acceptance"][0] = "A changed"
        self.assertNotEqual(self.phase.intent_plan_digest(manifest), digest)

    def test_atomic_v2_protected_change_is_rejected_before_callback(self):
        with tempfile.TemporaryDirectory() as root:
            path = pathlib.Path(root) / "T01.phases.json"
            current = {
                "schema_version": 2,
                "task_id": "T01",
                "manifest_revision": 0,
                "original_intent": {
                    "sources": [],
                    "requirements": [],
                    "decisions": [],
                    "planning_assessment": {"status": "NOT_ASSESSED", "assessment": None},
                    "task_acceptance": {"status": "NOT_ASSESSED", "review": None},
                },
                "task_mode": "SINGLE_PHASE",
                "semantic_contract": {},
                "phases": [],
            }
            path.write_text(json.dumps(current), encoding="utf-8")
            proposed = json.loads(json.dumps(current))
            proposed["original_intent"]["requirements"].append(
                {
                    "requirement_id": "F1",
                    "kind": "established_fact",
                    "statement": "Invented",
                    "source_fragments": [],
                    "acceptance_refs": [],
                    "evidence_records": [],
                }
            )
            callback_called = []
            with self.assertRaises(self.phase.PhaseError):
                self.phase._atomic_json(
                    path, proposed, before_save=lambda: callback_called.append(True)
                )
            self.assertEqual(callback_called, [])
            self.assertEqual(json.loads(path.read_text()), current)

    def test_pending_v2_save_evidence_append_and_lifecycle_status_succeed(self):
        manifest = self.pending_manifest()
        path = self.task_root / "T01.phases.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        saved = self.phase.save_manifest(path, manifest, self.receipt)
        self.assertEqual(saved["manifest_revision"], 1)
        changed = json.loads(json.dumps(saved))
        changed["phases"][0]["status"] = "EXECUTING"
        changed["original_intent"]["requirements"][0]["evidence_records"].append(
            ".codex/tasks/T01/evidence.json"
        )
        updated = self.phase.save_manifest(path, changed, self.receipt)
        self.assertEqual(updated["manifest_revision"], 2)
        self.assertEqual(updated["phases"][0]["status"], "EXECUTING")
        self.assertEqual(
            updated["original_intent"]["requirements"][0]["evidence_records"],
            [".codex/tasks/T01/evidence.json"],
        )

    def test_addressed_overlay_validates_and_original_definition_is_retained(self):
        manifest, original_fragment, _ = self.answered_manifest()
        self.phase.validate_manifest(manifest, self.receipt)
        effective = self.phase.effective_intent_requirements(manifest)
        self.assertEqual(
            effective["R1"]["statement"], "Recommend a different payment card when SBP is blocked."
        )
        self.assertEqual(
            manifest["original_intent"]["requirements"][0]["statement"],
            "Recommend a bank card when SBP is blocked.",
        )
        self.complete_assessment(manifest, original_fragment)
        self.phase.validate_manifest(manifest, self.receipt)

    def test_nested_malformed_decision_and_assessment_values_are_phase_errors(self):
        manifest, original_fragment, answer_fragment = self.answered_manifest()
        malformed = json.loads(json.dumps(manifest))
        malformed["original_intent"]["decisions"][0]["changes"][0]["requirement_id"] = ["R1"]
        with self.assertRaises(self.phase.PhaseError):
            self.phase.validate_manifest(malformed, self.receipt)
        assessed = json.loads(json.dumps(manifest))
        assessed["original_intent"]["decisions"][0]["changes"][0]["requirement_id"] = "R1"
        self.complete_assessment(
            assessed, original_fragment, result="INCOMPLETE", disposition="OMITTED"
        )
        assessed["original_intent"]["planning_assessment"]["assessment"]["source_outcomes"][0][
            "requirement_ids"
        ] = [["R1"]]
        with self.assertRaises(self.phase.PhaseError):
            self.phase.validate_manifest(assessed, self.receipt)
        assessed["original_intent"]["planning_assessment"]["assessment"]["source_outcomes"][0][
            "requirement_ids"
        ] = ["R1"]
        assessed["original_intent"]["planning_assessment"]["assessment"]["result"] = "COMPLETE"
        with self.assertRaisesRegex(self.phase.PhaseError, "omitted source outcome"):
            self.phase.validate_manifest(assessed, self.receipt)

    def test_missing_source_coverage_stale_digest_and_unauthorized_protected_writes_fail(self):
        manifest, original_fragment, _ = self.answered_manifest()
        self.complete_assessment(manifest, original_fragment)
        no_read = json.loads(json.dumps(manifest))
        no_read["original_intent"]["planning_assessment"]["assessment"]["source_reads"].pop()
        no_read["original_intent"]["planning_assessment"]["assessment"]["plan_sha256"] = (
            self.phase.intent_plan_digest(no_read)
        )
        with self.assertRaisesRegex(self.phase.PhaseError, "account for every"):
            self.phase.validate_manifest(no_read, self.receipt)
        stale = json.loads(json.dumps(manifest))
        stale["phases"][0]["acceptance"][0] += " changed"
        stale["original_intent"]["requirements"][0]["acceptance_refs"][0]["sha256"] = (
            hashlib.sha256(stale["phases"][0]["acceptance"][0].encode()).hexdigest()
        )
        with self.assertRaisesRegex(self.phase.PhaseError, "assessment identity"):
            self.phase.validate_manifest(stale, self.receipt)

        current = self.pending_manifest()
        path = self.task_root / "T01.phases.json"
        path.write_text(json.dumps(current), encoding="utf-8")
        for mutate in (
            lambda value: value["phases"][0]["acceptance"].append("Second acceptance"),
            lambda value: value["original_intent"]["decisions"].append({"forged": True}),
            lambda value: value.__setitem__("systemic_replan_transition", {"forged": True}),
        ):
            proposed = json.loads(json.dumps(current))
            mutate(proposed)
            with self.assertRaises(self.phase.PhaseError):
                self.phase.save_manifest(path, proposed, self.receipt)
            callback_called = []
            with self.assertRaises(self.phase.PhaseError):
                self.phase._atomic_json(
                    path, proposed, before_save=lambda: callback_called.append(True)
                )
            self.assertEqual(callback_called, [])
            self.assertEqual(json.loads(path.read_text()), current)

    def test_generic_save_and_atomic_guard_cover_assessment_mapping_decision_history_and_systemic_payload(
        self,
    ):
        assessed, fragment, _ = self.answered_manifest()
        self.complete_assessment(assessed, fragment)
        assessment_change = json.loads(json.dumps(assessed))
        assessment_change["original_intent"]["planning_assessment"]["assessment"][
            "technical_change_rationale"
        ] = "Changed while keeping the same candidate digest."

        mapping_current, _, _ = self.answered_manifest()
        mapping_current["original_intent"]["decisions"] = []
        mapping_change = json.loads(json.dumps(mapping_current))
        mapping_change["phases"][0]["acceptance"].append("A second observable result")
        mapping_change["original_intent"]["requirements"][0]["acceptance_refs"] = [
            {
                "phase_id": "P1",
                "acceptance_index": 1,
                "sha256": hashlib.sha256(
                    mapping_change["phases"][0]["acceptance"][1].encode()
                ).hexdigest(),
            }
        ]

        decision_change, _, _ = self.answered_manifest()
        decision_current = json.loads(json.dumps(decision_change))
        decision_current["original_intent"]["decisions"] = []

        history_current, fragment, _ = self.answered_manifest()
        prior_assessment = self.complete_assessment(history_current, fragment)
        history_change = json.loads(json.dumps(history_current))
        state = history_change["original_intent"]["planning_assessment"]
        state["history"] = [json.loads(json.dumps(prior_assessment))]
        current_assessment = state["assessment"]
        current_assessment["previous_plan_sha256"] = prior_assessment["plan_sha256"]
        current_assessment["obligation_checks"][0].update(
            {"change": "PRESERVED", "decision_id": None}
        )

        systemic_current = self.pending_manifest()
        systemic_change = json.loads(json.dumps(systemic_current))
        systemic_change["systemic_replan_transition"] = {"forged": True}

        for label, current, proposed in (
            ("assessment", assessed, assessment_change),
            ("mapping", mapping_current, mapping_change),
            ("decision", decision_current, decision_change),
            ("history", history_current, history_change),
            ("systemic", systemic_current, systemic_change),
        ):
            with self.subTest(label=label):
                self.phase.validate_manifest(current, self.receipt)
                self.phase.validate_manifest(proposed, self.receipt)
                path = self.task_root / f"{label}.phases.json"
                path.write_text(json.dumps(current), encoding="utf-8")
                with self.assertRaises(self.phase.PhaseError):
                    self.phase.save_manifest(path, proposed, self.receipt)
                self.assertEqual(json.loads(path.read_text()), current)
                callback_called = []
                with self.assertRaises(self.phase.PhaseError):
                    self.phase._atomic_json(
                        path, proposed, before_save=lambda: callback_called.append(True)
                    )
                self.assertEqual(callback_called, [])
                self.assertEqual(json.loads(path.read_text()), current)

    def test_stale_cas_precedes_callbacks(self):
        current = self.pending_manifest()
        path = self.task_root / "T01.phases.json"
        path.write_text(json.dumps(current), encoding="utf-8")
        stale = json.loads(json.dumps(current))
        latest = json.loads(json.dumps(current))
        latest["manifest_revision"] = 1
        path.write_text(json.dumps(latest), encoding="utf-8")
        callback_called = []
        with self.assertRaisesRegex(self.phase.PhaseError, "PHASE_MANIFEST_CONFLICT"):
            self.phase._atomic_json(path, stale, before_save=lambda: callback_called.append(True))
        self.assertEqual(callback_called, [])


if __name__ == "__main__":
    unittest.main()
