"""Owned process trees on native Windows using Win32 Job Objects.

A job identifier contains the leader's kernel creation time and PID. Reopening
that name in another CLI process binds cleanup to the original job, even if a
PID is reused. Jobs do not permit child breakaway. Persistent QA jobs survive
the creating CLI; transient check/worker jobs die with the owning handle.
"""

import ctypes
import msvcrt
import os
import socket
import subprocess
import time
import weakref
from contextlib import ExitStack, contextmanager
from ctypes import wintypes as w

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
iphelper = ctypes.WinDLL("iphlpapi", use_last_error=True)
P = ctypes.c_void_p
DWORD = w.DWORD
SIZE = ctypes.c_size_t
FILETIME = ctypes.c_ulonglong
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x100000
PROCESS_DUP_HANDLE = 0x0040
PROCESS_SET_QUOTA = 0x100
PROCESS_TERMINATE = 0x0001
JOB_OBJECT_QUERY = 0x0004
JOB_OBJECT_TERMINATE = 0x0008
ERROR_FILE_NOT_FOUND = 2
ERROR_INVALID_PARAMETER = 87
ERROR_INSUFFICIENT_BUFFER = 122
ERROR_MORE_DATA = 234
WAIT_TIMEOUT = 258


def _bind(lib, name, result, *args):
    func = getattr(lib, name)
    func.restype, func.argtypes = result, args
    return func


