# Contributing to motel

Issues and pull requests are welcome. By taking part you agree to the [code of conduct](CODE_OF_CONDUCT.md). To report
a vulnerability, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## How do I set up?

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/vicennial/motel && cd motel
uv run --extra dev pytest            # the whole suite
uv run --extra dev pytest tests/test_cli.py -q    # one file
uv run --extra dev ruff check .      # lint
```

`uv run --extra dev` installs pytest and ruff into a local `.venv`. Runtime code uses the standard library only: do not
add a dependency.

## How are the tests set up?

- Tests must not need root, a network, a cloud token or a real `motel` install. Use the fake provider, the local
  provider with a temporary directory, or a fake executable on `PATH`.
- `pyproject.toml` sets `--import-mode=importlib` and puts `src` and `tests` on `pythonpath`. There is no
  `tests/__init__.py`. Run pytest from the repository root, as above.
- Because `tests` is on `pythonpath`, test file names must be unique across the whole tree. Tour tests start with
  `test_tours_` when a core test has the same topic. Do not add `sys.path.insert` for `tests/`.
- CI also runs on macOS, where only the client side (CLI, MCP bridge, `motel try`) is meant to work. Mark tests of
  Linux-only parts with `@linux_only` from `tests/platform_marks.py`. Never use it to hide a portability bug.
- The live harness (`tests/live/`) runs against real cloud projects and is not part of a pull request. Maintainers run
  it before a release. Its own unit tests run with the rest of the suite.

## What are the ground rules?

[AGENTS.md](AGENTS.md) has the full list. The short version: the front desk runs as root, so it acts only on resources
motel created, validates every argument at the privilege boundary, fails closed with a message that ends `Try: <command>`,
and never gets a force flag that bypasses the guard. Changing a JSON key or an exit code is a breaking change
([docs/cli-json.md](docs/cli-json.md)).

## What must stay out of the tree and the commits?

Nothing in the code, tests, docs, examples or commit messages may contain real hostnames, IP addresses, home folder
paths, account names, handles, tokens or details of a private deployment. Use synthetic values: user `alice`, task
`video-render`, paths under `/mnt/motel/...`, server ids such as `1000001`. `tests/test_public_tree.py` fails on some of
these, but a passing test is not proof that a change is clean. Do not bypass a commit hook with `--no-verify`.

## How do I send a change?

1. Open an issue first for anything larger than a small fix, so we can agree on the approach.
2. Work on a branch and keep each commit to one logical change, with a message that says why.
3. Add a line under `## [Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.
4. Update the docs and the agent skill (`skills/motel/SKILL.md`) in the same change when behaviour changes.
5. Open a pull request. CI runs the tests on Linux and macOS, ruff, and a secret scan.

Docs are short and imperative. Keep the words consistent: room, cabin, tour, checkin, checkout, extend, restore, grace,
lost & found, housekeeping. Releases are described in [RELEASING.md](RELEASING.md).
