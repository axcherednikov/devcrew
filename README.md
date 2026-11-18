# DevCrew

[![Tests on master](https://img.shields.io/github/actions/workflow/status/axcherednikov/devcrew/checks.yml?branch=master&label=tests&logo=github)](https://github.com/axcherednikov/devcrew/actions/workflows/checks.yml?query=branch%3Amaster)
[![Latest release](https://img.shields.io/github/v/release/axcherednikov/devcrew?color=blue)](https://github.com/axcherednikov/devcrew/releases/latest)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-3776AB?logo=python&logoColor=white)](#requirements)
[![MIT license](https://img.shields.io/github/license/axcherednikov/devcrew?color=brightgreen)](LICENSE)

DevCrew is a development harness for Codex. It helps you plan, implement, test, and review changes in a Git repository.

The plugin uses the project's own test commands and saves their output alongside the task. For changes that need independent review, a separate agent checks the diff against the original request.

## Requirements

- Codex with plugin support
- Git and Python 3.11 or later on Linux, macOS, or native Windows 10+ (Server 2016+). Windows hooks use the Python launcher (`py -3`).

Delegated workflows also need a host that can start separate agents with the [required model capabilities](docs/en/guide.md#choose-an-environment).

## Installation

Unpack the DevCrew source archive and run these commands from its root:

```sh
codex plugin marketplace add "$PWD"
codex plugin add devcrew@devcrew-community
```

If you're installing from a Git clone, first [export the release files](docs/en/guide.md#install-the-plugin). Codex copies the directory you register.

## Quick Start

Open the repository you want to work on in Codex and run:

```text
$devcrew-setup Initialize DevCrew for this project.
```

Setup installs the project roles under `.codex/agents/`. Start a new session in the same project so Codex loads them. With a local change ready, try:

```text
$devcrew Review my unstaged changes. For each bug, show the file and line,
the input that triggers it, and what the user would see.
```

To request a fix instead, describe the behavior and how to check it:

```text
$devcrew Fix the CSV export so an empty result still includes the header row.
Add a regression test and run the export tests.
```

## Workflows

`$devcrew` selects a workflow from your request. You can also call a skill directly:

| Skill | Use it to |
| --- | --- |
| `$devcrew-review` | Review a staged diff, commit, or pull request. |
| `$devcrew-diagnose` | Trace a failure and explain its cause before changing code. |
| `$devcrew-product-onboard` | Map an existing product's user flows and trace them to the source. |
| `$devcrew-skill-doctor` | Find out why a skill activates for the wrong request. |

See the full workflow guide in [English](docs/en/workflows.md) or [Русский](docs/ru/workflows.md).

## Documentation

- Installation, hooks, and troubleshooting — [English](docs/en/guide.md) · [Русский](docs/ru/guide.md)
- Contributing and running tests — [English](CONTRIBUTING.md) · [Русский](docs/ru/contributing.md)
- [Changelog](CHANGELOG.md) · [Security reporting](SECURITY.md)

## License

[MIT](LICENSE)
