import hashlib
import importlib.util
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import validate_review_quality as validator


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def canon(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def copy_review_source(root):
    for relative in (
        ".codex-plugin/plugin.json",
        "skills/devcrew-review/SKILL.md",
        "skills/devcrew-review/references/coverage.md",
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / relative, target)


class ReviewQualityEvalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(directory.cleanup)
        cls.source = pathlib.Path(directory.name).resolve()
        copy_review_source(cls.source)
        git = ["git", "-C", str(cls.source)]
        subprocess.run([*git, "init", "--quiet"], check=True)
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run(
            [
                *git,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "-c",
                "core.hooksPath=disabled-hooks",
                "commit",
                "--quiet",
                "--no-gpg-sign",
                "-m",
                "Review fixture",
            ],
            check=True,
        )
        cls.head = subprocess.check_output([*git, "rev-parse", "HEAD"], text=True).strip()

    def fixture_run(self, variant):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = pathlib.Path(temp.name) / variant
        root.mkdir()
        source = ROOT / "evaluation/fixtures/review-quality" / variant
        shutil.copy(source / "invoice_api.py", root / "invoice_api.py")
        shutil.copy(ROOT / "evaluation/fixtures/review-quality/check.py", root / "check.py")
        obs = pathlib.Path(temp.name).resolve() / "observations"
        run_id = f"run-{variant}-unique"
        result = subprocess.run(
            [
                "python3",
                "-B",
                "check.py",
                "--run-id",
                run_id,
                "--fixture",
                variant,
                "--observations",
                str(obs),
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = json.loads((obs / f"{run_id}.json").read_text())
        return root, obs, run_id, result, marker

    def test_real_probe_executes_both_fixture_variants(self):
        a_root, _, _, _, a = self.fixture_run("a")
        b_root, _, _, _, b = self.fixture_run("b")
        self.assertEqual(a["invoice_calls"], [["tenant-a", "invoice-17"]])
        self.assertEqual(a["tenant_results"][0], a["tenant_results"][1])
        self.assertEqual(
            b["invoice_calls"], [["tenant-a", "invoice-17"], ["tenant-b", "invoice-17"]]
        )
        self.assertNotEqual(b["tenant_results"][0], b["tenant_results"][1])
        self.assertEqual(a["explicit_zero"], {"raised": False, "value": 25})
        self.assertEqual(b["explicit_zero"], {"raised": True, "type": "ValueError"})
        for result in (a, b):
            self.assertEqual(result["absent_limit_default"], 25)
            self.assertEqual(result["valid_boundary"], 100)
        self.assertEqual(a["cwd"], str(a_root.resolve()))
        self.assertEqual(b["cwd"], str(b_root.resolve()))

    def valid_bundle(self, variant="a"):
        root, obs, run_id, proc, marker = self.fixture_run(variant)
        findings = (
            [
                {
                    "id": "finding-opaque-01",
                    "severity": "P1",
                    "location": "invoice_api.py:get_invoice",
                    "trigger": "same invoice ID across two tenants in shared cache",
                    "consequence": "cross-tenant invoice reuse",
                    "evidence": "key is invoice_id only",
                    "verification": "static trace and probe",
                    "fix": "include tenant_id",
                },
                {
                    "id": "finding-opaque-02",
                    "severity": "P2",
                    "location": "invoice_api.py:listing_limit",
                    "trigger": "listing_limit(0)",
                    "consequence": "zero accepted as default",
                    "evidence": "raw or 25",
                    "verification": "static trace and probe",
                    "fix": "default only for None",
                },
            ]
            if variant == "a"
            else []
        )
        manifest_path = self.source / ".codex-plugin/plugin.json"
        manifest = json.loads(manifest_path.read_text())
        identity = {
            "kind": "source",
            "manifest": str(manifest_path),
            "manifest_sha256": sha(manifest_path.read_bytes()),
            "head": self.head,
            "version": manifest["version"],
            "root": str(self.source),
        }
        skill_path = str(self.source / "skills/devcrew-review/SKILL.md")
        coverage_path = str(self.source / "skills/devcrew-review/references/coverage.md")
        skill_digest = sha(pathlib.Path(skill_path).read_bytes())
        coverage_digest = sha(pathlib.Path(coverage_path).read_bytes())
        bundle_digests = {"SKILL.md": skill_digest, "references/coverage.md": coverage_digest}
        initial_manifest = validator.fixture_manifest(root, obs)
        initial = sha(canon(initial_manifest))
        expected = {
            "schema_version": 1,
            "pair_id": "pair-unique",
            "run_id": run_id,
            "fixture": variant,
            "cwd": str(root.resolve()),
            "command_argv": [
                "python3",
                "-B",
                "check.py",
                "--run-id",
                run_id,
                "--fixture",
                variant,
                "--observations",
                str(obs),
            ],
            "expected_scope": ["invoice_api.py"],
            "copy_identity": identity,
            "identity_sha256": sha(canon(identity)),
            "skill_path": skill_path,
            "skill_sha256": skill_digest,
            "coverage_path": coverage_path,
            "skill_bundle_digests": bundle_digests,
            "initial_fixture_fingerprint": initial,
            "initial_fixture_manifest": initial_manifest,
            "observation_dir": str(obs),
            "marker_absent": True,
            "oracle_sha256": "c" * 64,
        }
        findings_sha = sha(canon(findings))
        evidence = {
            "schema_version": 1,
            "pair_id": "pair-unique",
            "run_id": run_id,
            "fixture": variant,
            "identity_sha256": sha(canon(identity)),
            "copy_identity": identity,
            "expected_sha256": sha(canon(expected)),
            "native_session_id": "session-unique",
            "terminal_turn_id": "turn-1",
            "terminal_status": "completed",
            "skill_bundle_digests": bundle_digests,
            "command_event": {
                "fresh_session_id": "session-unique",
                "terminal_turn_id": "turn-1",
                "event_id": "event-command",
                "status": "completed",
                "exit_code": 0,
                "truncated": False,
                "argv": expected["command_argv"],
                "cwd": str(root.resolve()),
                "stdout": proc.stdout,
                "stdout_sha256": sha(proc.stdout.encode()),
            },
            "skill_read": {
                "event_id": "event-read",
                "session_id": "session-unique",
                "turn_id": "turn-1",
                "status": "completed",
                "exit_code": 0,
                "argv": ["cat", expected["skill_path"]],
                "path": expected["skill_path"],
                "sha256": skill_digest,
                "bundle_digests": bundle_digests,
                "truncated": False,
            },
            "coverage_read": {
                "event_id": "event-coverage",
                "session_id": "session-unique",
                "turn_id": "turn-1",
                "status": "completed",
                "exit_code": 0,
                "argv": ["cat", expected["coverage_path"]],
                "cwd": identity["root"],
                "path": expected["coverage_path"],
                "sha256": bundle_digests["references/coverage.md"],
                "stdout_sha256": bundle_digests["references/coverage.md"],
                "truncated": False,
            },
            "skill_output_sha256": skill_digest,
            "probe_output": {
                "truncated": False,
                "exit_code": 0,
                "result": marker,
                "marker_sha256": sha((obs / f"{run_id}.json").read_bytes()),
                "stdout_sha256": sha(proc.stdout.encode()),
            },
            "marker_path": str(obs / f"{run_id}.json"),
            "marker_sha256": sha((obs / f"{run_id}.json").read_bytes()),
            "final_fixture_fingerprint": sha(canon(validator.fixture_manifest(root, obs))),
            "final_fixture_manifest": validator.fixture_manifest(root, obs),
            "subject_findings_sha256": findings_sha,
        }
        subject = {
            "pair_id": "pair-unique",
            "run_id": run_id,
            "fixture": variant,
            "findings": findings,
            **(
                {}
                if variant == "a"
                else {
                    "outcome_rationale": "The tenant key is scoped and zero is rejected by the validator."
                }
            ),
        }
        semantic = {
            "pair_id": "pair-unique",
            "run_id": run_id,
            "fixture": variant,
            "subject_findings_sha256": findings_sha,
            "oracle_sha256": expected["oracle_sha256"],
            "evaluator_id": "independent-evaluator",
            "verdict": "PASS",
            **(
                {}
                if variant == "a"
                else {"no_findings_rationale_sha256": sha(subject["outcome_rationale"].encode())}
            ),
            "expected_findings": (
                [
                    {
                        "oracle_id": oracle_id,
                        "finding_id": finding_id,
                        "matched": True,
                        "trigger": True,
                        "consequence": True,
                        "location": True,
                    }
                    for oracle_id, finding_id in (
                        ("tenant-cache-isolation", "finding-opaque-01"),
                        ("zero-limit-defaulted", "finding-opaque-02"),
                    )
                ]
                if variant == "a"
                else []
            ),
            "unsupported_actionable_ids": [],
        }
        return expected, evidence, subject, semantic

    def rebind_to_exported_snapshot(self, bundle, root):
        expected, evidence, _, _ = bundle
        copy_review_source(root)
        skill_rel = pathlib.Path("skills/devcrew-review/SKILL.md")
        coverage_rel = pathlib.Path("skills/devcrew-review/references/coverage.md")
        identity = dict(
            expected["copy_identity"],
            root=str(root.resolve()),
            manifest=str((root / ".codex-plugin/plugin.json").resolve()),
            manifest_sha256=sha((root / ".codex-plugin/plugin.json").read_bytes()),
        )
        expected["copy_identity"] = identity
        expected["identity_sha256"] = sha(canon(identity))
        expected["skill_path"] = str((root / skill_rel).resolve())
        expected["coverage_path"] = str((root / coverage_rel).resolve())
        expected["skill_sha256"] = sha((root / skill_rel).read_bytes())
        expected["skill_bundle_digests"] = {
            "SKILL.md": expected["skill_sha256"],
            "references/coverage.md": sha((root / coverage_rel).read_bytes()),
        }
        evidence["copy_identity"] = identity
        evidence["identity_sha256"] = sha(canon(identity))
        evidence["expected_sha256"] = sha(canon(expected))
        evidence["skill_bundle_digests"] = expected["skill_bundle_digests"]
        evidence["skill_read"].update(
            path=expected["skill_path"],
            argv=["cat", expected["skill_path"]],
            sha256=expected["skill_sha256"],
        )
        evidence["coverage_read"].update(
            path=expected["coverage_path"],
            argv=["cat", expected["coverage_path"]],
            sha256=expected["skill_bundle_digests"]["references/coverage.md"],
            stdout_sha256=expected["skill_bundle_digests"]["references/coverage.md"],
        )
        evidence["skill_output_sha256"] = expected["skill_sha256"]
        return bundle

    def test_accepts_consistent_a_and_b_evidence(self):
        for variant in ("a", "b"):
            with self.subTest(variant=variant):
                result = validator.validate(*self.valid_bundle(variant))
                self.assertEqual(result["status"], "PASS")

    def test_rejects_constant_output_missing_identity_and_incomplete_events(self):
        mutations = (
            lambda e, s, m: s.update(findings="PASS"),
            lambda e, s, m: e["skill_read"].update(truncated=True),
            lambda e, s, m: e["command_event"].update(exit_code=1),
            lambda e, s, m: e["command_event"].update(status="running"),
            lambda e, s, m: e["probe_output"].update(truncated=True),
            lambda e, s, m: e.pop("marker_path"),
            lambda e, s, m: e["command_event"].update(fresh_session_id="other"),
            lambda e, s, m: e["command_event"].update(terminal_turn_id="other"),
            lambda e, s, m: e["skill_read"].update(path="other/SKILL.md"),
            lambda e, s, m: e["skill_read"].update(session_id="stale"),
            lambda e, s, m: e["coverage_read"].update(truncated=True),
            lambda e, s, m: e["probe_output"].update(stdout_sha256="0" * 64),
            lambda e, s, m: e["copy_identity"].update(head="f" * 40),
        )
        for change in mutations:
            bundle = list(self.valid_bundle())
            change(bundle[1], bundle[2], bundle[3])
            with self.subTest(change=change), self.assertRaises(ValueError):
                validator.validate(*bundle)

    def test_rejects_tampered_marker_reuse_wrong_cwd_and_fixture_changes(self):
        # Marker tampering.
        bundle = list(self.valid_bundle())
        pathlib.Path(bundle[1]["marker_path"]).write_text("{}")
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        # Wrong run and cwd are independently bound.
        bundle = list(self.valid_bundle())
        bundle[2]["run_id"] = "stale-run"
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        bundle = list(self.valid_bundle())
        bundle[1]["command_event"]["cwd"] = "/tmp"
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        # A source mutation after the host's pre-run fingerprint is detected.
        bundle = list(self.valid_bundle())
        root = pathlib.Path(bundle[0]["cwd"])
        (root / "invoice_api.py").write_text("# modified\n")
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_rejects_semantic_mismatch_and_unsupported_b_finding(self):
        bundle = list(self.valid_bundle())
        bundle[3]["expected_findings"][0]["trigger"] = False
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        bundle = list(self.valid_bundle("b"))
        bundle[3]["unsupported_actionable_ids"] = ["style-only"]
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_rejects_symlinks_and_marker_outside_allocated_directory(self):
        bundle = list(self.valid_bundle())
        root = pathlib.Path(bundle[0]["cwd"])
        (root / "link.py").symlink_to(root / "invoice_api.py")
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        bundle = list(self.valid_bundle())
        bundle[1]["marker_path"] = str(pathlib.Path(bundle[0]["cwd"]) / "marker.json")
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_accepts_exported_source_snapshot_and_rejects_snapshot_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            bundle = self.rebind_to_exported_snapshot(
                list(self.valid_bundle()), pathlib.Path(directory) / "source-snapshot"
            )
            self.assertFalse((pathlib.Path(bundle[0]["copy_identity"]["root"]) / ".git").exists())
            self.assertEqual(validator.validate(*bundle)["status"], "PASS")
            coverage_path = pathlib.Path(bundle[0]["copy_identity"]["root"]) / bundle[0][
                "coverage_path"
            ].removeprefix(bundle[0]["copy_identity"]["root"] + "/")
            coverage_path.unlink()
            coverage_path.symlink_to(ROOT / "skills/devcrew-review/references/coverage.md")
            with self.assertRaises(ValueError):
                validator.validate(*bundle)

    def test_rejects_rebound_source_identity_with_foreign_root(self):
        bundle = list(self.valid_bundle())
        expected, evidence, _, _ = bundle
        with tempfile.TemporaryDirectory(prefix="foreign-plugin-") as foreign_dir:
            fake_root = pathlib.Path(foreign_dir).resolve()
            manifest_path = fake_root / ".codex-plugin/plugin.json"
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps({"name": "devcrew", "version": "0.20.0"}))
            identity = dict(
                expected["copy_identity"],
                kind="installed",
                root=str(fake_root),
                manifest=str(manifest_path),
                version="0.20.0",
                manifest_sha256=sha(manifest_path.read_bytes()),
            )
            expected["copy_identity"] = identity
            expected["identity_sha256"] = sha(canon(identity))
            evidence["copy_identity"] = identity
            evidence["identity_sha256"] = sha(canon(identity))
            evidence["expected_sha256"] = sha(canon(expected))
            with self.assertRaises(ValueError):
                validator.validate(*bundle)

    def test_rejects_missing_expected_argv_or_oracle_digest(self):
        bundle = list(self.valid_bundle())
        del bundle[0]["command_argv"]
        bundle[1]["expected_sha256"] = sha(canon(bundle[0]))
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        bundle = list(self.valid_bundle())
        del bundle[0]["oracle_sha256"]
        bundle[1]["expected_sha256"] = sha(canon(bundle[0]))
        bundle[3]["oracle_sha256"] = None
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_rejects_boolean_values_in_integer_status_fields_and_schema_versions(self):
        mutations = (
            lambda b: b[0].update(schema_version=True),
            lambda b: b[1].update(schema_version=True),
            lambda b: b[1]["command_event"].update(exit_code=False),
            lambda b: b[1]["skill_read"].update(exit_code=False),
            lambda b: b[1]["coverage_read"].update(exit_code=False),
            lambda b: b[1]["probe_output"].update(exit_code=False),
        )
        for mutate in mutations:
            bundle = list(self.valid_bundle())
            mutate(bundle)
            bundle[1]["expected_sha256"] = sha(canon(bundle[0]))
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validator.validate(*bundle)
        bundle = list(self.valid_bundle())
        evidence = bundle[1]
        marker_path = pathlib.Path(evidence["marker_path"])
        marker = json.loads(marker_path.read_text())
        marker["schema_version"] = True
        marker_raw = canon(marker) + b"\n"
        marker_path.write_bytes(marker_raw)
        marker_sha = sha(marker_raw)
        marker_line = f"marker={marker_path} sha256={marker_sha}\n".encode()
        stdout = marker_raw + marker_line
        evidence["marker_sha256"] = marker_sha
        evidence["probe_output"].update(
            result=marker, marker_sha256=marker_sha, stdout_sha256=sha(stdout)
        )
        evidence["command_event"].update(stdout=stdout.decode("utf-8"), stdout_sha256=sha(stdout))
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_rejects_conflicting_extra_stdout_after_rehashing(self):
        bundle = list(self.valid_bundle())
        evidence = bundle[1]
        marker_path = pathlib.Path(evidence["marker_path"])
        marker_raw = marker_path.read_bytes()
        appended = b'{"run_id":"conflicting","fixture":"b"}\n'
        stdout = (
            marker_raw
            + f"marker={marker_path} sha256={evidence['marker_sha256']}\n".encode()
            + appended
        )
        evidence["command_event"].update(stdout=stdout.decode("utf-8"), stdout_sha256=sha(stdout))
        evidence["probe_output"]["stdout_sha256"] = sha(stdout)
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_rejects_nonabsolute_paths_and_missing_or_malformed_digests(self):
        mutations = (
            lambda b: b[0].update(cwd=""),
            lambda b: b[0].update(observation_dir="relative/observations"),
            lambda b: b[0]["copy_identity"].update(root="relative/root"),
            lambda b: b[0].update(oracle_sha256="not-a-digest"),
            lambda b: b[1]["command_event"].pop("stdout_sha256"),
            lambda b: b[0].update(command_argv=["python3", "-B", "check.py"]),
            lambda b: b[2].update(findings=[None]),
        )
        for mutate in mutations:
            bundle = list(self.valid_bundle())
            mutate(bundle)
            bundle[1]["expected_sha256"] = sha(canon(bundle[0]))
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                validator.validate(*bundle)

    def test_rejects_evaluator_identity_reuse_and_malformed_semantic_rows(self):
        bundle = list(self.valid_bundle())
        bundle[3]["evaluator_id"] = bundle[1]["native_session_id"]
        with self.assertRaises(ValueError):
            validator.validate(*bundle)
        bundle = list(self.valid_bundle())
        bundle[3]["expected_findings"] = [None]
        with self.assertRaises(ValueError):
            validator.validate(*bundle)

    def test_cli_returns_reject_for_malformed_top_level_and_nested_shapes(self):
        bundle = list(self.valid_bundle())
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for index, value in enumerate(bundle):
                path = pathlib.Path(directory) / f"{index}.json"
                path.write_text(json.dumps(value))
                paths.append(path)
            paths[0].write_text("[]")
            command = [
                sys.executable,
                "-B",
                str(ROOT / "scripts/validate_review_quality.py"),
                "--expected",
                str(paths[0]),
                "--evidence",
                str(paths[1]),
                "--subject",
                str(paths[2]),
                "--semantic",
                str(paths[3]),
            ]
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('"status": "REJECT"', result.stdout)
            paths[0].write_text(json.dumps(bundle[0]))
            paths[3].write_text(json.dumps(dict(bundle[3], expected_findings=[None])))
            nested_result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
            self.assertNotEqual(nested_result.returncode, 0)
            self.assertIn('"status": "REJECT"', nested_result.stdout)


if __name__ == "__main__":
    unittest.main()
