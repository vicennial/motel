## What does this change, and why?

<!-- Link the issue. One logical change per pull request. -->

## How did you check it?

- [ ] `uv run --extra dev pytest` passes
- [ ] `uv run --extra dev ruff check .` passes
- [ ] No root, network or cloud token is needed by the new tests

## Checklist

- [ ] Docs and `skills/motel/SKILL.md` updated if behaviour changed
- [ ] A line under `## [Unreleased]` in `CHANGELOG.md` for anything a user would notice
- [ ] A JSON key or exit-code change is marked as breaking, with the contract test updated
- [ ] No tokens, real hostnames, IP addresses, home paths or private details in the diff or the commit messages
