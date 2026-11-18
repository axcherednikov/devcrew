"""Small direct-API helpers for preparing real runtime test fixtures."""

import pathlib
import copy
import os
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import devcrew
import devcrew_worktree


class ExactPathSeed:
    """Immutable directory snapshot restored at its original absolute path."""

    def __init__(self, root):
        self.root = pathlib.Path(root).resolve()
        self.archive = tempfile.TemporaryDirectory()
        self.snapshot = pathlib.Path(self.archive.name) / "seed"
        shutil.copytree(self.root, self.snapshot, symlinks=True)
        self.environment = self._environment_contract()

    @staticmethod
    def _environment_contract():
        # Trace2 changes its output file per measurement; it does not change
        # Git behavior. Every other environment entry remains part of reuse.
        return tuple(sorted((key, value) for key, value in os.environ.items()
                            if not key.startswith("GIT_TRACE2_")))

    def matches_environment(self):
        return self.environment == self._environment_contract()

    def restore(self):
        for child in self.root.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        shutil.copytree(self.snapshot, self.root, dirs_exist_ok=True, symlinks=True)

    def close(self):
        self.archive.cleanup()


def copied_attributes(source, names):
    return {name: copy.deepcopy(getattr(source, name)) for name in names}


def restore_attributes(target, values):
    for name, value in values.items():
        setattr(target, name, copy.deepcopy(value))


def create_task_checkout(project, task_id, base_sha, *, worktree_request):
    """Create and resolve a genuine task checkout without a CLI bootstrap."""
    project = devcrew._project(project)
    devcrew_worktree.create(project, task_id, base_sha, worktree_request=worktree_request)
    return devcrew_worktree.resolve(project, task_id)


def run_check(project, *, name, command, execution_root=None, task_id=None,
              timeout=None, idle_timeout=None):
    """Run the same check operation as the CLI and return its record and payload."""
    project = devcrew._project(project)
    execution_root = devcrew._project(execution_root) if execution_root else project
    effective_timeout = devcrew.DEFAULT_TIMEOUT if timeout is None else float(timeout)
    effective_idle_timeout = None if idle_timeout is None else float(idle_timeout)
    return devcrew._run_check(project, name, list(command), effective_timeout,
                              effective_idle_timeout, execution_root=execution_root,
                              task_id=task_id)
