# Project onboarding

Run this read-first procedure for a new target repository. It discovers the repository's actual stack and conventions, then initializes project-local state through `$devcrew-setup`.

1. Read the applicable root and nested `AGENTS.md` files before choosing any state paths. Follow their references to project rules and indexes.
2. Determine the repository name and runtime from manifests and version files. Record disagreements as open questions.
3. Read declared build, test, lint, and migration commands verbatim. Do not invent commands.
4. Inspect existing `docs/`, `memory/`, `rules/`, agent configuration, task records, and repeated module/test patterns. Treat one example as evidence to verify, not as a universal convention.
5. If `.codex/devcrew.json` exists, use its paths. Otherwise choose locations that fit the repository; do not create a `docs/devcrew/` subtree by default. The helper defaults to `docs/context.md`, `.codex/tasks/`, `.codex/sessions/`, `memory/`, and `.codex/checks/`, with explicit path flags for a different discovered layout.
6. On explicit initialization, provision the ten shipped role files into `.codex/agents/*.toml`. Plugin role assets are templates, not an automatically loaded registry. Fill project-specific values from discovery; leave unknowns explicitly marked for follow-up.
7. Record discovered facts and unresolved questions in the configured context and maps. Keep durable lessons in the repository's configured memory location.

Onboarding never writes global settings, installed caches, marketplace files, external trackers, or user-specific invariants copied from another repository.
