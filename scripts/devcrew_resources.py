"""Cross-platform advisory resource ownership for cooperating DevCrew check runners."""

import devcrew_fs as fs
import json
import os
import pathlib
import re
import socket
import time


def _path(directory, name):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', name) or fs.unsafe_parts([name]):
        raise ValueError('invalid resource name')
    return pathlib.Path(directory).resolve() / (name + '.lock')


def _open_owner(path, *, create=False):
    # Windows sharing rights remain in force while any duplicate HANDLE
    # survives, including the one inherited by the supervised check process.
    fd = fs.open_regular(path, os.O_RDWR | (os.O_CREAT if create else 0), exclusive_writer=True)
    try:
        if os.name != "nt":
            fs.lock(fd, wait=False)
        return os.fdopen(fd, "r+", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise


def _owner(handle):
    handle.seek(0)
    raw = handle.read(8192)
    if not raw:
        return None
    value = json.loads(raw)
    if not isinstance(value, dict) or value.get('devcrew_resource') != 1:
        raise ValueError('resource lock contains unrelated data; refusing to overwrite')
    return value


class ResourceLock:
    def __init__(self, directory, name, project):
        self.path = _path(directory, name)
        self.name = name
        self.project = str(project)
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.handle = _open_owner(self.path, create=True)
        except BlockingIOError as exc:
            raise ValueError(f'resource {self.name} is busy; queue the check and inspect resource-status') from exc
        try:
            _owner(self.handle)
            owner = {'devcrew_resource': 1, 'pid': os.getpid(), 'host': socket.gethostname(),
                     'project': self.project, 'resource': self.name, 'started_at': time.time()}
            self.handle.seek(0)
            self.handle.truncate()
            json.dump(owner, self.handle)
            self.handle.flush()
            os.fsync(self.handle.fileno())
            return self
        except BaseException:
            self.handle.close()
            self.handle = None
            raise

    def __exit__(self, *_):
        if self.handle is not None:
            # Never unlink the lock inode: a waiting process may already have it open.
            self.handle.close()
            self.handle = None


def status(directory, name):
    path = _path(directory, name)
    try:
        handle = _open_owner(path)
        busy = False
    except FileNotFoundError:
        return {'resource': name, 'busy': False, 'owner': None}
    except BlockingIOError:
        handle = os.fdopen(fs.open_regular(path, os.O_RDONLY), 'r', encoding='utf-8')
        busy = True
    with handle:
        try:
            owner = _owner(handle)
        except (ValueError, UnicodeError):
            if not busy:
                raise
            owner = None  # Holder may be in the middle of writing its metadata.
        return {'resource': name, 'busy': busy, 'owner': owner if busy else None}
