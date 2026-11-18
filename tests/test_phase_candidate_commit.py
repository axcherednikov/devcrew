import json
import multiprocessing
import pathlib
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import devcrew_phase
import devcrew_worktree
import devcrew_dispatch
import devcrew_context
import devcrew_cost
import test_phase_review_lifecycle as phase_review
from devcrew_test_helpers import run_check


def delayed_lifecycle_resolve(project, paused, resume, results):
    original = devcrew_worktree._atomic_json

    def delayed_write(*args, **kwargs):
        paused.set()
        if not resume.wait(timeout=30):
            raise TimeoutError("delayed resolve was not released")
        return original(*args, **kwargs)

    devcrew_worktree._atomic_json = delayed_write
    try:
        receipt = devcrew_worktree.resolve(project, "T01")
        results.put({"operation": "resolve", "outcome": "SUCCESS", "receipt": receipt})
    except ValueError as exc:
        results.put({"operation": "resolve", "outcome": str(exc)})
    finally:
        devcrew_worktree._atomic_json = original


def commit_candidate_process(project, task, manifest, receipt, bindings, results):
    def run_check(argv, _verification):
        checked = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                  "--project", str(project), "--execution-root", str(task),
                                  "--task-id", "T01", "--name", "receipt-race", "--", *argv],
                                 capture_output=True, text=True, timeout=20)
        if checked.returncode:
            raise AssertionError(checked.stderr)
        return checked.stdout.splitlines()[0]

    committed = devcrew_phase.candidate_commit(project, task, manifest, receipt, bindings,
                                               __import__("devcrew")._verified_record, run_check)
    results.put({"operation": "candidate", "outcome": "SUCCESS", "result": committed})


