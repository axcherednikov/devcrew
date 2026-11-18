"""Acceptance coverage for durable engineering requirement memory."""

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
PYTHON = sys.executable


class RequirementCliTests(unittest.TestCase):
    def test_same_requirement_under_another_id_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            (project / ".codex").mkdir()
            (project / ".codex" / "devcrew.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "paths": {
                            "context": "docs/context.md",
                            "tasks": ".codex/tasks",
                            "sessions": ".codex/sessions",
                            "memory": "memory",
                            "checks": ".codex/checks",
                        },
                    }
                )
            )
            first = {
                "id": "atomic-state",
                "revision": 1,
                "selectors": {
                    "paths": ["scripts/"],
                    "roles": ["executor"],
                    "task_classes": ["IMPLEMENTATION"],
                },
                "trigger": "A task updates a JSON state file.",
                "action": "Write the complete new state atomically.",
                "check": "Verify the old state remains intact when replacement fails.",
                "origin": {"kind": "user_requirement", "reference": "current task"},
                "assessment": {
                    "status": "active",
                    "durable": True,
                    "decision": "distinct",
                    "rationale": "No existing instruction covers this behavior.",
                    "evidence": [{"kind": "user_requirement", "reference": "current task"}],
                },
            }
            cli = [
                PYTHON,
                "-B",
                str(ROOT / "scripts/devcrew.py"),
                "requirements-record",
                "--project",
                str(project),
                "--record",
            ]
            first_result = subprocess.run(
                cli + [json.dumps(first)], cwd=ROOT, capture_output=True, text=True
            )
            self.assertEqual(first_result.returncode, 0, first_result.stderr)
            duplicate = dict(first, id="same-rule-other-id")
            second_result = subprocess.run(
                [
                    PYTHON,
                    "-B",
                    str(ROOT / "scripts/devcrew.py"),
                    "requirements-record",
                    "--project",
                    str(project),
                    "--expected-registry-revision",
                    "1",
                    "--record",
                    json.dumps(duplicate),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(
                second_result.returncode, 0, "the same rule must reuse its canonical ID"
            )
            self.assertIn("duplicate", second_result.stderr.casefold())
            saved = json.loads((project / "memory/engineering-requirements.json").read_text())
            self.assertEqual([item["id"] for item in saved["requirements"]], ["atomic-state"])

    def test_first_assessment_persists_and_fresh_process_reads_requirement(self):
        with tempfile.TemporaryDirectory() as directory:
            project = pathlib.Path(directory)
            (project / ".codex").mkdir()
            (project / ".codex" / "devcrew.json").write_text(
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
            decision = {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "The existing guidance does not cover the stated failure mode.",
                "evidence": [{"kind": "user_requirement", "reference": "current task"}],
            }
            payload = {
                "id": "req-atomic-json",
                "revision": 1,
                "selectors": {
                    "paths": ["scripts/"],
                    "roles": ["executor", "reviewer"],
                    "task_classes": ["IMPLEMENTATION"],
                },
                "trigger": "A task updates a JSON state file.",
                "action": "Write the complete new state atomically.",
                "check": "Verify the old state remains intact when replacement fails.",
                "origin": {"kind": "user_requirement", "reference": "current task"},
                "assessment": decision,
            }
            recorded = subprocess.run(
                [
                    PYTHON,
                    "-B",
                    str(ROOT / "scripts/devcrew.py"),
                    "requirements-record",
                    "--project",
                    str(project),
                    "--record",
                    json.dumps(payload),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(recorded.returncode, 0, recorded.stderr)
            registry = project / "custom-memory" / "engineering-requirements.json"
            self.assertTrue(registry.is_file())
            context = subprocess.run(
                [
                    PYTHON,
                    "-B",
                    str(ROOT / "scripts/devcrew.py"),
                    "requirements-context",
                    "--project",
                    str(project),
                    "--paths",
                    "scripts/devcrew.py",
                    "--role",
                    "executor",
                    "--task-class",
                    "IMPLEMENTATION",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(context.returncode, 0, context.stderr)
            result = json.loads(context.stdout)
            self.assertEqual(result["requirements"][0]["id"], "req-atomic-json")
            self.assertGreater(result["context_bytes"], 0)


if __name__ == "__main__":
    unittest.main()


class RequirementRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.temp.name)
        self.memory = "custom-memory"
        self.record = {
            "id": "req-atomic-json",
            "revision": 1,
            "selectors": {
                "paths": ["scripts/"],
                "roles": ["executor", "reviewer"],
                "task_classes": ["IMPLEMENTATION"],
            },
            "trigger": "A task updates a JSON state file.",
            "action": "Write the complete new state atomically.",
            "check": "Verify replacement failure preserves old state.",
            "origin": {"kind": "user_requirement", "reference": "task feedback"},
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "distinct",
                "rationale": "No matching existing rule covers this case.",
                "evidence": [{"kind": "user_requirement", "reference": "task feedback"}],
            },
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_registry_is_empty_without_creating_files_and_scope_is_deterministic(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        initial, count = devcrew_requirements.load(self.root, self.memory)
        self.assertEqual((initial["requirements"], count), ([], 0))
        self.assertFalse((self.root / self.memory).exists())
        devcrew_requirements.record(
            self.root, self.memory, self.record, expected_registry_revision=0
        )
        self.assertEqual(
            devcrew_requirements.context(
                self.root, self.memory, ["docs/readme.md"], "executor", "IMPLEMENTATION"
            )["entry_count"],
            0,
        )
        self.assertEqual(
            devcrew_requirements.context(
                self.root, self.memory, ["scripts/devcrew.py"], "executor", "IMPLEMENTATION"
            )["entry_count"],
            1,
        )
        self.assertEqual(
            devcrew_requirements.context(
                self.root, self.memory, ["scripts/devcrew.py"], "planner", "IMPLEMENTATION"
            )["entry_count"],
            0,
        )

    def test_directory_scope_intersects_nested_requirement_prefix(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        nested = json.loads(json.dumps(self.record))
        nested["selectors"]["paths"] = ["scripts/narrow/"]
        devcrew_requirements.record(self.root, self.memory, nested, expected_registry_revision=0)
        envelope = devcrew_requirements.context(
            self.root, self.memory, ["scripts"], "executor", "IMPLEMENTATION"
        )
        self.assertEqual([row["id"] for row in envelope["requirements"]], [nested["id"]])
        for unrelated in ("scripts-other", "scripts/sibling/file.py"):
            self.assertEqual(
                devcrew_requirements.context(
                    self.root, self.memory, [unrelated], "executor", "IMPLEMENTATION"
                )["entry_count"],
                0,
            )

    def test_mixed_code_and_test_scope_selects_both_without_vendor(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        for index, path in enumerate(("scripts/", "tests/", "vendor/")):
            item = json.loads(json.dumps(self.record))
            item.update(id=f"req-mixed-{index}", action=f"Apply rule {index}.")
            item["selectors"]["paths"] = [path]
            devcrew_requirements.record(
                self.root, self.memory, item, expected_registry_revision=index
            )
        selected = devcrew_requirements.context(
            self.root,
            self.memory,
            ["scripts/devcrew.py", "tests/test_x.py"],
            "executor",
            "IMPLEMENTATION",
        )
        self.assertEqual(
            {row["id"] for row in selected["requirements"]}, {"req-mixed-0", "req-mixed-1"}
        )

    def test_context_measurement_matches_serialized_envelope_and_overflow_fails_closed(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        empty, size = devcrew_requirements.load(self.root, self.memory)
        self.assertEqual((empty["revision"], size), (0, 0))
        self.assertFalse((self.root / self.memory).exists())
        for index in range(12):
            item = json.loads(json.dumps(self.record))
            item.update(id=f"req-{index:02d}", action=f"Action {index}: " + "x" * 1500)
            item["selectors"]["paths"] = [f"scripts/{index:02d}.py"]
            devcrew_requirements.record(
                self.root, self.memory, item, expected_registry_revision=index
            )
        large = devcrew_requirements.context(
            self.root,
            self.memory,
            [f"scripts/{index:02d}.py" for index in range(12)],
            "executor",
            "IMPLEMENTATION",
        )
        self.assertGreater(large["context_bytes"], 16 * 1024)
        self.assertLessEqual(large["context_bytes"], 32 * 1024)
        self.assertEqual(len(large["requirements"]), 12)
        self.assertTrue(all(len(item["action"]) > 1500 for item in large["requirements"]))
        small_root = self.root / "small"
        small_root.mkdir()
        devcrew_requirements.record(
            small_root, self.memory, self.record, expected_registry_revision=0
        )
        envelope = devcrew_requirements.context(
            small_root, self.memory, ["scripts/a.py"], "executor", "IMPLEMENTATION"
        )
        serialized, measured = devcrew_requirements.serialized_context(envelope)
        self.assertEqual(envelope["context_bytes"], measured)
        self.assertEqual(measured, len(serialized.encode("utf-8")))

    def test_complete_envelope_limit_reports_measured_utf8_bytes(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        exactish = {
            "requirements": [],
            "registry_revision": 0,
            "registry_bytes": 0,
            "entry_count": 0,
            "padding": "",
        }
        target = devcrew_requirements.ENVELOPE_LIMIT
        padding = target
        for _ in range(5):
            exactish["padding"] = "x" * padding
            _, measured = devcrew_requirements.serialized_context(exactish)
            padding += target - measured
        serialized, measured = devcrew_requirements.checked_serialized_context(exactish)
        self.assertEqual(measured, len(serialized.encode("utf-8")))
        self.assertEqual(measured, devcrew_requirements.ENVELOPE_LIMIT)
        oversized = {**exactish, "padding": exactish["padding"] + "x"}
        _, expected = devcrew_requirements.serialized_context(oversized)
        with self.assertRaisesRegex(
            ValueError, rf"{expected} bytes; limit is {devcrew_requirements.ENVELOPE_LIMIT} bytes"
        ):
            devcrew_requirements.checked_serialized_context(oversized)

    def test_metadata_overflow_stops_ordinary_and_formal_builders(self):
        import sys
        from unittest import mock

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_context
        import devcrew_ordinary
        import devcrew_requirements

        target = devcrew_requirements.ENVELOPE_LIMIT - 1
        base = {
            "requirements": [],
            "registry_revision": 7,
            "registry_bytes": 80,
            "entry_count": 0,
            "padding": "",
        }
        padding = target
        for _ in range(5):
            base["padding"] = "x" * padding
            _, measured = devcrew_requirements.serialized_context(base)
            padding += target - measured
        self.assertEqual(devcrew_requirements.serialized_context(base)[1], target)
        registry = {"revision": 7, "requirements": [{"id": "req-large"}]}
        with (
            mock.patch.object(devcrew_requirements, "load", return_value=(registry, 80)),
            mock.patch.object(devcrew_requirements, "context", return_value=dict(base)),
            self.assertRaisesRegex(ValueError, "issuance stopped without truncation"),
        ):
            devcrew_ordinary._requirements_delivery(
                self.root,
                self.memory,
                "planner",
                "PLANNER",
                ["scripts/a.py"],
                "planner_declaration",
                0,
            )
        with (
            mock.patch.object(devcrew_requirements, "load", return_value=(registry, 80)),
            mock.patch.object(devcrew_requirements, "context", return_value=dict(base)),
            mock.patch("devcrew._state_paths", return_value={"memory": self.memory}),
            self.assertRaisesRegex(ValueError, "issuance stopped without truncation"),
        ):
            devcrew_context._requirements_envelope(
                self.root, "planner", {"paths": ["scripts/a.py"]}, "PLANNER", "metadata-test"
            )

    def test_source_identity_duplicates_and_symlink_sources_are_rejected(self):
        import hashlib
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        source = self.root / "RULES.md"
        source.write_text("Keep the existing user setting on failure.\n", encoding="utf-8")
        raw = source.read_bytes()
        item = {
            key: value
            for key, value in self.record.items()
            if key not in {"trigger", "action", "check"}
        }
        item["origin"] = {"kind": "project_rule", "reference": "RULES.md"}
        item["source"] = {
            "path": "RULES.md",
            "fragment": "Keep the existing user setting on failure.",
            "fragment_sha256": hashlib.sha256(
                b"Keep the existing user setting on failure."
            ).hexdigest(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        devcrew_requirements.record(self.root, self.memory, item, expected_registry_revision=0)
        duplicate = json.loads(json.dumps(item))
        duplicate["id"] = "other-rule-id"
        with self.assertRaisesRegex(ValueError, "duplicate canonical"):
            devcrew_requirements.record(
                self.root, self.memory, duplicate, expected_registry_revision=1
            )
        alias = self.root / "alias.md"
        alias.symlink_to(source)
        unsafe = json.loads(json.dumps(item))
        unsafe["id"] = "symlink-rule"
        unsafe["source"]["path"] = "alias.md"
        unsafe["source"]["sha256"] = hashlib.sha256(raw).hexdigest()
        with self.assertRaisesRegex(ValueError, "symlink"):
            devcrew_requirements.record(
                self.root, self.memory, unsafe, expected_registry_revision=1
            )

    def test_bounded_reader_rejects_large_file_before_reading_contents(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        path = self.root / "oversized.bin"
        path.write_bytes(b"x" * 32)
        with self.assertRaisesRegex(ValueError, "exceeds 16 bytes"):
            devcrew_requirements.read_bounded_file(path, 16, "test input")

    def test_replay_is_no_write_revision_updates_are_same_id_and_cas_conflicts(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        first = devcrew_requirements.record(
            self.root, self.memory, self.record, expected_registry_revision=0
        )
        registry = self.root / self.memory / devcrew_requirements.REGISTRY_NAME
        before = registry.read_bytes()
        replay = devcrew_requirements.record(
            self.root, self.memory, self.record, expected_registry_revision=1
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(registry.read_bytes(), before)
        with self.assertRaisesRegex(ValueError, "revision conflict"):
            devcrew_requirements.record(
                self.root, self.memory, self.record, expected_registry_revision=0
            )
        changed = dict(self.record)
        changed.update(
            revision=2,
            action="Write complete state using the existing atomic replacement helper.",
            change_reason="Clarified the approved implementation mechanism.",
        )
        updated = devcrew_requirements.record(
            self.root, self.memory, changed, expected_registry_revision=1
        )
        self.assertEqual(updated["requirement"]["id"], first["requirement"]["id"])
        self.assertEqual(updated["requirement"]["revision"], 2)
        self.assertEqual(len(updated["requirement"]["history"]), 1)

    def test_existing_source_reference_is_digest_checked_and_never_copied(self):
        import hashlib
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        source_path = self.root / "AGENTS.md"
        source_path.write_text("Use atomic replacement for persistent state.\n", encoding="utf-8")
        raw = source_path.read_bytes()
        referenced = {
            key: value
            for key, value in self.record.items()
            if key not in {"trigger", "action", "check"}
        }
        referenced["source"] = {
            "path": "AGENTS.md",
            "fragment": "Use atomic replacement for persistent state.",
            "sha256": hashlib.sha256(raw).hexdigest(),
            "fragment_sha256": hashlib.sha256(
                b"Use atomic replacement for persistent state."
            ).hexdigest(),
        }
        devcrew_requirements.record(
            self.root, self.memory, referenced, expected_registry_revision=0
        )
        saved = json.loads(
            (self.root / self.memory / devcrew_requirements.REGISTRY_NAME).read_text()
        )
        self.assertNotIn("action", saved["requirements"][0])
        source_path.write_text("Changed canonical instruction.\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            devcrew_requirements.context(
                self.root, self.memory, ["scripts/a.py"], "executor", "IMPLEMENTATION"
            )

    def test_unresolved_decision_and_unsafe_memory_symlink_fail_closed(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        unresolved = json.loads(json.dumps(self.record))
        unresolved["assessment"]["decision"] = "uncertain"
        with self.assertRaisesRegex(ValueError, "active distinct/equivalent"):
            devcrew_requirements.record(
                self.root, self.memory, unresolved, expected_registry_revision=0
            )
        target = self.root / "actual"
        target.mkdir()
        (self.root / "memory-link").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            devcrew_requirements.load(self.root, "memory-link")

    def test_equivalent_reuses_existing_body_and_similar_distinct_requirement_stays_separate(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        devcrew_requirements.record(
            self.root, self.memory, self.record, expected_registry_revision=0
        )
        registry = self.root / self.memory / devcrew_requirements.REGISTRY_NAME
        before = registry.read_bytes()
        equivalent = {
            "id": self.record["id"],
            "revision": 1,
            "selectors": self.record["selectors"],
            "origin": self.record["origin"],
            "assessment": {
                "status": "active",
                "durable": True,
                "decision": "equivalent",
                "canonical_id": self.record["id"],
                "rationale": "The candidate has the same scope and meaning.",
                "evidence": [{"kind": "user_requirement", "reference": "follow-up"}],
            },
        }
        result = devcrew_requirements.record(
            self.root, self.memory, equivalent, expected_registry_revision=1
        )
        self.assertTrue(result["equivalent_reused"])
        self.assertEqual(registry.read_bytes(), before)
        similar = dict(
            self.record,
            id="req-similar-but-distinct",
            action="Use an existing atomic replacement helper for changed state.",
        )
        distinct = devcrew_requirements.record(
            self.root, self.memory, similar, expected_registry_revision=1
        )
        self.assertEqual(distinct["registry_revision"], 2)
        self.assertEqual(len(json.loads(registry.read_text())["requirements"]), 2)
        colliding_update = dict(
            similar,
            revision=2,
            action=self.record["action"],
            check=self.record["check"],
            change_reason="This would collapse into the original canonical rule.",
        )
        with self.assertRaisesRegex(ValueError, "duplicate canonical"):
            devcrew_requirements.record(
                self.root, self.memory, colliding_update, expected_registry_revision=2
            )
        self.assertEqual(len(json.loads(registry.read_text())["requirements"]), 2)

    def test_untrusted_one_time_unconfirmed_review_and_conflicting_decisions_do_not_persist(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        candidates = []
        one_time = json.loads(json.dumps(self.record))
        one_time["assessment"]["status"] = "one_time"
        candidates.append(one_time)
        reviewer_claim = json.loads(json.dumps(self.record))
        reviewer_claim["origin"] = {"kind": "confirmed_review", "reference": "review"}
        reviewer_claim["assessment"]["confirmed_reusable"] = False
        candidates.append(reviewer_claim)
        untrusted = json.loads(json.dumps(self.record))
        untrusted["origin"]["kind"] = "tool_output"
        candidates.append(untrusted)
        conflict = json.loads(json.dumps(self.record))
        conflict["assessment"]["decision"] = "conflicting"
        candidates.append(conflict)
        for candidate in candidates:
            with self.subTest(origin=candidate["origin"], assessment=candidate["assessment"]):
                with self.assertRaises(ValueError):
                    devcrew_requirements.record(
                        self.root, self.memory, candidate, expected_registry_revision=0
                    )
        self.assertFalse((self.root / self.memory / devcrew_requirements.REGISTRY_NAME).exists())

    def test_project_isolation_and_registry_ceiling(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import devcrew_requirements

        project_b = self.root / "project-b"
        project_b.mkdir()
        devcrew_requirements.record(
            self.root, self.memory, self.record, expected_registry_revision=0
        )
        self.assertEqual(
            devcrew_requirements.context(
                project_b, self.memory, ["scripts/a.py"], "executor", "IMPLEMENTATION"
            )["entry_count"],
            0,
        )
        oversized = project_b / self.memory / devcrew_requirements.REGISTRY_NAME
        oversized.parent.mkdir()
        oversized.write_bytes(b" " * (devcrew_requirements.REGISTRY_LIMIT + 1))
        with self.assertRaisesRegex(ValueError, "exceeds 524288 bytes"):
            devcrew_requirements.load(project_b, self.memory)

    def test_diagnose_handles_empty_registry_route_then_first_requirement(self):
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        sys.path.insert(0, str(ROOT / "tests"))
        import devcrew_requirements
        import devcrew_phase
        import test_phase_review_lifecycle as phase_review

        fixture = phase_review.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.manifest = devcrew_phase.set_phase_status(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            "P1",
            "EXECUTING",
            project=fixture.host,
            verifier=fixture.verifier,
        )
        first_route = fixture.routed_executor_fields("P1")
        route_task_class = first_route["route"]["TASK_CLASS"]

        def diagnose(spawn_receipt_id):
            incident_path = self.root / "incident.json"
            incident_path.write_text(
                json.dumps(
                    {
                        "task_id": "T01",
                        "spawn_receipt_id": spawn_receipt_id,
                        "role": "executor",
                        "task_class": route_task_class,
                        "paths": ["one.txt"],
                    }
                )
            )
            result = subprocess.run(
                [
                    PYTHON,
                    str(ROOT / "scripts/devcrew.py"),
                    "requirements-diagnose",
                    "--project",
                    str(fixture.host),
                    "--incident",
                    str(incident_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

        first = json.loads(json.dumps(self.record))
        first["selectors"]["paths"] = ["one.txt"]
        first["selectors"]["task_classes"] = [route_task_class]
        devcrew_requirements.record(fixture.host, "memory", first, expected_registry_revision=0)
        after_first_requirement = diagnose(first_route["SPAWN_RECEIPT_ID"])
        self.assertEqual(after_first_requirement["diagnosis"], "undetermined")
        self.assertIsNone(after_first_requirement["observed_delivery"]["registry_revision"])
        self.assertEqual(after_first_requirement["observed_delivery"]["delivered_requirements"], [])

        known_route = fixture.routed_executor_fields("P1")
        same_revision = diagnose(known_route["SPAWN_RECEIPT_ID"])
        self.assertEqual(same_revision["diagnosis"], "undetermined")
        self.assertEqual(same_revision["observed_delivery"]["registry_revision"], 1)
        self.assertEqual(
            [row["id"] for row in same_revision["observed_delivery"]["delivered_requirements"]],
            [first["id"]],
        )

        second = json.loads(json.dumps(first))
        second["id"] = "req-second-rule"
        second["trigger"] = "A task updates another persistent JSON state file."
        second["action"] = "Write that complete state atomically."
        second["check"] = "Verify replacement failure preserves that file."
        second["origin"]["reference"] = "second task feedback"
        second["assessment"]["evidence"][0]["reference"] = "second task feedback"
        devcrew_requirements.record(fixture.host, "memory", second, expected_registry_revision=1)
        after_revision_change = diagnose(known_route["SPAWN_RECEIPT_ID"])
        self.assertEqual(after_revision_change["diagnosis"], "registry_changed_after_delivery")
