# SSH connection profiles

DevCrew stores named connection parameters per Git project in the ignored
`.codex/servers/profiles.json`. Profiles persist across CLI processes, but are
not shared between projects. The CLI stores connection data only; it does not
run SSH or inspect private key files.

```sh
printf '%s\n' '{"host":"[2001:db8::1]","user":"deploy","port":22,"identity_file":"~/.ssh/id_ed25519"}' |
  python3 scripts/devcrew.py servers save --project . --name 'staging eu'
python3 <plugin-root>/scripts/devcrew.py servers list --project <project-root>
python3 <plugin-root>/scripts/devcrew.py servers get --project <project-root> --name 'staging eu'
python3 <plugin-root>/scripts/devcrew.py servers delete --project <project-root> --name 'staging eu'
```

Run the helper from the DevCrew plugin checkout or use its absolute path;
`<project-root>` is the Git project whose profiles are being managed.

Input is one JSON object with exactly one required `host` or `ssh_alias`, and
optional `user`, `port` (integer 1–65535), and `identity_file` (path string).
Unknown fields, including password or key material, are rejected. Names may
contain Unicode and spaces, up to 128 characters, but no control characters.
Saving an existing name requires `--replace`; updates preserve other profiles.
Commands return JSON, and invalid input, missing profiles, corrupt stores, or
Git safety failures return a nonzero status and a JSON error.

Before writing, DevCrew refuses to proceed if any `.codex` path is already
tracked, adds a root `.codex` ignore rule when needed, and verifies the rule
with `git check-ignore`. The CLI never changes the Git index or removes tracked
files. Existing tracked runtime state must be handled separately by its owner.
Directories and files are private, writes are atomic, and symlinks in the
storage path are rejected.

When a user asks to connect to an environment, DevCrew lists the profiles and
reads the explicitly selected profile. If no profile is available, the store
is damaged, or several profiles are plausible without a clear choice, ask for
clarification instead of guessing or requesting already saved parameters.
