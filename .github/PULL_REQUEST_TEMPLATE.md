## What changed

<!-- Summarize the change and the behavior it affects. -->

## Why

<!-- The problem this solves or the reason for the change. Link the related issue. -->

## Checklist

- [ ] The title follows Conventional Commits (`type(scope): description`).
- [ ] Tests cover the change, and `uv run pytest` passes locally.
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run pyright` pass.
- [ ] `CHANGELOG.md` has an entry under `Unreleased` for user-visible changes.
- [ ] Documentation is updated where behavior changed.
- [ ] No secrets, session material, private evidence or personal data are included.
- [ ] The change stays within the scope described above.
