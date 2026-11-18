#!/usr/bin/env python3
"""Run unittest suites serially or in class-sized worker processes."""

from __future__ import annotations

import argparse
import io
import inspect
import multiprocessing
from multiprocessing.connection import wait
import os
import signal
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import unittest
from collections import OrderedDict
from contextlib import contextmanager, nullcontext
from pathlib import Path


_WORKER_POLL_SECONDS = 0.1
_PROCESS_SCAN_SECONDS = 0.5
_INT_GRACE_SECONDS = 1.5
_TERM_GRACE_SECONDS = 1.0
_WORKER_EXIT_GRACE_SECONDS = 1.0


def _flatten(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _flatten(item)
        else:
            yield item


def _test_id(test):
    try:
        return test.id()
    except Exception:
        return f"{type(test).__module__}.{type(test).__qualname__}"


def _class_key(test):
    cls = type(test)
    return cls.__module__, cls.__qualname__, cls


class _RecordingLoader(unittest.TestLoader):
    def __init__(self):
        super().__init__()
        self.module_hooks = {}
        self.origins = {}
        self.custom_suites = []

    def loadTestsFromModule(self, module, *, pattern=None):
        name = getattr(module, "__name__", "")
        hooks = tuple(
            hook
            for hook in ("setUpModule", "tearDownModule", "load_tests")
            if callable(getattr(module, hook, None))
        )
        suite = super().loadTestsFromModule(module, pattern=pattern)
        self.module_hooks[name] = hooks
        if type(suite) is not unittest.TestSuite:
            self.custom_suites.append((name, type(suite).__qualname__))
        for case in _flatten(suite):
            if isinstance(case, unittest.TestCase):
                self.origins.setdefault(id(case), set()).add(name)
        return suite


def _load_named_tests(loader, selectors):
    """Use unittest's canonical selector loader without discovering unrelated modules."""
    suite = unittest.TestSuite()
    for selector in selectors:
        suite.addTest(loader.loadTestsFromNames([selector]))
    return suite


def _suite_error_tests(loader, suite):
    return [test for test in _flatten(suite) if type(test).__name__ == "_FailedTest"]


def _print_discovery_errors(loader):
    for index, error in enumerate(loader.errors, 1):
        print(f"Discovery error {index}:\n{error}", file=sys.stderr)


def _parallel_blockers(selected, loader, discovered_suite):
    reasons = []
    if os.name not in ("posix", "nt"):
        reasons.append("parallel execution requires POSIX process-group support")
    try:
        if os.name == "posix":
            _ps_table()
    except (OSError, subprocess.CalledProcessError) as exc:
        reasons.append(f"parallel execution cannot inspect process trees with ps: {exc}")
    if loader.errors:
        reasons.append("test import/selection errors were recorded")
    failed = _suite_error_tests(loader, discovered_suite)
    if failed:
        reasons.append("selection contains failed imports or load_tests errors")
    relevant_modules = set()
    selected_objects = set()
    for test in selected:
        if not isinstance(test, unittest.TestCase):
            reasons.append(f"non-TestCase result is unsupported in parallel mode: {_test_id(test)}")
            continue
        cls = type(test)
        selected_objects.add(id(test))
        module_name = cls.__module__
        relevant_modules.add(module_name)
        relevant_modules.update(loader.origins.get(id(test), ()))
        if "<locals>" in cls.__qualname__:
            reasons.append(f"non-importable test class is unsupported: {_test_id(test)}")
    for module_name in sorted(relevant_modules):
        hooks = loader.module_hooks.get(module_name, ())
        if hooks:
            reasons.append(
                f"module hooks are unsupported in parallel mode ({', '.join(hooks)}): {module_name}"
            )
        module = sys.modules.get(module_name)
        if module is not None:
            for attr in ("setUpModule", "tearDownModule", "load_tests"):
                if callable(getattr(module, attr, None)) and attr not in hooks:
                    reasons.append(
                        f"module hook is unsupported in parallel mode ({attr}): {module_name}"
                    )
    for suite_name, suite_type in loader.custom_suites:
        if suite_name in relevant_modules:
            reasons.append(f"custom suite returned by {suite_name} is unsupported: {suite_type}")
    for suite in _walk_suites(discovered_suite):
        if type(suite) is not unittest.TestSuite and any(
            id(case) in selected_objects for case in _flatten(suite)
        ):
            reasons.append(
                f"custom suite type is unsupported: {type(suite).__module__}.{type(suite).__qualname__}"
            )
    for callback, _args, _kwargs in getattr(unittest.case, "_module_cleanups", ()):
        callback_module = getattr(callback, "__module__", None)
        if callback_module in relevant_modules:
            reasons.append(
                f"registered module cleanup is unsupported in parallel mode: {callback_module}"
            )
    return list(dict.fromkeys(reasons))


def _walk_suites(suite):
    if isinstance(suite, unittest.TestSuite):
        yield suite
        for item in suite:
            if isinstance(item, unittest.TestSuite):
                yield from _walk_suites(item)


def _build_groups(selected):
    groups = OrderedDict()
    class_objects = {}
    for test in selected:
        if not isinstance(test, unittest.TestCase):
            continue
        module, qualname, cls = _class_key(test)
        if "<locals>" in qualname or not module:
            raise ValueError(f"test is not importable by ID: {_test_id(test)}")
        key = (module, qualname)
        previous = class_objects.setdefault(key, cls)
        if previous is not cls:
            raise ValueError(f"multiple runtime classes share import path {module}.{qualname}")
        groups.setdefault(key, []).append(_test_id(test))
    plan = []
    for (module, qualname), test_ids in groups.items():
        plan.append(
            {
                "module": module,
                "qualname": qualname,
                "test_ids": test_ids,
                "label": f"{module}.{qualname}",
            }
        )
    return plan


def _result_entry(test, detail=None):
    return {"id": _test_id(test), "detail": detail}


@contextmanager
def _register_detached_children(connection):
    """Let the parent observe detached children before tests can crash or exit."""
    original = subprocess.Popen.__init__
    signature = inspect.signature(original)
    worker_pid = os.getpid()
    lock = threading.Lock()

    def registered_init(process, *args, **kwargs):
        original(process, *args, **kwargs)
        if os.getpid() != worker_pid:
            return
        options = signature.bind_partial(process, *args, **kwargs).arguments
        if options.get("start_new_session") or options.get("process_group") not in (None, -1):
            with lock:
                connection.send({"event": "detached_child", "pid": process.pid})
                connection.recv()  # Parent snapshots ownership before the test proceeds.

    subprocess.Popen.__init__ = registered_init
    try:
        yield
    finally:
        subprocess.Popen.__init__ = original


def _worker_main(connection, group, start_dir, verbosity):
    """Reload and run one complete class group in a fresh process."""
    payload = None
    try:
        if os.name == "nt":
            # The parent assigns this worker to a kill-on-close job before any
            # test or user module may start a descendant.
            if connection.recv() != "job_ready":
                raise RuntimeError("test worker job was not established")
        else:
            os.setsid()
        sys.path.insert(0, str(Path.cwd().resolve()))
        sys.path.insert(0, str(Path(start_dir).resolve()))
        loader = unittest.TestLoader()
        tests = []
        for test_id in group["test_ids"]:
            loaded = list(_flatten(loader.loadTestsFromName(test_id)))
            if len(loaded) != 1 or not isinstance(loaded[0], unittest.TestCase):
                raise RuntimeError(f"worker could not reload exactly one testcase for {test_id}")
            test = loaded[0]
            expected_class = f"{group['module']}.{group['qualname']}"
            actual_class = f"{type(test).__module__}.{type(test).__qualname__}"
            if actual_class != expected_class or _test_id(test) != test_id:
                raise RuntimeError(
                    f"worker reloaded a different test: expected {test_id}, got {_test_id(test)}"
                )
            tests.append(test)
        suite = unittest.TestSuite(tests)
        report = io.StringIO()
        captured_stdout = tempfile.TemporaryFile(mode="w+b")
        captured_stderr = tempfile.TemporaryFile(mode="w+b")
        saved_stdout, saved_stderr = os.dup(1), os.dup(2)
        try:
            os.dup2(captured_stdout.fileno(), 1)
            os.dup2(captured_stderr.fileno(), 2)
            runner = unittest.TextTestRunner(stream=report, verbosity=verbosity)
            with nullcontext() if os.name == "nt" else _register_detached_children(connection):
                result = runner.run(suite)
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            os.dup2(saved_stdout, 1)
            os.dup2(saved_stderr, 2)
            os.close(saved_stdout)
            os.close(saved_stderr)
        captured_stdout.seek(0)
        captured_stderr.seek(0)
        process_stdout = captured_stdout.read().decode("utf-8", errors="replace")
        process_stderr = captured_stderr.read().decode("utf-8", errors="replace")
        captured_stdout.close()
        captured_stderr.close()
        payload = {
            "ok": True,
            "ids": [_test_id(test) for test in tests],
            "tests_run": result.testsRun,
            "failures": [_result_entry(test, detail) for test, detail in result.failures],
            "errors": [_result_entry(test, detail) for test, detail in result.errors],
            "skipped": [_result_entry(test, reason) for test, reason in result.skipped],
            "expected_failures": [
                _result_entry(test, detail) for test, detail in result.expectedFailures
            ],
            "unexpected_successes": [_result_entry(test) for test in result.unexpectedSuccesses],
            "output": report.getvalue(),
            "process_stdout": process_stdout,
            "process_stderr": process_stderr,
            "successful": result.wasSuccessful(),
        }
    except BaseException as exc:
        payload = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }
    try:
        if payload is not None:
            connection.send(payload)
            if connection.poll(5):
                connection.recv()  # Parent snapshots and cleans the worker tree before exit.
    except (EOFError, OSError, KeyboardInterrupt):
        pass
    finally:
        connection.close()


