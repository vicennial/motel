# GitHub Action

`action.yml` at the repository root is a composite action with two modes. In both, a final step with `if: always()`
checks out whatever the action leased, even when the command or an earlier step failed.

| mode | what it does | where it can run |
|---|---|---|
| `cabin-run` | takes a [cabin](cabins.md) (a disposable server), copies your git checkout to it, runs `command` there, deletes the cabin | a **self-hosted runner that is itself a Hetzner Cloud server** |
| `room` | leases a [room](lifecycle.md) (a TTL scratch directory), runs `command` in it on the runner | any Linux runner with passwordless sudo, including GitHub-hosted `ubuntu-24.04` (loop-file volumes), or a Hetzner runner with a token |

## Minimal workflow: a CI job on a cabin

```yaml
name: test-on-cabin
on: [push]

permissions:
  contents: read

jobs:
  test:
    runs-on: [self-hosted, hetzner]       # a Hetzner Cloud server with motel and cabins installed
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - id: cabin
        uses: vicennial/motel@<full-commit-sha-of-a-motel-release>
        with:
          mode: cabin-run
          command: make test
          server-type: cpx32
          ttl: 1h
          hetzner-token: ${{ secrets.MOTEL_CABIN_TOKEN }}   # only needed until the runner has been set up once
      - run: echo "cabin ${{ steps.cabin.outputs.cabin-id }} cost EUR ${{ steps.cabin.outputs.cost-estimate }}"
        if: always()
```

Pin `vicennial/motel` by full commit SHA, like any other third-party action. The action's source is its own
installer source too, so pinning the action pins motel.

## Minimal workflow: a room on a hosted runner

```yaml
name: scratch-room
on: [workflow_dispatch]

permissions:
  contents: read

jobs:
  build:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683 # v4.2.2
        with:
          persist-credentials: false
      - uses: vicennial/motel@<full-commit-sha-of-a-motel-release>
        with:
          mode: room
          size: 20
          ttl: 2h
          sync-path: .
          command: ./build.sh && cp -r out "$GITHUB_WORKSPACE/out"
```

The command runs under `bash -c` in the room's directory (`MOTEL_ROOM_PATH` and `MOTEL_ROOM_ID` are set). Copy results
out before the step ends: the room is checked out afterwards. To use the room from later steps, set `keep-room: true`,
read `room-path`, and end the job with `motel checkout ${{ steps.x.outputs.room-id }} --now` in an `if: always()` step.
A kept room that is never checked out still expires at its TTL and is reaped by the housekeeping timer.

## Inputs

| input | default | meaning |
|---|---|---|
| `mode` | `cabin-run` | `cabin-run` or `room` |
| `command` | | `cabin-run`: runs under `sh -c` in `~/work` on the cabin, after the sync. `room`: runs under `bash -c` on the runner, in the room. Required, except `room` with `keep-room` |
| `server-type` | first allowed type | cabin server type, for example `cpx32` (`cabin-run`) |
| `ttl` | `1h` | lease length: `90m`, `2h`, `1d`. The lease is also ended when the command ends |
| `size` | `20` | room size: a number of GB, or `200G`, `1T` (`room`) |
| `sync-path` | `.` for `cabin-run`; nothing for `room` | `cabin-run`: the git checkout to copy (tracked files plus untracked files that are not ignored). `room`: a directory copied into the room |
| `hetzner-token` | | Hetzner Cloud API token, as a secret. Optional when the runner is already set up |
| `motel-version` | | git tag or branch of `vicennial/motel` to install from. See "Installation" |
| `keep-room` | `false` | `room`: do not check the room out at the end of the step |
| `fail-on-error` | `true` | fail the step when the command exits nonzero. With `false`, read `exit-code` |

## Outputs

| output | meaning |
|---|---|
| `exit-code` | exit status of the command |
| `cabin-id` | `cabin-run`: the cabin id |
| `room-id`, `room-path` | `room`: the room id and directory. The directory exists after the step only with `keep-room` |
| `cost-estimate` | `cabin-run`: estimated cost, EUR net, as motel prints it at checkout. The provider invoice is authoritative |

`cabin-id` and `cost-estimate` are read from the messages `motel cabin run` prints on stderr. `cabin run` has no
`--json` output, so these two can be empty. The cleanup does not depend on them: it finds the cabin by a task name
that is unique to the run (`gha-<run id>-<attempt>-<4 hex>`).

The exit status of the command and motel's own exit codes share the same numbers (a command that exits 3 looks like
"no vacancy"). The step log tells them apart: motel's own errors start with `motel:`.

## Prerequisites

### Both modes

- Linux with `bash`, `git`, `sudo` and Python 3.11 or newer as `/usr/bin/python3` (`ubuntu-24.04` has it;
  `ubuntu-22.04` has 3.10 and does not work).
- **Passwordless sudo** for the runner user. GitHub-hosted runners have it. A self-hosted runner needs it too, because
  installing the front desk and `motel-desk ... setup` are root operations.
- Network access to `github.com` only if you set `motel-version`.

### `cabin-run`

- The runner must be a **Hetzner Cloud server**. A cabin's controller reads its identity from the Hetzner metadata
  service, and the cabin's firewall admits SSH only from that server's public address. GitHub-hosted runners are not
  Hetzner servers, so they cannot be cabin controllers. Use a self-hosted runner on a Hetzner server.
