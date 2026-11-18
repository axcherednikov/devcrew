# Install and verify DevCrew

[Русский](../ru/guide.md) · [Workflow guide](workflows.md)

## Choose an environment

Install Git and Python 3.11+ on Linux, macOS, Windows 10+ or Windows Server 2016+. The CLI uses the standard library. On native Windows, the packaged hooks use the Python launcher (`py -3` in PATH) and a Codex host with Windows hook command support. Use a local NTFS workspace and user-private plugin data. Windows process management uses Job Objects. Run `codex plugin --help` to check marketplace support.

For chat workflows you need a client with skills. For delegated work the host must be able to start separate agents with a model and effort that meet the [model policy](../cost-policy.md). Normal implementation needs a model qualified for bounded work at medium effort. Normal independent review needs strong capability at medium effort. Use the model identifiers and priorities supplied by the host. An incompatible pinned role needs a compatible generic agent with the same contract. Inline publication uses an authenticated `gh` for GitHub or `glab` for GitLab on the selected host. Browser QA requires a host browser tool that can reach the test service on loopback.

## Install the plugin

1. Download and unpack the source archive. If you already have a Git clone, export its committed files to a new sibling directory with the following commands. Run them from the clone root. Choose an unused destination name.

   ```sh
   release_dir="../devcrew-release"
   mkdir "$release_dir" && git archive HEAD | tar -x -C "$release_dir"
   cd "$release_dir"
   ```

   Open a terminal at the unpacked root containing `.codex-plugin/plugin.json`. The local installer copies the directory, which is why the marketplace should contain only release files. A working checkout can also include Git history, ignored files and local runtime data.
2. Register this directory as a local marketplace and install the plugin:

   ```sh
   codex plugin marketplace add "$PWD"
   codex plugin add devcrew@devcrew-community
   ```

3. In Codex open the **target project**. Ask `$devcrew-setup` to initialize it. Setup reads project conventions, installs 12 roles under `.codex/agents/`, and configures the project's context, memory and checks. It preserves existing documents and reports conflicting role definitions.
4. Review the hook definitions when Codex asks for trust. Start a new session in the target project. Try `$devcrew-review Review my unstaged changes` with a small change present.

Keep the release directory in place while its marketplace is configured. To switch releases, remove the plugin and marketplace, then repeat the installation from the new unpacked release and open a fresh session. To uninstall, run `codex plugin remove devcrew@devcrew-community` followed by `codex plugin marketplace remove devcrew-community`. Target project documents, roles and saved checks are separate from the plugin installation.

## Try the CLI in a disposable project

Run this from the plugin source directory. It creates a temporary Git repository, installs the project files, runs one command and verifies the resulting record:

```sh
demo=$(mktemp -d)
git init -q "$demo"
python3 -B scripts/devcrew.py init --project "$demo"
python3 -B scripts/devcrew.py doctor --project "$demo"
output=$(python3 -B scripts/devcrew.py run-check --project "$demo" --name smoke -- python3 -c 'print("smoke ok")')
record=$(printf '%s\n' "$output" | head -n 1)
python3 -B scripts/devcrew.py verify-check --record "$record"
```

Look for `"status": "verified"` in the last response. The run log and record live under the demo's `.codex/checks/`. A new Git repository has no commit yet, so the record can carry a warning about the missing HEAD.

### Native Windows (PowerShell)

For installation from a clone in PowerShell, run `git archive --format=zip -o ../devcrew-release.zip HEAD`, then `Expand-Archive ../devcrew-release.zip ../devcrew-release` using a fresh destination. Open the unpacked directory and run the marketplace commands above.

From the unpacked plugin directory, run the same smoke check in a fresh local directory:

```powershell
$demo = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
git init -q $demo
py -3 -B scripts/devcrew.py init --project $demo
py -3 -B scripts/devcrew.py doctor --project $demo
$python = py -3 -c "import sys; print(sys.executable)"
$result = py -3 -B scripts/devcrew.py run-check --project $demo --name smoke -- $python --version
py -3 -B scripts/devcrew.py verify-check --record $result[0]
```

## Conversation and document language

The plugin instructions and project roles are written in English. DevCrew follows the language of your conversation for its replies, including Russian. You can request a different language for a deliverable, such as an English brief while discussing it in Russian. Commands, code identifiers and machine-readable fields retain their original spelling.

For fresh project documents the CLI defaults to English. Use `init --project PATH --language ru` for Russian or `--language en` for English. A repeat run preserves existing documents. See the [English brief](../../skills/devcrew-brief/references/template-main.md), [Russian brief](../../skills/devcrew-brief/references/ru/template-main.md) and matching plan, short brief, report, handoff and project templates.

## Data and hooks

- The target repository holds human-readable project context and durable memory. Runtime state such as task cards, agent roles and check logs goes under the ignored `.codex/` directory by default. Inspect the staged diff before sharing project documents.
- A trusted learning hook handles every submitted message in initialized projects. It records a local SHA-256 digest, session and turn identifiers, project path, timestamps and an optional explicit intent confirmation. The private receipt store has a 24-hour validity window and lazy cleanup. Older installations may still have raw-message stores. [Recovery and retention](../bootstrap-recovery.md)
- To disable the learning hook in one project, set `{"enabled":false}` in `.codex/devcrew-learning.json`. This also disables new hook-based contract confirmations. The sync notification hook is independent.
- Check records include the command arguments and combined output. Keep secrets out of both. A successful check certifies the recorded invocation. The reviewer still needs to establish that the invocation tests the requested behavior.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Marketplace or plugin missing | Register the absolute path of the release root, then use the exact selector `devcrew@devcrew-community`. |
| Roles missing after setup | Open a fresh project session. From the source checkout run `python3 -B scripts/devcrew.py doctor --project /path/to/project` to inspect the files. Live role availability is reported by the host. |
| `AGENT_RUNTIME_REQUIRED` | Check the available models, effort levels and separate-agent support. Matching TOML files establish only on-disk setup. |
| `PLUGIN_DATA` unavailable in a shell | Codex supplies this directory to a trusted hook. Check the hook inside Codex. The CLI smoke can still run. |
| An ordinary prompt is blocked | Check the error from the learning hook, the project registry and the 1 MiB event limit. Use the project switch above to recover access while investigating. |
