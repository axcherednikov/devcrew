#!/usr/bin/env python3
"""Validate bounded host evidence for the paired review-quality control."""

import argparse
import hashlib
import json
import os
import pathlib
import re
import stat
import subprocess
import sys

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def read_json(path):
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def need(condition, message):
    if not condition:
        raise ValueError(message)


def require_object(value, label):
    need(isinstance(value, dict), f"{label} must be a JSON object")
    return value


def require_text(value, label):
    need(isinstance(value, str) and value.strip(), f"{label} must be non-empty text")
    return value


def require_digest(value, label):
    need(
        isinstance(value, str) and SHA256_RE.fullmatch(value) is not None,
        f"{label} must be a lowercase SHA-256 digest",
    )
    return value


def require_absolute_existing_dir(value, label):
    require_text(value, label)
    path = pathlib.Path(value)
    need(path.is_absolute(), f"{label} must be absolute")
    resolved = path.resolve(strict=True)
    need(str(resolved) == str(path), f"{label} must be canonical and must not traverse symlinks")
    need(path.is_dir() and not path.is_symlink(), f"{label} must be a real directory")
    return path


def safe_regular_file(root, relative, label):
    root = pathlib.Path(root)
    rel = pathlib.PurePosixPath(relative) if isinstance(relative, str) else None
    need(
        rel is not None
        and not rel.is_absolute()
        and rel.parts
        and all(part not in {"", ".", ".."} for part in rel.parts),
        f"{label} path must be normalized and relative",
    )
    current = root
    for part in rel.parts:
        current = current / part
        need(not current.is_symlink(), f"{label} path contains a symlink")
        need(current.exists(), f"{label} path is missing")
    resolved = current.resolve(strict=True)
    need(resolved.is_relative_to(root.resolve(strict=True)), f"{label} escapes its bound root")
    info = current.lstat()
    need(stat.S_ISREG(info.st_mode), f"{label} must be a regular file")
    return current, current.read_bytes()


def fixture_manifest(root, excluded):
    root = pathlib.Path(root).resolve(strict=True)
    excluded = pathlib.Path(excluded).resolve(strict=False)
    rows = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = pathlib.Path(current)
        for name in list(dirs):
            path = current_path / name
            need(not path.is_symlink(), f"symlink directory rejected: {path}")
            if path.resolve(strict=False) == excluded:
                dirs.remove(name)
        for name in files:
            path = current_path / name
            need(not path.is_symlink(), f"symlink file rejected: {path}")
            if path.resolve(strict=False).is_relative_to(excluded):
                continue
            info = path.lstat()
            need(stat.S_ISREG(info.st_mode), f"non-regular fixture file rejected: {path}")
            rows.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "mode": stat.S_IMODE(info.st_mode),
                    "type": "file",
                    "sha256": digest(path.read_bytes()),
                }
            )
    rows.sort(key=lambda row: row["path"])
    return rows


def file_fingerprint(root, excluded):
    return digest(canonical(fixture_manifest(root, excluded)))


def validate_copy_identity(expected):
    identity = require_object(expected.get("copy_identity"), "expected.copy_identity")
    kind = identity.get("kind")
    need(kind in {"source", "installed"}, "copy identity kind must be source or installed")
    root = require_absolute_existing_dir(identity.get("root"), "copy_identity.root")
    manifest_rel = ".codex-plugin/plugin.json"
    manifest_path, manifest_raw = safe_regular_file(root, manifest_rel, "plugin manifest")
    require_text(identity.get("manifest"), "copy_identity.manifest")
    need(
        pathlib.Path(identity["manifest"]).is_absolute()
        and pathlib.Path(identity["manifest"]) == manifest_path,
        "copy_identity.manifest must identify root/.codex-plugin/plugin.json exactly",
    )
    manifest_sha = require_digest(identity.get("manifest_sha256"), "copy_identity.manifest_sha256")
    need(
        digest(manifest_raw) == manifest_sha,
        "plugin manifest digest differs from initial host identity",
    )
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"plugin manifest is invalid JSON: {exc}") from None
    require_object(manifest, "plugin manifest")
    need(manifest.get("name") == "devcrew", "plugin manifest does not identify DevCrew")
    version = require_text(identity.get("version"), "copy_identity.version")
    need(manifest.get("version") == version, "plugin version differs from initial host identity")
    head = require_text(identity.get("head"), "copy_identity.head")
    need(
        re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", head) is not None,
        "copy_identity.head must be a full source commit digest",
    )
    git_metadata = root / ".git"
    need(not git_metadata.is_symlink(), "copy identity .git metadata must not be a symlink")
    if git_metadata.exists():
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        need(
            result.returncode == 0 and result.stdout.strip() == head,
            "copy identity HEAD differs from the checkout",
        )
    return root