def _ps_table():
    result = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,pgid=,stat=,lstart=,command="],
        capture_output=True,
        text=True,
        check=True,
    )
    table = {}
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 9)
        if len(parts) < 10:
            continue
        try:
            pid, ppid, pgid = map(int, parts[:3])
        except ValueError:
            continue
        table[pid] = {
            "ppid": ppid,
            "pgid": pgid,
            "state": parts[3],
            "identity": (" ".join(parts[4:9]), parts[9]),
        }
    return table


def _descendants(table, root_pid):
    selected = {root_pid} if root_pid in table else set()
    changed = True
    while changed:
        changed = False
        for pid, item in table.items():
            if pid not in selected and item["ppid"] in selected:
                selected.add(pid)
                changed = True
    return selected


def _snapshot_worker_tree(pid, history=None, *, registered_pid=None):
    """Capture descendants before stopping; retain prior ownership for crashes."""
    owned = dict(history or {})
    try:
        table = _ps_table()
    except (OSError, subprocess.CalledProcessError) as exc:
        return owned, f"cannot inspect worker process tree with ps: {exc}"
    for child_pid in _descendants(table, pid):
        owned[child_pid] = table[child_pid]["identity"]
    if registered_pid is not None:
        # The worker's pipe identifies a child even if its parent has already
        # died and the child started a separate session before this snapshot.
        for child_pid in _descendants(table, registered_pid):
            owned[child_pid] = table[child_pid]["identity"]
    # setsid gives each worker its own group. Children in that group remain
    # attributable even if a fast crash reparents them before the next scan.
    for child_pid, item in table.items():
        if item["pgid"] == pid:
            owned[child_pid] = item["identity"]
    return owned, None


