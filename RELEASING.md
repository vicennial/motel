# Releasing motel

A release is a version bump, a dated changelog section and a tag. GitHub Actions does the rest: it runs the tests,
builds the package, publishes it to PyPI with Trusted Publishing (no token anywhere) and creates the GitHub Release
with the changelog section as its notes. Pre-release versions (`0.2.0a1`, `0.2.0rc1`) become GitHub pre-releases.

## Every change

- Work on a branch and open a pull request. CI runs the tests on Linux and macOS, ruff, and a secret scan.
- Add a line under `## [Unreleased]` in `CHANGELOG.md` for anything a user would notice (Added, Changed, Fixed,
  Removed, Security).

## A release

1. On `main`, in one commit named `motel X.Y.Z`:
   - set `__version__` in `src/motel/__init__.py`, `version` in `pyproject.toml` and `version` in `server.json`
     (a test checks that they match);
   - rename `## [Unreleased]` to `## [X.Y.Z] - YYYY-MM-DD`, add a new empty `## [Unreleased]` above it, and update the
     compare links at the bottom.
   Patch (`0.1.x`) for fixes, minor (`0.x.0`) for new features or changed behavior.
2. Push `main`, then the tag:

   ```bash
   git tag -a vX.Y.Z -m "motel X.Y.Z" && git push origin vX.Y.Z
   ```

3. Watch the "Release" workflow. It stops before publishing when the tag, `__version__` and the changelog do not agree
   or a test fails. A version that is already on PyPI is not uploaded again, so re-running the workflow is safe.
4. Upgrade a host: `sudo motel install --upgrade` (or re-run `install.sh` from the new tag), then `motel doctor`.

PyPI never accepts the same version twice. If a release is broken, fix it and release the next patch version.

## Live qualification

Before a minor release, run the live harness in a dedicated, empty cloud project (it refuses to start otherwise):
`python3 tests/live/run_live.py --help`. It creates one small server and a few volumes, checks the whole room and
cabin lifecycle, and deletes everything it made. A pass costs about one cent of server time.

## One-time setup

- PyPI: in the project's Publishing settings, add a trusted publisher: owner `vicennial`, repository `motel`,
  workflow `release.yml`, environment `pypi`.

## Keeping private details out

This repo is public. `tests/test_public_tree.py` fails on home folders, globally routable IP addresses and a short
list of hashed private words. A maintainer's own host names, ids and paths belong in a private pre-commit/pre-push
hook on their machine, with the word list kept outside the repo.
