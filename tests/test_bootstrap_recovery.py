"""Bootstrap recovery without planner, raw transcripts, or fabricated approvals."""

import hashlib
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_context
import devcrew_dispatch
import devcrew_phase
import devcrew_worktree

from hooks import user_prompt_submit as hook


class BootstrapRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name).resolve()
        self.project = self.root / "project"
        self.project.mkdir()
        self.plugin_data = self.root / "runtime-data"
        self.receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "task_id": "T01",
            "task_worktree_root": str(self.project),
        }
        self.contract = self.root / "contract.json"
        self.contract.write_text(
            json.dumps(
                {
                    "task_id": "T01",
                    "goal": "Repair bootstrap",
                    "requirements": ["Reject unconfirmed sources", "Never retain full messages"],
                    "acceptance": ["Preflight rejects a missing source"],
                    "non_goals": ["WordPress repair"],
                }
            )
        )
        self.digest = hashlib.sha256(self.contract.read_bytes()).hexdigest()

    def event(self, prompt, **fields):
        return {
            "session_id": "session-1",
            "turn_id": "turn-1",
            "cwd": str(self.project),
            "prompt": prompt,
            **fields,
        }

    def spec(self, **fields):
        identity = hook.capture_prompt(
            self.plugin_data, self.event(hook.confirmation_text("T01", self.digest)), self.project
        )
        return {
            "mode": "contract",
            "path": str(self.contract),
            "expected_sha256": self.digest,
            "plugin_data": str(self.plugin_data),
            "confirmation_source_id": identity["source_id"],
            **fields,
        }

    def bind(self, **fields):
        return devcrew_phase.bind_intent_source(self.project, self.receipt, self.spec(**fields))

    def test_environment_absent_never_invents_a_path(self):
        with self.assertRaisesRegex(ValueError, "PLUGIN_DATA is unavailable"):
            hook.capture_prompt(None, self.event("private text"), self.project)
        self.assertEqual(hook.environment_status(None)["status"], "UNAVAILABLE")
        self.assertFalse(self.plugin_data.exists())
        self.assertEqual(list(self.project.iterdir()), [])

    def test_runtime_declared_path_initializes_missing_ancestors(self):
        self.assertFalse(self.plugin_data.exists())
        hook.capture_prompt(self.plugin_data, self.event("private text"), self.project)
        self.assertTrue(hook.fs.has_private_mode(self.plugin_data, 0o700))
        self.assertEqual(hook.environment_status(str(self.plugin_data))["status"], "READY")
        nested = self.root / "missing-parent" / "runtime"
        hook.capture_prompt(nested, self.event("private text"), self.project)
        self.assertTrue(hook.fs.has_private_mode(nested, 0o700))
        for path in ("relative-data", self.root / "missing-parent" / ".." / "escape"):
            with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                hook.capture_prompt(path, self.event("private text"), self.project)
        self.assertFalse((self.root / "escape").exists())
        link = self.root / "alias"
        link.symlink_to(self.plugin_data, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            hook.capture_prompt(link / "child", self.event("private text"), self.project)
        self.assertFalse((self.plugin_data / "child").exists())

    def test_receipts_never_store_prompt_and_digest_alone_is_not_a_source(self):
        private_text = "sensitive unique request not to persist"
        identity = hook.capture_prompt(self.plugin_data, self.event(private_text), self.project)
        record = hook.claim_prompt(self.plugin_data, identity["source_id"])
        self.assertIsNone(record["confirmation"])
        for path in self.plugin_data.rglob("*"):
            if path.is_file():
                self.assertNotIn(private_text.encode(), path.read_bytes())
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_FULL_MESSAGE_FORBIDDEN"):
            devcrew_phase.bind_intent_source(
                self.project,
                self.receipt,
                {
                    "mode": "capture",
                    "source_id": identity["source_id"],
                    "plugin_data": str(self.plugin_data),
                    "expected_sha256": identity["sha256"],
                },
            )

    def test_unavailable_source_blocks_preflight_then_confirmed_contract_recovers(self):
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_REQUIRED"):
            devcrew_phase.intent_source_preflight(self.project, self.receipt)
        devcrew_phase.bind_intent_source(
            self.project,
            self.receipt,
            {
                "mode": "unavailable",
                "source_type": "user_prompt",
                "origin": "current turn",
                "reason": "no programmatic source available",
            },
        )
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_REQUIRED"):
            devcrew_phase.intent_source_preflight(self.project, self.receipt)
        source = self.bind()
        self.assertEqual(source["source_type"], "intent_contract")
        self.assertEqual(devcrew_phase.intent_source_preflight(self.project, self.receipt), source)
        self.assertEqual(
            (self.project / source["snapshot"]).read_bytes(), self.contract.read_bytes()
        )

    def test_confirmation_accepts_one_line_ending_and_keeps_exact_message_digest(self):
        expected = {"task_id": "T01", "sha256": self.digest}
        for index, ending in enumerate(("", "\n", "\r\n")):
            with self.subTest(ending=repr(ending)):
                prompt = hook.confirmation_text("T01", self.digest) + ending
                identity = hook.capture_prompt(
                    self.plugin_data, self.event(prompt, turn_id=f"ending-{index}"), self.project
                )
                self.assertEqual(identity["confirmation"], expected)
                self.assertEqual(identity["sha256"], hashlib.sha256(prompt.encode()).hexdigest())
                self.assertEqual(
                    hook.claim_prompt(self.plugin_data, identity["source_id"]), identity
                )
                source = devcrew_phase.bind_intent_source(
                    self.project,
                    self.receipt,
                    {
                        "mode": "contract",
                        "path": str(self.contract),
                        "expected_sha256": self.digest,
                        "plugin_data": str(self.plugin_data),
                        "confirmation_source_id": identity["source_id"],
                    },
                )
                self.assertEqual(
                    devcrew_phase.intent_source_preflight(self.project, self.receipt), source
                )

    def test_hook_does_not_classify_confirmation_with_extra_text_or_whitespace(self):
        line = hook.confirmation_text("T01", self.digest)
        prompts = (
            line + " ",
            " " + line,
            line + "\r",
            line + "\n\n",
            line + "\r\n\n",
            line + "\nApproved",
            "Approved\n" + line,
        )
        for index, prompt in enumerate(prompts):
            with self.subTest(index=index):
                identity = hook.capture_prompt(
                    self.plugin_data,
                    self.event(prompt, turn_id=f"unclassified-{index}"),
                    self.project,
                )
                self.assertIsNone(identity["confirmation"])
                self.assertEqual(
                    hook.claim_prompt(self.plugin_data, identity["source_id"]), identity
                )

    def legacy_spec(self, prompt, *, turn="legacy"):
        # Fixture for a v2 receipt captured before line-ending recognition.
        identity = hook.capture_prompt(
            self.plugin_data, self.event(prompt, turn_id=turn), self.project
        )
        store = self.plugin_data / "devcrew-original-intent" / hook.STORE_NAME
        value = json.loads(store.read_bytes())
        value["entries"][identity["source_id"]]["confirmation"] = None
        store.write_text(
            json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
        )
        return {
            "mode": "contract",
            "path": str(self.contract),
            "expected_sha256": self.digest,
            "plugin_data": str(self.plugin_data),
            "confirmation_source_id": identity["source_id"],
        }

    def test_cli_binds_legacy_line_ending_receipt_without_rewriting_it(self):
        for args in (
            ("init", "-q", "-b", "main"),
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.com"),
            ("commit", "--allow-empty", "-qm", "base"),
        ):
            subprocess.run(["git", "-C", str(self.project), *args], check=True, capture_output=True)
        devcrew_worktree.create(self.project, "T01")
        self.receipt = devcrew_worktree.resolve(self.project, "T01")
        prompt = hook.confirmation_text("T01", self.digest) + "\n"
        spec = self.legacy_spec(prompt)
        store = self.plugin_data / "devcrew-original-intent" / hook.STORE_NAME
        original = store.read_bytes()
        path = self.root / "source-spec.json"
        path.write_text(json.dumps(spec))
        proc = subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/devcrew.py"),
                "phase",
                "source-bind",
                "--project",
                str(self.project),
                "--task-id",
                "T01",
                "--source-spec",
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        source = json.loads(proc.stdout)["source"]
        self.assertIsNone(source["provenance"]["confirmation"]["confirmation"])
        self.assertEqual(store.read_bytes(), original)
        replay = hook.capture_prompt(
            self.plugin_data, self.event(prompt, turn_id="legacy"), self.project
        )
        self.assertIsNone(replay["confirmation"])
        self.assertEqual(store.read_bytes(), original)
        store.unlink()
        self.contract.unlink()
        self.assertEqual(devcrew_phase.intent_source_preflight(self.project, self.receipt), source)

    def test_legacy_receipt_rejects_extra_text_whitespace_and_other_contracts(self):
        line = hook.confirmation_text("T01", self.digest)
        prompts = (
            line + " ",
            " " + line,
            line + "\r",
            line + "\n\n",
            line + "\r\n\n",
            line + "\nApproved",
            "Approved\n" + line,
            hook.confirmation_text("T02", self.digest) + "\n",
            hook.confirmation_text("T01", "0" * 64) + "\n",
        )
        for index, prompt in enumerate(prompts):
            with self.subTest(index=index):
                spec = self.legacy_spec(prompt, turn=f"invalid-{index}")
                identity = hook.claim_prompt(self.plugin_data, spec["confirmation_source_id"])
                self.assertIsNone(identity["confirmation"])
                with self.assertRaisesRegex(
                    devcrew_phase.PhaseError, "INTENT_CONFIRMATION_INVALID"
                ):
                    devcrew_phase.bind_intent_source(self.project, self.receipt, spec)
        self.assertFalse((self.project / ".codex/tasks/T01/sources").exists())

    def test_confirmation_required_and_cannot_be_substituted(self):
        spec = self.spec()
        for key, value in (
            ("expected_sha256", "0" * 64),
            ("confirmation_source_id", "0" * 64),
            ("plugin_data", None),
        ):
            with self.subTest(key=key), self.assertRaises(devcrew_phase.PhaseError):
                devcrew_phase.bind_intent_source(
                    self.project, self.receipt, dict(spec, **{key: value})
                )
        for index, prompt in enumerate(
            (
                "yes",
                hook.confirmation_text("T02", self.digest),
                hook.confirmation_text("T01", "0" * 64),
            )
        ):
            identity = hook.capture_prompt(
                self.plugin_data, self.event(prompt, turn_id=f"other-{index}"), self.project
            )
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_CONFIRMATION_INVALID"):
                devcrew_phase.bind_intent_source(
                    self.project,
                    self.receipt,
                    dict(spec, confirmation_source_id=identity["source_id"]),
                )
        self.assertFalse((self.project / ".codex/tasks/T01/sources").exists())

    def test_foreign_project_and_agent_boolean_are_not_human_confirmation(self):
        foreign = self.root / "foreign"
        foreign.mkdir()
        identity = hook.capture_prompt(
            self.plugin_data,
            self.event(hook.confirmation_text("T01", self.digest), turn_id="foreign"),
            foreign,
        )
        spec = self.spec(confirmation_source_id=identity["source_id"])
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_CONFIRMATION_INVALID"):
            devcrew_phase.bind_intent_source(self.project, self.receipt, spec)
        spec = self.spec(confirmed=True)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_INTENT_SOURCE_SPEC"):
            devcrew_phase.bind_intent_source(self.project, self.receipt, spec)

    def test_resume_uses_bound_confirmation_after_receipt_expires_and_rejects_tampering(self):
        source = self.bind()
        store = self.plugin_data / "devcrew-original-intent" / hook.STORE_NAME
        store.unlink()
        self.contract.unlink()  # external draft is no longer needed
        self.assertEqual(devcrew_phase.intent_source_preflight(self.project, self.receipt), source)
        confirmation = source["provenance"]["confirmation"]
        confirmation["confirmation"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_CONFIRMATION_INVALID"):
            devcrew_phase.intent_source_preflight(self.project, self.receipt, source=source)
        source = devcrew_phase.intent_source_preflight(self.project, self.receipt)
        snapshot = self.project / source["snapshot"]
        snapshot.write_text("changed")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_SNAPSHOT_CHANGED"):
            devcrew_phase.intent_source_preflight(self.project, self.receipt)

    def test_contract_schema_rejects_raw_transcript_fields(self):
        contract = json.loads(self.contract.read_text())
        contract["prompt"] = "raw transcript"
        self.contract.write_text(json.dumps(contract))
        self.digest = hashlib.sha256(self.contract.read_bytes()).hexdigest()
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INVALID_INTENT_CONTRACT"):
            self.bind()

    def test_planner_issuance_fails_before_model_selection_or_route_storage(self):
        for args in (
            ("init", "-q"),
            ("config", "user.name", "Test"),
            ("config", "user.email", "test@example.com"),
            ("commit", "--allow-empty", "-qm", "base"),
        ):
            subprocess.run(["git", "-C", str(self.project), *args], check=True, capture_output=True)
        devcrew_worktree.create(self.project, "T01")
        receipt = devcrew_worktree.resolve(self.project, "T01")
        intent = {
            "role": "planner",
            "task_name": "plan",
            "message": "Plan repair",
            "runtime": {},
            "route_options": {},
            "role_contract": None,
        }
        with mock.patch.object(devcrew_context, "_spawn_selection") as select:
            with self.assertRaisesRegex(ValueError, "INTENT_SOURCE_REQUIRED"):
                devcrew_dispatch.issue_route(
                    self.project,
                    task_id="T01",
                    role="planner",
                    identity={"TASK_ID": "T01", "BASE_SHA": receipt["head"]},
                    intent=intent,
                )
            select.assert_not_called()
        path = devcrew_dispatch._store_path(self.project, "T01")
        self.assertFalse(path.exists())
        self.receipt = receipt
        source = self.bind()
        with mock.patch.object(
            devcrew_context, "_spawn_selection", side_effect=RuntimeError("selection reached")
        ):
            with self.assertRaisesRegex(RuntimeError, "selection reached"):
                devcrew_context._derive_dispatch_issuance(
                    self.project,
                    "T01",
                    "planner",
                    {"TASK_ID": "T01", "BASE_SHA": receipt["head"]},
                    intent,
                )
        self.assertEqual(devcrew_phase.intent_source_preflight(self.project, receipt), source)

    def test_manifest_cannot_drop_the_contract_pinned_in_issued_planner_payload(self):
        import test_phase_review_lifecycle as lifecycle

        fixture = lifecycle.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = json.loads(json.dumps(fixture.manifest))
        plan = devcrew_phase.validate_manifest(plan, fixture.receipt)
        for phase in plan["phases"]:
            devcrew_phase.pin_verification(phase)
        intent, assessment = fixture._v2_intent(plan)
        intent["planning_assessment"] = {"status": "NOT_ASSESSED", "assessment": None}
        identity = hook.capture_prompt(
            self.plugin_data, self.event(hook.confirmation_text("T01", self.digest)), fixture.task
        )
        source = devcrew_phase.bind_intent_source(
            fixture.host,
            fixture.receipt,
            {
                "mode": "contract",
                "path": str(self.contract),
                "expected_sha256": self.digest,
                "plugin_data": str(self.plugin_data),
                "confirmation_source_id": identity["source_id"],
            },
        )
        planner = lifecycle.issue_planner_receipt(
            fixture.host, "T01", fixture.base, "Plan confirmed contract", "new-planner"
        )
        path = fixture.path.with_name("substitution.phases.json")
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_BINDING_MISMATCH"):
            devcrew_phase.initialize_manifest(
                path,
                plan,
                fixture.receipt,
                fixture.host,
                planner["SPAWN_RECEIPT_ID"],
                intent_assessment={"assessment": assessment, "decisions": []},
            )
        self.assertFalse(path.exists())
        plan["original_intent"]["sources"].append(source)
        assessment["source_reads"].append(
            {
                "source_id": source["source_id"],
                "sha256": source["sha256"],
                "findings": "Read the separately confirmed bootstrap contract.",
            }
        )
        assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(plan)
        accepted = devcrew_phase.initialize_manifest(
            path,
            plan,
            fixture.receipt,
            fixture.host,
            planner["SPAWN_RECEIPT_ID"],
            intent_assessment={"assessment": assessment, "decisions": []},
        )
        devcrew_phase.verify_planner_dispatch(fixture.host, accepted)
        accepted["original_intent"]["sources"].remove(source)
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "INTENT_SOURCE_BINDING_MISMATCH"):
            devcrew_phase.verify_planner_dispatch(fixture.host, accepted)

    def test_confirmed_contract_keeps_final_intent_review_required(self):
        import test_phase_review_lifecycle as lifecycle

        fixture = lifecycle.PhaseReviewLifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        original_builder = fixture._v2_intent

        def confirmed_intent(manifest, previous=None, *, explicit_obligation=False):
            intent, assessment = original_builder(
                manifest, previous, explicit_obligation=explicit_obligation
            )
            data = json.dumps(
                {
                    "task_id": "T01",
                    "goal": "Preserve checkout",
                    "requirements": ["The checkout option must remain available."],
                    "acceptance": ["The checkout option remains available."],
                    "non_goals": [],
                }
            ).encode()
            self.contract.write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            identity = hook.capture_prompt(
                self.plugin_data, self.event(hook.confirmation_text("T01", digest)), fixture.task
            )
            source = devcrew_phase.bind_intent_source(
                fixture.host,
                fixture.receipt,
                {
                    "mode": "contract",
                    "path": str(self.contract),
                    "expected_sha256": digest,
                    "plugin_data": str(self.plugin_data),
                    "confirmation_source_id": identity["source_id"],
                },
            )
            fragment = {
                "source_id": source["source_id"],
                "start_byte": 0,
                "end_byte": len(data),
                "sha256": digest,
            }
            intent["sources"] = [source]
            intent["requirements"][0]["source_fragments"] = [fragment]
            assessment["source_reads"] = [
                {
                    "source_id": source["source_id"],
                    "sha256": digest,
                    "findings": "Read the user-confirmed checkout contract.",
                }
            ]
            assessment["source_outcomes"][0]["source_fragments"] = [fragment]
            assessment["plan_sha256"] = devcrew_phase.intent_plan_digest(manifest)
            planner = lifecycle.issue_planner_receipt(
                fixture.host,
                "T01",
                fixture.base,
                "Plan confirmed checkout contract",
                "contract-planner",
            )
            manifest["planner_dispatch"] = {"spawn_receipt_id": planner["SPAWN_RECEIPT_ID"]}
            return intent, assessment

        fixture._v2_intent = confirmed_intent
        _candidate, report = fixture._v2_final_review_fixture()
        self.assertEqual(
            fixture.manifest["original_intent"]["sources"][0]["source_type"], "intent_contract"
        )
        report_path = fixture.host / "contract-review.json"
        missing_review = dict(report)
        missing_review.pop("INTENT_REVIEW")
        report_path.write_text(json.dumps(missing_review))
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.submit_review(
                fixture.path,
                fixture.manifest,
                fixture.receipt,
                fixture.host,
                fixture.task,
                missing_review,
                fixture.verifier,
                report_path.name,
                report_path.read_bytes(),
            )
        report_path.write_text(json.dumps(report))
        devcrew_phase.submit_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            report,
            fixture.verifier,
            report_path.name,
            report_path.read_bytes(),
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        fixture.manifest = devcrew_phase.load_for_resume(
            fixture.path, fixture.receipt, fixture.host
        )
        suite = fixture.run_full_suite_record("contract-final-acceptance")
        devcrew_phase.accept_review(
            fixture.path,
            fixture.manifest,
            fixture.receipt,
            fixture.host,
            fixture.task,
            "P1",
            fixture.verifier,
            [suite],
        )
        fixture.receipt = devcrew_worktree.resolve(fixture.host, "T01")
        saved = devcrew_phase.load_for_resume(fixture.path, fixture.receipt, fixture.host)
        self.assertEqual(
            devcrew_phase.resume(saved, fixture.host, fixture.task, fixture.verifier)[
                "task_status"
            ],
            "TASK_DONE",
        )

    def test_hook_missing_runtime_reports_unavailable_without_losing_learning_context(self):
        subprocess.run(
            [
                sys.executable,
                "-B",
                str(ROOT / "scripts/devcrew.py"),
                "init",
                "--project",
                str(self.project),
            ],
            capture_output=True,
            check=True,
        )
        event = self.event("Sensitive message", hook_event_name="UserPromptSubmit")
        environment = dict(os.environ)
        environment.pop("PLUGIN_DATA", None)
        result = subprocess.run(
            [sys.executable, "-B", str(ROOT / "hooks/user_prompt_submit.py")],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            cwd=self.project,
            env=environment,
            check=True,
        )
        output = json.loads(result.stdout)
        context = output["hookSpecificOutput"]["additionalContext"]
        self.assertIn("DEV CREW PROJECT LEARNING", context)
        self.assertIn("ORIGINAL INTENT SOURCE CAPTURE: unavailable", context)
        self.assertIn("PLUGIN_DATA is unavailable", context)
        self.assertNotIn("Sensitive message", result.stdout)