def _signal_owned(owned, sig):
    try:
        current = _ps_table()
    except (OSError, subprocess.CalledProcessError):
        current = {}
    for pid, identity in sorted(owned.items(), reverse=True):
        item = current.get(pid)
        if item is None or item["identity"] != identity:
            continue
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:
            continue


def _owned_alive(owned):
    try:
        current = _ps_table()
    except (OSError, subprocess.CalledProcessError):
        return dict(owned)
    return {
        pid: identity
        for pid, identity in owned.items()
        if pid in current
        and current[pid]["identity"] == identity
        and not current[pid]["state"].startswith("Z")
    }


def _terminate_owned(owned):
    """Stop a previously snapshotted set of owned processes with bounded escalation."""
    if not owned:
        return {}
    _signal_owned(owned, signal.SIGINT)
    deadline = time.monotonic() + _INT_GRACE_SECONDS
    alive = _owned_alive(owned)
    while alive and time.monotonic() < deadline:
        time.sleep(0.05)
        alive = _owned_alive(owned)
    if alive:
        _signal_owned(alive, signal.SIGTERM)
        deadline = time.monotonic() + _TERM_GRACE_SECONDS
        while alive and time.monotonic() < deadline:
            time.sleep(0.05)
            alive = _owned_alive(owned)
    if alive:
        _signal_owned(alive, signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while alive and time.monotonic() < deadline:
            time.sleep(0.05)
            alive = _owned_alive(owned)
    return alive


def _stop_workers(active, histories):
    warnings = []
    owned_by_pid = {}
    for process, (_connection, _group, _index) in active.items():
        owned, warning = _snapshot_worker_tree(process.pid, histories.get(process.pid))
        owned_by_pid[process.pid] = owned
        if warning:
            warnings.append(warning)
    for owned in owned_by_pid.values():
        _signal_owned(owned, signal.SIGINT)
    deadline = time.monotonic() + _INT_GRACE_SECONDS
    while time.monotonic() < deadline and any(p.is_alive() for p in active):
        time.sleep(0.05)
    for owned in owned_by_pid.values():
        _signal_owned(owned, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_SECONDS
    while time.monotonic() < deadline and any(p.is_alive() for p in active):
        time.sleep(0.05)
    for owned in owned_by_pid.values():
        _signal_owned(owned, signal.SIGKILL)
    for process, (connection, _group, _index) in active.items():
        process.join(timeout=2)
        if process.is_alive():
            # This handle refers to the worker started by this runner; use it
            # only for that worker after the descendant snapshot/escalation.
            process.kill()
            process.join(timeout=2)
        connection.close()
    return warnings


def _finish_posix_worker(process, connection, label, payload, histories, infra_errors):
    # A worker waits for this acknowledgement after sending its result,
    # keeping the parent process tree available for a final snapshot.
    owned, warning = _snapshot_worker_tree(process.pid, histories.get(process.pid))
    if warning:
        infra_errors.append(warning)
    descendants = {
        pid: identity
        for pid, identity in owned.items()
        if pid != process.pid and "multiprocessing.resource_tracker" not in identity[1]
    }
    if _owned_alive(descendants):
        alive = _terminate_owned(descendants)
        if alive:
            infra_errors.append(f"worker for {label} left live descendants after forced cleanup")
        else:
            infra_errors.append(
                f"worker for {label} returned while child processes were still live; "
                "owned descendants were stopped"
            )
    if process.is_alive() and payload is not None:
        try:
            connection.send(True)
        except (BrokenPipeError, EOFError, OSError):
            pass
    process.join(timeout=_WORKER_EXIT_GRACE_SECONDS)
    if process.is_alive():
        infra_errors.append(
            f"worker for {label} did not exit after its result acknowledgement; "
            "forced cleanup was required"
        )
        worker_tree, warning = _snapshot_worker_tree(process.pid, histories.get(process.pid))
        if warning:
            infra_errors.append(warning)
        alive = _terminate_owned(worker_tree)
        if alive:
            infra_errors.append(f"worker for {label} remained alive after forced cleanup")
        process.kill()
        process.join(timeout=1)


def _finish_windows_worker(process, connection, label, payload, job, infra_errors):
    import devcrew_winprocess as winprocess

    group, handle = job
    try:
        if set(winprocess.members(group)) - {process.pid}:
            infra_errors.append(
                f"worker for {label} returned while child processes were still live; "
                "owned descendants were stopped"
            )
        if process.is_alive() and payload is not None:
            try:
                connection.send(True)
            except (BrokenPipeError, EOFError, OSError):
                pass
        process.join(timeout=_WORKER_EXIT_GRACE_SECONDS)
        if process.is_alive():
            infra_errors.append(f"worker for {label} did not exit after its result acknowledgement")
        winprocess.terminate(group)
    finally:
        winprocess.close(handle)  # Also kills a descendant created during shutdown.
        process.join(timeout=2)


def _parallel_run(groups, jobs, start_dir, verbosity):
    context = multiprocessing.get_context("spawn")
    pending = list(enumerate(groups))
    active = {}
    histories = {}
    windows_jobs = {}
    outputs = {}
    infra_errors = []
    last_scan = 0.0

    def launch():
        while pending and len(active) < jobs:
            index, group = pending.pop(0)
            parent_conn, child_conn = context.Pipe(duplex=True)
            process = context.Process(
                target=_worker_main,
                args=(child_conn, group, str(start_dir), verbosity),
                name=f"unittest-{group['label']}",
            )
            try:
                process.start()
                if os.name == "nt":
                    import devcrew_winprocess as winprocess

                    windows_jobs[process] = winprocess.attach(process.pid)
                    parent_conn.send("job_ready")
            except KeyboardInterrupt:
                if process.pid is not None:
                    child_conn.close()
                    active[process] = (parent_conn, group, index)
                    histories[process.pid] = {}
                else:
                    parent_conn.close()
                    child_conn.close()
                raise
            except Exception as exc:
                if process.pid is not None:
                    if process in windows_jobs:
                        winprocess.close(windows_jobs.pop(process)[1])
                    process.kill()
                    process.join(timeout=2)
                parent_conn.close()
                child_conn.close()
                infra_errors.append(f"cannot start worker for {group['label']}: {exc}")
                outputs[index] = {"group": group, "payload": None}
                continue
            child_conn.close()
            active[process] = (parent_conn, group, index)
            histories[process.pid] = {}

    try:
        launch()
        while active:
            now = time.monotonic()
            if os.name != "nt" and now - last_scan >= _PROCESS_SCAN_SECONDS:
                for process in list(active):
                    histories[process.pid], warning = _snapshot_worker_tree(
                        process.pid, histories.get(process.pid)
                    )
                    if warning:
                        infra_errors.append(warning)
                last_scan = now
            ready = wait([item[0] for item in active.values()], timeout=_WORKER_POLL_SECONDS)
            ready_set = set(ready)
            finished = []
            for process, (connection, group, index) in list(active.items()):
                if connection in ready_set:
                    try:
                        payload = connection.recv()
                    except EOFError:
                        payload = None
                    if isinstance(payload, dict) and payload.get("event") == "detached_child":
                        histories[process.pid], warning = _snapshot_worker_tree(
                            process.pid,
                            histories.get(process.pid),
                            registered_pid=payload.get("pid"),
                        )
                        if warning:
                            infra_errors.append(warning)
                        try:
                            connection.send(True)
                        except (BrokenPipeError, EOFError, OSError):
                            pass  # Normal dead-worker handling still owns cleanup.
                        continue
                    finished.append((process, connection, group, index, payload))
                elif not process.is_alive():
                    if connection.poll():
                        try:
                            payload = connection.recv()
                        except EOFError:
                            payload = None
                    else:
                        payload = None
                    finished.append((process, connection, group, index, payload))
            for process, connection, group, index, payload in finished:
                if os.name == "nt":
                    _finish_windows_worker(
                        process,
                        connection,
                        group["label"],
                        payload,
                        windows_jobs.pop(process),
                        infra_errors,
                    )
                else:
                    _finish_posix_worker(
                        process, connection, group["label"], payload, histories, infra_errors
                    )
                connection.close()
                active.pop(process, None)
                if payload is None:
                    infra_errors.append(
                        f"worker for {group['label']} exited without a result (exit code {process.exitcode})"
                    )
                    if os.name != "nt":
                        owned, warning = _snapshot_worker_tree(
                            process.pid, histories.get(process.pid)
                        )
                        if warning:
                            infra_errors.append(warning)
                        _signal_owned(owned, signal.SIGTERM)
                        time.sleep(0.05)
                        _signal_owned(owned, signal.SIGKILL)
                    outputs[index] = {"group": group, "payload": None}
                elif not payload.get("ok"):
                    infra_errors.append(
                        f"worker for {group['label']} failed protocol/reload validation: {payload.get('error')}\n"
                        f"{payload.get('traceback', '')}"
                    )
                    outputs[index] = {"group": group, "payload": None}
                elif payload.get("ids") != group["test_ids"] or payload.get("tests_run", 0) > len(
                    group["test_ids"]
                ):
                    infra_errors.append(
                        f"worker for {group['label']} returned a mismatched ID/count result"
                    )
                    outputs[index] = {"group": group, "payload": None}
                else:
                    outputs[index] = {"group": group, "payload": payload}
            launch()
    except KeyboardInterrupt:
        previous_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            if os.name == "nt":
                warnings = []
                for process, (connection, _, _) in active.items():
                    if process in windows_jobs:
                        import devcrew_winprocess as winprocess

                        winprocess.close(windows_jobs.pop(process)[1])
                    elif process.is_alive():
                        process.kill()  # A just-started worker still at its gate.
                    process.join(timeout=2)
                    connection.close()
            else:
                warnings = _stop_workers(active, histories)
        finally:
            signal.signal(signal.SIGINT, previous_sigint)
        for warning in warnings:
            print(f"runner cleanup warning: {warning}", file=sys.stderr)
        print("Interrupted; stopped dispatch and cleaned up active workers.", file=sys.stderr)
        return None, 130
    finally:
        for _, handle in windows_jobs.values():
            import devcrew_winprocess as winprocess

            winprocess.close(handle)

    total = {
        name: 0
        for name in (
            "tests_run",
            "failures",
            "errors",
            "skipped",
            "expected_failures",
            "unexpected_successes",
        )
    }
    for index in range(len(groups)):
        item = outputs.get(index, {"group": groups[index], "payload": None})
        payload = item["payload"]
        if payload is None:
            continue
        if payload.get("process_stdout"):
            sys.stdout.write(payload["process_stdout"])
        if payload.get("process_stderr"):
            sys.stderr.write(payload["process_stderr"])
        sys.stdout.write(payload.get("output", ""))
        total["tests_run"] += payload["tests_run"]
        for name in ("failures", "errors", "skipped", "expected_failures", "unexpected_successes"):
            total[name] += len(payload[name])
    return (total, infra_errors), 0


def _print_parallel_summary(total, infra_errors):
    print(f"Ran {total['tests_run']} tests across class workers.")
    fragments = []
    for key, label in (
        ("failures", "failures"),
        ("errors", "errors"),
        ("skipped", "skipped"),
        ("expected_failures", "expected failures"),
        ("unexpected_successes", "unexpected successes"),
    ):
        if total[key]:
            fragments.append(f"{label}={total[key]}")
    if fragments:
        print("RESULT (" + ", ".join(fragments) + ")")
    else:
        print("OK")
    for error in infra_errors:
        print(f"RUNNER ERROR: {error}", file=sys.stderr)


def _parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-j", "--jobs", type=int, default=4, metavar="JOBS")
    parser.add_argument("-s", "--start-directory", default="tests", metavar="DIR")
    parser.add_argument("-p", "--pattern", default="test_*.py", metavar="PATTERN")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--list", action="store_true", dest="list_tests")
    parser.add_argument("selectors", nargs="*", metavar="TEST")
    args = parser.parse_args(argv)
    if args.jobs < 1:
        parser.error("--jobs must be a positive integer")
    return args


def main(argv=None):
    args = _parse_args(argv)
    start_dir = Path(args.start_directory).resolve()
    if not start_dir.is_dir():
        print(f"test start directory does not exist: {start_dir}", file=sys.stderr)
        return 2
    project_root = Path.cwd().resolve()
    if str(start_dir) not in sys.path:
        sys.path.insert(0, str(start_dir))
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    loader = _RecordingLoader()
    try:
        suite = (
            _load_named_tests(loader, args.selectors)
            if args.selectors
            else loader.discover(str(start_dir), pattern=args.pattern)
        )
        selected = list(_flatten(suite))
    except (ImportError, OSError, ValueError) as exc:
        print(f"test discovery/selection error: {exc}", file=sys.stderr)
        _print_discovery_errors(loader)
        return 2
    if not selected:
        print("no tests selected", file=sys.stderr)
        _print_discovery_errors(loader)
        return 2
    if args.list_tests:
        for test in selected:
            print(_test_id(test))
        print(f"{len(selected)} test occurrence(s)")
        _print_discovery_errors(loader)
        return 2 if loader.errors else 0

    verbosity = 2 if args.verbose else 1
    if args.jobs == 1:
        runner = unittest.TextTestRunner(verbosity=verbosity)
        result = runner.run(suite)
        return 0 if result.wasSuccessful() else 1

    blockers = _parallel_blockers(selected, loader, suite)
    if blockers:
        print("parallel execution is unsupported for this selection:", file=sys.stderr)
        for reason in blockers:
            print(f"  - {reason}", file=sys.stderr)
        _print_discovery_errors(loader)
        print("Use --jobs 1 to run with standard unittest lifecycle semantics.", file=sys.stderr)
        return 2
    try:
        groups = _build_groups(selected)
    except ValueError as exc:
        print(f"parallel test plan error: {exc}", file=sys.stderr)
        print("Use --jobs 1 to run with standard unittest lifecycle semantics.", file=sys.stderr)
        return 2
    result, code = _parallel_run(groups, args.jobs, start_dir, verbosity)
    if result is None:
        return code
    total, infra_errors = result
    _print_parallel_summary(total, infra_errors)
    successful = (
        not infra_errors
        and not total["failures"]
        and not total["errors"]
        and not total["unexpected_successes"]
    )
    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