def validate_skill_snapshot(expected, identity_root):
    skill_rel = "skills/devcrew-review/SKILL.md"
    coverage_rel = "skills/devcrew-review/references/coverage.md"
    skill_path = require_text(expected.get("skill_path"), "expected.skill_path")
    coverage_path = require_text(expected.get("coverage_path"), "expected.coverage_path")
    skill_file, skill_raw = safe_regular_file(identity_root, skill_rel, "selected skill")
    coverage_file, coverage_raw = safe_regular_file(
        identity_root, coverage_rel, "coverage reference"
    )
    need(
        pathlib.Path(skill_path).is_absolute() and pathlib.Path(skill_path) == skill_file,
        "skill_path must identify the selected skill under copy_identity.root",
    )
    need(
        pathlib.Path(coverage_path).is_absolute() and pathlib.Path(coverage_path) == coverage_file,
        "coverage_path must identify the selected reference under copy_identity.root",
    )
    skill_sha = require_digest(expected.get("skill_sha256"), "expected.skill_sha256")
    need(digest(skill_raw) == skill_sha, "selected skill digest differs from initial host identity")
    coverage_sha = digest(coverage_raw)
    bundle = require_object(expected.get("skill_bundle_digests"), "expected.skill_bundle_digests")
    need(
        bundle == {"SKILL.md": skill_sha, "references/coverage.md": coverage_sha},
        "skill/reference bundle digests differ from files under identity.root",
    )
    return skill_file, skill_raw, coverage_file, coverage_raw


