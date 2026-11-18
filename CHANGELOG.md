# Changelog

## 0.24.3 — 2026-11-18

- Fix Windows reserved-name checks on Python 3.15 while retaining support for Python 3.11 and 3.12. Native hook tests now report the reason when a hook blocks.
- Make temporary Git projects used in the test suite ignore local DevCrew state regardless of the contributor’s global Git settings.
- Start loopback HTTP test services without a reverse DNS lookup on macOS.

## 0.24.2 — 2026-11-18

Initial public source distribution. Includes 16 Codex skills, 12 project roles, local verification and QA tools, and project-scoped hooks.
