"""Confirm verified project synchronization without modifying the project."""

import hashlib
import json
import os
import pathlib
import re
import sys

PACKAGE = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE))
sys.path.insert(0, str(PACKAGE / "scripts"))
import devcrew
import devcrew_fs as fs
from devcrew_hook_deadline import run

from hooks.user_prompt_submit import (
    INPUT_LIMIT,
    _write_store,
    project_root,
    read_object,
    runtime_directory,
)


def notice_language(prompt):
    """Choose the language for this fixed UI notice, independent of agent replies."""
    if isinstance(prompt, str):
        prose = re.sub(r"```.*?```|`[^`\n]*`", "", prompt, flags=re.DOTALL)
        words = re.findall(r"[^\W\d_]+", prose)
        if words:
            russian = sum(bool(re.search(r"[А-Яа-яЁё]", word)) for word in words)
            starts_in_russian = bool(re.search(r"[А-Яа-яЁё]", words[0]))
            return "ru" if starts_in_russian or russian * 2 >= len(words) else "en"
    locale = os.getenv("LC_ALL") or os.getenv("LC_MESSAGES") or os.getenv("LANG", "")
    return "ru" if locale.lower().split("_")[0].split(".")[0] == "ru" else "en"


def notification(project, plugin_data, language="en"):
    project = pathlib.Path(project).resolve()
    state_path = project / devcrew.PROJECT_SYNC_STATE_FILE
    if fs.is_link(state_path) or not state_path.is_file():
        return None
    before = state_path.read_bytes()
    status = devcrew._sync_analysis(project)
    if status["status"] != "SYNC_OK" or state_path.read_bytes() != before:
        return None
    digest = hashlib.sha256(before).hexdigest()
    plugin_version = status["plugin_version"]
    directory = runtime_directory(plugin_data, initialize=True) / "devcrew-sync-notices"
    if fs.is_link(directory):
        raise ValueError("sync notice directory must not be a symlink")
    directory.mkdir(mode=0o700, exist_ok=True)
    fs.private(directory, 0o700)
    key = hashlib.sha256(str(project).encode("utf-8")).hexdigest()
    marker = directory / (key + ".json")
    fd = fs.open_regular(directory / (key + ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fs.lock(fd)
        if fs.is_link(marker):
            raise ValueError("sync notice marker must not be a symlink")
        previous = read_object(marker) if marker.exists() else {}
        if (
            previous.get("plugin_version") == plugin_version
            and previous.get("state_sha256") == digest
        ):
            return None
        # Do not confirm a state replaced while waiting for another hook.
        if state_path.read_bytes() != before:
            return None
        _write_store(
            directory,
            marker,
            {
                "plugin_version": plugin_version,
                "state_sha256": digest,
            },
        )
    finally:
        fs.unlock(fd)
        os.close(fd)
    name = json.dumps(project.name, ensure_ascii=False)
    overrides = ", ".join(status["locally_modified_roles"])
    if language == "ru":
        message = f"DevCrew {plugin_version}: проект {name} синхронизирован, файлы ролей проверены."
        if overrides:
            message += f" Локальные настройки сохранены: {overrides}."
        return message + " Чтобы загрузить обновлённые инструкции и роли, открой новый чат."
    message = f"DevCrew {plugin_version}: project {name} is in sync and its role files have been verified."
    if overrides:
        message += f" Local overrides preserved: {overrides}."
    return message + " Start a new chat to load any updated instructions and roles."


def main():
    output = {}
    try:
        raw = sys.stdin.buffer.read(INPUT_LIMIT + 1)
        if len(raw) > INPUT_LIMIT:
            raise ValueError("hook event exceeds input limit")
        event = json.loads(raw)
        cwd = pathlib.Path.cwd().resolve()
        if (
            isinstance(event, dict)
            and event.get("hook_event_name") in ("UserPromptSubmit", "Stop")
            and isinstance(event.get("cwd"), str)
            and pathlib.Path(event["cwd"]).resolve() == cwd
        ):
            project = project_root(cwd)
            if project is not None:
                message = notification(
                    project, os.environ.get("PLUGIN_DATA"), notice_language(event.get("prompt"))
                )
                if message:
                    output["systemMessage"] = message
    except Exception:
        # Notification failure cannot block work or manufacture sync success.
        pass
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    run(main, str(pathlib.Path(__file__).resolve()), {})
