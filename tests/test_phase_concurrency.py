import copy
import json
import multiprocessing
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import devcrew_phase
from test_task_phases import plan


def update_manifest(
    path, receipt, operation, barrier, first_saved, first, results, write_gates=None
):
    """Both processes validate the same snapshot before their ordered writes."""
    atomic_json = devcrew_phase._atomic_json
    replace_json = getattr(devcrew_phase, "_replace_json", None)
    try:
        manifest = devcrew_phase.load_for_resume(path, receipt)
        loaded = json.loads(json.dumps(manifest))

        def synchronized_write(*args, **kwargs):
            barrier.wait(timeout=15)
            if not first:
                if write_gates is not None:
                    paused, attempted, release, finished = write_gates
                    if not paused.wait(timeout=15):
                        raise TimeoutError("first writer did not enter the critical section")
                    attempted.set()
                elif not first_saved.wait(timeout=15):
                    raise TimeoutError("first writer did not finish")
            return atomic_json(*args, **kwargs)

        devcrew_phase._atomic_json = synchronized_write
        if first and write_gates is not None:

            def paused_replace(*args, **kwargs):
                paused, attempted, release, finished = write_gates
                paused.set()
                if not release.wait(timeout=15):
                    raise TimeoutError("first writer was not released")
                return replace_json(*args, **kwargs)

            devcrew_phase._replace_json = paused_replace
        if operation == "goal":
            manifest["phases"][0]["goal"] = "Updated health endpoint goal"
            updated = devcrew_phase.save_manifest(path, manifest, receipt)
        else:
            updated = devcrew_phase.set_phase_status(path, manifest, receipt, "P1", operation)
        results.put(
            {
                "operation": operation,
                "outcome": "SUCCESS",
                "loaded": loaded,
                "saved": json.loads(json.dumps(updated)),
            }
        )
    except devcrew_phase.PhaseError as exc:
        results.put({"operation": operation, "outcome": exc.code, "loaded": loaded})
    finally:
        devcrew_phase._atomic_json = atomic_json
        if replace_json is not None:
            devcrew_phase._replace_json = replace_json
        if first:
            first_saved.set()
        elif write_gates is not None:
            write_gates[3].set()


