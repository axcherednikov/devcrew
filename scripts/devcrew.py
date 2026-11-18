#!/usr/bin/env python3
"""Small, dependency-free runtime helpers for the DevCrew skills."""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from typing import Any

import devcrew_capacity
import devcrew_context
import devcrew_cost
import devcrew_dispatch
import devcrew_fs as fs
import devcrew_memory
import devcrew_ordinary
import devcrew_phase
import devcrew_qa
import devcrew_qa_environment
import devcrew_requirements
import devcrew_resources
import devcrew_servers
import devcrew_worktree

ROOT = pathlib.Path(__file__).resolve().parent.parent
ASSET_ROOT = ROOT / "skills" / "devcrew-setup" / "assets"
ROLE_ROOT = ASSET_ROOT / "agents"
PROJECT_ASSETS = ASSET_ROOT / "project"
ROLE_NAMES = (
    "api-analyst",
    "architect-agent",
    "claim-verifier",
    "doc-digest",
    "decision-agent",
    "executor-agent",
    "gitlab-commenter",
    "product-agent",
    "reviewer-agent",
    "scout",
    "test-runner",
    "tester",
)
SCHEMA_VERSION = 1
PROJECT_SCHEMA_VERSION = 1
MANAGED_FIELDS = ("name", "description", "developer_instructions")
OPTIONAL_MANAGED_FIELDS = ("sandbox_mode",)
LOCAL_OVERRIDE_FIELDS = ("model", "model_reasoning_effort")
ROLE_STATE_FIELDS = (
    "managed_fields",
    "managed_fields_sha256",
    "local_overrides",
    "override_fields_sha256",
)
DEFAULT_TIMEOUT = 300.0
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
STATE_FILE = ".codex/devcrew.json"
PROJECT_SYNC_STATE_FILE = ".codex/devcrew-state.json"
DEFAULT_STATE_PATHS = {
    "context": "docs/context.md",
    "tasks": ".codex/tasks",
    "sessions": ".codex/sessions",
    "memory": "memory",
    "checks": ".codex/checks",
}


class ContractError(Exception):
    """An expected command or input contract failure."""


def _json_error(message: str, *, code: int = 2, details: Any = None) -> int:
    payload: dict[str, Any] = {"ok": False, "error": message}
    if details is not None:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return code


def _atomic_write(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            os.chmod(temp_name, path.stat().st_mode & 0o777)
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _json_dump(path: pathlib.Path, value: Any) -> None:
    data = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(path, data.encode("utf-8"))


def _utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="microseconds")


def _project(path: str | os.PathLike[str]) -> pathlib.Path:
    candidate = pathlib.Path(path)
    if not candidate.exists() or not candidate.is_dir():
        raise ContractError(f"project directory does not exist: {path}")
    return candidate.resolve()


