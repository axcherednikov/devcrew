"""Windows handle and ACL operations for private DevCrew files (standard library)."""

import ctypes as c
from ctypes import wintypes as w
from contextlib import contextmanager
from functools import lru_cache
import msvcrt
import os
import stat


def _bind(dll, name, result, *args):
    func = getattr(dll, name)
    func.restype = result
    func.argtypes = args
    return func


_kernel = c.WinDLL("kernel32", use_last_error=True)
_sec = c.WinDLL("advapi32", use_last_error=True)
ptr = c.c_void_p
pp = c.POINTER(ptr)
dword_p = c.POINTER(w.DWORD)
_create = _bind(
    _kernel, "CreateFileW", w.HANDLE, w.LPCWSTR, w.DWORD, w.DWORD, ptr, w.DWORD, w.DWORD, w.HANDLE
)
_reopen = _bind(_kernel, "ReOpenFile", w.HANDLE, w.HANDLE, w.DWORD, w.DWORD, w.DWORD)
_close = _bind(_kernel, "CloseHandle", w.BOOL, w.HANDLE)
_free = _bind(_kernel, "LocalFree", ptr, ptr)
_get_process = _bind(_kernel, "GetCurrentProcess", w.HANDLE)
_get_attr = _bind(_kernel, "GetFileInformationByHandleEx", w.BOOL, w.HANDLE, c.c_int, ptr, w.DWORD)
_open_token = _bind(_sec, "OpenProcessToken", w.BOOL, w.HANDLE, w.DWORD, pp)
_token_info = _bind(_sec, "GetTokenInformation", w.BOOL, w.HANDLE, c.c_int, ptr, w.DWORD, dword_p)
_sid_string = _bind(_sec, "ConvertSidToStringSidW", w.BOOL, ptr, pp)
_from_sddl = _bind(
    _sec,
    "ConvertStringSecurityDescriptorToSecurityDescriptorW",
    w.BOOL,
    w.LPCWSTR,
    w.DWORD,
    pp,
    dword_p,
)
_get_dacl = _bind(
    _sec, "GetSecurityDescriptorDacl", w.BOOL, ptr, c.POINTER(w.BOOL), pp, c.POINTER(w.BOOL)
)
_get_sd = _bind(_sec, "GetSecurityInfo", w.DWORD, w.HANDLE, c.c_int, w.DWORD, pp, pp, pp, pp, pp)
_set_sd = _bind(_sec, "SetSecurityInfo", w.DWORD, w.HANDLE, c.c_int, w.DWORD, ptr, ptr, ptr, ptr)
_get_ace = _bind(_sec, "GetAce", w.BOOL, ptr, w.DWORD, pp)
_move = _bind(_kernel, "MoveFileExW", w.BOOL, w.LPCWSTR, w.LPCWSTR, w.DWORD)
_INVALID_HANDLE = ptr(-1).value
_READ_CONTROL, _WRITE_DAC = 0x20000, 0x40000
_OPEN_EXISTING, _OPEN_ALWAYS, _CREATE_NEW = 3, 4, 1
_OWNER, _DACL, _PROTECTED = 1, 4, 0x80000000


def _require(ok):
    if not ok:
        raise c.WinError(c.get_last_error())


def _checked_handle(path, access, disposition=_OPEN_EXISTING, *, share=7):
    # FILE_FLAG_OPEN_REPARSE_POINT opens the link itself. Inspect that same handle
    # before using it, even if the final directory entry changed during the open.
    handle = _create(
        os.fspath(path), access, share, None, disposition, 0x00200000 | 0x02000000, None
    )
    if handle == _INVALID_HANDLE:
        raise c.WinError(c.get_last_error())
    try:
        attrs = (w.DWORD * 2)()
        _require(_get_attr(handle, 9, c.byref(attrs), c.sizeof(attrs)))  # FileAttributeTagInfo
        if attrs[0] & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise PermissionError(f"refusing to follow a reparse point: {path}")
        return handle
    except BaseException:
        _close(handle)
        raise


