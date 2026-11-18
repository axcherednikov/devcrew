# Contributing

[Русский](docs/ru/contributing.md)

Use a small example when reporting a bug. Include the DevCrew version, Python and Codex versions, operating system, expected result and observed output. Replace repository names, user paths, tokens and customer data with synthetic examples. Use [private reporting](SECURITY.md) for security issues.

## Work on a change

1. Read [repository conventions](AGENTS.md) and find the owner of the behavior. Workflow instructions live in `skills/`, runtime code in `scripts/`, and bundled event handlers in `hooks/`.
2. Make a focused change on a branch. For a behavior change, include a regression test for the affected public command or workflow. Update both user guides when the change affects users.
3. From the repository root, run the relevant checks with Python 3.11+ and Git:

   ```sh
   make check-package
   make test-serial TESTS='test_devcrew.DevCrewCLITest'
   ```

   Select the test module or class for your change. Run `make test` for the full suite. Routing or model changes also need the separate [evaluation scenarios](tests/routing-evals.md). A local clone can be exercised with the [CLI smoke test](docs/en/guide.md).
4. Use an English imperative subject, such as `fix(cli): preserve the exit code`. In the pull request, describe the user-visible effect, what you ran, and any checks that still need a live environment.

Changes to a source checkout take effect in Codex after reinstalling the plugin and opening a fresh session.