_open_process = _bind(kernel, "OpenProcess", w.HANDLE, DWORD, w.BOOL, DWORD)
close = _bind(kernel, "CloseHandle", w.BOOL, w.HANDLE)
_current_process = _bind(kernel, "GetCurrentProcess", w.HANDLE)
_duplicate = _bind(
    kernel, "DuplicateHandle", w.BOOL, w.HANDLE, w.HANDLE, w.HANDLE, P, DWORD, w.BOOL, DWORD
)
_get_times = _bind(kernel, "GetProcessTimes", w.BOOL, w.HANDLE, P, P, P, P)
_query_image = _bind(kernel, "QueryFullProcessImageNameW", w.BOOL, w.HANDLE, DWORD, P, P)
_session = _bind(kernel, "ProcessIdToSessionId", w.BOOL, DWORD, P)
_wait = _bind(kernel, "WaitForSingleObject", DWORD, w.HANDLE, DWORD)
_create_job = _bind(kernel, "CreateJobObjectW", w.HANDLE, P, w.LPCWSTR)
_open_job = _bind(kernel, "OpenJobObjectW", w.HANDLE, DWORD, w.BOOL, w.LPCWSTR)
_assign = _bind(kernel, "AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)
_query_job = _bind(kernel, "QueryInformationJobObject", w.BOOL, w.HANDLE, ctypes.c_int, P, DWORD, P)
_set_job = _bind(kernel, "SetInformationJobObject", w.BOOL, w.HANDLE, ctypes.c_int, P, DWORD)
_stop_job = _bind(kernel, "TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)
_in_job = _bind(kernel, "IsProcessInJob", w.BOOL, w.HANDLE, w.HANDLE, P)
_file_type = _bind(kernel, "GetFileType", DWORD, w.HANDLE)
_get_std = _bind(kernel, "GetStdHandle", w.HANDLE, DWORD)
_init_attrs = _bind(kernel, "InitializeProcThreadAttributeList", w.BOOL, P, DWORD, DWORD, P)
_delete_attrs = _bind(kernel, "DeleteProcThreadAttributeList", None, P)
_update_attrs = _bind(kernel, "UpdateProcThreadAttribute", w.BOOL, P, DWORD, SIZE, P, SIZE, P, P)
_create_process = _bind(
    kernel, "CreateProcessW", w.BOOL, w.LPCWSTR, P, P, P, w.BOOL, DWORD, P, w.LPCWSTR, P, P
)
_exit_code = _bind(kernel, "GetExitCodeProcess", w.BOOL, w.HANDLE, P)
_resume = _bind(kernel, "ResumeThread", DWORD, w.HANDLE)
_tcp_table = _bind(
    iphelper, "GetExtendedTcpTable", DWORD, P, P, w.BOOL, w.ULONG, ctypes.c_int, w.ULONG
)


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", DWORD),
        ("minimum_working_set", SIZE),
        ("maximum_working_set", SIZE),
        ("active_processes", DWORD),
        ("affinity", SIZE),
        ("priority", DWORD),
        ("scheduling", DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io_counters", ctypes.c_ulonglong * 6),
        ("memory_limits_and_peaks", SIZE * 4),
    ]


class StartupInfo(ctypes.Structure):
    _fields_ = [
        ("size", DWORD),
        ("reserved", P),
        ("desktop", P),
        ("title", P),
        ("coordinates_sizes_attributes", DWORD * 7),
        ("flags", DWORD),
        ("show_window", w.WORD),
        ("reserved_size", w.WORD),
        ("reserved_data", P),
        ("stdin", w.HANDLE),
        ("stdout", w.HANDLE),
        ("stderr", w.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("info", StartupInfo), ("attributes", P)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [("process", w.HANDLE), ("thread", w.HANDLE), ("pid", DWORD), ("tid", DWORD)]


def _checked(value):
    if not value:
        raise ctypes.WinError(ctypes.get_last_error())
    return value


@contextmanager
def _handle(value):
    try:
        yield value
    finally:
        if value:
            close(value)


def _creation(handle):
    times = [FILETIME() for _ in range(4)]
    _checked(_get_times(handle, *(ctypes.byref(value) for value in times)))
    return times[0].value


def _name(group):
    return f"Local\\DevCrew-{group:x}"


def _open(group, access):
    handle = _open_job(access, False, _name(group))
    if not handle and ctypes.get_last_error() != ERROR_FILE_NOT_FOUND:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


def owns_group(pid, identity, group):
    return (
        type(group) is int
        and 0 < group < 1 << 96
        and pid == group & 0xFFFFFFFF
        and isinstance(identity, dict)
        and identity.get("start") == str(group >> 32)
    )


def _contains(job, process):
    answer = w.BOOL()
    _checked(_in_job(process, job, ctypes.byref(answer)))
    return bool(answer.value)


def identity(pid):
    if not 1 < pid <= 0xFFFFFFFF:
        return None
    handle = _open_process(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
    if not handle:
        return None
    with _handle(handle):
        if _wait(handle, 0) != WAIT_TIMEOUT:
            return None
        started = _creation(handle)
        size = DWORD(32768)
        image = ctypes.create_unicode_buffer(size.value)
        _checked(_query_image(handle, 0, image, ctypes.byref(size)))
        session = DWORD()
        _checked(_session(pid, ctypes.byref(session)))
        group = started << 32 | pid
        with _handle(_open(group, JOB_OBJECT_QUERY)) as job:
            owned = bool(job) and _contains(job, handle)
        return {
            "start": str(started),
            "executable": image.value,
            "session": session.value,
            "process_group": group if owned else 0,
        }


def alive(pid):
    if not 1 < pid <= 0xFFFFFFFF:
        return False
    handle = _open_process(SYNCHRONIZE, False, pid)
    if not handle:
        # An inaccessible process must never be mistaken for a dead owner.
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    with _handle(handle):
        return _wait(handle, 0) != 0


def members(group):
    with _handle(_open(group, JOB_OBJECT_QUERY)) as handle:
        if not handle:
            return []
        capacity = 16
        while capacity <= 1048576:
            data = ctypes.create_string_buffer(8 + capacity * ctypes.sizeof(SIZE))
            if _query_job(handle, 3, data, len(data), None):  # JobObjectBasicProcessIdList
                count = DWORD.from_buffer(data, 4).value
                if count > capacity:
                    raise OSError("invalid process list returned by Windows")
                return list((SIZE * count).from_buffer(data, 8))
            if ctypes.get_last_error() != ERROR_MORE_DATA:
                raise ctypes.WinError(ctypes.get_last_error())
            capacity *= 2
        raise OSError("process job exceeds inspection limit")


def terminate(group):
    with _handle(_open(group, JOB_OBJECT_TERMINATE | JOB_OBJECT_QUERY)) as handle:
        if handle:
            _checked(_stop_job(handle, 1))
            deadline = time.monotonic() + 5
            while members(group):
                if time.monotonic() >= deadline:
                    raise OSError("Windows process job did not terminate in time")
                time.sleep(0.02)


def attach(pid, *, persistent=False, resource_fd=None):
    """Bind a suspended child (or a waiting test worker) to a new job."""
    access = PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if persistent or resource_fd is not None:
        access |= PROCESS_DUP_HANDLE
    with _handle(_checked(_open_process(access, False, pid))) as proc:
        group = _creation(proc) << 32 | pid
        ctypes.set_last_error(0)
        job = _checked(_create_job(None, _name(group)))
        try:
            if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
                raise OSError("the process job name is already in use")
            _set_kill_on_close(job, True)
            _checked(_assign(job, proc))
            if resource_fd is not None:
                # Keep the original share-deny-write file object alive in the
                # check child until it exits, including abrupt wrapper death.
                remote = w.HANDLE()
                _checked(
                    _duplicate(
                        _current_process(),
                        msvcrt.get_osfhandle(resource_fd),
                        proc,
                        ctypes.byref(remote),
                        0,
                        False,
                        0,
                    )
                )
            if persistent:
                # Keep the name open across CLI invocations while the QA
                # leader lives. Its non-inheritable query-only handle closes
                # on leader exit; surviving descendants then die with the job.
                remote = w.HANDLE()
                _checked(
                    _duplicate(
                        _current_process(),
                        job,
                        proc,
                        ctypes.byref(remote),
                        JOB_OBJECT_QUERY,
                        False,
                        0,
                    )
                )
        except BaseException:
            close(job)
            raise
    return group, job


class Child:
    """The subset of Popen used by the check and QA lifecycle."""

    def __init__(self, argv, info):
        self.args, self.pid, self.handle = argv, info.pid, info.process
        self.returncode = None
        self.devcrew_job_handle = None
        self._finalizer = weakref.finalize(self, close, self.handle)

    def poll(self):
        if self.returncode is None:
            status = _wait(self.handle, 0)
            if status == WAIT_TIMEOUT:
                return None
            if status != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            code = DWORD()
            _checked(_exit_code(self.handle, ctypes.byref(code)))
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout=None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        while self.poll() is None:
            remaining = 0.1 if deadline is None else min(deadline - time.monotonic(), 0.1)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(self.args, timeout)
            # Bounded intervals allow Python to deliver Ctrl+C to the caller.
            _wait(self.handle, max(1, int(remaining * 1000)))
        return self.returncode


def _set_kill_on_close(job, enabled):
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000 if enabled else 0
    _checked(_set_job(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))


def _stdio(stack, stream, number):
    if stream is None:
        source = _get_std(-10 - number)
        if not source or source == P(-1).value:
            stream = subprocess.DEVNULL
    if stream == subprocess.DEVNULL:
        stream = stack.enter_context(open(os.devnull, "rb" if number == 0 else "wb"))
    if stream is not None:
        source = msvcrt.get_osfhandle(stream.fileno())
    duplicate = w.HANDLE()
    _checked(
        _duplicate(
            _current_process(), source, _current_process(), ctypes.byref(duplicate), 0, True, 2
        )
    )  # DUPLICATE_SAME_ACCESS
    stack.callback(close, duplicate.value)
    return duplicate.value


def _create_suspended(argv, cwd, environment, stdin, stdout, stderr, guard):
    """CreateProcessW assigns the startup guard *at creation*, before returning
    a PID. A hard-killed wrapper cannot strand an unassigned suspended process.
    """
    with ExitStack() as stack:
        startup = StartupInfoEx()
        startup.info.size = ctypes.sizeof(startup)
        startup.info.flags = 0x100  # STARTF_USESTDHANDLES
        startup.info.stdin = _stdio(stack, stdin, 0)
        startup.info.stdout = _stdio(stack, stdout, 1)
        startup.info.stderr = (
            startup.info.stdout if stderr == subprocess.STDOUT else _stdio(stack, stderr, 2)
        )
        stdio = list(dict.fromkeys((startup.info.stdin, startup.info.stdout, startup.info.stderr)))
        # Console pseudo-handles travel with the console and cannot appear
        # in PROC_THREAD_ATTRIBUTE_HANDLE_LIST (as in CPython's Popen).
        inherited = [h for h in stdio if h & 3 != 3 or _file_type(h) != 2]
        handles = (w.HANDLE * len(inherited))(*inherited)
        attribute_count = 2 if inherited else 1
        guard_handle = w.HANDLE(guard)
        length = SIZE()
        _init_attrs(None, attribute_count, 0, ctypes.byref(length))
        if ctypes.get_last_error() != ERROR_INSUFFICIENT_BUFFER:
            raise ctypes.WinError(ctypes.get_last_error())
        attributes = ctypes.create_string_buffer(length.value)
        _checked(_init_attrs(attributes, attribute_count, 0, ctypes.byref(length)))
        stack.callback(_delete_attrs, attributes)
        if inherited:
            _checked(
                _update_attrs(attributes, 0, 0x20002, handles, ctypes.sizeof(handles), None, None)
            )
        _checked(
            _update_attrs(
                attributes,
                0,
                0x2000D,
                ctypes.byref(guard_handle),
                ctypes.sizeof(guard_handle),
                None,
                None,
            )
        )
        startup.attributes = ctypes.addressof(attributes)
        env = None
        if environment is not None:
            # The final entry and the block each have a terminating wide NUL.
            entries = [key + "=" + environment[key] for key in sorted(environment, key=str.upper)]
            env = ctypes.create_unicode_buffer("\0".join(entries) + "\0")
        info = ProcessInformation()
        command = subprocess.list2cmdline(argv)
        if "\0" in command:
            raise ValueError("NUL in command")
        command_line = ctypes.create_unicode_buffer(command)
        flags = (
            0x80000 | 0x400 | 0x4
        )  # EXTENDED_STARTUPINFO_PRESENT | UNICODE_ENVIRONMENT | SUSPENDED
        _checked(
            _create_process(
                None,
                command_line,
                None,
                None,
                bool(inherited),
                flags,
                env,
                os.fspath(cwd) if cwd is not None else None,
                ctypes.byref(startup),
                ctypes.byref(info),
            )
        )
        return info


def spawn(
    argv,
    *,
    persistent=False,
    resource_fd=None,
    cwd=None,
    env=None,
    stdin=None,
    stdout=None,
    stderr=None,
    close_fds=True,
):
    if not close_fds:
        raise ValueError("managed Windows commands inherit only explicit handles")
    with _handle(_checked(_create_job(None, None))) as guard:
        _set_kill_on_close(guard, True)
        info = _create_suspended(argv, cwd, env, stdin, stdout, stderr, guard)
        with _handle(info.thread):
            child = Child(argv, info)
            group, job = attach(child.pid, persistent=persistent, resource_fd=resource_fd)
            try:
                child.devcrew_group = group
                if _resume(info.thread) != 1:
                    raise OSError("could not resume the managed child")
                # The named job now protects the live command. During setup,
                # loss of this guard terminates even a partially assigned child.
                _set_kill_on_close(guard, False)
            except BaseException:
                close(job)
                raise
            if persistent:
                close(job)
            else:
                child.devcrew_job_handle = job
            return child


def release(child):
    handle = getattr(child, "devcrew_job_handle", None)
    if handle:
        try:
            terminate(child.devcrew_group)
        finally:
            close(handle)
            child.devcrew_job_handle = None


def listener_pids(port):
    """Get all IPv4 and IPv6 TCP listener owners, including wildcard binds."""
    result = set()
    # MIB_TCPROW_OWNER_PID / MIB_TCP6ROW_OWNER_PID store the local port in
    # network order and scope addresses differently. Both use four-byte integer fields.
    for family, row_size, port_offset, pid_offset in (
        (socket.AF_INET, 24, 8, 20),
        (socket.AF_INET6, 56, 20, 52),
    ):
        size = DWORD(0)
        for _ in range(8):
            buffer = ctypes.create_string_buffer(size.value) if size.value else None
            status = _tcp_table(
                buffer, ctypes.byref(size), False, family, 3, 0
            )  # OWNER_PID_LISTENER
            if status == ERROR_INSUFFICIENT_BUFFER:
                continue
            if status or buffer is None:
                raise ctypes.WinError(status)
            count = DWORD.from_buffer(buffer).value
            if 4 + count * row_size > len(buffer):
                raise OSError("Windows returned an incomplete TCP listener table")
            for i in range(count):
                offset = 4 + i * row_size
                endpoint = socket.ntohs(
                    DWORD.from_buffer(buffer, offset + port_offset).value & 0xFFFF
                )
                if endpoint == port:
                    result.add(DWORD.from_buffer(buffer, offset + pid_offset).value)
            break
        else:
            raise OSError("Windows TCP listener table kept changing")
    return result
