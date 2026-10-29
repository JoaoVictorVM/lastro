# Contributing to Lastro

Thank you for helping. This guide covers the local setup, the quality checks every pull request
must pass and the project conventions. By participating you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Setup

You need [uv](https://docs.astral.sh/uv/) and Git. uv installs Python 3.12 when needed.

```bash
git clone https://github.com/JoaoVictorVM/lastro.git
cd lastro
uv sync --locked
uv run pre-commit install
uv run lastro --version
```

`uv sync --locked` installs the exact versions in `uv.lock` and fails if `pyproject.toml` and the
lockfile disagree. When you change dependencies, edit them with `uv add` or `uv remove` so that
both files change together.

## Quality checks

CI runs the same commands on Windows and Ubuntu. Run them before opening a pull request:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pyright
uv run pytest --cov --cov-report=term --cov-report=xml
uv run pre-commit run --all-files
```

- `ruff` handles lint and formatting; `uv run ruff format .` fixes formatting.
- `pyright` type checks `src` and `tests` (strict for `src/lastro`).
- `pytest` runs the whole suite, including the repository contract tests in
  `tests/repository/`. Coverage is reported for `lastro` and `lastro_demo`; there is no minimum
  percentage yet.
- `pre-commit` runs ruff, the gitleaks secret scan, end-of-file and trailing-whitespace fixers
  and blocks files larger than 1 MB. The first run downloads and builds the hook environments,
  including gitleaks, so it takes a few minutes.

Never skip hooks with `--no-verify`. If gitleaks reports a secret, remove it from the file and
from the commit. If it is a false positive, explain it in the pull request and follow the
allowlist policy in [SECURITY.md](SECURITY.md).

## Branches and commits

- Branch names follow `type/short-description` in kebab-case, for example
  `feat/report-export` or `fix/doctor-git-check`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):
  `type(scope): description`, with types `feat`, `fix`, `refactor`, `test`, `docs`, `style`,
  `perf`, `build`, `ci` and `chore`.
- Pull request titles follow the same format. Keep each commit a single reviewable change.
- Add an entry under `Unreleased` in [CHANGELOG.md](CHANGELOG.md) for user-visible changes.

## Language policy

- Public documentation, issue and pull request templates and custom CI messages are in English.
- English is the canonical language for new public CLI messages and CLI documentation. Some
  existing CLI messages are in Portuguese; they stay as they are for compatibility until they
  are migrated in a dedicated change.
- The bundled demo is the exception: its UI and demo-specific messages are in Brazilian
  Portuguese on purpose, to mirror a real Brazilian back-office system.

## Adding a QA heuristic skill

QA heuristic skills are part of the mission intake work on the [roadmap](README.md#roadmap) and
do not exist in the repository yet. Until the skill layout lands, propose a heuristic through a
feature request issue describing the heuristic, the kind of defect it targets and how a tester
would recognize a finding.

## Adding a seeded defect to the demo

The demo in `src/lastro_demo` ships defects on purpose so the agent can be evaluated. To add one:

1. Register a new flag id in `DEFECT_IDS` in `src/lastro_demo/flags.py`.
2. Implement the behavior behind that flag, keeping the correct behavior when the flag is off.
3. Add an entry to `evals/catalog/demo-bugs.yaml` with the id, category, flow, trigger and
   expected symptom. The catalog stays outside the package and must never be read or served by
   the demo app.
4. Add tests in `tests/demo/test_defects.py` for the flag on and off, and update any count that
   the demo prints or tests assert (for example the ready message).
5. Keep the UI text in Portuguese, consistent with the rest of the demo.

## Keeping secrets and private data out

- Never commit `.env`, keys, certificates, browser session state, HAR files, traces, screenshots
  of real systems or run databases. `.gitignore` covers these paths and
  `tests/repository/test_gitignore.py` checks it.
- Use obviously fictitious values in examples and tests. Test code that needs a secret-shaped
  value must build it at runtime inside a temporary directory.
- Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).
