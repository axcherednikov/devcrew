# Repository context

DevCrew ships a Codex plugin, project-role templates, a standard-library Python CLI and bundled hooks. The plugin manifest owns the version. Keep `.version`, `VERSION` and `CHANGELOG.md` consistent with it.

Use [AGENTS.md](../AGENTS.md) for checkout and verification rules and [CONTRIBUTING.md](../CONTRIBUTING.md) for the development workflow. The [English guide](en/guide.md) and [Russian guide](ru/guide.md) cover installation and storage. The [runtime contract](runtime-contract.md), [model policy](cost-policy.md) and [state layout](project-state-layout.md) define implementation behavior.

Use this repository's helper when checking a source change. Project-local roles and working records belong under ignored `.codex/`; they are generated in the target project. A clean source clone does not need local DevCrew state to run the package check or unit tests. Passing source tests does not update an installed plugin. Reinstall and start a fresh Codex session to exercise host integration. For a blocked bootstrapping process, see [recovery](bootstrap-recovery.md).
