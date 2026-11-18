# Repository Guidelines

## Project Structure & Module Organization

DevCrew is a Codex plugin containing workflow instructions and a Python CLI.

- `skills/devcrew-*/`: workflow `SKILL.md` files, supporting `references/`, and `agents/openai.yaml` metadata. `skills/devcrew/` is the public router.
- `skills/devcrew-setup/assets/`: project templates and agent role configurations.
- `scripts/`: CLI entry point (`devcrew.py`) and runtime modules (`devcrew_*.py`).
- `hooks/`: packaged prompt hook and its configuration.
- `tests/`: CLI regression tests, workflow contract tests, and routing evaluation instructions.
- `docs/` and `evaluation/`: runtime contracts, design guidance, and evaluation scenarios.
- `.codex-plugin/plugin.json`: plugin manifest; release records live in `.version`, `VERSION`, and `CHANGELOG.md`.

## Build, Test, and Development Commands

Run commands from the repository root with Python 3.11+ and Git available. There is no separate build step or dependency installation for the standard-library runtime.

- `python3 -B scripts/devcrew.py --help`: inspect the local CLI.
- `python3 -B scripts/check_package.py`: validate release metadata, role configurations, skill metadata, and documentation links.
- `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`: run the full automated suite.
- `python3 -B -m unittest discover -s tests -p 'test_scope.py' -v`: run a focused regression module.

## Coding Style & Naming Conventions

Use four-space Python indentation, `snake_case` functions and modules, and `UPPER_CASE` constants. Follow surrounding quoting and formatting; no formatter or linter configuration is checked in. Keep workflow directory names hyphenated (`devcrew-task`). Preserve existing frontmatter and JSON/TOML/YAML schemas. Keep workflow instructions, runtime behavior, and documented contracts consistent.

## Testing Guidelines

Tests use `unittest`; name files `test_*.py` and methods `test_*`. Add regression coverage for behavior changes, including failure cases. CLI tests use temporary Git repositories; avoid modifying the contributor's checkout. No numeric coverage threshold is configured. Follow `tests/routing-evals.md` separately for routing changes; automated contract checks do not establish model evaluation results.

## Commit & Pull Request Guidelines

Use the opensource commit-message profile with an English imperative subject. Common prefixes include `fix:`, `feat(devcrew):`, or `chore(devcrew):`, followed by a concise action. Keep commits focused. PR descriptions should explain the problem, resulting behavior, related issues when applicable, and exact verification outcomes. Update contracts and release metadata when relevant.

## Agent & Configuration Guidelines

Read `docs/context.md` for project-specific DevCrew self-development context and verification boundaries.

Work in the current repository directory without worktrees. Inspect the current branch before changes: keep an existing non-primary branch; on the primary branch, create a suitable task branch automatically from the current HEAD, preserving staged, unstaged, untracked and ignored files. Do not ask the user to choose or confirm a branch. Commit, merge into the primary branch and push when the user requests those actions. Work on exactly one concrete task at a time; finish it or explicitly agree on a change of task before starting another.

Follow activated workflows, including verification and stop conditions. For ordinary tasks, make the smallest complete change, verify affected behavior, and avoid unrelated abstractions. Report instruction conflicts and failed checks explicitly; never fabricate results. Keep `.codex/` runtime state untracked. Do not pass secrets in CLI arguments: execution logs do not redact them.