def validate(expected, evidence, subject, semantic):
    expected = require_object(expected, "expected")
    evidence = require_object(evidence, "evidence")
    subject = require_object(subject, "subject")
    semantic = require_object(semantic, "semantic")
    need(
        type(expected.get("schema_version")) is int and expected["schema_version"] == 1,
        "unsupported expected schema",
    )
    need(
        type(evidence.get("schema_version")) is int and evidence["schema_version"] == 1,
        "unsupported evidence schema",
    )

    pair_id = require_text(expected.get("pair_id"), "expected.pair_id")
    run_id = require_text(expected.get("run_id"), "expected.run_id")
    fixture = expected.get("fixture")
    need(fixture in {"a", "b"}, "invalid fixture")
    need(pair_id != run_id, "pair and run IDs must be distinct")
    for label, obj in (("evidence", evidence), ("subject", subject), ("semantic", semantic)):
        for key, value in (("pair_id", pair_id), ("run_id", run_id), ("fixture", fixture)):
            need(obj.get(key) == value, f"{label}.{key} binding mismatch")

    cwd = require_absolute_existing_dir(expected.get("cwd"), "expected.cwd")
    observation_dir = require_absolute_existing_dir(
        expected.get("observation_dir"), "expected.observation_dir"
    )
    need(
        not observation_dir.is_relative_to(cwd) and not cwd.is_relative_to(observation_dir),
        "observation directory must be separate from fixture",
    )
    need(expected.get("expected_scope") == ["invoice_api.py"], "unexpected review scope")
    need(expected.get("marker_absent") is True, "host must attest marker was absent before run")
    identity_root = validate_copy_identity(expected)
    skill_file, skill_raw, coverage_file, coverage_raw = validate_skill_snapshot(
        expected, identity_root
    )

    expected_identity_sha = require_digest(
        expected.get("identity_sha256"), "expected.identity_sha256"
    )
    identity = expected["copy_identity"]
    need(expected_identity_sha == digest(canonical(identity)), "expected identity digest mismatch")
    evidence_identity = require_object(evidence.get("copy_identity"), "evidence.copy_identity")
    evidence_identity_sha = require_digest(
        evidence.get("identity_sha256"), "evidence.identity_sha256"
    )
    need(
        evidence_identity_sha == digest(canonical(evidence_identity)),
        "evidence identity digest mismatch",
    )
    need(
        evidence_identity == identity,
        "observed copy identity differs from independently verified initial host identity",
    )
    need(
        require_digest(evidence.get("expected_sha256"), "evidence.expected_sha256")
        == digest(canonical(expected)),
        "expected record binding mismatch",
    )

    command_argv = [
        "python3",
        "-B",
        "check.py",
        "--run-id",
        run_id,
        "--fixture",
        fixture,
        "--observations",
        str(observation_dir),
    ]
    need(
        expected.get("command_argv") == command_argv,
        "expected probe argv does not match run, fixture and observation directory",
    )
    session_id = require_text(evidence.get("native_session_id"), "native_session_id")
    turn_id = require_text(evidence.get("terminal_turn_id"), "terminal_turn_id")
    need(session_id != turn_id, "session and terminal turn IDs must be distinct")
    need(evidence.get("terminal_status") == "completed", "native turn did not complete")

    command = require_object(evidence.get("command_event"), "evidence.command_event")
    command_id = require_text(command.get("event_id"), "command_event.event_id")
    need(
        command_id not in {session_id, turn_id, run_id, pair_id}, "command event ID is not distinct"
    )
    need(command.get("fresh_session_id") == session_id, "command is not bound to fresh session")
    need(command.get("terminal_turn_id") == turn_id, "command is not bound to terminal turn")
    need(
        command.get("status") == "completed"
        and type(command.get("exit_code")) is int
        and command["exit_code"] == 0,
        "probe did not complete successfully",
    )
    need(command.get("truncated") is False, "probe event is missing or truncated")
    need(command.get("argv") == command_argv, "probe command event argv mismatch")
    event_cwd = require_absolute_existing_dir(command.get("cwd"), "command_event.cwd")
    need(event_cwd == cwd, "probe cwd mismatch")
    stdout = command.get("stdout")
    need(isinstance(stdout, str), "native command stdout must be complete text")
    command_stdout_sha = require_digest(command.get("stdout_sha256"), "command_event.stdout_sha256")
    need(
        command_stdout_sha == digest(stdout.encode("utf-8")),
        "native command stdout digest mismatch",
    )

    skill = require_object(evidence.get("skill_read"), "evidence.skill_read")
    skill_id = require_text(skill.get("event_id"), "skill_read.event_id")
    need(
        skill_id not in {command_id, session_id, turn_id, run_id, pair_id},
        "skill read event ID is not distinct",
    )
    need(
        skill.get("path") == expected["skill_path"]
        and skill.get("argv") == ["cat", expected["skill_path"]],
        "selected skill read path/command mismatch",
    )
    need(
        skill.get("status") == "completed"
        and type(skill.get("exit_code")) is int
        and skill["exit_code"] == 0
        and skill.get("truncated") is False,
        "selected skill read did not complete successfully",
    )
    need(
        skill.get("session_id") == session_id and skill.get("turn_id") == turn_id,
        "selected skill read is not bound to fresh session/turn",
    )
    skill_sha = require_digest(skill.get("sha256"), "skill_read.sha256")
    skill_stdout_sha = require_digest(
        evidence.get("skill_output_sha256"), "evidence.skill_output_sha256"
    )
    need(
        skill_sha == digest(skill_raw) and skill_stdout_sha == digest(skill_raw),
        "selected skill stdout digest differs from file under identity.root",
    )
    if "stdout_sha256" in skill:
        need(
            require_digest(skill.get("stdout_sha256"), "skill_read.stdout_sha256")
            == skill_stdout_sha,
            "selected skill nested stdout digest mismatch",
        )
    need(
        skill_sha == expected["skill_sha256"],
        "selected skill differs from pre-run expected content",
    )

    companion = require_object(evidence.get("coverage_read"), "evidence.coverage_read")
    coverage_id = require_text(companion.get("event_id"), "coverage_read.event_id")
    need(
        coverage_id not in {command_id, skill_id, session_id, turn_id, run_id, pair_id},
        "coverage read event ID is not distinct",
    )
    need(
        companion.get("path") == expected["coverage_path"]
        and companion.get("argv") == ["cat", expected["coverage_path"]],
        "coverage read path/command mismatch",
    )
    need(
        companion.get("status") == "completed"
        and type(companion.get("exit_code")) is int
        and companion["exit_code"] == 0
        and companion.get("truncated") is False,
        "coverage read did not complete successfully",
    )
    need(
        companion.get("session_id") == session_id and companion.get("turn_id") == turn_id,
        "coverage read is not bound to fresh session/turn",
    )
    coverage_sha = require_digest(companion.get("sha256"), "coverage_read.sha256")
    coverage_stdout_sha = require_digest(
        companion.get("stdout_sha256"), "coverage_read.stdout_sha256"
    )
    need(
        coverage_sha == digest(coverage_raw) and coverage_stdout_sha == digest(coverage_raw),
        "coverage read stdout digest differs from file under identity.root",
    )
    need(
        evidence.get("skill_bundle_digests") == expected["skill_bundle_digests"],
        "observed skill/reference bundle differs from initial host identity",
    )

    marker_path_text = require_text(evidence.get("marker_path"), "evidence.marker_path")
    marker_path = pathlib.Path(marker_path_text)
    need(marker_path.is_absolute(), "marker path must be absolute")
    expected_marker_path = observation_dir / f"{run_id}.json"
    need(
        marker_path == expected_marker_path,
        "marker outside allocated observation directory or wrong run name",
    )
    need(not marker_path.is_symlink() and marker_path.is_file(), "real marker missing or unsafe")
    need(marker_path.resolve(strict=True) == expected_marker_path, "marker path traverses symlinks")
    marker_raw = marker_path.read_bytes()
    marker_sha = require_digest(evidence.get("marker_sha256"), "evidence.marker_sha256")
    need(digest(marker_raw) == marker_sha, "marker digest mismatch")
    marker = read_json(marker_path)
    require_object(marker, "on-disk marker")
    need(
        type(marker.get("schema_version")) is int and marker["schema_version"] == 1,
        "unsupported on-disk marker schema",
    )
    need(
        marker.get("run_id") == run_id and marker.get("fixture") == fixture,
        "marker run/fixture mismatch",
    )
    need(
        marker_raw == canonical(marker) + b"\n",
        "on-disk marker is not the exact canonical probe output",
    )
    probe = require_object(evidence.get("probe_output"), "evidence.probe_output")
    need(
        probe.get("truncated") is False
        and type(probe.get("exit_code")) is int
        and probe["exit_code"] == 0,
        "probe output incomplete",
    )
    need(
        require_digest(probe.get("marker_sha256"), "probe_output.marker_sha256") == marker_sha,
        "probe output marker digest mismatch",
    )
    need(probe.get("result") == marker, "probe output and on-disk marker differ")
    need(marker.get("cwd") == str(cwd), "marker cwd mismatch")
    need(
        marker.get("invoice_calls")
        in ([["tenant-a", "invoice-17"]], [["tenant-a", "invoice-17"], ["tenant-b", "invoice-17"]]),
        "probe did not report actual invoice calls",
    )
    need(
        isinstance(marker.get("tenant_results"), list) and len(marker["tenant_results"]) == 2,
        "missing tenant results",
    )
    zero = require_object(marker.get("explicit_zero"), "marker.explicit_zero")
    if fixture == "a":
        need(
            zero == {"raised": False, "value": 25},
            "A zero limit behavior differs from fixture oracle",
        )
        need(
            marker["invoice_calls"] == [["tenant-a", "invoice-17"]],
            "A did not reuse the cross-tenant cache entry",
        )
        need(
            marker["tenant_results"][0] == marker["tenant_results"][1],
            "A results did not expose cross-tenant reuse",
        )
    else:
        need(
            zero.get("raised") is True and zero.get("type") == "ValueError",
            "B zero limit must fail validation",
        )
        need(
            marker["invoice_calls"] == [["tenant-a", "invoice-17"], ["tenant-b", "invoice-17"]],
            "B did not execute both tenant-specific cache misses",
        )
        need(
            marker["tenant_results"]
            == [
                {"tenant_id": "tenant-a", "invoice_id": "invoice-17"},
                {"tenant_id": "tenant-b", "invoice_id": "invoice-17"},
            ],
            "B results do not preserve tenant identity",
        )
    need(marker.get("absent_limit_default") == 25, "absent limit default probe mismatch")
    need(marker.get("valid_boundary") == 100, "valid boundary probe mismatch")
    probe_stdout_sha = require_digest(probe.get("stdout_sha256"), "probe_output.stdout_sha256")
    need(probe_stdout_sha == command_stdout_sha, "parsed probe is not bound to native stdout")
    expected_stdout = marker_raw + f"marker={marker_path} sha256={marker_sha}\n".encode("utf-8")
    need(
        stdout.encode("utf-8") == expected_stdout,
        "native stdout must be exactly the on-disk marker bytes and marker digest line",
    )

    initial_manifest = expected.get("initial_fixture_manifest")
    need(
        isinstance(initial_manifest, list) and initial_manifest,
        "complete initial fixture manifest missing",
    )
    for index, row in enumerate(initial_manifest):
        row = require_object(row, f"initial_fixture_manifest[{index}]")
        require_text(row.get("path"), f"initial_fixture_manifest[{index}].path")
        require_digest(row.get("sha256"), f"initial_fixture_manifest[{index}].sha256")
        need(
            type(row.get("mode")) is int and row.get("type") == "file",
            "invalid fixture manifest row",
        )
    need(
        len({row["path"] for row in initial_manifest}) == len(initial_manifest),
        "duplicate fixture manifest path",
    )
    initial_sha = require_digest(
        expected.get("initial_fixture_fingerprint"), "initial_fixture_fingerprint"
    )
    need(
        initial_sha == digest(canonical(initial_manifest)),
        "initial fixture fingerprint does not bind full manifest",
    )
    need(
        initial_manifest == fixture_manifest(cwd, observation_dir),
        "initial fixture fingerprint mismatch",
    )
    final_manifest = evidence.get("final_fixture_manifest")
    need(
        isinstance(final_manifest, list) and final_manifest == initial_manifest,
        "fixture changed during review",
    )
    final_sha = require_digest(
        evidence.get("final_fixture_fingerprint"), "final_fixture_fingerprint"
    )
    need(final_sha == digest(canonical(final_manifest)), "final fixture fingerprint is stale")

    findings = subject.get("findings")
    need(isinstance(findings, list), "subject findings must be a structured list")
    for index, finding in enumerate(findings):
        finding = require_object(finding, f"subject.findings[{index}]")
        for key in (
            "id",
            "severity",
            "location",
            "trigger",
            "consequence",
            "evidence",
            "verification",
            "fix",
        ):
            require_text(finding.get(key), f"subject.findings[{index}].{key}")
    finding_ids = [finding["id"] for finding in findings]
    need(len(set(finding_ids)) == len(finding_ids), "subject finding IDs must be unique")
    event_ids = {command_id, skill_id, coverage_id, session_id, turn_id, run_id, pair_id}
    need(
        not (set(finding_ids) & event_ids),
        "subject finding IDs must be distinct from run/session/event IDs",
    )
    if fixture == "a":
        need(len(findings) == 2, "A subject report must have two distinct findings")
    else:
        need(not findings, "B must not contain actionable findings")
        require_text(subject.get("outcome_rationale"), "subject.outcome_rationale")
    findings_sha = digest(canonical(findings))
    need(
        require_digest(evidence.get("subject_findings_sha256"), "evidence.subject_findings_sha256")
        == findings_sha,
        "subject finding digest mismatch",
    )
    need(
        require_digest(semantic.get("subject_findings_sha256"), "semantic.subject_findings_sha256")
        == findings_sha,
        "semantic finding binding mismatch",
    )
    oracle_sha = require_digest(expected.get("oracle_sha256"), "expected.oracle_sha256")
    need(
        require_digest(semantic.get("oracle_sha256"), "semantic.oracle_sha256") == oracle_sha,
        "semantic oracle binding mismatch",
    )
    evaluator_id = require_text(semantic.get("evaluator_id"), "semantic.evaluator_id")
    need(
        evaluator_id not in event_ids,
        "evaluator identity must differ from native session/turn/event IDs",
    )
    need(semantic.get("verdict") == "PASS", "independent semantic evaluation did not pass")
    if fixture == "b":
        rationale_sha = digest(subject["outcome_rationale"].encode("utf-8"))
        need(
            require_digest(
                semantic.get("no_findings_rationale_sha256"),
                "semantic.no_findings_rationale_sha256",
            )
            == rationale_sha,
            "B absence rationale is not bound to independent evaluation",
        )

    assessments = semantic.get("expected_findings")
    need(isinstance(assessments, list), "semantic expected-finding assessments must be an array")
    for index, row in enumerate(assessments):
        row = require_object(row, f"semantic.expected_findings[{index}]")
        require_text(row.get("oracle_id"), f"semantic.expected_findings[{index}].oracle_id")
        require_text(row.get("finding_id"), f"semantic.expected_findings[{index}].finding_id")
        for key in ("matched", "trigger", "consequence", "location"):
            need(row.get(key) is True, f"semantic.expected_findings[{index}].{key} did not pass")
    oracle_ids = [row["oracle_id"] for row in assessments]
    mapped_finding_ids = [row["finding_id"] for row in assessments]
    required_oracle_ids = (
        {"tenant-cache-isolation", "zero-limit-defaulted"} if fixture == "a" else set()
    )
    need(
        set(oracle_ids) == required_oracle_ids and len(oracle_ids) == len(required_oracle_ids),
        "semantic assessment IDs do not match fixture oracle",
    )
    need(
        len(set(mapped_finding_ids)) == len(mapped_finding_ids)
        and set(mapped_finding_ids) == set(finding_ids),
        "semantic mapping must bind every actual finding exactly once and omit none",
    )
    unsupported = semantic.get("unsupported_actionable_ids")
    need(
        isinstance(unsupported, list)
        and all(isinstance(value, str) and value.strip() for value in unsupported),
        "semantic unsupported_actionable_ids must be a string array",
    )
    need(not unsupported, "unsupported actionable findings present")
    return {
        "status": "PASS",
        "pair_id": pair_id,
        "run_id": run_id,
        "fixture": fixture,
        "semantic_verdict": semantic["verdict"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("expected", "evidence", "subject", "semantic"):
        parser.add_argument("--" + name, required=True, type=pathlib.Path)
    args = parser.parse_args()
    try:
        result = validate(
            *(
                read_json(getattr(args, name))
                for name in ("expected", "evidence", "subject", "semantic")
            )
        )
    except (
        OSError,
        UnicodeDecodeError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        json.JSONDecodeError,
    ) as exc:
        print(json.dumps({"status": "REJECT", "reason": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
