"""Cross-platform operations for the plugin's file and lock contracts."""

import errno
import os
import stat
import time
from pathlib import Path

try:
    from ntpath import isreserved as _is_reserved_name
except ImportError:  # Python 3.11 and 3.12
    from pathlib import PureWindowsPath

    def _is_reserved_name(part):
        return PureWindowsPath(part).is_reserved()


if os.name == "nt":
    import msvcrt

    import devcrew_winfiles


def windows_safe_parts(parts):
    """Reject alternate data streams, DOS devices and names that alias other entries."""
    return all(
        part
        and not any(ord(c) < 32 or c in '<>:"/\\|?*' for c in part)
        and not part.endswith((" ", "."))
        and not _is_reserved_name(part)
        for part in parts
    )


def unsafe_parts(parts):
    return os.name == "nt" and not windows_safe_parts(parts)


def is_link(path):
    if os.name != "nt":
        return path.is_symlink()
    try:
        return bool(path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
    except (FileNotFoundError, NotADirectoryError):
        return False


def open_regular(path, flags, mode=0o600, *, exclusive_writer=False):
    if os.name == "nt":
        fd = devcrew_winfiles.open_regular(path, flags, exclusive_writer=exclusive_writer)
    else:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, "expected a regular file", os.fspath(path))
    return fd


def lock(fd, *, wait=True):
    if os.name != "nt":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        return
    # Lock a byte beyond all resource metadata. The data remains readable for
    # status inspection while Windows enforces the inter-process byte-range lock.
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0x7FFFFFFF, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EDEADLK, errno.EAGAIN):
                    raise
                if not wait:
                    raise BlockingIOError(errno.EWOULDBLOCK, "resource is locked") from exc
                time.sleep(0.05)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def unlock(fd):
    if os.name != "nt":
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    position = os.lseek(fd, 0, os.SEEK_CUR)
    try:
        os.lseek(fd, 0x7FFFFFFF, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    finally:
        os.lseek(fd, position, os.SEEK_SET)


def private(path, mode, *, fd=None):
    if os.name == "nt":
        devcrew_winfiles.private_permissions(path, mode == 0o700, fd)
    elif fd is not None:
        os.fchmod(fd, mode)
    else:
        os.chmod(path, mode)


def has_private_mode(path, mode):
    if os.name == "nt":
        return devcrew_winfiles.trusted_access(path)
    return stat.S_IMODE(Path(path).stat().st_mode) == mode


def safe_writer_permissions(path):
    if os.name == "nt":
        return devcrew_winfiles.trusted_access(path, writable_only=True)
    return not Path(path).stat().st_mode & 0o022


def publish(source, path, *, replace):
    if os.name == "nt":
        devcrew_winfiles.publish(source, path, replace=replace)
        return
    if replace:
        os.replace(source, path)
    else:
        os.link(source, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