def _git(project: pathlib.Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if check and result.returncode:
        raise ContractError((result.stderr or result.stdout).strip() or "git command failed")
    return result


def _task_worktree(args: argparse.Namespace) -> int:
    project = _project(args.project)
    try:
        if args.operation == "create":
            if args.subcommand == "task-worktree" and not args.worktree_request:
                raise ValueError(
                    "WORKTREE_REQUEST_REQUIRED: record the actual explicit user request"
                )
            receipt = devcrew_worktree.create(
                project, args.task_id, args.base_sha, worktree_request=args.worktree_request
            )
        else:
            receipt = devcrew_worktree.resolve(
                project, args.task_id, worktree_request=args.worktree_request
            )
    except ValueError as error:
        raise ContractError(str(error)) from None
    print(json.dumps(receipt))
    return (
        1
        if receipt.get("status")
        in (
            "UNSAFE_MISSING_WORKTREE",
            "TASK_WORKTREE_ALREADY_EXISTS",
            "TASK_WORKTREE_BRANCH_COLLISION",
            "TASK_CHECKOUT_ALREADY_EXISTS",
        )
        else 0
    )


def _phase_command(args: argparse.Namespace) -> int:
    project = _project(args.project)
    try:
        environment_only = getattr(args, "environment_only", False)
        approved_environment = getattr(args, "approve_environment_sha256", None)
        if (
            environment_only or approved_environment is not None
        ) and args.operation != "pre-candidate-reapprove":
            raise devcrew_phase.PhaseError(
                "INVALID_ENVIRONMENT_APPROVAL",
                "environment approval flags are supported only for pre-candidate-reapprove",
            )
        if environment_only != (approved_environment is not None):
            raise devcrew_phase.PhaseError(
                "INVALID_ENVIRONMENT_APPROVAL",
                "--environment-only and --approve-environment-sha256 must be supplied together",
            )
        assessment_path = getattr(args, "intent_assessment", None)
        supported_assessment_operations = {"init", "pre-candidate-reapprove", "replan-complete"}
        if assessment_path and args.operation not in supported_assessment_operations:
            raise devcrew_phase.PhaseError(
                "INVALID_INTENT_ASSESSMENT",
                "--intent-assessment is supported only for init, pre-candidate-reapprove, and replan-complete",
            )
        intent_assessment = None
        if assessment_path:
            try:
                with pathlib.Path(assessment_path).open("rb") as stream:
                    raw_assessment = stream.read(1024 * 1024 + 1)
                if len(raw_assessment) > 1024 * 1024:
                    raise ValueError("intent assessment exceeds the 1 MiB limit")
                intent_assessment = json.loads(raw_assessment.decode("utf-8"))
                if not isinstance(intent_assessment, dict):
                    raise ValueError("intent assessment must be a JSON object")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise devcrew_phase.PhaseError("INVALID_INTENT_ASSESSMENT", str(exc)) from exc
        if args.operation == "source-bind":
            if bool(args.source_spec) == bool(args.ordinary_source_gap):
                raise devcrew_phase.PhaseError(
                    "INVALID_INTENT_SOURCE_SPEC",
                    "source-bind requires exactly one of --source-spec or --ordinary-source-gap",
                )
            source_path = args.ordinary_source_gap or args.source_spec
            try:
                with pathlib.Path(source_path).open("rb") as stream:
                    raw_spec = stream.read(1024 * 1024 + 1)
                if len(raw_spec) > 1024 * 1024:
                    raise ValueError("source spec exceeds the 1 MiB limit")
                spec = json.loads(raw_spec.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise devcrew_phase.PhaseError("INVALID_INTENT_SOURCE_SPEC", str(exc)) from exc
            receipt = devcrew_worktree.resolve(project, args.task_id, read_only=True)
            if receipt.get("state") != "RESOLVED":
                raise devcrew_phase.PhaseError(
                    "WORKTREE_UNRESOLVED", "task worktree did not resolve cleanly"
                )
            if args.ordinary_source_gap:
                result = devcrew_phase.bind_ordinary_source_gap(project, receipt, spec)
                print(json.dumps(result, ensure_ascii=False))
            else:
                source = devcrew_phase.bind_intent_source(project, receipt, spec)
                print(
                    json.dumps(
                        {"status": "INTENT_SOURCE_BOUND", "source": source}, ensure_ascii=False
                    )
                )
            return 0
        if args.operation == "pre-candidate-reapprove":
            if not args.phase_id or not args.planner_output or not args.spawn_receipt_id:
                raise devcrew_phase.PhaseError(
                    "INVALID_PRE_CANDIDATE_REAPPROVAL",
                    "pre-candidate-reapprove requires --phase-id, --planner-output, and --spawn-receipt-id",
                )
            try:
                receipt = devcrew_worktree.resolve(project, args.task_id, read_only=True)
                planner = json.loads(pathlib.Path(args.planner_output).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                raise devcrew_phase.PhaseError(
                    "INVALID_PRE_CANDIDATE_REAPPROVAL", str(exc)
                ) from exc
            if receipt.get("state") != "RESOLVED":
                raise devcrew_phase.PhaseError(
                    "WORKTREE_UNRESOLVED", "task worktree did not resolve cleanly"
                )
            result = devcrew_phase.reapprove_pre_candidate(
                devcrew_phase.manifest_path(project, args.task_id),
                planner,
                receipt,
                project,
                args.phase_id,
                args.spawn_receipt_id,
                intent_assessment=intent_assessment,
                environment_only=environment_only,
                approved_environment_sha256=approved_environment,
                verifier=_verified_record,
            )
            print(
                json.dumps(
                    {
                        "status": (
                            "PRE_CANDIDATE_ENVIRONMENT_REAPPROVED"
                            if environment_only
                            else "PRE_CANDIDATE_REAPPROVED"
                        ),
                        "current_phase_id": result["current_phase_id"],
                        "contract_generation": result["contract_generation"],
                    }
                )
            )
            return 0
        *_, state_path = devcrew_worktree.location(project, args.task_id)
        devcrew_worktree._require_no_candidate_transition(state_path)
        devcrew_phase.recover_systemic_replan(project, args.task_id, verifier=_verified_record)
        devcrew_phase.recover_local_fix(project, args.task_id, verifier=_verified_record)
        try:
            receipt = devcrew_worktree.resolve(
                project, args.task_id, read_only=args.operation == "resume"
            )
        except ValueError as exc:
            if (
                args.operation == "resume"
                and devcrew_phase.manifest_path(project, args.task_id).exists()
            ):
                raise devcrew_phase.PhaseError("PHASE_COMMIT_INCONSISTENT", str(exc)) from exc
            raise
        if receipt.get("state") != "RESOLVED":
            raise devcrew_phase.PhaseError(
                "WORKTREE_UNRESOLVED", "task worktree did not resolve cleanly"
            )
        path = devcrew_phase.manifest_path(project, args.task_id)
        if args.operation == "init":
            if not args.planner_output:
                raise devcrew_phase.PhaseError(
                    "INVALID_PLANNER_OUTPUT", "init requires --planner-output"
                )
            if not args.spawn_receipt_id:
                raise devcrew_phase.PhaseError(
                    "INVALID_PLANNER_ROUTE", "init requires --spawn-receipt-id"
                )
            try:
                planner = json.loads(pathlib.Path(args.planner_output).read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise devcrew_phase.PhaseError("INVALID_PLANNER_OUTPUT", str(exc)) from exc
            result = devcrew_phase.initialize_manifest(
                path,
                planner,
                receipt,
                project,
                args.spawn_receipt_id,
                intent_assessment=intent_assessment,
            )
            print(
                json.dumps(
                    {
                        "status": "PHASE_MANIFEST_CREATED",
                        "path": str(path),
                        "manifest": result,
                        "original_intent_status": devcrew_phase.original_intent_status(result),
                    }
                )
            )
            return 0
        try:
            manifest = devcrew_phase.load_for_resume(path, receipt, project)
        except devcrew_phase.PhaseError as exc:
            if args.operation == "resume":
                try:
                    saved_base = (
                        json.loads(path.read_text(encoding="utf-8"))
                        .get("worktree", {})
                        .get("base_sha")
                    )
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    saved_base = None
                if saved_base is None or receipt.get("expected_head") != saved_base:
                    raise devcrew_phase.PhaseError(
                        "PHASE_COMMIT_INCONSISTENT", exc.message
                    ) from exc
            raise
        if args.operation == "transition":
            if not args.phase_id or not args.status:
                raise devcrew_phase.PhaseError(
                    "INVALID_TRANSITION", "transition requires --phase-id and --status"
                )
            evidence = []
            if args.status == "READY_FOR_CLOSE":
                if not args.spawn_receipt_id:
                    raise devcrew_phase.PhaseError(
                        "INVALID_EXECUTOR_ROUTE", "READY_FOR_CLOSE requires --spawn-receipt-id"
                    )
                evidence = devcrew_phase.verify_evidence_records(
                    args.evidence_record,
                    project,
                    pathlib.Path(receipt["task_worktree_root"]),
                    args.task_id,
                    _verified_record,
                )
            elif args.evidence_record:
                raise devcrew_phase.PhaseError(
                    "UNEXPECTED_PHASE_EVIDENCE", "evidence may be supplied only for READY_FOR_CLOSE"
                )
            result = devcrew_phase.set_phase_status(
                path,
                manifest,
                receipt,
                args.phase_id,
                args.status,
                evidence,
                project,
                _verified_record,
                args.spawn_receipt_id,
            )
            print(
                json.dumps(
                    {
                        "status": "PHASE_STATUS_UPDATED",
                        "current_phase_id": result["current_phase_id"],
                        "phase_status": args.status,
                    }
                )
            )
            return 0
        if args.operation == "review-start":
            if not args.phase_id:
                raise devcrew_phase.PhaseError(
                    "INVALID_PHASE_REVIEW", "review-start requires --phase-id"
                )
            result = devcrew_phase.begin_review(
                path,
                manifest,
                receipt,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                args.phase_id,
                _verified_record,
            )
            print(json.dumps(result))
            return 0
        continuation = None
        if args.continuation:
            try:
                continuation = json.loads(
                    pathlib.Path(args.continuation).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise devcrew_phase.PhaseError("INVALID_CONTINUATION", str(exc)) from exc
        if args.operation == "review-reconsider":
            if not args.phase_id or continuation is None:
                raise devcrew_phase.PhaseError(
                    "INVALID_CONTINUATION",
                    "review-reconsider requires --phase-id and --continuation",
                )
            result = devcrew_phase.reconsider_review(
                path,
                manifest,
                receipt,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                args.phase_id,
                _verified_record,
                continuation,
            )
            print(json.dumps(result))
            return 0
        if args.operation == "local-fix-start":
            if not args.phase_id:
                raise devcrew_phase.PhaseError(
                    "INVALID_PHASE_REVIEW", "local-fix-start requires --phase-id"
                )
            result = devcrew_phase.start_local_fix(
                path,
                manifest,
                receipt,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                args.phase_id,
                _verified_record,
                owner_decision=args.owner_decision,
                continuation=continuation,
            )
            print(json.dumps(result))
            return 0
        if args.operation == "replan-complete":
            if not args.phase_id or not args.planner_output or not args.spawn_receipt_id:
                raise devcrew_phase.PhaseError(
                    "INVALID_SYSTEMIC_REPLAN",
                    "replan-complete requires --phase-id, --planner-output, and --spawn-receipt-id",
                )
            if args.phase_id != manifest["current_phase_id"]:
                raise devcrew_phase.PhaseError(
                    "CURRENT_PHASE_ONLY", "only the current phase can be replanned"
                )
            try:
                replacement = json.loads(
                    pathlib.Path(args.planner_output).read_text(encoding="utf-8")
                )
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise devcrew_phase.PhaseError("INVALID_REPLACEMENT_CONTRACT", str(exc)) from exc
            result = devcrew_phase.complete_systemic_replan(
                path,
                manifest,
                receipt,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                replacement,
                args.spawn_receipt_id,
                _verified_record,
                owner_decision=args.owner_decision,
                continuation=continuation,
                intent_assessment=intent_assessment,
            )
            print(json.dumps(result))
            return 0
        if args.operation == "review-report":
            if not args.report:
                raise devcrew_phase.PhaseError(
                    "INVALID_PHASE_REVIEW", "review-report requires --report"
                )
            report_path = pathlib.Path(args.report)
            if not report_path.is_absolute():
                report_path = project / report_path
            try:
                report_path.resolve().relative_to(project.resolve())
            except ValueError:
                raise devcrew_phase.PhaseError(
                    "FOREIGN_PHASE_REVIEW", "review report must be stored under the host project"
                )
            if fs.is_link(report_path) or not report_path.is_file():
                raise devcrew_phase.PhaseError(
                    "INVALID_PHASE_REVIEW", "review report must be a regular host file"
                )
            try:
                report_bytes = report_path.read_bytes()
                report = json.loads(report_bytes.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise devcrew_phase.PhaseError("INVALID_PHASE_REVIEW", str(exc)) from exc
            result = devcrew_phase.submit_review(
                path,
                manifest,
                receipt,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                report,
                _verified_record,
                report_path.resolve().relative_to(project.resolve()).as_posix(),
                report_bytes,
            )
            print(json.dumps(result))
            return 0
        if args.operation == "accept":
            if not args.phase_id:
                raise devcrew_phase.PhaseError("INVALID_PHASE_REVIEW", "accept requires --phase-id")
            result = devcrew_phase.accept_review(
                path,
                manifest,
                receipt,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                args.phase_id,
                _verified_record,
                args.evidence_record,
            )
            print(json.dumps(result))
            return 0
        if args.operation == "advance":
            if not args.phase_id:
                raise devcrew_phase.PhaseError(
                    "INVALID_PHASE_ADVANCE", "advance requires --phase-id"
                )
            result = devcrew_phase.advance_phase(
                path,
                manifest,
                receipt,
                args.phase_id,
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                _verified_record,
            )
            print(
                json.dumps(
                    {
                        "status": "PHASE_ADVANCED",
                        "current_phase_id": result["current_phase_id"],
                        "base_sha": next(
                            p["base_sha"]
                            for p in result["phases"]
                            if p["phase_id"] == args.phase_id
                        ),
                    }
                )
            )
            return 0
        if args.operation == "resume":
            print(
                json.dumps(
                    {
                        **devcrew_phase.resume(
                            manifest,
                            project,
                            pathlib.Path(receipt["task_worktree_root"]),
                            _verified_record,
                        ),
                        "path": str(path),
                        "current_phase_id": manifest["current_phase_id"],
                        "original_intent_status": devcrew_phase.original_intent_status(manifest),
                    }
                )
            )
            return 0
        if args.operation == "dispatch":
            payload = devcrew_phase.executor_input(
                manifest, pathlib.Path(receipt["task_worktree_root"]), project, _verified_record
            )
            print(json.dumps({"status": "PHASE_DISPATCH_READY", "executor_input": payload}))
            return 0
        if args.operation == "candidate-commit":
            if not args.verification_bindings:
                raise devcrew_phase.PhaseError(
                    "INVALID_VERIFICATION_BINDINGS",
                    "candidate-commit requires --verification-bindings",
                )
            phase = next(
                p for p in manifest["phases"] if p["phase_id"] == manifest["current_phase_id"]
            )
            planned_checks = iter(devcrew_phase.required_verification(phase))

            def run_phase_check(argv, verification):
                binding = next(planned_checks)
                record, payload = _run_check(
                    project,
                    f"phase-{args.task_id}-{len(argv)}",
                    argv,
                    binding["timeout"],
                    binding["idle_timeout"],
                    execution_root=pathlib.Path(receipt["task_worktree_root"]),
                    task_id=args.task_id,
                )
                if payload["status"] != "passed":
                    raise devcrew_phase.PhaseError(
                        "PHASE_CHECK_FAILED", f"{verification}: {payload['status']}"
                    )
                return str(record)

            result = devcrew_phase.candidate_commit(
                project,
                pathlib.Path(receipt["task_worktree_root"]),
                manifest,
                receipt,
                pathlib.Path(args.verification_bindings),
                _verified_record,
                run_phase_check,
            )
            print(json.dumps(result))
            return 0
    except devcrew_phase.PhaseError as error:
        print(
            json.dumps({"ok": False, "error": error.code, "message": error.message}),
            file=sys.stderr,
        )
        return 2
    except ValueError as error:
        raise ContractError(str(error)) from None
    return 2


def _ordinary_formal_failure(project: pathlib.Path) -> bool:
    """A rejected formal manifest cannot be escaped by starting ordinary work."""
    try:
        git_dir = pathlib.Path(
            subprocess.check_output(
                ["git", "-C", str(project), "rev-parse", "--absolute-git-dir"],
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                stderr=subprocess.DEVNULL,
            ).strip()
        )
    except (OSError, subprocess.CalledProcessError):
        return False
    state = git_dir / "devcrew-checkouts"
    if not state.is_dir() or fs.is_link(state):
        return False
    for manifest in state.glob("*.phases.json"):
        if fs.is_link(manifest):
            raise ContractError("formal task manifest is a symlink")
        try:
            phases = json.loads(manifest.read_text(encoding="utf-8")).get("phases", [])
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if any(
            isinstance(item, dict)
            and item.get("status") in {"LOCAL_FIX_REQUIRED", "REPLAN_REQUIRED", "BLOCKED"}
            for item in phases
        ):
            return True
    return False


def _ordinary_context(path: str) -> dict:
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid ordinary chat context: {exc}") from None
    return value


def _ordinary_command(args: argparse.Namespace) -> int:
    project = _project(args.project)
    try:
        state = _capture_state(
            project, include_files=True, checks_relative=_state_paths(project)["checks"]
        )
        memory_relative = _state_paths(project)["memory"]
        if args.operation in {
            "init",
            "attach-routing",
            "dispatch",
            "issue-dispatch",
            "plan-report",
            "plan-accept",
            "candidate",
            "review-start",
            "review-report",
            "accept",
            "continue",
            "executor-followup",
            "followup-report",
        } and _ordinary_formal_failure(project):
            raise ContractError(
                "FORMAL_TASK_UNRESOLVED: ordinary mode cannot bypass a rejected formal task in this checkout"
            )
        if args.operation == "init":
            if not args.chat_context:
                raise ContractError("ordinary init requires --chat-context")
            result = devcrew_ordinary.init(
                project, args.task_id, _ordinary_context(args.chat_context), state["git_head"]
            )
            print(
                json.dumps(
                    {
                        "status": "ORDINARY_TASK_CREATED",
                        "task_id": result["task_id"],
                        "source_id": result["source"]["source_id"],
                    },
                    ensure_ascii=False,
                )
            )
        elif args.operation == "dispatch":
            if not args.role:
                raise ContractError("ordinary dispatch requires --role")
            print(
                json.dumps(
                    devcrew_ordinary.dispatch(project, args.task_id, args.role), ensure_ascii=False
                )
            )
        elif args.operation == "attach-routing":
            if not args.routing_assessment:
                raise ContractError("ordinary attach-routing requires --routing-assessment")
            print(
                json.dumps(
                    devcrew_ordinary.attach_routing(
                        project,
                        args.task_id,
                        _load_json_object(args.routing_assessment, "ordinary routing assessment"),
                    ),
                    ensure_ascii=False,
                )
            )
        elif args.operation == "issue-dispatch":
            if not args.role or not args.runtime:
                raise ContractError("ordinary issue-dispatch requires --role and --runtime")
            runtime = _load_json_object(args.runtime, "ordinary runtime")
            scope = None
            if args.requirements_scope:
                scope_object = _load_json_object(
                    args.requirements_scope, "ordinary requirements scope"
                )
                if set(scope_object) != {"paths"} or not isinstance(scope_object["paths"], list):
                    raise ContractError(
                        "ordinary requirements scope must be an object containing only paths"
                    )
                scope = scope_object["paths"]
            print(
                json.dumps(
                    devcrew_ordinary.issue_dispatch(
                        project,
                        args.task_id,
                        args.role,
                        runtime,
                        args.role_contract,
                        memory_relative,
                        scope,
                    ),
                    ensure_ascii=False,
                )
            )
        elif args.operation == "spawn-report":
            if not args.report:
                raise ContractError("ordinary spawn-report requires --report")
            print(
                json.dumps(
                    {
                        "status": "ORDINARY_SPAWN_RECORDED",
                        "receipt": devcrew_ordinary.record_spawn(
                            project,
                            args.task_id,
                            _load_json_object(args.report, "ordinary spawn report"),
                            memory_relative,
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        elif args.operation == "plan-report":
            if not args.report:
                raise ContractError("ordinary plan-report requires --report")
            plan = devcrew_ordinary.submit_plan(
                project, args.task_id, _load_json_object(args.report, "ordinary plan report")
            )
            print(
                json.dumps({"status": "ORDINARY_PLAN_REPORTED", "plan": plan}, ensure_ascii=False)
            )
        elif args.operation == "plan-accept":
            if not args.plan_sha256:
                raise ContractError("ordinary plan-accept requires --plan-sha256")
            plan = devcrew_ordinary.accept_plan(project, args.task_id, args.plan_sha256)
            print(
                json.dumps({"status": "ORDINARY_PLAN_ACCEPTED", "plan": plan}, ensure_ascii=False)
            )
        elif args.operation == "candidate":
            if not args.executor_receipt_id:
                raise ContractError("ordinary candidate requires --executor-receipt-id")
            result = devcrew_ordinary.candidate(
                project,
                args.task_id,
                args.executor_receipt_id,
                state,
                args.evidence_record,
                _verified_record,
                memory_relative,
            )
            print(
                json.dumps(
                    {"status": "ORDINARY_REVIEW_READY", "candidate": result}, ensure_ascii=False
                )
            )
        elif args.operation == "review-start":
            print(
                json.dumps(
                    devcrew_ordinary.review_start(
                        project, args.task_id, state, _verified_record, memory_relative
                    ),
                    ensure_ascii=False,
                )
            )
        elif args.operation == "review-report":
            if not args.report:
                raise ContractError("ordinary review-report requires --report")
            report = _load_json_object(args.report, "ordinary review report")
            result = devcrew_ordinary.submit_review(
                project, args.task_id, report, state, _verified_record, memory_relative
            )
            print(
                json.dumps(
                    {"status": "ORDINARY_REVIEW_RECORDED", "review": result}, ensure_ascii=False
                )
            )
        elif args.operation == "accept":
            print(
                json.dumps(
                    devcrew_ordinary.accept(
                        project, args.task_id, state, _verified_record, memory_relative
                    ),
                    ensure_ascii=False,
                )
            )
        elif args.operation == "continue":
            if not args.report:
                raise ContractError("ordinary continue requires --report")
            routing = (
                _load_json_object(args.routing_assessment, "ordinary routing assessment")
                if args.routing_assessment
                else None
            )
            print(
                json.dumps(
                    devcrew_ordinary.continue_cycle(
                        project,
                        args.task_id,
                        _load_json_object(args.report, "ordinary continuation"),
                        state,
                        _verified_record,
                        routing,
                        memory_relative,
                    ),
                    ensure_ascii=False,
                )
            )
        elif args.operation == "executor-followup":
            print(
                json.dumps(
                    devcrew_ordinary.executor_followup(project, args.task_id), ensure_ascii=False
                )
            )
        elif args.operation == "followup-report":
            if not args.report:
                raise ContractError("ordinary followup-report requires --report")
            print(
                json.dumps(
                    {
                        "status": "ORDINARY_FOLLOWUP_RECORDED",
                        "receipt": devcrew_ordinary.record_executor_followup(
                            project,
                            args.task_id,
                            _load_json_object(args.report, "ordinary followup report"),
                            memory_relative,
                        ),
                    },
                    ensure_ascii=False,
                )
            )
        else:
            print(
                json.dumps(
                    devcrew_ordinary.resume(
                        project, args.task_id, state, _verified_record, memory_relative
                    ),
                    ensure_ascii=False,
                )
            )
        return 0
    except (ContractError, devcrew_ordinary.OrdinaryError, OSError, ValueError) as exc:
        return _json_error(str(exc))


def _inside(root: pathlib.Path, candidate: pathlib.Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ensure_dir(root: pathlib.Path, relative: str) -> pathlib.Path:
    current = root
    for part in pathlib.PurePosixPath(relative).parts:
        if part in ("", "."):
            continue
        current = current / part
        if current.exists() or fs.is_link(current):
            if fs.is_link(current):
                raise ContractError(f"refusing to follow symlink in project state: {current}")
            if not current.is_dir():
                raise ContractError(f"required directory is a file: {current}")
        else:
            try:
                current.mkdir()
            except FileExistsError:
                # Another local QA run may have created this shared parent.
                if fs.is_link(current):
                    raise ContractError(f"refusing to follow symlink in project state: {current}")
                if not current.is_dir():
                    raise ContractError(f"required directory is a file: {current}")
    return current


def _safe_destination(root: pathlib.Path, relative: str) -> pathlib.Path:
    if fs.unsafe_parts(pathlib.PurePosixPath(relative).parts):
        raise ContractError(f"destination contains a reserved Windows name: {relative}")
    path = root / pathlib.PurePosixPath(relative)
    parent = path.parent
    if not _inside(root, parent):
        raise ContractError(f"destination escapes project: {relative}")
    _ensure_dir(root, parent.relative_to(root).as_posix())
    if fs.is_link(path):
        raise ContractError(f"refusing to write through symlink: {path}")
    if path.exists() and not path.is_file():
        raise ContractError(f"required file is not a regular file: {path}")
    return path


def _preflight_destination(root: pathlib.Path, relative: str) -> pathlib.Path:
    if fs.unsafe_parts(pathlib.PurePosixPath(relative).parts):
        raise ContractError(f"destination contains a reserved Windows name: {relative}")
    path = root / pathlib.PurePosixPath(relative)
    if not _inside(root, path.parent):
        raise ContractError(f"destination escapes project: {relative}")
    current = root
    for part in path.parent.relative_to(root).parts:
        if part in ("", "."):
            continue
        current = current / part
        if fs.is_link(current):
            raise ContractError(f"refusing to follow symlink in project state: {current}")
        if current.exists() and not current.is_dir():
            raise ContractError(f"required directory is a file: {current}")
    if fs.is_link(path):
        raise ContractError(f"refusing to write through symlink: {path}")
    if path.exists() and not path.is_file():
        raise ContractError(f"required file is not a regular file: {path}")
    return path


def _state_relative(value: Any, *, key: str, file: bool) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError(f"state path {key} must be a non-empty relative path")
    raw = value.replace("\\", "/")
    path = pathlib.PurePosixPath(raw)
    if (
        path.is_absolute()
        or re.match(r"^[A-Za-z]:", raw)
        or any(part in ("", ".", "..") for part in path.parts)
        or any(char in raw for char in "*?[")
    ):
        raise ContractError(f"state path {key} must stay inside the project: {value}")
    if fs.unsafe_parts(path.parts):
        raise ContractError(f"state path {key} contains a reserved Windows name")
    if file and path.name in ("", "."):
        raise ContractError(f"state path {key} must name a file")
    return path.as_posix()


def _validate_state_paths(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != set(DEFAULT_STATE_PATHS):
        raise ContractError(f"state paths must define exactly: {', '.join(DEFAULT_STATE_PATHS)}")
    paths = {
        key: _state_relative(value[key], key=key, file=key == "context")
        for key in DEFAULT_STATE_PATHS
    }
    path_class = pathlib.PureWindowsPath if os.name == "nt" else pathlib.PurePosixPath
    directories = [path_class(paths[key]) for key in ("tasks", "sessions", "memory", "checks")]
    if any(
        left == right or left in right.parents or right in left.parents
        for index, left in enumerate(directories)
        for right in directories[index + 1 :]
    ):
        raise ContractError("state directories must be distinct and non-overlapping")
    context = path_class(paths["context"])
    if any(context == directory or directory in context.parents for directory in directories):
        raise ContractError("context file must not be inside a managed state directory")
    return paths


def _read_state_file(project: pathlib.Path) -> dict[str, str] | None:
    path = project / STATE_FILE
    if not path.exists():
        return None
    if not path.is_file() or fs.is_link(path):
        raise ContractError(f"state configuration is not a regular file: {STATE_FILE}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid state configuration {STATE_FILE}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or type(data.get("schema_version")) is not int
        or data["schema_version"] != 1
    ):
        raise ContractError(f"invalid state configuration schema in {STATE_FILE}")
    return _validate_state_paths(data.get("paths"))


def _state_paths(project: pathlib.Path) -> dict[str, str]:
    configured = _read_state_file(project)
    if configured is not None:
        return configured
    return dict(DEFAULT_STATE_PATHS)


def _requested_state_paths(args: argparse.Namespace) -> dict[str, str]:
    return _validate_state_paths(
        {
            "context": args.context or DEFAULT_STATE_PATHS["context"],
            "tasks": args.tasks_dir or DEFAULT_STATE_PATHS["tasks"],
            "sessions": args.sessions_dir or DEFAULT_STATE_PATHS["sessions"],
            "memory": args.memory_dir or DEFAULT_STATE_PATHS["memory"],
            "checks": args.checks_dir or DEFAULT_STATE_PATHS["checks"],
        }
    )


def _plugin_version() -> str:
    manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    version = manifest.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ContractError("plugin manifest has no valid version")
    return version.split("+", 1)[0]


def _project_sync_state(project: pathlib.Path) -> dict[str, Any] | None:
    path = project / PROJECT_SYNC_STATE_FILE
    if not path.exists() and not fs.is_link(path):
        return None
    if fs.is_link(path) or not path.is_file():
        raise ContractError(f"project sync state is not a regular file: {PROJECT_SYNC_STATE_FILE}")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid project sync state {PROJECT_SYNC_STATE_FILE}: {exc}") from exc
    if (
        not isinstance(state, dict)
        or type(state.get("state_version")) is not int
        or state["state_version"] != 1
    ):
        raise ContractError(f"unsupported project sync state format in {PROJECT_SYNC_STATE_FILE}")
    if (
        not isinstance(state.get("plugin_version"), str)
        or type(state.get("project_schema_version")) is not int
        or state["project_schema_version"] < 0
        or not isinstance(state.get("managed_roles"), dict)
        or not isinstance(state.get("synced_at"), str)
        or (
            state.get("sync_commit_id") is not None
            and not isinstance(state.get("sync_commit_id"), str)
        )
    ):
        raise ContractError(f"malformed project sync state in {PROJECT_SYNC_STATE_FILE}")
    for name, role in state["managed_roles"].items():
        if (
            name not in ROLE_NAMES
            or not isinstance(role, dict)
            or not _is_sha256(role.get("template_sha256"))
            or not _is_sha256(role.get("installed_sha256"))
            or type(role.get("locally_modified")) is not bool
        ):
            raise ContractError(f"malformed managed role state for {name!r}")
        if any(key in role for key in ROLE_STATE_FIELDS):
            overrides = role.get("local_overrides")
            hashes = role.get("override_fields_sha256")
            if (
                role.get("managed_fields")
                not in (list(MANAGED_FIELDS), list(MANAGED_FIELDS + OPTIONAL_MANAGED_FIELDS))
                or not _is_sha256(role.get("managed_fields_sha256"))
                or not isinstance(overrides, dict)
                or not set(overrides) <= set(LOCAL_OVERRIDE_FIELDS)
                or any(
                    not isinstance(value, str) or not value.strip() for value in overrides.values()
                )
                or not isinstance(hashes, dict)
                or set(hashes) != set(LOCAL_OVERRIDE_FIELDS)
                or any(not _is_sha256(value) for value in hashes.values())
                or any(hashes[key] != _value_digest(value) for key, value in overrides.items())
                or role["locally_modified"] != bool(overrides)
            ):
                raise ContractError(f"malformed field-aware role state for {name!r}")
    return state


def _value_digest(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _role_data(data: bytes, name: str) -> dict[str, str]:
    try:
        role = tomllib.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ContractError(f"{name}: invalid role TOML ({exc})") from exc
    fields = set(MANAGED_FIELDS) | {"model_reasoning_effort"}
    allowed = fields | set(OPTIONAL_MANAGED_FIELDS + LOCAL_OVERRIDE_FIELDS)
    if not fields <= set(role) or not set(role) <= allowed:
        raise ContractError(
            f"{name}: role fields outside contract or missing: {sorted((set(role) - allowed) | (fields - set(role)))}"
        )
    if any(not isinstance(value, str) or not value.strip() for value in role.values()):
        raise ContractError(f"{name}: role fields must be non-empty strings")
    if role["name"] != name:
        raise ContractError(f"{name}: role name mismatch")
    return role


def _managed_digest(role: dict[str, str]) -> str:
    return _value_digest(
        {key: role[key] for key in MANAGED_FIELDS + OPTIONAL_MANAGED_FIELDS if key in role}
    )


def _legacy_role_templates() -> dict[str, list[dict[str, Any]]]:
    path = ASSET_ROOT / "legacy-managed-role-fields.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["schema_version"] != 1 or set(data["roles"]) != set(ROLE_NAMES):
            raise ValueError("unsupported legacy role catalog")
        for name, entries in data["roles"].items():
            if not isinstance(entries, list):
                raise ValueError(f"invalid legacy role entries: {name}")
            for entry in entries:
                projection = entry["managed_fields"]
                if (
                    not isinstance(projection, dict)
                    or set(projection)
                    not in (set(MANAGED_FIELDS), set(MANAGED_FIELDS + OPTIONAL_MANAGED_FIELDS))
                    or any(
                        not isinstance(value, str) or not value.strip()
                        for value in projection.values()
                    )
                    or projection["name"] != name
                    or _value_digest(projection) != entry["managed_fields_sha256"]
                    or not _is_sha256(entry["template_sha256"])
                ):
                    raise ValueError(f"invalid legacy template: {name}")
        return data["roles"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ContractError(f"invalid legacy role catalog: {exc}") from exc


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _role_record(
    template: bytes, installed: bytes, name: str, overrides: dict[str, str]
) -> dict[str, Any]:
    role = _role_data(installed, name)
    return {
        "template_sha256": hashlib.sha256(template).hexdigest(),
        "installed_sha256": hashlib.sha256(installed).hexdigest(),
        "locally_modified": bool(overrides),
        "managed_fields": [key for key in MANAGED_FIELDS + OPTIONAL_MANAGED_FIELDS if key in role],
        "managed_fields_sha256": _managed_digest(role),
        "local_overrides": overrides,
        "override_fields_sha256": {
            key: _value_digest(role.get(key)) for key in LOCAL_OVERRIDE_FIELDS
        },
    }


def _merge_role(
    name: str,
    template: bytes,
    installed: bytes | None,
    prior: dict[str, Any] | None,
    history: list[dict[str, Any]],
) -> tuple[bytes, dict[str, Any]]:
    desired = _role_data(template, name)
    template_hash = hashlib.sha256(template).hexdigest()
    overrides: dict[str, str] = {}
    if installed is None:
        return template, _role_record(template, template, name, overrides)
    local = _role_data(installed, name)
    for key in OPTIONAL_MANAGED_FIELDS:
        if (
            key in local
            and key not in desired
            and not any(key in entry["managed_fields"] for entry in history)
        ):
            raise ContractError(f"{name}: {key} is not a managed field for this role")
    current_hash = hashlib.sha256(installed).hexdigest()
    managed_hash = _managed_digest(local)
    known_managed = {
        _managed_digest(desired),
        *(entry["managed_fields_sha256"] for entry in history),
    }
    known_templates = {template_hash, *(entry["template_sha256"] for entry in history)}
    if prior is not None and "managed_fields" in prior:
        if managed_hash != prior["managed_fields_sha256"]:
            raise ContractError(f"{name}: local edit to DevCrew managed fields")
        overrides = dict(prior["local_overrides"])
        changed_overrides = False
        for key in LOCAL_OVERRIDE_FIELDS:
            if _value_digest(local.get(key)) != prior["override_fields_sha256"][key]:
                changed_overrides = True
                if key not in local or local[key] == desired.get(key):
                    overrides.pop(key, None)
                else:
                    overrides[key] = local[key]
        if current_hash != prior["installed_sha256"] and not changed_overrides:
            raise ContractError(f"{name}: file changed outside permitted override values")
    elif prior is not None:
        # Old whole-file state proves an unmodified installed template, but cannot
        # authorize arbitrary managed-field edits or infer missing provenance.
        if current_hash != prior["installed_sha256"]:
            raise ContractError(f"{name}: legacy state cannot prove local changes safe")
        if not prior["locally_modified"] and current_hash == prior["template_sha256"]:
            pass
        elif managed_hash in known_managed:
            overrides = {
                key: local[key]
                for key in LOCAL_OVERRIDE_FIELDS
                if key in local and local[key] != desired.get(key)
            }
        else:
            raise ContractError(f"{name}: legacy state has unknown managed instructions")
    elif current_hash in known_templates:
        pass
    elif managed_hash in known_managed:
        overrides = {
            key: local[key]
            for key in LOCAL_OVERRIDE_FIELDS
            if key in local and local[key] != desired.get(key)
        }
    else:
        raise ContractError(f"{name}: managed fields do not match a known DevCrew template")
    merged = {**desired, **overrides}
    template_changed = prior is not None and prior["template_sha256"] != template_hash
    if merged == local and not template_changed:
        content = installed
    elif not overrides:
        content = template
    else:
        # Only validated contract fields are serialized. Unknown settings
        # are rejected above, never silently dropped by this restricted renderer.
        order = (
            "name",
            "description",
            *LOCAL_OVERRIDE_FIELDS,
            *OPTIONAL_MANAGED_FIELDS,
            "developer_instructions",
        )
        content = "".join(
            f"{key} = {json.dumps(merged[key], ensure_ascii=False)}\n"
            for key in order
            if key in merged
        ).encode("utf-8")
    if _role_data(content, name) != merged:
        raise ContractError(f"{name}: generated role does not match planned fields")
    return content, _role_record(template, content, name, overrides)


def _sync_state_for(
    project: pathlib.Path, *, managed_roles: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    if managed_roles is None:
        managed_roles = {}
        for name in ROLE_NAMES:
            template = (ROLE_ROOT / f"{name}.toml").read_bytes()
            installed_path = project / ".codex/agents" / f"{name}.toml"
            if not installed_path.is_file() or fs.is_link(installed_path):
                continue
            installed = installed_path.read_bytes()
            role, defaults = _role_data(installed, name), _role_data(template, name)
            overrides = {
                key: role[key]
                for key in LOCAL_OVERRIDE_FIELDS
                if key in role and role[key] != defaults.get(key)
            }
            managed_roles[name] = _role_record(template, installed, name, overrides)
    return {
        "state_version": 1,
        "plugin_version": _plugin_version(),
        "project_schema_version": PROJECT_SCHEMA_VERSION,
        "synced_at": _utc_now(),
        "sync_commit_id": None,
        "git_base_commit": _git_head(project),
        "managed_roles": managed_roles,
    }


def _git_output(project: pathlib.Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", "-C", str(project), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if check and result.returncode:
        raise ContractError(result.stderr.strip() or f"git {' '.join(args)} failed")
    return result.stdout.strip()


def _git_has_staged_changes(project: pathlib.Path) -> bool:
    return bool(_git_output(project, "diff", "--cached", "--name-only"))


def _git_path_has_staged_changes(project: pathlib.Path, relative: str) -> bool:
    return bool(_git_output(project, "diff", "--cached", "--name-only", "--", relative))


def _git_sync_safety_conflicts(project: pathlib.Path) -> list[str]:
    conflicts: list[str] = []
    if _git_output(project, "ls-files", "--unmerged"):
        conflicts.append("Git index contains unmerged entries")
    for state_name in (
        "MERGE_HEAD",
        "CHERRY_PICK_HEAD",
        "REVERT_HEAD",
        "rebase-apply",
        "rebase-merge",
        "sequencer",
        "index.lock",
    ):
        raw_path = _git_output(project, "rev-parse", "--git-path", state_name, check=False)
        if raw_path:
            state_path = pathlib.Path(raw_path)
            if not state_path.is_absolute():
                state_path = project / state_path
            if state_path.exists():
                conflicts.append(f"Git operation is in progress: {state_name}")
    return conflicts


def _last_sync_commit(project: pathlib.Path, state: dict[str, Any] | None) -> str | None:
    sync_id = state.get("sync_commit_id") if state else None
    if not isinstance(sync_id, str) or _git_root(project) is None:
        return None
    return (
        _git_output(
            project,
            "log",
            "--all",
            "-n",
            "1",
            "--format=%H",
            "--grep",
            f"DevCrew-Sync-ID: {sync_id}",
            check=False,
        )
        or None
    )


def _sync_analysis(project: pathlib.Path, *, with_contents: bool = False) -> dict[str, Any]:
    current_plugin = _plugin_version()
    base: dict[str, Any] = {
        "plugin_version": current_plugin,
        "plugin_project_schema": PROJECT_SCHEMA_VERSION,
        "project_recorded_version": None,
        "recorded_project_schema": None,
        "last_sync_commit": None,
        "missing_roles": [],
        "locally_modified_roles": [],
        "role_updates": [],
        "conflicts": [],
        "managed_roles": {},
    }
    try:
        state_file = _project_sync_state(project)
        legacy_config = project / STATE_FILE
        existing_roles = any(
            (project / f".codex/agents/{name}.toml").exists()
            or fs.is_link(project / f".codex/agents/{name}.toml")
            for name in ROLE_NAMES
        )
        if state_file is None and not legacy_config.exists() and not existing_roles:
            return {**base, "status": "PROJECT_NOT_INITIALIZED"}
        if legacy_config.exists():
            _read_state_file(project)
        recorded_schema = state_file["project_schema_version"] if state_file else 0
        base["project_recorded_version"] = state_file.get("plugin_version") if state_file else None
        base["recorded_project_schema"] = recorded_schema
        base["last_sync_commit"] = _last_sync_commit(project, state_file)
        if recorded_schema > PROJECT_SCHEMA_VERSION:
            base["conflicts"].append("project schema is newer than this plugin")
        history = _legacy_role_templates()
        contents: dict[str, bytes] = {}
        state_update = state_file is None
        _, ignore_before, ignore_content = _runtime_ignore_plan(project)
        state_update |= ignore_before != ignore_content
        _preflight_destination(project, PROJECT_SYNC_STATE_FILE)
        for name in ROLE_NAMES:
            relative = f".codex/agents/{name}.toml"
            try:
                path = _preflight_destination(project, relative)
                template = (ROLE_ROOT / f"{name}.toml").read_bytes()
                installed = path.read_bytes() if path.exists() else None
                prior = (state_file or {}).get("managed_roles", {}).get(name)
                content, record = _merge_role(name, template, installed, prior, history[name])
                contents[name] = content
                base["managed_roles"][name] = record
                state_update |= prior != record
                if record["local_overrides"]:
                    base["locally_modified_roles"].append(name)
                if installed is None:
                    base["missing_roles"].append(name)
                elif content != installed:
                    base["role_updates"].append(name)
            except (ContractError, OSError, ValueError) as exc:
                base["conflicts"].append(f"{relative}: {exc}")
        if state_file is not None:
            for old_name in set(state_file["managed_roles"]) - set(ROLE_NAMES):
                base["conflicts"].append(f"managed role no longer shipped: {old_name}")
        if base["conflicts"]:
            status = "SYNC_CONFLICT"
        elif (
            recorded_schema < PROJECT_SCHEMA_VERSION
            or state_update
            or base["missing_roles"]
            or base["role_updates"]
        ):
            status = "SYNC_REQUIRED"
        else:
            status = "SYNC_OK"
        if status == "SYNC_REQUIRED" and _git_root(project) is not None:
            base["conflicts"].extend(_git_sync_safety_conflicts(project))
            managed_paths = [
                PROJECT_SYNC_STATE_FILE,
                *(f".codex/agents/{name}.toml" for name in ROLE_NAMES),
            ]
            for relative in managed_paths:
                if _git_path_has_staged_changes(project, relative):
                    base["conflicts"].append(f"managed path has existing Git changes: {relative}")
                if relative == PROJECT_SYNC_STATE_FILE and _git_output(
                    project, "diff", "--name-only", "--", relative
                ):
                    base["conflicts"].append(f"managed path has existing Git changes: {relative}")
            if base["conflicts"]:
                status = "SYNC_CONFLICT"
        if with_contents:
            base["_role_contents"] = contents
        return {**base, "status": status}
    except (ContractError, OSError, ValueError) as exc:
        return {**base, "status": "SYNC_CONFLICT", "conflicts": [str(exc)]}


def _sync_status(args: argparse.Namespace) -> int:
    project = _project(args.project)
    payload = _sync_analysis(project)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if payload["status"] == "SYNC_CONFLICT" else 0


def _sync_error(message: str, details: Any = None) -> int:
    payload: dict[str, Any] = {"ok": False, "status": "SYNC_CONFLICT", "error": message}
    if details is not None:
        payload["details"] = details
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr)
    return 1


def _sync_project(args: argparse.Namespace) -> int:
    project = _project(args.project)
    plan = _sync_analysis(project, with_contents=True)
    role_contents = plan.pop("_role_contents", {})
    if plan["status"] != "SYNC_REQUIRED":
        print(
            json.dumps(
                {**plan, "changed": False, "changed_paths": [], "sync_commit": None},
                ensure_ascii=False,
            )
        )
        return 1 if plan["status"] == "SYNC_CONFLICT" else 0

    ignore_path, ignore_before, ignore_content = _runtime_ignore_plan(project)
    changed_paths = sorted(
        [f".codex/agents/{name}.toml" for name in plan["missing_roles"] + plan["role_updates"]]
        + [PROJECT_SYNC_STATE_FILE]
        + ([".gitignore"] if ignore_content != ignore_before else [])
    )
    backups: dict[str, bytes | None] = {}
    for relative in changed_paths:
        target = project / relative
        if target.exists():
            if fs.is_link(target) or not target.is_file():
                return _sync_error("managed destination is not a regular file", details=relative)
            backups[relative] = target.read_bytes()
        else:
            backups[relative] = None
    sync_state = _sync_state_for(project, managed_roles=plan["managed_roles"])
    try:
        if ignore_content != ignore_before:
            _atomic_write(ignore_path, ignore_content)
        for name in plan["missing_roles"] + plan["role_updates"]:
            relative = f".codex/agents/{name}.toml"
            target = _safe_destination(project, relative)
            _atomic_write(target, role_contents[name])
        _json_dump(project / PROJECT_SYNC_STATE_FILE, sync_state)
        restart = any(
            name in plan["missing_roles"] or name in plan["role_updates"] for name in ROLE_NAMES
        )
        status = "RUNTIME_RESTART_RECOMMENDED" if restart else "SYNC_OK"
        print(
            json.dumps(
                {
                    "status": status,
                    "changed": True,
                    "changed_paths": changed_paths,
                    "plugin_version": _plugin_version(),
                    "project_schema_version": PROJECT_SCHEMA_VERSION,
                    "sync_commit": None,
                    "sync_commit_id": None,
                    "message": (
                        "DevCrew project integration updated. A new Codex task/session is recommended to load new agent roles."
                        if restart
                        else None
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 0
    except (ContractError, OSError, subprocess.CalledProcessError) as exc:
        for relative, prior in backups.items():
            target = project / relative
            if prior is None:
                target.unlink(missing_ok=True)
            else:
                _atomic_write(target, prior)
        return _sync_error("project sync failed safely", details=str(exc))


def _runtime_ignore_plan(project: pathlib.Path) -> tuple[pathlib.Path, bytes, bytes]:
    path = _preflight_destination(project, ".gitignore")
    before = path.read_bytes() if path.exists() else b""
    if _git_root(project) is not None:
        # Let Git resolve rule precedence, including global and info/exclude rules.
        # Ignore index membership: tracked runtime files need separate cleanup.
        ignored = _git(
            project, "check-ignore", "--quiet", "--no-index", "--", ".codex/", check=False
        )
        if ignored.returncode == 0:
            return path, before, before
        if ignored.returncode != 1:
            raise ContractError(
                (ignored.stderr or ignored.stdout).strip() or "git check-ignore failed"
            )
    else:
        # Without a Git repository, preserve the conservative literal-rule fallback.
        lines = before.rstrip().splitlines()
        if lines and lines[-1] in (b"/.codex/", b".codex/"):
            return path, before, before
    separator = b"\n" if before and not before.endswith(b"\n") else b""
    return path, before, before + separator + b"/.codex/\n"


def _migrate_state(args: argparse.Namespace) -> int:
    project = _project(args.project)
    configured = _read_state_file(project)
    if configured is None:
        raise ContractError("state migration requires an initialized project")
    config_path = _preflight_destination(project, STATE_FILE)
    config_before = config_path.read_bytes()
    config = json.loads(config_before)
    paths = dict(configured)
    moves = {}
    for key in ("tasks", "sessions"):
        if pathlib.PurePosixPath(paths[key]).parts[0] != ".codex":
            moves[key] = {"from": paths[key], "to": DEFAULT_STATE_PATHS[key]}
            paths[key] = DEFAULT_STATE_PATHS[key]
    _validate_state_paths(paths)
    for move in moves.values():
        source = _preflight_destination(project, move["from"] + "/.probe").parent
        target = _preflight_destination(project, move["to"] + "/.probe").parent
        if not source.is_dir():
            raise ContractError(f"migration source directory is missing: {move['from']}")
        if target.exists():
            raise ContractError(f"migration destination already exists: {move['to']}")
        for path in source.rglob("*"):
            if fs.is_link(path) or not (path.is_file() or path.is_dir()):
                raise ContractError(f"unsupported migration source: {path.relative_to(project)}")
    ignore_path, ignore_before, ignore_content = _runtime_ignore_plan(project)
    payload = {
        "ok": True,
        "dry_run": args.dry_run,
        "moves": moves,
        "state_paths": paths,
        "changed": False,
        "gitignore_update": ignore_content != ignore_before,
    }
    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    moved = []
    ignore_existed = ignore_path.exists()
    try:
        for move in moves.values():
            source, target = project / move["from"], project / move["to"]
            source.rename(target)
            moved.append((source, target))
        if moves:
            config["paths"] = paths
            _json_dump(config_path, config)
        if ignore_content != ignore_before:
            _atomic_write(ignore_path, ignore_content)
    except OSError:
        for source, target in reversed(moved):
            target.rename(source)
        if config_path.read_bytes() != config_before:
            _atomic_write(config_path, config_before)
        if ignore_existed:
            _atomic_write(ignore_path, ignore_before)
        else:
            ignore_path.unlink(missing_ok=True)
        raise
    payload["changed"] = bool(moves) or payload["gitignore_update"]
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def _init(args: argparse.Namespace) -> int:
    project = _project(args.project)
    configured = _read_state_file(project)
    requested = _requested_state_paths(args)
    explicit_layout = any(
        (args.context, args.tasks_dir, args.sessions_dir, args.memory_dir, args.checks_dir)
    )
    ignore_path, ignore_before, ignore_content = _runtime_ignore_plan(project)
    if configured is not None:
        if explicit_layout and configured != requested:
            return _json_error(
                "requested state paths differ from existing configuration",
                details={"configured": configured, "requested": requested},
            )
        sync_status = _sync_analysis(project)
        if sync_status["status"] != "SYNC_OK":
            result = _sync_project(args)
            if result == 0 and ignore_content != ignore_before:
                _atomic_write(ignore_path, ignore_content)
            return result
        state_paths = configured
    else:
        state_paths = requested
    role_sources = {f".codex/agents/{name}.toml": ROLE_ROOT / f"{name}.toml" for name in ROLE_NAMES}
    project_templates = PROJECT_ASSETS / "ru" if args.language == "ru" else PROJECT_ASSETS
    docs_sources = {
        state_paths["context"]: project_templates / "context.md",
        f"{state_paths['tasks']}/README.md": project_templates / "tasks/README.md",
        f"{state_paths['tasks']}/card.template.md": project_templates / "task-ledger.template.md",
        f"{state_paths['sessions']}/README.md": project_templates / "sessions/README.md",
        f"{state_paths['memory']}/README.md": project_templates / "memory/README.md",
    }
    # Read all package inputs and preflight destinations before any project write.
    asset_data = {
        relative: source.read_bytes()
        for relative, source in {**role_sources, **docs_sources}.items()
    }
    for name in ROLE_NAMES:
        _role_data(asset_data[f".codex/agents/{name}.toml"], name)
    conflicts: list[str] = []
    for relative, source in role_sources.items():
        destination = project / relative
        if fs.is_link(destination):
            conflicts.append(f"{relative}: symlink destination")
        elif (
            configured is None
            and destination.exists()
            and destination.read_bytes() != asset_data[relative]
        ):
            conflicts.append(f"{relative}: existing file differs from shipped role")
    if conflicts:
        return _json_error("initialization preflight found role conflicts", details=conflicts)
    try:
        _project_sync_state(project)
    except ContractError as exc:
        return _json_error(
            "initialization preflight found invalid project sync state", details=str(exc)
        )
    try:
        for relative in (
            list(role_sources) + list(docs_sources) + [STATE_FILE, PROJECT_SYNC_STATE_FILE]
        ):
            _preflight_destination(project, relative)
        _preflight_destination(project, f"{state_paths['checks']}/.probe")
    except (OSError, ContractError) as exc:
        return _json_error(f"initialization preflight failed: {exc}")

    context_parent = pathlib.PurePosixPath(state_paths["context"]).parent.as_posix()
    for directory in (
        ".codex/agents",
        context_parent,
        state_paths["tasks"],
        state_paths["sessions"],
        state_paths["memory"],
        state_paths["checks"],
    ):
        _ensure_dir(project, directory)
    created: list[str] = []
    skipped: list[str] = []
    if ignore_content != ignore_before:
        _atomic_write(ignore_path, ignore_content)
        created.append(".gitignore")
    else:
        skipped.append(".gitignore")
    for relative, source in role_sources.items():
        destination = project / relative
        if destination.exists():
            skipped.append(relative)
        else:
            destination.write_bytes(asset_data[relative])
            created.append(relative)
    for relative, source in docs_sources.items():
        destination = project / relative
        if destination.exists():
            skipped.append(relative)
        else:
            destination.write_bytes(asset_data[relative])
            created.append(relative)
    state_file = project / STATE_FILE
    if state_file.exists():
        skipped.append(STATE_FILE)
    else:
        _json_dump(state_file, {"schema_version": 1, "paths": state_paths})
        created.append(STATE_FILE)
    sync_state_path = project / PROJECT_SYNC_STATE_FILE
    if sync_state_path.exists() or fs.is_link(sync_state_path):
        try:
            _project_sync_state(project)
        except ContractError as exc:
            return _json_error("existing project sync state is invalid", details=str(exc))
        skipped.append(PROJECT_SYNC_STATE_FILE)
    else:
        _json_dump(sync_state_path, _sync_state_for(project))
        created.append(PROJECT_SYNC_STATE_FILE)
    instruction_files = [name for name in ("AGENTS.md", "CLAUDE.md") if (project / name).is_file()]
    print(
        json.dumps(
            {
                "ok": True,
                "created": created,
                "skipped": skipped,
                "roles": list(ROLE_NAMES),
                "project_sync_state": PROJECT_SYNC_STATE_FILE,
                "project_schema_version": PROJECT_SCHEMA_VERSION,
                "state_paths": state_paths,
                "instruction_files": instruction_files,
            }
        )
    )
    return 0


def _doctor(args: argparse.Namespace) -> int:
    try:
        project = _project(args.project)
    except ContractError as exc:
        return _json_error(str(exc))
    errors: list[str] = []
    warnings: list[str] = []
    try:
        state_paths = _state_paths(project)
    except ContractError as exc:
        state_paths = dict(DEFAULT_STATE_PATHS)
        errors.append(str(exc))
    intent_environment = devcrew_phase._prompt_receipts().environment_status(
        os.environ.get("PLUGIN_DATA")
    )
    if intent_environment["status"] != "READY":
        warnings.append("Intent capture environment: " + intent_environment["reason"])
    required_files = [
        pathlib.PurePosixPath(state_paths["context"]),
        pathlib.PurePosixPath(state_paths["tasks"]) / "README.md",
        pathlib.PurePosixPath(state_paths["sessions"]) / "README.md",
        pathlib.PurePosixPath(state_paths["memory"]) / "README.md",
    ]
    for relative in required_files:
        path = project / relative
        if not path.is_file() or fs.is_link(path):
            errors.append(f"missing required project file: {relative}")
    for relative in (
        state_paths["tasks"],
        state_paths["sessions"],
        state_paths["memory"],
        state_paths["checks"],
    ):
        path = project / relative
        if not path.is_dir() or fs.is_link(path):
            errors.append(f"missing required project directory: {relative}")
    role_dir = project / ".codex/agents"
    if not role_dir.is_dir() or fs.is_link(role_dir):
        errors.append("missing required project directory: .codex/agents")
    for name in ROLE_NAMES:
        relative = f".codex/agents/{name}.toml"
        path = project / relative
        if not path.is_file() or fs.is_link(path):
            errors.append(f"missing role file: {relative}")
            continue
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"invalid role TOML {relative}: {exc}")
            continue
        for field in ("name", "description", "developer_instructions"):
            if not isinstance(data.get(field), str) or not data[field].strip():
                errors.append(f"role {relative} has missing or empty {field}")
        if data.get("name") != name:
            errors.append(f"role {relative} has name {data.get('name')!r}, expected {name!r}")
        for optional in ("model", "model_reasoning_effort"):
            if optional in data and (
                not isinstance(data[optional], str) or not data[optional].strip()
            ):
                errors.append(f"role {relative} has invalid {optional}")
    if not (project / "AGENTS.md").is_file():
        warnings.append(
            "AGENTS.md not found; repository-specific instructions could not be discovered there"
        )
    payload = {
        "ok": not errors,
        "project": str(project),
        "errors": errors,
        "warnings": warnings,
        "roles": list(ROLE_NAMES),
        "state_paths": state_paths,
        "intent_environment": intent_environment,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


def _git_root(project: pathlib.Path) -> pathlib.Path | None:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return pathlib.Path(output).resolve()


def _git_head(project: pathlib.Path) -> str | None:
    try:
        return subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_tree(project: pathlib.Path) -> str | None:
    try:
        # write-tree can update the index's cache-tree extension even when the
        # checked command is read-only. Use a private copy of the active index.
        index_name = subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "--git-path", "index"],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="surrogateescape",
        ).strip()
        index = pathlib.Path(index_name)
        if not index.is_absolute():
            index = project / index
        with tempfile.TemporaryDirectory(prefix="devcrew-index-") as directory:
            private_index = pathlib.Path(directory) / "index"
            if index.exists():
                shutil.copyfile(index, private_index)
            env = {**os.environ, "GIT_INDEX_FILE": str(private_index)}
            return subprocess.check_output(
                ["git", "-C", str(project), "write-tree"],
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="surrogateescape",
                env=env,
            ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _tracked_paths(project: pathlib.Path) -> list[str] | None:
    if _git_root(project) is None:
        return None

    def legacy_paths():
        try:
            raw = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(project),
                    "ls-files",
                    "-co",
                    "--exclude-standard",
                    "-z",
                    "--",
                    ".",
                ],
                stderr=subprocess.DEVNULL,
            )
            staged = subprocess.check_output(
                ["git", "-C", str(project), "ls-files", "--stage", "-z", "--", "."],
                stderr=subprocess.DEVNULL,
            )
            if any(entry.startswith(b"160000 ") for entry in staged.split(b"\0")):
                raise ContractError(
                    "Git submodules are not supported by check evidence; run checks inside each submodule as a separate project"
                )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise ContractError(f"cannot enumerate Git working tree: {exc}") from exc
        return {item.decode("utf-8", "surrogateescape") for item in raw.split(b"\0") if item}

    try:
        raw = subprocess.check_output(
            [
                "git",
                "-C",
                str(project),
                "ls-files",
                "-t",
                "--stage",
                "--cached",
                "--others",
                "--exclude-standard",
                "-z",
                "--",
                ".",
            ],
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.CalledProcessError):
        values = legacy_paths()
    else:
        values = set()
        malformed = False
        known_tags = {b"H", b"S", b"M", b"R", b"C", b"K"}
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            if len(entry) < 3 or entry[1:2] != b" ":
                malformed = True
                break
            tag, payload = entry[:1], entry[2:]
            if tag == b"?":
                if not payload:
                    malformed = True
                    break
                values.add(payload.decode("utf-8", "surrogateescape"))
                continue
            if tag not in known_tags:
                malformed = True
                break
            metadata, separator, name = payload.partition(b"\t")
            fields = metadata.split(b" ")
            if (
                not separator
                or not name
                or len(fields) != 3
                or not re.fullmatch(rb"[0-7]{6}", fields[0])
                or not re.fullmatch(rb"[0-9a-fA-F]+", fields[1])
                or fields[2] not in {b"0", b"1", b"2", b"3"}
            ):
                malformed = True
                break
            if fields[0] == b"160000":
                raise ContractError(
                    "Git submodules are not supported by check evidence; run checks inside each submodule as a separate project"
                )
            values.add(name.decode("utf-8", "surrogateescape"))
        if malformed:
            values = legacy_paths()
    if any(value.endswith("/") for value in values):
        raise ContractError(
            "Untracked nested Git repositories are not supported by check evidence; run checks inside each nested repository as a separate project"
        )
    return sorted(values)


def _file_digest(path: pathlib.Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _validate_fingerprint(value: Any, label: str) -> None:
    if (
        not isinstance(value, dict)
        or value.get("algorithm") != "sha256"
        or not isinstance(value.get("digest"), str)
        or re.fullmatch(r"[0-9a-f]{64}", value["digest"]) is None
        or type(value.get("files")) is not int
        or value["files"] < 0
        or type(value.get("git")) is not bool
    ):
        raise ContractError(f"{label} is malformed")


def _tree_entries(
    project: pathlib.Path, checks_relative: str | None = None
) -> list[tuple[str, str, str]]:
    paths = _tracked_paths(project)
    if paths is None:
        paths = []
        for current, dirs, files in os.walk(project, followlinks=False):
            current_path = pathlib.Path(current)
            kept_dirs: list[str] = []
            for dirname in dirs:
                candidate = current_path / dirname
                if dirname == ".git":
                    continue
                if fs.is_link(candidate):
                    if not _inside(project, candidate):
                        raise ContractError(
                            f"symlink escapes project: {candidate.relative_to(project)}"
                        )
                    paths.append(candidate.relative_to(project).as_posix())
                    continue
                kept_dirs.append(dirname)
            dirs[:] = kept_dirs
            for filename in files:
                path = current_path / filename
                relative = path.relative_to(project).as_posix()
                paths.append(relative)
    excluded_path = (
        checks_relative if checks_relative is not None else _state_paths(project)["checks"]
    )
    excluded = pathlib.PurePosixPath(excluded_path) if excluded_path else None
    entries: list[tuple[str, str, str]] = []
    for relative in sorted(set(paths)):
        rel_path = pathlib.PurePosixPath(relative)
        if excluded is not None and (rel_path == excluded or excluded in rel_path.parents):
            continue
        path = project / rel_path
        if not _inside(project, path.parent):
            raise ContractError(f"tracked path escapes project: {relative}")
        try:
            stat = path.lstat()
        except FileNotFoundError:
            entries.append((relative, "missing", ""))
            continue
        if fs.is_link(path):
            target = os.readlink(path)
            if not _inside(project, path.parent / target):
                raise ContractError(f"symlink escapes project: {relative}")
            entries.append((relative, "symlink", target))
        elif stat.st_mode & 0o170000 == 0o100000:
            digest = _file_digest(path)
            entries.append((relative, "file", f"{stat.st_mode & 0o7777:o}:{digest}"))
        else:
            entries.append((relative, "other", str(stat.st_mode)))
    return entries


def _entries_fingerprint(entries: list, git: bool) -> dict[str, Any]:
    digest = hashlib.sha256()
    for relative, kind, value in entries:
        digest.update(relative.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
        digest.update(kind.encode())
        digest.update(b"\0")
        digest.update(value.encode("utf-8", "surrogateescape"))
        digest.update(b"\0")
    return {"algorithm": "sha256", "digest": digest.hexdigest(), "files": len(entries), "git": git}


def _fingerprint(project: pathlib.Path, checks_relative: str | None = None) -> dict[str, Any]:
    return _entries_fingerprint(
        _tree_entries(project, checks_relative), _git_root(project) is not None
    )


def _capture_state(
    project: pathlib.Path, *, include_files: bool = False, checks_relative: str | None = None
) -> dict[str, Any]:
    head = _git_head(project)
    entries = _tree_entries(project, checks_relative)
    fingerprint = _entries_fingerprint(entries, _git_root(project) is not None)
    if head != _git_head(project):
        raise ContractError("Git HEAD changed while capturing state; retry without writers")
    state = {
        "schema_version": 1,
        "cwd": str(project),
        "git_head": head,
        "worktree_fingerprint": fingerprint,
    }
    if include_files:
        state["files"] = entries
    return state


def _capture_state_command(args: argparse.Namespace) -> int:
    host = _project(args.project)
    execution = _project(args.execution_root) if args.execution_root else host
    checks_relative = _state_paths(host)["checks"] if execution == host else ""
    print(
        json.dumps(
            _capture_state(
                execution, include_files=args.include_files, checks_relative=checks_relative
            )
        )
    )
    return 0


def _scope_check(args: argparse.Namespace) -> int:
    project = _project(args.project)
    execution = _project(args.execution_root) if args.execution_root else project
    checks_relative = _state_paths(project)["checks"] if execution == project else ""
    before = _load_json_object(args.before, "scope snapshot")
    if (
        type(before.get("schema_version")) is not int
        or before["schema_version"] != 1
        or before.get("cwd") != str(execution)
    ):
        raise ContractError("scope snapshot schema or project differs")
    _validate_fingerprint(before.get("worktree_fingerprint"), "scope fingerprint")
    entries = before.get("files")
    if not isinstance(entries, list):
        raise ContractError("capture scope with capture-state --include-files")
    for entry in entries:
        if (
            not isinstance(entry, list)
            or len(entry) != 3
            or any(not isinstance(item, str) for item in entry)
            or _normalize_rel_file(entry[0], literal=True) != entry[0]
            or entry[1] not in ("file", "symlink", "missing", "other")
        ):
            raise ContractError("malformed scope file entry")
    names = [entry[0] for entry in entries]
    if (
        names != sorted(set(names))
        or _entries_fingerprint(entries, before["worktree_fingerprint"]["git"])
        != before["worktree_fingerprint"]
    ):
        raise ContractError("scope entries disagree with snapshot fingerprint")
    ownership = json.loads(pathlib.Path(args.files).read_text(encoding="utf-8"))
    if not isinstance(ownership, list) or not ownership:
        raise ContractError("ownership must be a nonempty JSON array of explicit file paths")
    allowed = [_normalize_rel_file(path) for path in ownership]
    if len(set(allowed)) != len(allowed):
        raise ContractError("ownership has duplicate aliases")
    current = _capture_state(execution, include_files=True, checks_relative=checks_relative)
    if "git_head" not in before or before["git_head"] != current["git_head"]:
        raise ContractError("Git HEAD changed during the slice; inspect before accepting")
    old = {entry[0]: entry[1:] for entry in entries}
    new = {entry[0]: list(entry[1:]) for entry in current["files"]}
    changed = sorted(path for path in old.keys() | new.keys() if old.get(path) != new.get(path))
    outside = sorted(set(changed) - set(allowed))
    print(json.dumps({"ok": not outside, "changed": changed, "outside_scope": outside}))
    return 1 if outside else 0


def _verify_state(state: Any, project: pathlib.Path, *, checks_relative: str | None = None) -> None:
    if (
        not isinstance(state, dict)
        or type(state.get("schema_version")) is not int
        or state["schema_version"] != 1
    ):
        raise ContractError("state schema_version must be 1; capture state before review")
    if (
        not isinstance(state.get("cwd"), str)
        or not pathlib.Path(state["cwd"]).is_absolute()
        or pathlib.Path(state["cwd"]).resolve() != project
    ):
        raise ContractError("state belongs to a different project")
    if "git_head" not in state or (
        state["git_head"] is not None
        and (not isinstance(state["git_head"], str) or not state["git_head"])
    ):
        raise ContractError("state git_head is invalid")
    _validate_fingerprint(state.get("worktree_fingerprint"), "state worktree_fingerprint")
    current = _capture_state(project, checks_relative=checks_relative)
    if (
        state["git_head"] != current["git_head"]
        or state["worktree_fingerprint"] != current["worktree_fingerprint"]
    ):
        raise ContractError("review state is stale; review the changed tree before approving")


def _safe_check_root(project: pathlib.Path) -> pathlib.Path:
    checks = _safe_destination(project, f"{_state_paths(project)['checks']}/.probe").parent
    if checks.exists() and fs.is_link(checks):
        raise ContractError("checks directory is a symlink")
    return checks


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ContractError("name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "nt":
        import devcrew_winprocess

        devcrew_winprocess.terminate(process.devcrew_group)
        process.wait(timeout=5)
        return
    group_sent = False
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
            group_sent = True
        else:
            process.terminate()
        process.wait(timeout=0.5)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "posix" and group_sent:
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except OSError:
        pass
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass


def _wait_check(
    process: subprocess.Popen, log: pathlib.Path, timeout: float | None, idle_timeout: float | None
) -> str | None:
    if idle_timeout is None:
        try:
            process.wait(timeout=timeout)
            return None
        except subprocess.TimeoutExpired:
            return "wall"
    started = last_output = time.monotonic()
    size = log.stat().st_size
    while process.poll() is None:
        now = time.monotonic()
        observed = log.stat().st_size
        if observed != size:
            size, last_output = observed, now
        if timeout is not None and now - started >= timeout:
            return "wall"
        if now - last_output >= idle_timeout:
            return "idle"
        time.sleep(min(0.1, idle_timeout / 4))
    return None


def _interrupt_check(signum, frame):
    raise KeyboardInterrupt


def _run_check(
    project: pathlib.Path,
    name: str,
    command: list[str],
    timeout: float | None,
    idle_timeout: float | None = None,
    resource_fd: int | None = None,
    execution_root: pathlib.Path | None = None,
    task_id: str | None = None,
) -> tuple[pathlib.Path, dict[str, Any]]:
    _validate_name(name)
    if not command or any(not isinstance(item, str) for item in command):
        raise ContractError("command must contain at least one argv string")
    if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
        raise ContractError("timeout must be a finite positive number")
    if idle_timeout is not None and (not math.isfinite(idle_timeout) or idle_timeout <= 0):
        raise ContractError("idle-timeout must be a finite positive number")
    if (
        task_id is not None
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", task_id)
        and (
            _git_root(project) is not None
            or any(
                (parent / ".git").exists() or fs.is_link(parent / ".git")
                for parent in (project, *project.parents)
            )
        )
    ):
        # Free check labels and non-Git projects have no task lifecycle state.
        # Checks execute arbitrary commands and publish evidence; fail before
        # either side effect when this task has unfinished/inconsistent state.
        *_, state_path = devcrew_worktree.location(project, task_id)
        devcrew_worktree._require_no_candidate_transition(state_path)
        if state_path.exists() or state_path.with_name(f"{task_id}.phases.json").exists():
            fresh = devcrew_worktree.resolve(project, task_id, read_only=True)
            if fresh.get("state") != "RESOLVED":
                raise ContractError(
                    "WORKTREE_UNRESOLVED: task state requires manual reconciliation before checks"
                )
    checks = _safe_check_root(project)
    checks_relative = checks.relative_to(project).as_posix()
    execution_root = execution_root or project
    execution_checks = checks_relative if execution_root == project else ""
    token = f"{int(time.time() * 1000)}-{name}-{uuid.uuid4().hex[:10]}"
    log_name = f"{token}.log"
    record_name = f"{token}.json"
    log_path = checks / log_name
    record_path = checks / record_name
    environment = dict(os.environ)
    environment_sha256 = devcrew_phase.environment_digest(environment)
    started = _utc_now()
    head_before = _git_head(execution_root)
    tree_before = _git_tree(execution_root)
    before = _fingerprint(execution_root, execution_checks)
    status = "passed"
    exit_code: int | None = None
    error: str | None = None
    timeout_kind = None
    process = None
    with log_path.open("xb") as output:
        previous_term = (
            signal.signal(signal.SIGTERM, _interrupt_check) if os.name == "posix" else None
        )
        try:
            kwargs: dict[str, Any] = {
                "cwd": str(execution_root),
                "stdout": output,
                "stderr": subprocess.STDOUT,
                "env": environment,
            }
            if os.name == "posix":
                kwargs["start_new_session"] = True
                if resource_fd is not None:
                    kwargs["pass_fds"] = (resource_fd,)
            if os.name == "nt":
                import devcrew_winprocess

                process = devcrew_winprocess.spawn(command, resource_fd=resource_fd, **kwargs)
            else:
                process = subprocess.Popen(command, **kwargs)
            print(
                json.dumps(
                    {
                        "event": "check_started",
                        "pid": process.pid,
                        "log": str(log_path),
                        "record": str(record_path),
                    }
                ),
                file=sys.stderr,
                flush=True,
            )
            timeout_kind = _wait_check(process, log_path, timeout, idle_timeout)
            if timeout_kind is None:
                exit_code = process.returncode
                if exit_code != 0:
                    status = "failed"
            else:
                _terminate_process(process)
                exit_code = process.returncode
                status = "timeout"
        except KeyboardInterrupt:
            if process is not None:
                _terminate_process(process)
                exit_code = process.returncode
            status = "interrupted"
        except OSError as exc:
            if process is not None:
                _terminate_process(process)
                exit_code = process.returncode
            status = "error"
            error = str(exc)
            output.write(error.encode("utf-8", "replace"))
        finally:
            if os.name == "nt" and process is not None:
                try:
                    devcrew_winprocess.release(process)
                except OSError as exc:
                    status = "error"
                    error = f"could not verify process tree cleanup: {exc}"
                    output.write(error.encode("utf-8", "replace"))
            if previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)
    after = _fingerprint(execution_root, execution_checks)
    tree_after = _git_tree(execution_root)
    finished = _utc_now()
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "command": command,
        "timeout_seconds": timeout,
        "idle_timeout_seconds": idle_timeout,
        "environment_sha256": environment_sha256,
        "cwd": str(project),
        "host_project_root": str(project),
        "execution_root": str(execution_root),
        "execution_head_sha": _git_head(execution_root),
        "candidate_tree_before": tree_before,
        "candidate_tree_after": tree_after,
        "started_at": started,
        "finished_at": finished,
        "exit_code": exit_code,
        "pid": process.pid if process is not None else None,
        "status": status,
        "git_head": _git_head(execution_root),
        "git_head_before": head_before,
        "worktree_fingerprint_before": before,
        "worktree_fingerprint_after": after,
        "worktree_fingerprint": after,
        "log": log_name,
        "checks_dir": checks_relative,
        "log_sha256": _file_digest(log_path),
    }
    if task_id is not None:
        payload["task_id"] = task_id
    if error:
        payload["error"] = error
    if timeout_kind:
        payload["timeout_kind"] = timeout_kind
    _json_dump(record_path, payload)
    return record_path, payload


def _run_check_command(args: argparse.Namespace) -> int:
    try:
        project = _project(args.project)
        execution_root = _project(args.execution_root) if args.execution_root else project
        if args.task_id is not None and (not args.task_id or "\x00" in args.task_id):
            raise ContractError("task-id must be a non-empty string")
        timeout = float(args.timeout) if args.timeout is not None else DEFAULT_TIMEOUT
        idle_timeout = float(args.idle_timeout) if args.idle_timeout is not None else None
        if bool(args.resource) != bool(args.resource_dir):
            raise ContractError("resource and resource-dir must be supplied together")
        lock = (
            devcrew_resources.ResourceLock(args.resource_dir, args.resource, project)
            if args.resource
            else contextlib.nullcontext()
        )
        with lock:
            fd = lock.handle.fileno() if args.resource else None
            record, payload = _run_check(
                project,
                args.name,
                args.command,
                timeout,
                idle_timeout,
                fd,
                execution_root,
                args.task_id,
            )
    except (ContractError, ValueError, OverflowError) as exc:
        return _json_error(str(exc))
    print(record)
    print(
        json.dumps(
            {"status": payload["status"], "exit_code": payload["exit_code"], "log": payload["log"]}
        )
    )
    return 0 if payload["status"] == "passed" else 1


def _record_path(path_arg: str) -> tuple[pathlib.Path, dict[str, Any], pathlib.Path]:
    record = pathlib.Path(path_arg)
    if not record.exists() or not record.is_file() or fs.is_link(record):
        raise ContractError(f"record does not exist or is not a regular file: {path_arg}")
    try:
        data = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid record JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("record must be a JSON object")
    if not isinstance(data.get("cwd"), str) or not data["cwd"]:
        raise ContractError("record cwd must be a non-empty string")
    project = _project(data["cwd"])
    checks = project / _state_paths(project)["checks"]
    if not _inside(checks, record):
        raise ContractError("record must be inside the project's checks directory")
    return record.resolve(), data, project


def _verified_record(
    path_arg: str,
    *,
    current: bool = True,
    expected_execution_root: str | None = None,
    expected_task_id: str | None = None,
) -> tuple[pathlib.Path, dict[str, Any], pathlib.Path]:
    record, data, project = _record_path(path_arg)
    required = (
        "schema_version",
        "command",
        "cwd",
        "started_at",
        "finished_at",
        "exit_code",
        "status",
        "git_head",
        "worktree_fingerprint_before",
        "worktree_fingerprint_after",
        "worktree_fingerprint",
        "log",
        "log_sha256",
    )
    missing = [key for key in required if key not in data]
    if missing:
        raise ContractError(f"record missing fields: {', '.join(missing)}")
    if (
        isinstance(data["schema_version"], bool)
        or not isinstance(data["schema_version"], int)
        or data["schema_version"] != SCHEMA_VERSION
    ):
        raise ContractError("record schema_version is invalid")
    if (
        data["status"] != "passed"
        or isinstance(data["exit_code"], bool)
        or not isinstance(data["exit_code"], int)
        or data["exit_code"] != 0
    ):
        raise ContractError("record is not a successful check")
    timestamps = []
    for timestamp in (data["started_at"], data["finished_at"]):
        if not isinstance(timestamp, str):
            raise ContractError("record timestamps must be strings")
        try:
            parsed = _dt.datetime.fromisoformat(timestamp)
        except ValueError as exc:
            raise ContractError("record timestamps must be ISO-8601") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ContractError("record timestamps must include timezone offsets")
        timestamps.append(parsed)
    if timestamps[1] < timestamps[0]:
        raise ContractError("record finished_at precedes started_at")
    if data["git_head"] is not None and (
        not isinstance(data["git_head"], str) or not data["git_head"]
    ):
        raise ContractError("record git_head is invalid")
    for field in (
        "worktree_fingerprint_before",
        "worktree_fingerprint_after",
        "worktree_fingerprint",
    ):
        _validate_fingerprint(data[field], f"record {field}")
    if data["worktree_fingerprint"] != data["worktree_fingerprint_after"]:
        raise ContractError("record fingerprint fields disagree")
    execution_value = data.get("execution_root", data["cwd"])
    if not isinstance(execution_value, str) or not execution_value:
        raise ContractError("record execution_root is invalid")
    execution = _project(execution_value)
    if "host_project_root" in data and data["host_project_root"] != str(project):
        raise ContractError("record host project root does not match receipt location")
    if "execution_head_sha" in data and data["execution_head_sha"] != data["git_head"]:
        raise ContractError("record execution head fields disagree")
    if "task_id" in data and (not isinstance(data["task_id"], str) or not data["task_id"]):
        raise ContractError("record task_id is invalid")
    if expected_execution_root is not None and execution != _project(expected_execution_root):
        raise ContractError("record execution root does not match expected execution root")
    if expected_task_id is not None and data.get("task_id") != expected_task_id:
        raise ContractError("record task ID does not match expected task ID")
    if (
        not isinstance(data["command"], list)
        or not data["command"]
        or any(not isinstance(item, str) for item in data["command"])
    ):
        raise ContractError("record command is malformed")
    log_name = data["log"]
    if (
        not isinstance(log_name, str)
        or not log_name
        or fs.unsafe_parts([log_name])
        or pathlib.PurePosixPath(log_name).is_absolute()
        or pathlib.PurePosixPath(log_name).name != log_name
    ):
        raise ContractError("record log name is malformed")
    checks = project / _state_paths(project)["checks"]
    if "checks_dir" in data and data["checks_dir"] != checks.relative_to(project).as_posix():
        raise ContractError("record checks directory differs from project configuration")
    log = checks / data["log"]
    if not _inside(checks, log) or not log.is_file() or fs.is_link(log):
        raise ContractError("record log is missing or escapes checks directory")
    actual_hash = _file_digest(log)
    if actual_hash != data["log_sha256"]:
        raise ContractError("record log digest does not match")
    if (
        "git_head_before" in data
        and data["git_head_before"] is not None
        and (not isinstance(data["git_head_before"], str) or not data["git_head_before"])
    ):
        raise ContractError("record git_head_before is invalid")
    if current:
        if data["git_head"] != _git_head(execution):
            raise ContractError("record Git HEAD is stale")
        checks_relative = checks.relative_to(project).as_posix() if execution == project else ""
        if _fingerprint(execution, checks_relative) != data["worktree_fingerprint_after"]:
            raise ContractError("record worktree fingerprint is stale")
    return record, data, project


def _verify_check(args: argparse.Namespace) -> int:
    try:
        record, data, _ = _verified_record(
            args.record, expected_execution_root=args.execution_root, expected_task_id=args.task_id
        )
        payload: dict[str, Any] = {"ok": True, "record": str(record), "status": "verified"}
        if data["git_head"] is None:
            payload["warning"] = (
                "Git HEAD unavailable; freshness uses file fingerprints without a committed revision"
            )
        print(json.dumps(payload, ensure_ascii=False))
        return 0
    except (ContractError, OSError, ValueError, json.JSONDecodeError) as exc:
        return _json_error(str(exc), code=1)


def _reuse_check(args: argparse.Namespace) -> int:
    try:
        record, data, project = _verified_record(
            args.record, expected_execution_root=args.execution_root, expected_task_id=args.task_id
        )
        execution = (
            _project(args.execution_root)
            if args.execution_root
            else _project(data.get("execution_root", data["cwd"]))
        )
        checks_relative = _state_paths(project)["checks"] if execution == project else ""
        state = _capture_state(execution, checks_relative=checks_relative)
        if not devcrew_cost.reusable_check(data, state, args.command):
            raise ContractError("record command or tree does not match required check")
        print(json.dumps({"reuse": True, "record": str(record), "command": data["command"]}))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        return _json_error(str(exc), code=1)


def _normalize_rel_file(value: Any, *, literal: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ContractError("file paths must be non-empty strings")
    raw = value if literal else value.replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise ContractError(f"file path must be relative: {value}")
    parts: list[str] = []
    for part in raw.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                raise ContractError(f"file path escapes repository: {value}")
            parts.pop()
        else:
            parts.append(part)
    normalized = "/".join(parts)
    if fs.unsafe_parts(parts):
        raise ContractError(f"file path contains a reserved Windows name: {value}")
    if not normalized or (not literal and any(char in normalized for char in "*?")):
        raise ContractError(f"file path must name an explicit file: {value}")
    return normalized


def _load_json_object(path: str, label: str) -> dict[str, Any]:
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ContractError(f"invalid {label}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError(f"{label} must be a JSON object")
    return data


def _validate_plan(
    plan: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, set[str]]]]:
    waves = plan.get("waves")
    if not isinstance(waves, list):
        raise ContractError("plan.waves must be a list")
    seen_wave: set[str] = set()
    seen_slice: set[str] = set()
    ownership: dict[str, dict[str, set[str]]] = {}
    normalized_waves: list[dict[str, Any]] = []
    for wave in waves:
        if (
            not isinstance(wave, dict)
            or not isinstance(wave.get("id"), str)
            or not wave["id"]
            or not isinstance(wave.get("slices"), list)
        ):
            raise ContractError("each plan wave needs a unique string id and slices list")
        wave_id = wave["id"]
        if wave_id in seen_wave:
            raise ContractError(f"duplicate wave id: {wave_id}")
        seen_wave.add(wave_id)
        files_seen: set[str] = set()
        slices: list[dict[str, Any]] = []
        for item in wave["slices"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not item["id"]
                or not isinstance(item.get("files"), list)
            ):
                raise ContractError(f"invalid slice in wave {wave_id}")
            slice_id = item["id"]
            if slice_id in seen_slice:
                raise ContractError(f"duplicate slice id: {slice_id}")
            seen_slice.add(slice_id)
            normalized_list = [_normalize_rel_file(path) for path in item["files"]]
            if len(normalized_list) != len(set(normalized_list)):
                raise ContractError(f"duplicate file ownership in wave {wave_id}: {slice_id}")
            normalized = set(normalized_list)
            collision = files_seen.intersection(normalized)
            if collision:
                raise ContractError(
                    f"duplicate file ownership in wave {wave_id}: {sorted(collision)}"
                )
            files_seen.update(normalized)
            slices.append({"id": slice_id, "files": normalized})
        ownership[wave_id] = {item["id"]: item["files"] for item in slices}
        verification = wave.get("verification")
        if (
            not isinstance(verification, list)
            or not verification
            or any(not isinstance(item, str) or not item.strip() for item in verification)
        ):
            raise ContractError(f"wave {wave_id} requires planned verification")
        try:
            bindings = devcrew_phase.verification_bindings(
                verification, wave.get("verification_bindings")
            )
        except devcrew_phase.PhaseError as exc:
            raise ContractError(str(exc)) from exc
        normalized_waves.append(
            {
                "id": wave_id,
                "slices": slices,
                "verification": verification,
                "verification_bindings": bindings,
            }
        )
    return normalized_waves, ownership


def _validate_results(
    results: dict[str, Any], ownership: dict[str, dict[str, set[str]]]
) -> dict[str, dict[str, dict[str, Any]]]:
    waves = results.get("waves")
    if not isinstance(waves, list):
        raise ContractError("results.waves must be a list")
    output: dict[str, dict[str, dict[str, Any]]] = {}
    for wave in waves:
        if (
            not isinstance(wave, dict)
            or not isinstance(wave.get("id"), str)
            or not isinstance(wave.get("slices"), list)
        ):
            raise ContractError("each result wave needs an id and slices list")
        wave_id = wave["id"]
        if wave_id not in ownership:
            raise ContractError(f"unknown result wave id: {wave_id}")
        if wave_id in output:
            raise ContractError(f"duplicate result wave id: {wave_id}")
        output[wave_id] = {}
        for item in wave["slices"]:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("id"), str)
                or not isinstance(item.get("done"), bool)
                or not isinstance(item.get("problems"), list)
                or any(not isinstance(problem, str) for problem in item["problems"])
            ):
                raise ContractError(f"invalid result slice in wave {wave_id}")
            slice_id = item["id"]
            if slice_id not in ownership[wave_id]:
                raise ContractError(f"unknown result slice id: {slice_id}")
            if slice_id in output[wave_id]:
                raise ContractError(f"duplicate result slice id: {slice_id}")
            output[wave_id][slice_id] = item
    return output


def _wave_gate(args: argparse.Namespace) -> int:
    try:
        project = _project(args.project)
        execution = _project(args.execution_root) if args.execution_root else None
        plan_data = _load_json_object(args.plan, "plan")
        plan, ownership = _validate_plan(plan_data)
        result_data = _load_json_object(args.results, "results")
        results = _validate_results(result_data, ownership)
        evidence = {item["id"]: item.get("checks") for item in result_data["waves"]}
        wave = int(args.wave)
        if wave < 0 or wave > len(plan):
            raise ContractError(f"requested wave must be between 0 and {len(plan)}")
        # Freeze the accepted input before evidence exists; this gate cannot approve replacements.
        identity = {
            "plan": plan_data,
            "execution_root": str(execution or project),
            "task_id": args.task_id,
        }
        key = hashlib.sha256(
            json.dumps(
                {"execution_root": identity["execution_root"], "task_id": args.task_id},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        approval_path = _safe_check_root(project) / f"wave-plan-{key}.json"
        if fs.is_link(approval_path):
            raise ContractError("wave approval must be a regular host file")
        if args.replan_reason is not None and (wave != 0 or not args.replan_reason.strip()):
            raise ContractError(
                "wave replanning request requires wave zero and a nonempty --replan-reason"
            )
        if approval_path.exists():
            approval = _load_json_object(str(approval_path), "accepted wave plan")
            if approval.get("identity") != identity:
                return _json_error(
                    "wave verification plan changed; separate trusted approval is required",
                    code=1,
                    details={
                        "status": "REPLAN_APPROVAL_REQUIRED",
                        "replan_requested": args.replan_reason is not None,
                    },
                )
        else:
            approval = None
        if args.replan_reason is not None:
            return _json_error(
                "replanning requested; wave-gate cannot approve plan changes",
                code=1,
                details={"status": "REPLAN_APPROVAL_REQUIRED", "replan_requested": True},
            )
        if wave == 0 and approval is None:
            if any(item.get("checks") for item in result_data["waves"]):
                raise ContractError("approve the wave plan before executing checks")
            approval = {
                "identity": identity,
                "verification_approved_at": _utc_now(),
                "verification_environment_sha256": devcrew_phase.environment_digest(),
                "replan_reason": args.replan_reason,
            }
            devcrew_phase._atomic_create_json(approval_path, approval)
        if approval is None:
            raise ContractError("wave plan must be approved at wave zero before execution")
        for index, prior in enumerate(plan[:wave]):
            wave_id = prior["id"]
            result_slices = results.get(wave_id, {})
            for expected in prior["slices"]:
                item = result_slices.get(expected["id"])
                if item is None:
                    raise ContractError(f"wave {wave_id} slice {expected['id']} has no result")
                if item["done"] is not True or item["problems"]:
                    raise ContractError(f"wave {wave_id} slice {expected['id']} is unresolved")
            paths = evidence.get(wave_id)
            if (
                not isinstance(paths, list)
                or not paths
                or any(not isinstance(path, str) or not path for path in paths)
            ):
                raise ContractError(
                    f"wave {wave_id} needs nonempty checks from the integrated tree"
                )
            seen: set[pathlib.Path] = set()
            checked_state = None
            matched = set()
            for path in paths:
                candidate = pathlib.Path(path)
                if not candidate.is_absolute():
                    candidate = project / candidate
                record, data, owner = _verified_record(
                    str(candidate),
                    current=index == wave - 1,
                    expected_execution_root=str(execution) if execution else None,
                    expected_task_id=args.task_id,
                )
                if owner != project:
                    raise ContractError(f"wave {wave_id} check belongs to a different project")
                record_execution = _project(data.get("execution_root", data["cwd"]))
                if record_execution != project and execution is None:
                    raise ContractError("split-root wave evidence requires --execution-root")
                if record in seen:
                    raise ContractError(f"wave {wave_id} has duplicate check records")
                seen.add(record)
                if "git_head_before" not in data:
                    raise ContractError(
                        "wave check lacks git_head_before; rerun with the updated helper"
                    )
                if (
                    data["git_head_before"] != data["git_head"]
                    or data["worktree_fingerprint_before"] != data["worktree_fingerprint_after"]
                ):
                    raise ContractError(
                        f"wave {wave_id} tree changed during a check; rerun on the integrated tree"
                    )
                state = (
                    str(record_execution),
                    data.get("task_id"),
                    data["git_head"],
                    data["worktree_fingerprint_after"],
                )
                if checked_state is not None and state != checked_state:
                    raise ContractError(f"wave {wave_id} checks cover different trees")
                checked_state = state
                matched.update(
                    i
                    for i, binding in enumerate(prior["verification_bindings"])
                    if devcrew_phase.match_verification(data, binding, approval)
                )
            if matched != set(range(len(prior["verification_bindings"]))):
                raise ContractError(
                    f"wave {wave_id} lacks successful evidence for required commands and execution parameters"
                )
        print(
            json.dumps(
                {
                    "ok": True,
                    "wave": wave,
                    "waves": len(plan),
                    "status": "complete" if wave == len(plan) else "authorized",
                }
            )
        )
        return 0
    except (ContractError, OSError, ValueError) as exc:
        return _json_error(str(exc), code=1)


def _review_gate(args: argparse.Namespace) -> int:
    try:
        project = _project(args.project)
        execution = _project(args.execution_root) if args.execution_root else project
        report = _load_json_object(args.report, "review report")
        expected = report.get("expected_lenses")
        lenses = report.get("lenses")
        if (
            not isinstance(expected, list)
            or not expected
            or any(not isinstance(item, str) or not item for item in expected)
            or len(set(expected)) != len(expected)
        ):
            raise ContractError("expected_lenses must be a nonempty list of unique strings")
        if not isinstance(lenses, list):
            raise ContractError("lenses must be a list")
        seen: set[str] = set()
        blocking: list[str] = []
        for lens in lenses:
            if (
                not isinstance(lens, dict)
                or not isinstance(lens.get("id"), str)
                or lens["id"] in seen
            ):
                raise ContractError("lenses must contain unique objects with string ids")
            seen.add(lens["id"])
            if lens["id"] not in expected:
                raise ContractError(f"unexpected lens: {lens['id']}")
            if lens.get("status") not in ("complete", "failed") or not isinstance(
                lens.get("findings"), list
            ):
                raise ContractError(f"malformed lens: {lens['id']}")
            if lens["status"] != "complete":
                raise ContractError(f"lens failed: {lens['id']}")
            for finding in lens["findings"]:
                if (
                    not isinstance(finding, dict)
                    or finding.get("severity") not in ("P0", "P1", "P2", "P3")
                    or finding.get("verification") not in ("confirmed", "rejected", "unverified")
                    or not isinstance(finding.get("summary"), str)
                    or not finding["summary"].strip()
                ):
                    raise ContractError(f"malformed finding in lens: {lens['id']}")
                if finding["verification"] in ("confirmed", "unverified") and finding[
                    "severity"
                ] in ("P0", "P1"):
                    blocking.append(
                        f"{lens['id']}: {finding['severity']} {finding['verification']}"
                    )
        if set(expected) != seen:
            missing = sorted(set(expected) - seen)
            raise ContractError(f"missing expected lenses: {', '.join(missing)}")
        if report.get("approved") is not True:
            raise ContractError("report approved must be true")
        if blocking:
            raise ContractError("blocking findings: " + "; ".join(blocking))
        checks_relative = _state_paths(project)["checks"] if execution == project else ""
        _verify_state(report.get("state"), execution, checks_relative=checks_relative)
        print(json.dumps({"ok": True, "status": "approved", "lenses": len(lenses)}))
        return 0
    except (ContractError, OSError, ValueError) as exc:
        return _json_error(str(exc), code=1)


def _flaky_hunt(args: argparse.Namespace) -> int:
    try:
        project = _project(args.project)
        _validate_name(args.name)
        max_runs = int(args.max_runs)
        timeout = float(args.timeout)
        max_seconds = float(args.max_seconds) if args.max_seconds is not None else None
        if max_runs <= 0:
            raise ContractError("max-runs must be positive")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ContractError("timeout must be finite and positive")
        if max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0):
            raise ContractError("max-seconds must be finite and positive")
        if not args.command:
            raise ContractError("command must not be empty")
        started = time.monotonic()
        records: list[str] = []
        counts = {"passed": 0, "failed": 0, "timeout": 0, "error": 0}
        outcome = "not_caught"
        for number in range(1, max_runs + 1):
            remaining = None if max_seconds is None else max_seconds - (time.monotonic() - started)
            if remaining is not None and remaining <= 0:
                break
            run_timeout = timeout if remaining is None else min(timeout, remaining)
            record, payload = _run_check(project, args.name, args.command, run_timeout)
            records.append(str(record))
            counts[payload["status"]] = counts.get(payload["status"], 0) + 1
            if payload["status"] == "failed":
                outcome = "caught"
                break
            if payload["status"] in ("timeout", "error", "interrupted"):
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "status": "environment_error",
                            "run_status": payload["status"],
                            "outcome": "environment_error",
                            "runs": len(records),
                            "counts": counts,
                            "records": records,
                        }
                    )
                )
                return 2
        result = {
            "ok": outcome == "not_caught",
            "status": outcome,
            "runs": len(records),
            "max_runs": max_runs,
            "counts": counts,
            "records": records,
        }
        if max_seconds is not None and len(records) < max_runs and outcome == "not_caught":
            result["limit_reached"] = "max-seconds"
        elif len(records) == max_runs and outcome == "not_caught":
            result["limit_reached"] = "max-runs"
        print(json.dumps(result))
        return 1 if outcome == "caught" else 0
    except (ContractError, ValueError, OverflowError) as exc:
        return _json_error(str(exc))


def _resource_status(args: argparse.Namespace) -> int:
    print(json.dumps(devcrew_resources.status(args.resource_dir, args.name)))
    return 0


def _qa_command(args: argparse.Namespace) -> int:
    project = _project(args.project)
    if getattr(args, "qa_operation", None) in (
        "environment-start",
        "environment-stop",
        "environment-status",
    ):
        return devcrew_qa_environment.command(
            args, project, _state_paths(project), _safe_destination, _preflight_destination
        )
    return devcrew_qa.command(
        args, project, _state_paths(project), _safe_destination, _preflight_destination
    )


def _memory_audit(args: argparse.Namespace) -> int:
    project = _project(args.project)
    result = devcrew_memory.audit(
        project, _state_paths(project)["memory"], args.max_files, args.max_bytes
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _requirements_record(args: argparse.Namespace) -> int:
    project = _project(args.project)
    if len(args.record.encode("utf-8")) > devcrew_requirements.REGISTRY_LIMIT:
        raise ContractError("requirements input exceeds 512 KiB")
    candidate = json.loads(args.record)
    source_root = _requirements_source_root(project, args.task_id, args.execution_root)
    result = devcrew_requirements.record(
        project,
        _state_paths(project)["memory"],
        candidate,
        expected_registry_revision=args.expected_registry_revision,
        source_root=source_root,
    )
    print(json.dumps({"ok": True, **result}, ensure_ascii=False))
    return 0


def _requirements_context(args: argparse.Namespace) -> int:
    project = _project(args.project)
    source_root = _requirements_source_root(project, args.task_id, args.execution_root)
    result = devcrew_requirements.context(
        project,
        _state_paths(project)["memory"],
        args.paths,
        args.role,
        args.task_class,
        source_root=source_root,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _requirements_diagnose(args: argparse.Namespace) -> int:
    project = _project(args.project)
    incident = json.loads(
        devcrew_requirements.read_bounded_file(
            args.incident, 16 * 1024, "requirements incident"
        ).decode("utf-8")
    )
    if not isinstance(incident, dict) or set(incident) - {
        "task_id",
        "route_id",
        "spawn_receipt_id",
        "role",
        "paths",
        "task_class",
        "check_evidence",
        "agent_evidence",
    }:
        raise ContractError("incident must contain bounded task, route, scope and check evidence")
    if not isinstance(incident.get("task_id"), str) or not (
        isinstance(incident.get("route_id"), str)
        or isinstance(incident.get("spawn_receipt_id"), str)
    ):
        raise ContractError("incident must identify a task and issued route or spawn receipt")
    for field, maximum in (("check_evidence", 64), ("agent_evidence", 32)):
        value = incident.get(field, [])
        if (
            not isinstance(value, list)
            or len(value) > maximum
            or any(not isinstance(row, dict) for row in value)
        ):
            raise ContractError(f"incident {field} must be a bounded list of objects")
    delivery = devcrew_dispatch.inspect_issued(
        project,
        incident.get("task_id"),
        route_id=incident.get("route_id"),
        spawn_receipt_id=incident.get("spawn_receipt_id"),
    )
    source_root = _requirements_source_root(project, incident.get("task_id"), None)
    delivered = {row["id"] for row in delivery["delivered_requirements"]}
    try:
        result = devcrew_requirements.context(
            project,
            _state_paths(project)["memory"],
            incident.get("paths", []),
            incident.get("role"),
            incident.get("task_class"),
            source_root=source_root,
        )
        expected = {row["id"] for row in result["requirements"]}
        delivered_revision = delivery.get("registry_revision")
        if delivered_revision is None:
            result["diagnosis"] = "undetermined"
        elif expected - delivered and result["registry_revision"] <= delivered_revision:
            result["diagnosis"] = "missing_delivery"
        elif result["registry_revision"] > delivered_revision:
            result["diagnosis"] = "registry_changed_after_delivery"
        else:
            result["diagnosis"] = "undetermined"
    except ValueError as exc:
        if "stale" not in str(exc).casefold() and "source" not in str(exc).casefold():
            raise
        result = {"diagnosis": "stale_source", "source_evidence": str(exc)}
    result["observed_delivery"] = delivery
    if incident.get("check_evidence"):
        result["check_evidence"] = incident["check_evidence"]
    if incident.get("agent_evidence"):
        result["agent_evidence"] = incident["agent_evidence"]
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _requirements_source_root(
    project: pathlib.Path, task_id: str | None, execution_root: str | None
) -> pathlib.Path:
    """Resolve source evidence against the registered task checkout when supplied."""
    if task_id is None:
        if execution_root is not None and pathlib.Path(execution_root).resolve() != project:
            raise ContractError(
                "an execution root outside the project requires its registered --task-id"
            )
        return project
    binding, _receipt = devcrew_context._dispatch_worktree_binding(project, task_id)
    root = pathlib.Path(binding["task_worktree_root"]).resolve()
    if execution_root is not None and pathlib.Path(execution_root).resolve() != root:
        raise ContractError("--execution-root differs from the registered task checkout")
    return root


def _estimate_summary(args: argparse.Namespace) -> int:
    print(json.dumps(devcrew_memory.summarize(args.records), ensure_ascii=False))
    return 0


def _agent_context_config(project: pathlib.Path) -> dict[str, Any]:
    path = project / STATE_FILE
    if not path.exists():
        return devcrew_context.policy()
    if fs.is_link(path) or not path.is_file():
        raise ContractError(f"state configuration is not a regular file: {STATE_FILE}")
    data = json.loads(path.read_text(encoding="utf-8"))
    _read_state_file(project)
    return devcrew_context.policy(data.get("agent_context"))


def _cost_config(project: pathlib.Path) -> dict[str, str]:
    path = project / STATE_FILE
    if not path.exists():
        return devcrew_cost.policy()
    _read_state_file(project)
    data = json.loads(path.read_text(encoding="utf-8"))
    return devcrew_cost.policy(data.get("cost"))


def _cost_route(args: argparse.Namespace) -> int:
    settings = _cost_config(_project(args.project))
    result = devcrew_cost.route(
        args.role,
        args.risk,
        cost_mode=settings["cost_mode"],
        escalation_reason=args.escalation_reason,
        retry_stage=args.retry_stage,
    )
    print(json.dumps({"cost_mode": settings["cost_mode"], **result}))
    return 0


def _check_summary(args: argparse.Namespace) -> int:
    print(json.dumps(devcrew_cost.check_summary(args.record), ensure_ascii=False))
    return 0


def _json_input(path: str) -> Any:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def _context_evaluate(args: argparse.Namespace) -> int:
    agent = _json_input(args.agent)
    settings = _agent_context_config(_project(args.project))
    result = devcrew_context.evaluate(agent, settings)
    result.update(
        {
            name: agent.get(name)
            for name in (
                "agent_id",
                "role",
                "task_id",
                "model",
                "reasoning_effort",
                "started_at",
                "finished_at",
            )
        }
    )
    result["handoff_count"] = agent.get("handoff_count", 0)
    result["usage"] = devcrew_context.normalize_usage(
        agent.get("usage") if settings["telemetry"] else None
    )
    raw_allowance = agent.get("allowance") if settings["telemetry"] else None
    allowance = (
        devcrew_context.codex_limits_allowance(raw_allowance)
        if isinstance(raw_allowance, dict)
        and ("rateLimits" in raw_allowance or "rateLimitsByLimitId" in raw_allowance)
        else devcrew_context.normalize_allowance(raw_allowance)
    )
    result["allowance"] = allowance
    result["allowance_guardrail"] = devcrew_context.allowance_guardrail(allowance, settings)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _context_handoff(args: argparse.Namespace) -> int:
    result = devcrew_context.prepare_handoff(
        _json_input(args.agent),
        _json_input(args.contract),
        _json_input(args.state),
        args.replacement_id,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _context_report(args: argparse.Namespace) -> int:
    settings = _agent_context_config(_project(args.project))
    if not settings["telemetry"]:
        print(json.dumps({"status": "TELEMETRY_DISABLED"}))
        return 0
    agents = _json_input(args.agents)
    report = (
        devcrew_context.task_report(args.task_id, agents)
        if args.task_id
        else devcrew_context.phase_report(agents)
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _capacity_report(args: argparse.Namespace) -> int:
    result = devcrew_capacity.report(args.project, _json_input(args.report))
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _capacity_resume(args: argparse.Namespace) -> int:
    result = devcrew_capacity.resume(args.project, args.task_id, host_initiated=args.host_initiated)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _executor_delivery(args: argparse.Namespace) -> int:
    result = devcrew_dispatch.prepare_executor_delivery(args.project, args.task_id, args.route_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def _reviewer_delivery(args: argparse.Namespace) -> int:
    result = devcrew_dispatch.prepare_reviewer_delivery(args.project, args.task_id, args.route_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devcrew.py")
    sub = parser.add_subparsers(dest="subcommand", required=True)
    servers = sub.add_parser("servers", help="manage project-local SSH connection profiles")
    servers_sub = servers.add_subparsers(dest="servers_operation", required=True)
    for operation in ("save", "list", "get", "delete"):
        command = servers_sub.add_parser(operation)
        command.add_argument("--project", required=True)
        if operation in ("save", "get", "delete"):
            command.add_argument("--name", required=True)
        if operation == "save":
            command.add_argument("--replace", action="store_true")
        command.set_defaults(function=devcrew_servers.command)
    qa = sub.add_parser("qa", help="run or inspect local QA sessions")
    qa_sub = qa.add_subparsers(dest="qa_operation", required=True)
    qa_run = qa_sub.add_parser("run-api", help="run a versioned local API scenario")
    qa_run.add_argument("--project", required=True)
    qa_run.add_argument("--scenario", required=True)
    qa_run.set_defaults(function=_qa_command)
    qa_list = qa_sub.add_parser("list", help="list saved QA runs")
    qa_list.add_argument("--project", required=True)
    qa_list.set_defaults(function=_qa_command)
    qa_report = qa_sub.add_parser("report", help="read a saved QA report")
    qa_report.add_argument("--project", required=True)
    qa_report.add_argument("--run-id", required=True)
    qa_report.set_defaults(function=_qa_command)
    qa_web_start = qa_sub.add_parser("web-start", help="open a bounded web QA run for a live stand")
    qa_web_start.add_argument("--project", required=True)
    qa_web_start.add_argument("--scenario", required=True)
    qa_web_start.set_defaults(function=_qa_command, qa_web_operation="start")
    qa_web_replay = qa_sub.add_parser(
        "web-replay-start", help="open a fresh attempt linked to a saved web QA run"
    )
    qa_web_replay.add_argument("--project", required=True)
    qa_web_replay.add_argument("--source-run-id", required=True)
    qa_web_replay.add_argument("--environment-run-id", required=True)
    qa_web_replay.add_argument("--base-url", required=True)
    qa_web_replay.add_argument("--scenario")
    qa_web_replay.add_argument("--defect")
    qa_web_replay.add_argument("--isolated", action="store_true")
    qa_web_replay.set_defaults(function=_qa_command, qa_web_operation="replay-start")
    qa_web_find = qa_sub.add_parser("web-find", help="find saved web QA runs read-only")
    qa_web_find.add_argument("--project", required=True)
    qa_web_find.add_argument("--run-id")
    qa_web_find.add_argument("--scenario")
    qa_web_find.add_argument("--defect")
    qa_web_find.add_argument("--latest", action="store_true")
    qa_web_find.set_defaults(function=_qa_command, qa_web_operation="find")
    qa_web_record = qa_sub.add_parser(
        "web-record", help="record browser evidence and verify the backend"
    )
    qa_web_record.add_argument("--project", required=True)
    qa_web_record.add_argument("--run-id", required=True)
    qa_web_record.add_argument("--evidence", required=True)
    qa_web_record.set_defaults(function=_qa_command, qa_web_operation="record")
    qa_web_list = qa_sub.add_parser("web-list", help="list saved web QA runs")
    qa_web_list.add_argument("--project", required=True)
    qa_web_list.set_defaults(function=_qa_command, qa_web_operation="list")
    qa_web_report = qa_sub.add_parser("web-report", help="read a saved web QA run")
    qa_web_report.add_argument("--project", required=True)
    qa_web_report.add_argument("--run-id", required=True)
    qa_web_report.set_defaults(function=_qa_command, qa_web_operation="report")
    for operation, action, help_text in (
        ("environment-start", "start", "start a managed local QA environment"),
        ("environment-stop", "stop", "stop an owned QA environment"),
        ("environment-status", "status", "inspect a managed QA environment"),
    ):
        command = qa_sub.add_parser(operation, help=help_text)
        command.add_argument("--project", required=True)
        if action == "start":
            command.add_argument("--profile", required=True)
        else:
            command.add_argument("--run-id", required=True)
        command.set_defaults(function=_qa_command, qa_environment_operation=action)
    memory = sub.add_parser("memory-audit")
    memory.add_argument("--project", required=True)
    memory.add_argument("--max-files", type=int, default=50)
    memory.add_argument("--max-bytes", type=int, default=512000)
    memory.set_defaults(function=_memory_audit)
    requirements_record = sub.add_parser("requirements-record")
    requirements_record.add_argument("--project", required=True)
    requirements_record.add_argument("--record", required=True)
    requirements_record.add_argument("--expected-registry-revision", type=int, default=0)
    requirements_record.add_argument("--task-id")
    requirements_record.add_argument("--execution-root")
    requirements_record.set_defaults(function=_requirements_record)
    requirements_context = sub.add_parser("requirements-context")
    requirements_context.add_argument("--project", required=True)
    requirements_context.add_argument("--paths", nargs="*", default=[])
    requirements_context.add_argument(
        "--role", required=True, choices=("planner", "executor", "reviewer")
    )
    requirements_context.add_argument("--task-class", required=True)
    requirements_context.add_argument("--task-id")
    requirements_context.add_argument("--execution-root")
    requirements_context.set_defaults(function=_requirements_context)
    requirements_diagnose = sub.add_parser("requirements-diagnose")
    requirements_diagnose.add_argument("--project", required=True)
    requirements_diagnose.add_argument("--incident", required=True)
    requirements_diagnose.set_defaults(function=_requirements_diagnose)
    estimates = sub.add_parser("estimate-summary")
    estimates.add_argument("--records", required=True)
    estimates.set_defaults(function=_estimate_summary)
    context_check = sub.add_parser("context-evaluate")
    context_check.add_argument("--project", required=True)
    context_check.add_argument("--agent", required=True)
    context_check.set_defaults(function=_context_evaluate)
    handoff = sub.add_parser("context-handoff")
    handoff.add_argument("--agent", required=True)
    handoff.add_argument("--contract", required=True)
    handoff.add_argument("--state", required=True)
    handoff.add_argument("--replacement-id", required=True)
    handoff.set_defaults(function=_context_handoff)
    usage_report = sub.add_parser("context-report")
    usage_report.add_argument("--project", required=True)
    usage_report.add_argument("--task-id")
    usage_report.add_argument("--agents", required=True)
    usage_report.set_defaults(function=_context_report)
    capacity_report = sub.add_parser("capacity-report")
    capacity_report.add_argument("--project", required=True)
    capacity_report.add_argument("--report", required=True)
    capacity_report.set_defaults(function=_capacity_report)
    capacity_resume = sub.add_parser("capacity-resume")
    capacity_resume.add_argument("--project", required=True)
    capacity_resume.add_argument("--task-id", required=True)
    capacity_resume.add_argument("--host-initiated", action="store_true")
    capacity_resume.set_defaults(function=_capacity_resume)
    executor_delivery = sub.add_parser("executor-delivery")
    executor_delivery.add_argument("--project", required=True)
    executor_delivery.add_argument("--task-id", required=True)
    executor_delivery.add_argument("--route-id", required=True)
    executor_delivery.set_defaults(function=_executor_delivery)
    reviewer_delivery = sub.add_parser("reviewer-delivery")
    reviewer_delivery.add_argument("--project", required=True)
    reviewer_delivery.add_argument("--task-id", required=True)
    reviewer_delivery.add_argument("--route-id", required=True)
    reviewer_delivery.set_defaults(function=_reviewer_delivery)
    cost = sub.add_parser("cost-route")
    cost.add_argument("--project", required=True)
    cost.add_argument("--role", required=True, choices=("executor", "reviewer", "coordinator"))
    cost.add_argument("--risk", choices=devcrew_cost.RISKS, default="low")
    cost.add_argument("--retry-stage", type=int, default=0)
    cost.add_argument("--escalation-reason", choices=devcrew_cost.ESCALATION_REASONS)
    cost.set_defaults(function=_cost_route)
    summary = sub.add_parser("check-summary")
    summary.add_argument("--record", required=True)
    summary.set_defaults(function=_check_summary)
    init = sub.add_parser("init")
    init.add_argument("--project", required=True)
    init.add_argument(
        "--language",
        choices=("en", "ru"),
        default="en",
        help="language of newly created project documents (default: en)",
    )
    init.add_argument("--context")
    init.add_argument("--tasks-dir")
    init.add_argument("--sessions-dir")
    init.add_argument("--memory-dir")
    init.add_argument("--checks-dir")
    init.set_defaults(function=_init)
    migrate = sub.add_parser("migrate-state")
    migrate.add_argument("--project", required=True)
    migrate.add_argument("--dry-run", action="store_true")
    migrate.set_defaults(function=_migrate_state)
    sync_status = sub.add_parser("sync-status")
    sync_status.add_argument("--project", required=True)
    sync_status.set_defaults(function=_sync_status)
    sync = sub.add_parser("sync")
    sync.add_argument("--project", required=True)
    sync.set_defaults(function=_sync_project)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--project", required=True)
    doctor.set_defaults(function=_doctor)
    check = sub.add_parser("run-check")
    check.add_argument("--project", required=True)
    check.add_argument("--execution-root")
    check.add_argument("--task-id")
    check.add_argument("--name", required=True)
    check.add_argument("--timeout", default=None)
    check.add_argument("--idle-timeout", default=None)
    check.add_argument("--resource")
    check.add_argument("--resource-dir")
    check.add_argument("command", nargs=argparse.REMAINDER)
    check.set_defaults(function=_run_check_command)
    resource = sub.add_parser("resource-status")
    resource.add_argument("--resource-dir", required=True)
    resource.add_argument("--name", required=True)
    resource.set_defaults(function=_resource_status)
    verify = sub.add_parser("verify-check")
    verify.add_argument("--record", required=True)
    verify.add_argument("--execution-root")
    verify.add_argument("--task-id")
    verify.set_defaults(function=_verify_check)
    reuse = sub.add_parser("reuse-check")
    reuse.add_argument("--record", required=True)
    reuse.add_argument("--execution-root")
    reuse.add_argument("--task-id")
    reuse.add_argument("command", nargs=argparse.REMAINDER)
    reuse.set_defaults(function=_reuse_check)
    capture = sub.add_parser("capture-state")
    capture.add_argument("--project", required=True)
    capture.add_argument("--execution-root")
    capture.add_argument("--include-files", action="store_true")
    capture.set_defaults(function=_capture_state_command)
    scope = sub.add_parser("scope-check")
    scope.add_argument("--project", required=True)
    scope.add_argument("--execution-root")
    scope.add_argument("--before", required=True)
    scope.add_argument("--files", required=True)
    scope.set_defaults(function=_scope_check)
    wave = sub.add_parser("wave-gate")
    wave.add_argument("--project", required=True)
    wave.add_argument("--execution-root")
    wave.add_argument("--task-id")
    wave.add_argument("--plan", required=True)
    wave.add_argument("--results", required=True)
    wave.add_argument("--wave", required=True)
    wave.add_argument("--replan-reason")
    wave.set_defaults(function=_wave_gate)
    review = sub.add_parser("review-gate")
    review.add_argument("--project", required=True)
    review.add_argument("--execution-root")
    review.add_argument("--report", required=True)
    review.set_defaults(function=_review_gate)
    flaky = sub.add_parser("flaky-hunt")
    flaky.add_argument("--project", required=True)
    flaky.add_argument("--name", required=True)
    flaky.add_argument("--max-runs", required=True)
    flaky.add_argument("--timeout", required=True)
    flaky.add_argument("--max-seconds", default=None)
    flaky.add_argument("command", nargs=argparse.REMAINDER)
    flaky.set_defaults(function=_flaky_hunt)
    worktree = sub.add_parser("task-checkout", aliases=["task-worktree"])
    worktree.add_argument("operation", choices=("create", "resolve"))
    worktree.add_argument("--project", required=True)
    worktree.add_argument("--task-id", required=True)
    worktree.add_argument("--base-sha")
    worktree.add_argument(
        "--worktree-request", help="Actual explicit user request for parallel work in a worktree"
    )
    worktree.set_defaults(function=_task_worktree)
    phase = sub.add_parser("phase")
    phase.add_argument(
        "operation",
        choices=(
            "init",
            "resume",
            "source-bind",
            "dispatch",
            "transition",
            "candidate-commit",
            "pre-candidate-reapprove",
            "review-start",
            "review-reconsider",
            "local-fix-start",
            "replan-complete",
            "review-report",
            "accept",
            "advance",
        ),
    )
    phase.add_argument("--project", required=True)
    phase.add_argument("--task-id", required=True)
    phase.add_argument("--planner-output")
    phase.add_argument("--source-spec")
    phase.add_argument(
        "--ordinary-source-gap",
        help="JSON contract binding an exact attachment to an existing ordinary task",
    )
    phase.add_argument(
        "--intent-assessment",
        help="Separate host-authored original-intent planning assessment JSON",
    )
    phase.add_argument("--spawn-receipt-id")
    phase.add_argument("--phase-id")
    phase.add_argument(
        "--owner-decision",
        help="Actual owner answer resolving a requirement, authorization or additional-cost decision",
    )
    phase.add_argument(
        "--continuation",
        help="JSON assessment with exact failed candidate/report, verified evidence and bounded next action",
    )
    phase.add_argument("--status", choices=tuple(sorted(devcrew_phase.STATUSES)))
    phase.add_argument("--evidence-record", action="append", default=[])
    phase.add_argument("--verification-bindings")
    phase.add_argument("--report")
    phase.add_argument(
        "--environment-only",
        action="store_true",
        help="explicitly approve only the current verification environment after archived local-fix history",
    )
    phase.add_argument(
        "--approve-environment-sha256",
        help="current environment fingerprint approved for environment-only reapproval",
    )
    phase.set_defaults(function=_phase_command)
    ordinary = sub.add_parser(
        "ordinary", help="ordinary current-chat task lifecycle without a candidate commit"
    )
    ordinary.add_argument(
        "operation",
        choices=(
            "init",
            "attach-routing",
            "dispatch",
            "issue-dispatch",
            "spawn-report",
            "plan-report",
            "plan-accept",
            "candidate",
            "review-start",
            "review-report",
            "accept",
            "continue",
            "executor-followup",
            "followup-report",
            "resume",
        ),
    )
    ordinary.add_argument("--project", required=True)
    ordinary.add_argument("--task-id", required=True)
    ordinary.add_argument("--chat-context")
    ordinary.add_argument("--role", choices=("planner", "executor", "reviewer"))
    ordinary.add_argument("--runtime")
    ordinary.add_argument("--role-contract")
    ordinary.add_argument("--requirements-scope", help="JSON file containing bounded planner paths")
    ordinary.add_argument("--executor-receipt-id")
    ordinary.add_argument("--plan-sha256")
    ordinary.add_argument("--evidence-record", action="append", default=[])
    ordinary.add_argument("--report")
    ordinary.add_argument("--routing-assessment")
    ordinary.set_defaults(function=_ordinary_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if hasattr(args, "command") and args.command and args.command[0] == "--":
        args.command = args.command[1:]
    try:
        return args.function(args)
    except (ContractError, OSError, ValueError) as exc:
        return _json_error(str(exc))


if __name__ == "__main__":
    if os.name == "nt":
        # Redirected CLI JSON is UTF-8 regardless of the system console code page.
        for stream in (sys.stdin, sys.stdout, sys.stderr):
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
