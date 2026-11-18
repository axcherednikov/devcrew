# Changelog

## 0.24.5 — 2026-11-19

- Split phase manifest validation into ordered checks. The CLI preserves the first error reported and the point at which each phase’s owned scope is normalized.
- Format Python sources with Ruff and check formatting, imports, and lint rules in CI.
- Add issue forms, a pull request template, and English and Russian community and accessibility guidance.

## 0.24.4 — 2026-11-18

- Prevent background Git maintenance from racing with snapshot restores in candidate and review tests.

## 0.24.3 — 2026-11-18

- Fix Windows reserved-name checks on Python 3.15 while retaining support for Python 3.11 and 3.12. Native hook tests now report the reason when a hook blocks.
- Make temporary Git projects used in the test suite ignore local DevCrew state regardless of the contributor’s global Git settings.
- Start loopback HTTP test services without a reverse DNS lookup on macOS.

## 0.24.2 — 2026-11-18

Initial public source distribution. Includes 16 Codex skills, 12 project roles, local verification and QA tools, and project-scoped hooks.
