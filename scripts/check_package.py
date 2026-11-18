#!/usr/bin/env python3
"""Read-only, stdlib checks for this repository's package and release metadata."""

import ast
import json
import pathlib
import re
import tomllib


def check(root: pathlib.Path) -> dict:
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    version = manifest["version"].split("+", 1)[0]
    metadata = dict(
        line.split("=", 1)
        for line in (root / ".version").read_text(encoding="utf-8").splitlines()
        if "=" in line
    )
    errors = []
    hooks_path = root / "hooks/hooks.json"
    hook_script = root / "hooks/user_prompt_submit.py"
    if not hooks_path.is_file() or not hook_script.is_file():
        errors.append("supported UserPromptSubmit hook packaging is incomplete")
    else:
        try:
            hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
            entries = hooks["hooks"]["UserPromptSubmit"][0]["hooks"]
            if not any(
                entry.get("type") == "command"
                and "user_prompt_submit.py" in entry.get("command", "")
                for entry in entries
            ):
                errors.append("UserPromptSubmit hook does not invoke the packaged hook script")
            for event_groups in hooks["hooks"].values():
                for group in event_groups:
                    for entry in group["hooks"]:
                        if entry.get("type") == "command" and "commandWindows" not in entry:
                            errors.append("packaged hook is missing its native Windows command")
            notice_script = root / "hooks/project_sync_notice.py"
            for event in ("UserPromptSubmit", "Stop"):
                handlers = [entry for group in hooks["hooks"][event] for entry in group["hooks"]]
                if not notice_script.is_file() or not any(
                    entry.get("type") == "command"
                    and "project_sync_notice.py" in entry.get("command", "")
                    for entry in handlers
                ):
                    errors.append(f"{event} project sync notice hook packaging is incomplete")
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            errors.append(f"hooks/hooks.json has invalid UserPromptSubmit configuration: {exc}")
    if metadata.get("current") != version:
        errors.append("manifest and .version disagree")
    if not (root / "VERSION").read_text(encoding="utf-8").startswith(version + " "):
        errors.append("VERSION must start with the current release")
    if f"## {version} " not in (root / "CHANGELOG.md").read_text(encoding="utf-8"):
        errors.append("CHANGELOG lacks the current release")
    roles = list((root / "skills/devcrew-setup/assets/agents").glob("*.toml"))
    runtime_tree = ast.parse((root / "scripts/devcrew.py").read_text(encoding="utf-8"))
    runtime_assignments = {
        node.targets[0].id: node.value
        for node in runtime_tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
    }
    try:
        role_names = set(ast.literal_eval(runtime_assignments["ROLE_NAMES"]))
        project_schema = ast.literal_eval(runtime_assignments["PROJECT_SCHEMA_VERSION"])
        sync_state_path = ast.literal_eval(runtime_assignments["PROJECT_SYNC_STATE_FILE"])
    except (KeyError, ValueError, TypeError) as exc:
        errors.append(f"scripts/devcrew.py has invalid role/schema sync declarations: {exc}")
        role_names, project_schema, sync_state_path = set(), None, None
    template_names = {path.stem for path in roles}
    if template_names != role_names:
        errors.append(
            f"role templates and ROLE_NAMES differ: templates={sorted(template_names)}, configured={sorted(role_names)}"
        )
    if type(project_schema) is not int or project_schema < 1:
        errors.append("project integration schema version must be a positive integer")
    if sync_state_path != ".codex/devcrew-state.json":
        errors.append("project sync state must use .codex/devcrew-state.json")
    for path in roles:
        role = tomllib.loads(path.read_text(encoding="utf-8"))
        for field in ("name", "description", "developer_instructions"):
            if not isinstance(role.get(field), str) or not role[field].strip():
                errors.append(f"{path.relative_to(root)}: missing {field}")
        if role.get("name") != path.stem:
            errors.append(f"{path.relative_to(root)}: role name mismatch")
    skills = sorted((root / "skills").glob("*/SKILL.md"))
    for path in skills:
        if not (path.parent / "agents/openai.yaml").is_file():
            errors.append(f"{path.parent.name}: missing UI metadata")
        if f"name: {path.parent.name}\n" not in path.read_text(encoding="utf-8"):
            errors.append(f"{path.parent.name}: frontmatter name mismatch")
    links = 0
    for path in [
        *root.glob("*.md"),
        *root.glob("docs/**/*.md"),
        *root.glob("skills/**/*.md"),
        *root.glob("evaluation/**/*.md"),
    ]:
        for target in re.findall(r"\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
            if "://" in target or target.startswith("#") or "<" in target:
                continue
            links += 1
            if not (path.parent / target.split("#", 1)[0]).exists():
                errors.append(f"{path.relative_to(root)}: broken link {target}")
    return {
        "ok": not errors,
        "version": version,
        "project_schema": project_schema,
        "skills": len(skills),
        "roles": len(roles),
        "links": links,
        "errors": errors,
    }


if __name__ == "__main__":
    result = check(pathlib.Path(__file__).resolve().parents[1])
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)