def open_regular(path, flags, *, exclusive_writer=False):
    from pathlib import Path
    from devcrew_fs import windows_safe_parts

    name = Path(path)
    if not windows_safe_parts(part for part in name.parts if part != name.anchor):
        raise PermissionError(f"reserved Windows name: {path}")
    access = 0x80000000  # GENERIC_READ
    if flags & os.O_RDWR:
        access |= 0x40000000
    elif flags & os.O_WRONLY:
        access = 0x40000000
    disposition = (
        _CREATE_NEW if flags & os.O_EXCL else _OPEN_ALWAYS if flags & os.O_CREAT else _OPEN_EXISTING
    )
    try:
        handle = _checked_handle(path, access, disposition, share=1 if exclusive_writer else 7)
    except OSError as exc:
        if exclusive_writer and exc.winerror == 32:  # ERROR_SHARING_VIOLATION
            import errno

            raise BlockingIOError(errno.EWOULDBLOCK, "resource is locked") from exc
        raise
    try:
        return msvcrt.open_osfhandle(
            handle, os.O_BINARY | (flags & (os.O_RDWR | os.O_WRONLY | os.O_APPEND))
        )
    except BaseException:
        _close(handle)
        raise


def publish(source, destination, *, replace):
    # Same-volume rename, durable publication. Without REPLACE_EXISTING this
    # also provides atomic create-if-absent without depending on hard links.
    _require(_move(os.fspath(source), os.fspath(destination), 8 | int(replace)))


def _sid(sid):
    value = ptr()
    _require(_sid_string(sid, c.byref(value)))
    try:
        return c.wstring_at(value.value)
    finally:
        _free(value)


@lru_cache(maxsize=1)
def _user_sid():
    token = ptr()
    _require(_open_token(_get_process(), 8, c.byref(token)))  # TOKEN_QUERY
    try:
        size = w.DWORD()
        _token_info(token, 1, None, 0, c.byref(size))  # TokenUser
        if not size.value:
            raise c.WinError(c.get_last_error())
        buf = c.create_string_buffer(size.value)
        _require(_token_info(token, 1, buf, size, c.byref(size)))
        return _sid(ptr.from_buffer(buf))
    finally:
        _close(token)


@contextmanager
def _metadata_handle(path, write=False, fd=None):
    access = _READ_CONTROL | (_WRITE_DAC if write else 0)
    if fd is None:
        handle = _checked_handle(path, access)
    else:
        # Reopen the already-owned kernel object, even if the pathname changes.
        handle = _reopen(msvcrt.get_osfhandle(fd), access, 7, 0)
        if handle == _INVALID_HANDLE:
            raise c.WinError(c.get_last_error())
    try:
        yield handle
    finally:
        _close(handle)


def private_permissions(path, directory, fd=None):
    flags = "OICI" if directory else ""
    sddl = f"D:P(A;{flags};FA;;;{_user_sid()})(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"
    sd = ptr()
    _require(_from_sddl(sddl, 1, c.byref(sd), None))
    try:
        present, defaulted, acl = w.BOOL(), w.BOOL(), ptr()
        _require(_get_dacl(sd, c.byref(present), c.byref(acl), c.byref(defaulted)))
        if not present or not acl:
            raise PermissionError("private ACL unavailable")
        with _metadata_handle(path, write=True, fd=fd) as handle:
            status = _set_sd(handle, 1, _DACL | _PROTECTED, None, None, acl, None)
            if status:
                raise c.WinError(status)
    finally:
        _free(sd)
    if not trusted_access(path, fd=fd):
        raise PermissionError(f"filesystem did not retain the private ACL: {path}")


def trusted_access(path, *, writable_only=False, fd=None):
    """Reject a foreign owner or ACE granting untrusted access to protected data."""
    with _metadata_handle(path, fd=fd) as handle:
        owner, acl, sd = ptr(), ptr(), ptr()
        status = _get_sd(
            handle, 1, _OWNER | _DACL, c.byref(owner), None, c.byref(acl), None, c.byref(sd)
        )
        if status:
            raise c.WinError(status)
        try:
            trusted = {_user_sid(), "S-1-5-18", "S-1-5-32-544"}
            if not owner or _sid(owner) not in trusted or not acl:
                return False
            # ACL header AceCount. Unknown grant types are rejected conservatively.
            count = c.c_ushort.from_address(acl.value + 4).value
            for index in range(count):
                ace = ptr()
                _require(_get_ace(acl, index, c.byref(ace)))
                kind, flags = (c.c_ubyte * 2).from_address(ace.value)
                if kind == 1 or flags & 8:  # ACCESS_DENIED / INHERIT_ONLY
                    continue
                if kind != 0:
                    return False
                mask = w.DWORD.from_address(ace.value + 4).value
                # write/append data, attributes, delete child/object, owner/DACL, generic write/all
                if writable_only and not mask & 0x500D0156:
                    continue
                if _sid(ace.value + 8) not in trusted:
                    return False
            return True
        finally:
            _free(sd)
