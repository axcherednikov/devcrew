# DevCrew bootstrap recovery

## Environment and source failures are separate

`hooks/hooks.json` runs `python3 -B "${PLUGIN_ROOT}/hooks/user_prompt_submit.py"`
on POSIX hosts. Its Windows command uses the native `py -3` launcher and reads
`PLUGIN_ROOT` from the environment. Both run the same trusted `UserPromptSubmit` hook. It does not initialize or
export `PLUGIN_DATA`. Codex supplies that path to the hook process. Previously,
`_private_store` required it to be an already existing directory and failed
before creating its private child. An unset variable, a nonexistent supplied
path, an unsafe path, and a source-retention policy conflict are different
failures; choosing a random writable directory does not fix them.

The hook creates the runtime-supplied directory if needed. Missing or relative paths, symlinks and unwritable locations produce diagnostics. Start a trusted hook in Codex to check its environment.

The former v1 hook queue stored the prompt for up to 24 hours; `mode: capture`
then copied it into a permanent task snapshot. The repaired v2 queue stores
metadata only and cannot serve as a verified source. Existing v1 queues and
legacy task snapshots are not imported, silently rewritten or deleted. An owner
may remove obsolete raw-message stores under their retention policy; TTL was
lazy eviction, not a guarantee that an idle old store was erased on disk.

## Supported message access

The official [plugin hook documentation](https://learn.chatgpt.com/docs/hooks#plugin-bundled-hooks)
documents the runtime-injected writable plugin-data location. The
[App Server API](https://learn.chatgpt.com/docs/app-server#api-overview) documents
thread metadata and paginated turn/item reads. Availability depends on the
actual client/server; an installed Python plugin cannot assume an authenticated
connection to the desktop session.

When thread reads are available, verify that the human message is complete. Never infer exact input from a preview or truncated response.
The CLI does not automatically call that desktop tool. Without a verified
adapter, it uses the explicitly confirmed contract source documented in the
[runtime contract](runtime-contract.md#original-intent-and-task-completion).
If the trusted hook itself is unavailable, contract confirmation binding also
stops; an agent-authored `confirmed: true` is not an alternative.

## Owner-authorized maintenance

Use this procedure only for a concrete bootstrap defect that prevents the
normal DevCrew workflow, after explicit owner authorization for bounded
maintenance. Do not invoke the broken DevCrew task workflow to repair itself.
First read applicable instructions: higher-priority mandatory requirements
remain binding. If they expressly prohibit maintenance, report the exact
conflict and propose the smallest policy amendment; do not bypass it.

1. Establish the failing path, environment observations and expected behavior.
   Separate runtime configuration from source/provenance policy. Keep the
   current checkout and branch, preserve the index and unrelated files, and
   load applicable engineering requirements through the existing public CLI.
   A context load failure stops dependent coding; maintenance is not a bypass.
2. Limit changes to restoring bootstrap and its contracts. Never fabricate an
   original prompt, human confirmation, routed agent receipt, successful check,
   or Intent Review acceptance. Do not edit installed plugin caches or global
   instructions to obtain a pass.
3. Use ordinary local implementation and targeted regression/static checks,
   independent of the broken bootstrap. Obtain a separate read-only review
   context without claiming it is an authoritative DevCrew phase review.
   Respect the owner's verification policy; do not run a full suite without
   authorization when it is reserved to the owner.
4. Create a local candidate commit only when authorized, containing only this
   task's paths and excluding earlier staged files. Push, merge, tag, release,
   installing the repaired plugin, or modifying global policy need their own
   authorization. Report exact checks, findings, candidate identity and limits.
5. End maintenance after this repair. Subsequent product work uses the normal
   workflow: a sufficient current chat message takes the ordinary no-commit
   lifecycle, while a formal schema-v1/v2 task keeps its verified
   source/confirmed contract, planner preflight, independent Intent Review and
   existing acceptance gates. Resume an existing valid formal manifest rather
   than replacing it or erasing its history. New formal source confirmation is
   needed if an unbound receipt expired; a bound confirmation survives expiry
   and is revalidated from immutable task state.