class CandidateCommitTests(unittest.TestCase):
    worktree_request = "Use a worktree for this task"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = pathlib.Path(self.temp.name) / "host"
        self.host.mkdir()
        self.git(self.host, "init", "-q")
        # Snapshot restores require Git to finish writing before each command returns.
        self.git(self.host, "config", "maintenance.auto", "false")
        self.git(self.host, "config", "user.name", "Test")
        self.git(self.host, "config", "user.email", "test@example.com")
        (self.host / ".gitignore").write_text("/.codex/\n")
        (self.host / "owned.txt").write_text("base\n")
        (self.host / "other.txt").write_text("base\n")
        (self.host / " owned.txt").write_text("outside base\n")
        (self.host / "tests").mkdir()
        (self.host / "tests/test_owned.py").write_text(
            "import unittest\nfrom pathlib import Path\n"
            "class OwnedTest(unittest.TestCase):\n"
            "    def test_owned(self):\n"
            "        self.assertEqual(Path('owned.txt').read_text(), 'candidate\\n')\n")
        self.git(self.host, "add", ".")
        self.git(self.host, "commit", "-qm", "base")
        self.base = self.git(self.host, "rev-parse", "HEAD")
        devcrew_worktree.create(self.host, "T01", self.base, worktree_request=self.worktree_request)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.task = pathlib.Path(self.receipt["task_worktree_root"])
        self.verifier = __import__("devcrew")._verified_record
        self.manifest = {
            "schema_version": 1, "task_id": "T01", "task_mode": "SINGLE_PHASE",
            "semantic_contract": {"capabilities": ["owned change"]},
            "worktree": {"repository_id": self.receipt["repository_id"],
                         "task_worktree_root": str(self.task), "branch": self.receipt["branch"],
                         "base_sha": self.base},
            "current_phase_id": "P1",
            "phases": [{"phase_id": "P1", "goal": "change owned file", "depends_on": [],
                        "owned_scope": ["owned.txt"], "acceptance": ["change is present"],
                        "verification": ["check owned behavior"], "commit_boundary": "owned change",
                        "commit_subject": "type(T01): change owned file", "status": "PLANNED"}],
        }
        planner = phase_review.issue_planner_receipt(self.host, "T01", self.base,
                                                     "planner payload", "planner-agent")
        self.manifest["planner_dispatch"] = {"spawn_receipt_id": planner["SPAWN_RECEIPT_ID"]}
        self.manifest["phases"][0]["verification_bindings"] = [
            {"index": 0, "verification": "check owned behavior",
             "argv": [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"]}]
        self.manifest_path = devcrew_phase.manifest_path(self.host, "T01")
        devcrew_phase.save_manifest(self.manifest_path, self.manifest, self.receipt)
        self.manifest = devcrew_phase.set_phase_status(self.manifest_path, self.manifest, self.receipt,
                                                       "P1", "EXECUTING")
        runtime = {"models": [{"id": "bounded-test", "capabilities": ["bounded"],
                               "efforts": ["low", "medium"]}],
                   "roles": {"executor-agent": {"available": True, "supports_overrides": True}},
                   "generic_available": True, "explicit_model_effort": True}
        prepared = devcrew_context.routed_task_spawn(
            "executor_T01", "Implement the accepted phase", devcrew_cost.route("executor", runtime=runtime),
            runtime, task_id="T01", project_root=self.host, task_worktree_root=self.task.resolve(),
            base_sha=self.base, owned_scope=self.manifest["phases"][0]["owned_scope"],
            acceptance=self.manifest["phases"][0]["acceptance"],
            verification=self.manifest["phases"][0]["verification"], receipt=self.receipt,
            role_contract="Implement only this phase.")
        route, executor_dispatch, executor_payload = prepared["route"], prepared["dispatch"], prepared["spawn"]
        executor_identity = {"TASK_ID": "T01", "PHASE_ID": "P1", "BASE_SHA": self.base,
                             "WRITER_CYCLE": 0, "REJECTED_CANDIDATE_SHA": None}
        executor = devcrew_dispatch.record_spawn(self.host, {
            "STATUS": "SPAWN_SUCCESS", "REQUESTED_ROLE": "executor", **executor_identity,
            "AGENT_ID": "executor-1", "SPAWN_MODEL": executor_payload["model"],
            "SPAWN_REASONING_EFFORT": executor_payload["reasoning_effort"],
            "SPAWN_MODE": executor_dispatch["SPAWN_MODE"],
            "FORK_TURNS": "none",
            "PREPARED": {"route": route, "dispatch": executor_dispatch},
            "SPAWN_PAYLOAD": executor_payload,
            "SPAWN_PAYLOAD_SHA256": devcrew_dispatch._canonical_digest(executor_payload),
        })
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        (self.task / "owned.txt").write_text("candidate\n")
        check_path, check_payload = run_check(
            self.host, execution_root=self.task, task_id="T01", name="preflight",
            command=[sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"])
        if check_payload["status"] != "passed":
            raise AssertionError(check_payload)
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        evidence = devcrew_phase.verify_evidence_records(
            [str(check_path)], self.host, self.task, "T01", self.verifier)
        self.manifest = devcrew_phase.set_phase_status(self.manifest_path, self.manifest, self.receipt,
                                                       "P1", "VERIFYING")
        self.receipt = devcrew_worktree.resolve(self.host, "T01")
        self.manifest = devcrew_phase.set_phase_status(self.manifest_path, self.manifest, self.receipt,
                                                       "P1", "READY_FOR_CLOSE", evidence, self.host,
                                                       self.verifier, executor["SPAWN_RECEIPT_ID"])
        self.bindings = pathlib.Path(self.temp.name) / "bindings.json"
        self.bindings.write_text(json.dumps([{"index": 0, "verification": "check owned behavior",
                                              "argv": [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests"]}]))

    def test_candidate_requires_full_suite_binding(self):
        bindings = json.loads(self.bindings.read_text())
        bindings[0]["argv"] += ["-p", "test_owned.py"]
        self.bindings.write_text(json.dumps(bindings))
        with self.assertRaisesRegex(devcrew_phase.PhaseError, "VERIFICATION_PLAN_CHANGED"):
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest, self.receipt,
                                           self.bindings, self.verifier,
                                           lambda *_: self.fail("checks must not run"))
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def _interrupt_candidate(self, boundary, *, after_replace=False):
        state_path = devcrew_worktree.location(self.host, "T01")[-1]
        original = devcrew_phase.os.replace

        def replace(source, destination):
            data = json.loads(pathlib.Path(source).read_text())
            hit = ((boundary == "receipt" and pathlib.Path(destination) == state_path and
                    data.get("expected_head") != self.base) or
                   (boundary == "manifest" and pathlib.Path(destination) == self.manifest_path and
                    data["phases"][0]["status"] == "COMMITTED") or
                   (boundary == "intent" and pathlib.Path(destination) == self.manifest_path and
                    "candidate_transition" in data and "sha" not in data["candidate_transition"]) or
                   (boundary == "sha" and pathlib.Path(destination) == self.manifest_path and
                    "sha" in data.get("candidate_transition", {})))
            if hit and not after_replace:
                raise OSError("injected replace failure")
            original(source, destination)
            if hit:
                raise OSError("injected failure after replace")

        def run_check(argv, _verification):
            checked = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                      "--project", str(self.host), "--execution-root", str(self.task),
                                      "--task-id", "T01", "--name", "recovery", "--", *argv],
                                     capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)
            return checked.stdout.splitlines()[0]

        with mock.patch.object(devcrew_phase.os, "replace", side_effect=replace):
            with self.assertRaises((OSError, devcrew_phase.PhaseError)):
                devcrew_phase.candidate_commit(self.host, self.task, self.manifest, self.receipt,
                                               self.bindings, self.verifier, run_check)
        return json.loads(self.manifest_path.read_text()), json.loads(state_path.read_text())

    def _resume_cli(self):
        return subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "phase", "resume",
                               "--project", str(self.host), "--task-id", "T01"],
                              capture_output=True, text=True)

    def test_failed_full_suite_cannot_commit_candidate(self):
        (self.task / "owned.txt").write_text("incorrect behavior\n")
        before = self.git(self.task, "rev-parse", "HEAD")
        result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "phase",
                                 "candidate-commit", "--project", str(self.host), "--task-id", "T01",
                                 "--verification-bindings", str(self.bindings)], capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), before)

    def test_candidate_commit_revalidates_persisted_executor_receipt(self):
        dispatch = self.manifest["phases"][0]["executor_dispatch"]
        store_path = self.host / ".codex/sessions/agent-dispatch-T01.json"
        store = json.loads(store_path.read_text())
        record = next(item for item in store["receipts"]
                      if item["SPAWN_RECEIPT_ID"] == dispatch["spawn_receipt_id"])
        record["AGENT_ID"] = "different-agent"
        store_path.write_text(json.dumps(store))
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest, self.receipt,
                                           self.bindings, self.verifier, lambda *_args: None)
        self.assertEqual(caught.exception.code, "INVALID_DISPATCH_ROUTE")

    @staticmethod
    def git(root, *args):
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                text=True, check=True)
        return result.stdout.strip()

    def _commit_ready_candidate(self, check_runner=None):
        """Commit the fixture candidate through the runtime API for focused cases."""
        if check_runner is None:
            def check_runner(argv, _verification):
                check_path, payload = run_check(
                    self.host, execution_root=self.task, task_id="T01", name="candidate",
                    command=list(argv))
                if payload["status"] != "passed":
                    self.fail(f"candidate verification failed: {payload}")
                return str(check_path)

        receipt = devcrew_worktree.resolve(self.host, "T01")
        manifest = devcrew_phase.load_for_resume(self.manifest_path, receipt, project=self.host)
        return devcrew_phase.candidate_commit(
            self.host, self.task, manifest, receipt, self.bindings, self.verifier, check_runner)

    def test_candidate_commit_binds_checks_and_stops_for_review(self):
        def run_check(argv, _verification):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                     "--project", str(self.host), "--execution-root", str(self.task),
                                     "--task-id", "T01", "--name", "candidate", "--", *argv],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.splitlines()[0]

        result = self._commit_ready_candidate(run_check)
        commit = result["commit"]
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), commit["sha"])
        self.assertEqual(self.git(self.task, "show", "-s", "--format=%P", "HEAD"), self.base)
        self.assertEqual(self.git(self.task, "show", "-s", "--format=%T", "HEAD"), commit["tree"])
        self.assertEqual(self.git(self.task, "status", "--porcelain", "--untracked-files=all"), "")
        saved = json.loads(self.manifest_path.read_text())
        self.assertEqual(saved["phases"][0]["status"], "COMMITTED")
        self.assertEqual(saved["phases"][0]["candidate_commit"]["evidence"], commit["evidence"])
        fresh = devcrew_worktree.resolve(self.host, "T01")
        response = devcrew_phase.resume(devcrew_phase.load_for_resume(self.manifest_path, fresh),
                                        self.host, self.task, self.verifier)
        self.assertEqual(response["status"], "PHASE_COMMITTED_AWAITING_REVIEW")
        with self.assertRaises(devcrew_phase.PhaseError):
            devcrew_phase.executor_input(saved, self.task)

    def test_manifest_conflict_during_checks_does_not_advance_git_or_receipt(self):
        def run_check(argv, _verification):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                     "--project", str(self.host), "--execution-root", str(self.task),
                                     "--task-id", "T01", "--name", "candidate-conflict", "--", *argv],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # An actual second process can update shared state while checks run.
            # A lock held across run_check would make this process time out.
            updater = subprocess.run([
                sys.executable, "-B", "-c",
                "import sys,json,pathlib; sys.path.insert(0,sys.argv[1]); import devcrew_phase as p; "
                "path=pathlib.Path(sys.argv[2]); receipt=json.loads(sys.argv[3]); "
                "manifest=p.load_for_resume(path,receipt); "
                "manifest['phases'][0]['goal']='Concurrent goal change'; "
                "p.save_manifest(path,manifest,receipt)",
                str(ROOT / "scripts"), str(self.manifest_path), json.dumps(self.receipt)],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(updater.returncode, 0, updater.stderr)
            return result.stdout.splitlines()[0]

        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest, self.receipt,
                                           self.bindings, self.verifier, run_check)
        self.assertEqual(caught.exception.code, "PHASE_MANIFEST_CONFLICT")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
        fresh = devcrew_worktree.resolve(self.host, "T01")
        self.assertEqual(fresh["expected_head"], self.base)
        actual = devcrew_phase.load_for_resume(self.manifest_path, fresh)
        self.assertEqual(actual["phases"][0]["status"], "READY_FOR_CLOSE")
        self.assertEqual(actual["phases"][0]["goal"], "Concurrent goal change")
        self.assertNotIn("candidate_commit", actual["phases"][0])

    def test_delayed_resolve_cannot_revert_successful_candidate_receipt(self):
        ctx = multiprocessing.get_context("spawn")
        paused, resume, results = ctx.Event(), ctx.Event(), ctx.Queue()
        resolver = ctx.Process(target=delayed_lifecycle_resolve,
                               args=(self.host, paused, resume, results))
        candidate = ctx.Process(target=commit_candidate_process,
                                args=(self.host, self.task, self.manifest, self.receipt, self.bindings, results))
        processes = [resolver, candidate]
        try:
            resolver.start()
            self.assertTrue(paused.wait(timeout=15), "resolve did not reach its stale write")
            candidate.start()
            committed = results.get(timeout=25)
            self.assertEqual(committed["operation"], "candidate")
            self.assertEqual(committed["outcome"], "SUCCESS")
            sha = committed["result"]["commit"]["sha"]
            state_path = devcrew_worktree.location(self.host, "T01")[-1]
            saved_receipt = json.loads(state_path.read_text())
            self.assertEqual(saved_receipt["expected_head"], sha)
            resume.set()
            resolved = results.get(timeout=15)
            self.assertEqual(resolved["operation"], "resolve")
            for process in processes:
                process.join(timeout=15)
                self.assertEqual(process.exitcode, 0)
            actual = json.loads(state_path.read_text())
            self.assertEqual(actual, saved_receipt, resolved)
            self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), sha)
            self.assertEqual(json.loads(self.manifest_path.read_text())["phases"][0]["candidate_commit"]["sha"], sha)
            fresh = devcrew_worktree.resolve(self.host, "T01")
            self.assertEqual(fresh["expected_head"], sha)
            devcrew_phase.load_for_resume(self.manifest_path, fresh)
            self.assertTrue(resolved["outcome"].startswith("TASK_WORKTREE_STATE_CONFLICT"), resolved)
        finally:
            resume.set()
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
            results.close()
            results.join_thread()


    def test_candidate_commit_rejects_force_staged_codex_runtime_state(self):
        runtime_file = self.task / ".codex" / "agents" / "local.toml"
        runtime_file.parent.mkdir(parents=True)
        runtime_file.write_text("reinstallable\n")
        self.git(self.task, "add", "-f", ".codex/agents/local.toml")
        self.manifest["phases"][0]["owned_scope"].append(".codex")

        with self.assertRaisesRegex(devcrew_phase.PhaseError, r"reinstallable \.codex runtime state"):
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier,
                                           lambda *_: self.fail("verification must not run"))

    def test_unrelated_pre_staged_path_is_rejected_and_preserved(self):
        (self.task / "other.txt").write_text("outside staged\n")
        self.git(self.task, "add", "other.txt")
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier, lambda *_a: None)
        self.assertEqual(caught.exception.code, "PHASE_SCOPE_VIOLATION", caught.exception.message)
        self.assertEqual(self.git(self.task, "diff", "--cached", "--name-only"), "other.txt")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)



    def test_candidate_commit_cli_records_and_commits_from_ordered_bindings(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "phase",
                                 "candidate-commit", "--project", str(self.host), "--task-id", "T01",
                                 "--verification-bindings", str(self.bindings)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "PHASE_COMMITTED_AWAITING_REVIEW")
        resumed = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "phase", "resume",
                                  "--project", str(self.host), "--task-id", "T01"],
                                 capture_output=True, text=True)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertEqual(json.loads(resumed.stdout)["status"], "PHASE_COMMITTED_AWAITING_REVIEW")

    def test_candidate_commit_verification_reads_type_and_metadata_once(self):
        self._commit_ready_candidate()
        saved = json.loads(self.manifest_path.read_text())
        phase = saved["phases"][0]
        calls = []
        original = devcrew_phase._git

        def recording_git(root, *args, **kwargs):
            if args and args[0] in {"show", "cat-file"}:
                calls.append(args)
            return original(root, *args, **kwargs)

        with mock.patch.object(devcrew_phase, "_git", side_effect=recording_git):
            devcrew_phase._verify_candidate_commit_object(saved, phase, self.task)

        self.assertEqual(calls, [("show", "-s", "--format=%H%n%P%n%T%n%B",
                                  phase["candidate_commit"]["sha"] + "^{commit}")])

    def test_candidate_commit_object_types_and_missing_sha_keep_inconsistent_error(self):
        self._commit_ready_candidate()
        saved = json.loads(self.manifest_path.read_text())
        phase = saved["phases"][0]
        sha = phase["candidate_commit"]["sha"]
        self.git(self.task, "tag", "-a", "candidate-tag", sha, "-m", "annotated candidate")
        object_shas = {
            "tag": self.git(self.task, "rev-parse", "refs/tags/candidate-tag"),
            "blob": self.git(self.task, "hash-object", "owned.txt"),
            "tree": phase["candidate_commit"]["tree"],
            "missing": "f" * len(sha),
        }
        for object_type, invalid_sha in object_shas.items():
            with self.subTest(object_type=object_type):
                altered_phase = {**phase, "candidate_commit": {**phase["candidate_commit"],
                                                                  "sha": invalid_sha}}
                with self.assertRaises(devcrew_phase.PhaseError) as caught:
                    devcrew_phase._verify_candidate_commit_object(saved, altered_phase, self.task)
                self.assertEqual(caught.exception.code, "PHASE_COMMIT_INCONSISTENT")

    def test_malformed_combined_commit_metadata_uses_legacy_validation(self):
        self._commit_ready_candidate()
        saved = json.loads(self.manifest_path.read_text())
        phase = saved["phases"][0]
        original = devcrew_phase._git
        calls = []

        def malformed_once(root, *args, **kwargs):
            if args and args[0] == "show":
                calls.append(args)
                if "--format=%H%n%P%n%T%n%B" in args:
                    return "malformed"
            return original(root, *args, **kwargs)

        with mock.patch.object(devcrew_phase, "_git", side_effect=malformed_once):
            devcrew_phase._verify_candidate_commit_object(saved, phase, self.task)

        self.assertEqual(calls, [
            ("show", "-s", "--format=%H%n%P%n%T%n%B",
             phase["candidate_commit"]["sha"] + "^{commit}"),
            ("show", "-s", "--format=%P", phase["candidate_commit"]["sha"]),
            ("show", "-s", "--format=%T", phase["candidate_commit"]["sha"]),
            ("show", "-s", "--format=%B", phase["candidate_commit"]["sha"]),
        ])

    def test_verifier_callback_replacement_is_seen_by_fresh_commit_validation(self):
        self._commit_ready_candidate()
        saved = json.loads(self.manifest_path.read_text())
        phase = saved["phases"][0]
        candidate = phase["candidate_commit"]
        replacement = self.git(self.task, "commit-tree", candidate["tree"], "-p", candidate["base"],
                               "-m", "replacement object")
        replaced = []

        def mutate_after_verification(*args, **kwargs):
            result = self.verifier(*args, **kwargs)
            if not replaced:
                self.git(self.task, "replace", candidate["sha"], replacement)
                replaced.append(True)
            return result

        try:
            with self.assertRaises(devcrew_phase.PhaseError) as caught:
                devcrew_phase._verify_candidate_evidence(self.host, self.task, saved, phase,
                                                          mutate_after_verification)
            self.assertEqual(caught.exception.code, "PHASE_COMMIT_INCONSISTENT")
            self.assertTrue(replaced)
        finally:
            if replaced:
                subprocess.run(["git", "-C", str(self.task), "replace", "-d", candidate["sha"]],
                               capture_output=True, text=True, check=True)


    def test_committed_resume_is_read_only_when_checkout_is_dirty(self):
        self._commit_ready_candidate()
        *_, state_path = devcrew_worktree.location(self.host, "T01")
        before = state_path.read_bytes()
        (self.task / "owned.txt").write_text("unexpected dirty state\n")
        failed = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "phase", "resume",
                                 "--project", str(self.host), "--task-id", "T01"],
                                capture_output=True, text=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("PHASE_COMMIT_INCONSISTENT", failed.stderr)
        self.assertEqual(state_path.read_bytes(), before)

    def test_verification_receipt_for_different_tree_is_rejected(self):
        def run_check(argv, _verification):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                     "--project", str(self.host), "--execution-root", str(self.task),
                                     "--task-id", "T01", "--name", "candidate", "--", *argv],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            record_path = pathlib.Path(result.stdout.splitlines()[0])
            payload = json.loads(record_path.read_text())
            payload["candidate_tree_before"] = "0" * len(self.base)
            record_path.write_text(json.dumps(payload))
            return str(record_path)
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier,
                                           run_check)
        self.assertEqual(caught.exception.code, "STALE_PHASE_EVIDENCE")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_stale_verification_cannot_commit_changed_candidate(self):
        def run_check(argv, _verification):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                     "--project", str(self.host), "--execution-root", str(self.task),
                                     "--task-id", "T01", "--name", "candidate", "--", *argv],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            (self.task / "owned.txt").write_text("changed after verification\n")
            return result.stdout.splitlines()[0]
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier,
                                           run_check)
        self.assertEqual(caught.exception.code, "INVALID_PHASE_EVIDENCE")
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_committed_resume_detects_corrupt_candidate_tree(self):
        self._commit_ready_candidate()
        saved = json.loads(self.manifest_path.read_text())
        saved["phases"][0]["candidate_commit"]["tree"] = "0" * len(self.base)
        self.manifest_path.write_text(json.dumps(saved))
        fresh = devcrew_worktree.resolve(self.host, "T01")
        loaded = devcrew_phase.load_for_resume(self.manifest_path, fresh)
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.resume(loaded, self.host, self.task, self.verifier)
        self.assertEqual(caught.exception.code, "PHASE_COMMIT_INCONSISTENT")



    def test_leading_space_staged_deletion_is_outside_scope(self):
        self.git(self.task, "rm", "--", " owned.txt")
        (self.task / "owned.txt").write_text("candidate edit\n")
        def run_check(argv, _verification):
            result = subprocess.run([sys.executable, str(ROOT / "scripts/devcrew.py"), "run-check",
                                     "--project", str(self.host), "--execution-root", str(self.task),
                                     "--task-id", "T01", "--name", "candidate", "--", *argv],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.splitlines()[0]
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier, run_check)
        self.assertEqual(caught.exception.code, "PHASE_SCOPE_VIOLATION")
        self.assertIn(" owned.txt", caught.exception.message)

    def test_pre_staged_rename_checks_both_source_and_destination_scope(self):
        self.manifest["phases"][0]["owned_scope"] = ["inside.txt"]
        devcrew_phase.save_manifest(self.manifest_path, self.manifest, self.receipt)
        self.git(self.task, "checkout", "--", "owned.txt")
        self.git(self.task, "mv", "other.txt", "inside.txt")
        changed = self.git(self.task, "diff", "--no-renames", "--name-only", "HEAD").splitlines()
        self.assertEqual(set(changed), {"inside.txt", "other.txt"})
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier, lambda *_a: None)
        self.assertEqual(caught.exception.code, "PHASE_SCOPE_VIOLATION", caught.exception.message)
        self.assertIn("other.txt", caught.exception.message)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)

    def test_out_of_scope_change_is_rejected_before_commit(self):
        (self.task / "other.txt").write_text("outside\n")
        with self.assertRaises(devcrew_phase.PhaseError) as caught:
            devcrew_phase.candidate_commit(self.host, self.task, self.manifest,
                                           devcrew_worktree.resolve(self.host, "T01"),
                                           self.bindings, self.verifier, lambda *_a: None)
        self.assertEqual(caught.exception.code, "PHASE_SCOPE_VIOLATION", caught.exception.message)
        self.assertEqual(self.git(self.task, "rev-parse", "HEAD"), self.base)