class PhaseConcurrencyTests(unittest.TestCase):
    def fixture(self, root):
        manifest = plan()
        manifest["worktree"]["task_worktree_root"] = str(root)
        receipt = {
            "owner": "DEVCREW",
            "state": "RESOLVED",
            "repository_id": "repo",
            "task_id": "T01",
            "task_worktree_root": str(root),
            "branch": "codex/tasks/T01",
            "expected_head": "a" * 40,
        }
        path = root / "T01.phases.json"
        manifest = devcrew_phase.save_manifest(path, manifest, receipt)
        manifest = devcrew_phase.set_phase_status(path, manifest, receipt, "P1", "EXECUTING")
        return path, receipt, manifest

    def concurrent_updates(self, first, second, hold_lock=False):
        ctx = multiprocessing.get_context("spawn")
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, initial = self.fixture(pathlib.Path(directory))
            barrier, first_saved, results = ctx.Barrier(2), ctx.Event(), ctx.Queue()
            write_gates = tuple(ctx.Event() for _ in range(4)) if hold_lock else None
            processes = [
                ctx.Process(
                    target=update_manifest,
                    args=(
                        path,
                        receipt,
                        operation,
                        barrier,
                        first_saved,
                        index == 0,
                        results,
                        write_gates,
                    ),
                )
                for index, operation in enumerate((first, second))
            ]
            try:
                for process in processes:
                    process.start()
                if hold_lock:
                    paused, attempted, release, finished = write_gates
                    self.assertTrue(paused.wait(timeout=15))
                    self.assertTrue(attempted.wait(timeout=15))
                    self.assertFalse(
                        finished.wait(timeout=0.2), "second writer escaped the held manifest lock"
                    )
                    release.set()
                outcomes = [results.get(timeout=20) for _ in processes]
                for process in processes:
                    process.join(timeout=20)
                    self.assertEqual(process.exitcode, 0)
                by_operation = {result["operation"]: result for result in outcomes}
                self.assertEqual(by_operation[first]["loaded"], initial)
                self.assertEqual(by_operation[second]["loaded"], initial)
                self.assertEqual(by_operation[first]["outcome"], "SUCCESS")
                self.assertEqual(by_operation[second]["outcome"], "PHASE_MANIFEST_CONFLICT")
                actual = devcrew_phase.load_for_resume(path, receipt)
                self.assertEqual(actual, by_operation[first]["saved"])
                return actual
            finally:
                if write_gates is not None:
                    write_gates[2].set()
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                results.close()
                results.join_thread()

    def test_conflicting_status_transitions_reject_stale_writer(self):
        for iteration in range(5):
            with self.subTest(iteration=iteration):
                actual = self.concurrent_updates("BLOCKED", "VERIFYING")
                self.assertEqual(actual["phases"][0]["status"], "BLOCKED")

    def test_assessed_v2_competing_status_writers_preserve_original_intent(self):
        sys.path.insert(0, str(ROOT / "tests"))
        import test_pre_candidate_reapproval

        source_fixture = test_pre_candidate_reapproval.PreCandidateReapprovalTests()
        source_fixture.setUp()
        try:
            v2 = source_fixture.v2_plan()
            devcrew_phase.pin_verification(v2["phases"][0])
            planner = test_pre_candidate_reapproval.planner_receipt(
                source_fixture.host, "T01", source_fixture.base, "v2 CAS concurrency fixture"
            )
            assessment = source_fixture.v2_assessment(v2)
            path = devcrew_phase.manifest_path(source_fixture.host, "T01")
            path.unlink()
            initial = devcrew_phase.initialize_manifest(
                path,
                v2,
                source_fixture.receipt,
                source_fixture.host,
                planner["SPAWN_RECEIPT_ID"],
                intent_assessment={"assessment": assessment, "decisions": []},
            )
            initial = devcrew_phase.set_phase_status(
                path, initial, source_fixture.receipt, "P1", "EXECUTING"
            )
            intent_before = copy.deepcopy(initial["original_intent"])
            ctx = multiprocessing.get_context("spawn")
            barrier, first_saved, results = ctx.Barrier(2), ctx.Event(), ctx.Queue()
            processes = [
                ctx.Process(
                    target=update_manifest,
                    args=(
                        path,
                        source_fixture.receipt,
                        operation,
                        barrier,
                        first_saved,
                        index == 0,
                        results,
                    ),
                )
                for index, operation in enumerate(("BLOCKED", "VERIFYING"))
            ]
            try:
                for process in processes:
                    process.start()
                outcomes = [results.get(timeout=30) for _ in processes]
                for process in processes:
                    process.join(timeout=30)
                    self.assertEqual(process.exitcode, 0)
                by_operation = {item["operation"]: item for item in outcomes}
                self.assertEqual(by_operation["BLOCKED"]["loaded"], initial)
                self.assertEqual(by_operation["VERIFYING"]["loaded"], initial)
                self.assertEqual(by_operation["BLOCKED"]["outcome"], "SUCCESS")
                self.assertEqual(by_operation["VERIFYING"]["outcome"], "PHASE_MANIFEST_CONFLICT")
                actual = devcrew_phase.load_for_resume(
                    path, source_fixture.receipt, source_fixture.host
                )
                self.assertEqual(actual["phases"][0]["status"], "BLOCKED")
                self.assertEqual(actual["original_intent"], intent_before)
            finally:
                for process in processes:
                    if process.is_alive():
                        process.terminate()
                        process.join(timeout=5)
                results.close()
                results.join_thread()
        finally:
            source_fixture.doCleanups()

    def test_independent_goal_and_status_updates_reject_stale_writer(self):
        for first, second in (("goal", "VERIFYING"), ("VERIFYING", "goal")):
            for iteration in range(5):
                with self.subTest(first=first, iteration=iteration):
                    actual = self.concurrent_updates(first, second)
                    self.assertEqual(
                        actual["phases"][0]["goal"],
                        "Updated health endpoint goal"
                        if first == "goal"
                        else "Add health endpoint",
                    )
                    self.assertEqual(
                        actual["phases"][0]["status"],
                        "VERIFYING" if first == "VERIFYING" else "EXECUTING",
                    )

    def test_fresh_retry_preserves_both_independent_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, manifest = self.fixture(pathlib.Path(directory))
            changed = copy.deepcopy(manifest)
            changed["phases"][0]["goal"] = "Updated health endpoint goal"
            devcrew_phase.save_manifest(path, changed, receipt)
            fresh = devcrew_phase.load_for_resume(path, receipt)
            devcrew_phase.set_phase_status(path, fresh, receipt, "P1", "VERIFYING")
            actual = devcrew_phase.load_for_resume(path, receipt)
            self.assertEqual(actual["phases"][0]["goal"], "Updated health endpoint goal")
            self.assertEqual(actual["phases"][0]["status"], "VERIFYING")

    def test_revision_check_and_replace_share_an_interprocess_lock(self):
        actual = self.concurrent_updates("BLOCKED", "VERIFYING", hold_lock=True)
        self.assertEqual(actual["phases"][0]["status"], "BLOCKED")

    def test_legacy_manifest_is_upgraded_without_allowing_a_stale_write(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, manifest = self.fixture(pathlib.Path(directory))
            manifest.pop("manifest_revision")
            path.write_text(json.dumps(manifest))
            first = devcrew_phase.load_for_resume(path, receipt)
            stale = devcrew_phase.load_for_resume(path, receipt)
            updated = devcrew_phase.set_phase_status(path, first, receipt, "P1", "BLOCKED")
            self.assertEqual(updated["manifest_revision"], 1)
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_MANIFEST_CONFLICT"):
                devcrew_phase.set_phase_status(path, stale, receipt, "P1", "VERIFYING")
            self.assertEqual(json.loads(path.read_text()), updated)

    def test_manifest_aba_rejects_original_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, original = self.fixture(pathlib.Path(directory))
            changed = copy.deepcopy(original)
            changed["phases"][0]["goal"] = "temporary goal"
            devcrew_phase.save_manifest(path, changed, receipt)
            changed["phases"][0]["goal"] = original["phases"][0]["goal"]
            restored = devcrew_phase.save_manifest(path, changed, receipt)
            self.assertEqual(restored["phases"], original["phases"])
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_MANIFEST_CONFLICT"):
                devcrew_phase.save_manifest(path, original, receipt)
            self.assertEqual(json.loads(path.read_text()), restored)

    def test_invalid_manifest_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, manifest = self.fixture(pathlib.Path(directory))
            for revision in (True, -1, "1", None):
                with self.subTest(revision=revision):
                    invalid = copy.deepcopy(manifest)
                    invalid["manifest_revision"] = revision
                    with self.assertRaisesRegex(
                        devcrew_phase.PhaseError, "INVALID_MANIFEST_REVISION"
                    ):
                        devcrew_phase.save_manifest(path, invalid, receipt)
                    self.assertEqual(json.loads(path.read_text()), manifest)

    def test_unavailable_file_locks_fail_without_changing_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, manifest = self.fixture(pathlib.Path(directory))
            with mock.patch.object(devcrew_phase.fs, "lock", side_effect=OSError("unavailable")):
                with self.assertRaisesRegex(
                    devcrew_phase.PhaseError, "PHASE_MANIFEST_LOCK_UNSUPPORTED"
                ):
                    devcrew_phase.set_phase_status(path, manifest, receipt, "P1", "BLOCKED")
            self.assertEqual(devcrew_phase.load_for_resume(path, receipt), manifest)

    def test_conflict_does_not_run_coupled_mutations(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, stale = self.fixture(pathlib.Path(directory))
            updated = devcrew_phase.set_phase_status(path, stale, receipt, "P1", "BLOCKED")
            mutation = mock.Mock()
            with self.assertRaisesRegex(devcrew_phase.PhaseError, "PHASE_MANIFEST_CONFLICT"):
                devcrew_phase._atomic_json(path, stale, before_save=mutation)
            mutation.assert_not_called()
            self.assertEqual(json.loads(path.read_text()), updated)

    def test_failed_write_releases_lock_and_does_not_consume_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            path, receipt, manifest = self.fixture(pathlib.Path(directory))
            with mock.patch.object(
                devcrew_phase.os, "replace", side_effect=OSError("failed replace")
            ):
                with self.assertRaisesRegex(OSError, "failed replace"):
                    devcrew_phase.set_phase_status(path, manifest, receipt, "P1", "BLOCKED")
            self.assertEqual(json.loads(path.read_text()), manifest)
            ctx = multiprocessing.get_context("spawn")
            results = ctx.Queue()
            barrier, first_saved = ctx.Barrier(1), ctx.Event()
            process = ctx.Process(
                target=update_manifest,
                args=(path, receipt, "BLOCKED", barrier, first_saved, True, results),
            )
            try:
                process.start()
                result = results.get(timeout=20)
                process.join(timeout=20)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(result["outcome"], "SUCCESS")
                self.assertEqual(
                    result["saved"]["manifest_revision"], manifest["manifest_revision"] + 1
                )
            finally:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=5)
                results.close()
                results.join_thread()


if __name__ == "__main__":
    unittest.main()
