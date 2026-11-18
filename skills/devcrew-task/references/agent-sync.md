# Role synchronization

Project roles are explicit `.codex/agents/*.toml` files. Use the shared
[project sync preflight](../../devcrew-setup/references/project-sync-preflight.md)
and its CLI; the plugin templates are the source of managed role fields.

DevCrew owns `name`, `description`, and `developer_instructions`. It also owns
`sandbox_mode` for roles whose current or recognized historical template
contains that optional field. A local edit to these fields is a conflict.
Only `model` and `model_reasoning_effort` are allowed local overrides. Unknown
TOML fields also produce a conflict and are never silently removed.

Sync preserves allowed overrides while updating proven managed fields. For
example, executor-agent can retain a user-selected model and `high` effort while receiving new
DevCrew instructions. Later local model/effort changes update the recorded
overrides. Per-field baseline hashes keep inherited template defaults distinct
from user overrides during future updates.

Missing shipped roles are installed. Legacy roles without state can migrate
only when the packaged historical fingerprints or managed-field projections
prove their provenance. Unknown instructions require explicit resolution.
The state, rollback guarantees, Git commit behavior, and crash-recovery limits
are defined in the shared preflight contract. Do not infer active runtime role
availability from the presence of a TOML file.

Durable lessons belong in the repository's configured memory location, not in
plugin templates.