- `rsync`, `ssh` and `ssh-keygen` in `/usr/bin` (`apt-get install rsync openssh-client`).
- motel installed with cabins enabled: `[cabins]` in `/etc/motel/motel.toml`, the runner user in `allow_users`, the
  cabin verbs in the sudoers rule, and `sudo motel-desk cabin setup hetzner` done once (or by this action, with
  `hetzner-token`). All of this is described in [Cabins](cabins.md). At the commit this action was written against,
  the core config loader still rejects `[cabins]` and `install.sh` does not render the cabin sudoers lines, so the
  action only works on a host where those are in place. The prepare step runs `motel cabin ls` and stops with the
  CLI's message if they are not.
- A **dedicated, empty Hetzner project** for cabins and its token. The cabin token must see only cabins
  ([why](cabins.md#the-cloud-project-must-be-dedicated)). Store it as a repository or environment secret.
- A price cap that allows your server type (`max_eur_per_hour`) and `max_concurrent` above the number of parallel jobs.
  A refused checkin (exit 3) fails the step.

### `room`

- Hosted `ubuntu-24.04` or any Linux runner: with no `hetzner-token` and no motel installed, the action installs the
  front desk with the `local` provider (sparse loop files under `/var/lib/motel/local`) and runs
  `motel-desk setup local`. That needs `losetup` and `mkfs.ext4`, which the hosted images have. It needs no cloud
  account and costs nothing, but the "volume" is a file on the runner's disk, so `size` is bounded by free disk.
- A Hetzner runner can pass `hetzner-token` (Read & Write) to use real volumes; setup then runs
  `motel-desk setup hetzner --token-file`, which also records every volume already in that project as never-touch.

## Installation

If `motel` is already on `PATH`, the action uses it and does not reinstall. If it is missing, the action runs
`install.sh` from its own checkout (`github.action_path`), so the motel you get is the one at the ref you pinned. With
`motel-version`, it always installs from that tag or branch of `https://github.com/vicennial/motel`, which needs
network access to GitHub. A tag or branch can move; pin the action by SHA when you want a fixed motel.

The installer refuses a source that the runner user can change, and the action's checkout belongs to that user. So
the action first copies `install.sh` and `src/` to `/usr/local/src/motel-action` (root-owned, nothing group- or
world-writable) and runs the installer from that copy. The directory must be a direct child of `/usr/local/src`
whose name starts with `motel-`, and it carries a marker file: the action replaces a directory only when the marker is
there, and never changes the mode or owner of an existing `/usr/local/src`.

The installer also refuses to put root-run code under a directory that anybody else can write (the desk, the CLI and
`/usr/local/lib/motel`, and every directory above them), and it is right to. GitHub-hosted Ubuntu runners ship
`/usr/local/bin` with mode 777, so on them the installer would stop with `writable by group or others`. The installer's
check is not relaxed. Instead, just before an install, the action runs `sudo chmod go-w` on each of those directories (and
on any parent) that root owns and that group or others can write, and prints a line for each. This changes the runner's
own filesystem: a later step that writes to `/usr/local/bin` without `sudo` will fail, so add `sudo` there. A directory
owned by someone other than root is left alone, and the installer refuses it with its own message. If `motel` is already
installed (and `motel-version` is empty) the action does not install and does not change any mode.

The installer writes `/etc/motel/motel.toml` only if it does not exist yet, so a configuration you provisioned is kept.

## The token

`hetzner-token` is the only secret input. It reaches the action as the environment variable `HETZNER_TOKEN` of one
step. The script creates a file owned by root with mode `0600` first, then writes the token to it from standard input
(`printf '%s' "$HETZNER_TOKEN" | sudo tee FILE`), runs `motel-desk ... setup --token-file FILE`, and the cleanup step
deletes the file. The token is never an argument to any command, never echoed, and the scripts never use `set -x`.
`tests/test_action.py` checks this in `action.yml` and in `action/motel-action.sh`.

Do not give secrets to workflows triggered by pull requests from forks. A self-hosted runner that runs untrusted
code can reach whatever the runner can, including the token file while it exists.

## Cleanup and its limits

The last step has `if: always()` and:

1. lists your cabins or rooms and checks out the ones whose task name is this run's (cabin: `motel cabin checkout`;
   room: `motel checkout --now`, retried a few times because open files give exit 4);
2. removes the token file and the action's private state directory.

If a cabin cannot be checked out, the step fails with `cabin ID is NOT checked out and still bills`, so the job turns
red instead of leaking quietly.

What this does not cover: a job that is cancelled or whose runner dies can skip even an `always()` step. Then the
TTL is the backstop. A cabin is deleted by the reaper after its checkout time plus a 10 minute grace, after 30 minutes
verified idle, and by a dead-man's switch inside the guest. A room is reaped by the housekeeping timer. Keep `ttl`
short, and run `motel cabin ls` on the runner now and then.

Other things to know:

- `cabin run` copies untracked files that are not ignored. Check `git status` in CI that nothing sensitive is in the
  work tree, and keep secrets in ignored files only.
- A cabin can reach the public internet (HTTP and HTTPS and DNS) and so can send out whatever you synced to it.
- The sync is one-way. Results written on the cabin are lost when it is deleted; print them to the log, or upload them
  from the cabin in `command`.
- Jobs that run in parallel on one runner host share its `max_concurrent` and price cap.
- Cabins are experimental (see [Cabins](cabins.md)). The action itself is tested against a fake `motel` and a fake
  `sudo` (`tests/test_action.py`), not yet on a live runner.
