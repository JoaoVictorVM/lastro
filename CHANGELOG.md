# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

No version has been released yet. The version sections below v0.1.0 are planned releases; they
list the intended scope and contain no shipped changes until their release date is set.

## [Unreleased]

### Added

- `lastro` CLI with `init`, `doctor`, `demo up` and `demo down`, configuration profiles in
  `lastro.toml` and a private data root outside the repository.
- Bundled demo application with two clinics, seeded employees, ten seeded defects and two
  safety scenarios.
- Public repository files: README, license, notices, security policy, contributing guide,
  code of conduct and this changelog.
- Ignore policy, pre-commit hooks with gitleaks secret scanning, issue forms, pull request
  template and CI on Windows and Ubuntu with coverage reporting.
- Run registry and event log in a per-project SQLite history with versioned migrations,
  secret redaction, `lastro run start`, `lastro tail`, `lastro history list` and
  `lastro backup`, plus a stable `project_id` in `lastro.toml` that `lastro init` adds to
  existing projects.
- Scripted Playwright login per profile with credentials read only from the environment,
  private storage state reused for up to 8 hours, fail-closed tenant verification by API,
  cookie, signed JWT or page element, and `lastro auth check`.

## [1.0.0] - Planned

Planned scope: autonomous mode.

## [0.9.0] - Planned

Planned scope: independent verification of findings.

## [0.8.0] - Planned

Planned scope: knowledge sources (documentation retrieval and code-inferred docs).

## [0.7.0] - Planned

Planned scope: change context from issues and Git.

## [0.6.0] - Planned

Planned scope: native executor and observability.

## [0.5.0] - Planned

Planned scope: Codex executor.

## [0.4.0] - Planned

Planned scope: persistent memory.

## [0.3.0] - Planned

Planned scope: evaluation harness and finding reproduction.

## [0.2.0] - Planned

Planned scope: human approval gates.

## [0.1.0] - Planned

Planned scope: walking skeleton. The changes listed under `Unreleased` will move here when
v0.1.0 is released.

[Unreleased]: https://github.com/JoaoVictorVM/lastro/commits/main
