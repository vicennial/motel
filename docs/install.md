# Installing and upgrading the front desk

The desk runs as root, so it must run from a copy that only root can change. `sudo motel install` and
`install.sh` copy the package to `/usr/local/lib/motel`, write `/usr/local/sbin/motel-desk` and
`/usr/local/bin/motel` (both start the system `/usr/bin/python3 -I`, never the virtualenv that happens to
hold the wheel), the sudoers rule and the housekeeping units, and leave the config, credentials and state alone.

## Install and upgrade

```
sudo uvx --from motel motel install --provider hetzner --users alice          # first install
sudo uvx --from motel==X.Y.Z motel install --upgrade                          # later: replace the code, keep everything else
```

Run it with `sudo uvx`, so the code comes from **root's own** uv cache. `sudo motel install --upgrade` with the
installed `motel` is refused: its source is the installed copy itself, and copying a directory onto itself upgrades
nothing. The refusal names the installed version.

**Never run a user-owned binary under sudo.** A `uvx` that lives in your home directory (`~/.local/bin`) is not on sudo's
`secure_path`, and putting it there with `sudo "$(command -v uvx)"` would run a program that you can change as root. Use
one of two routes instead:

- install uv for root, into a root-owned directory, with the official installer (one line):
  `curl -LsSf https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh`,
  then run the `sudo uvx` commands above;
- or use the root-owned clone below, which needs no uv at all.

From a checkout, use a **root-owned** clone, never one in your home directory:

```
sudo git clone https://github.com/vicennial/motel /usr/local/src/motel && sudo /usr/local/src/motel/install.sh
```

Use `sudo git -C /usr/local/src/motel pull` for later upgrades. The GitHub Action stages such a copy itself
(`/usr/local/src/motel-action`), and the live harness unpacks into a mode 0700 directory in root's home.

## What the installer refuses

Because root copies this tree and later runs it, the installer refuses a source that somebody else can change:

- any directory above the package, or any file in it, that is not owned by root or that group or others can write;
- any symbolic link, device, socket or pipe in the package (a link could pull a secret into the installed copy).

There is no flag to override this. The copy is staged in a private directory and swapped in with renames, so a
failed install leaves the working copy in place, says why in a `Try:` message, and a run that died half way through the
swap restores the old copy on the next run.

An existing `/etc/motel/motel.toml` that does not parse stops every reinstall and upgrade. The installer never
guesses users, providers or cabin settings around a broken config: fix it (`sudo motel-desk doctor`) and run again.

## Cabins and the reaper

`[cabins] enabled = true` installs the cabin user, the key tree, the cabin sudoers lines and a separate cabin
housekeeping unit (its own timer, so a slow rooms pass cannot delay it). `enabled = false` means **no new checkins**:
the cabin reaper and the user verbs that only look at or end a cabin (`cabin ls`, `status`, `checkout`) stay
installed for as long as a `[cabins]` table or any cabin state exists, because a server may still be running and
billing. With cabins off and no `[cabins]` table or cabin state, the installer removes the cabin unit, timer and cron line
(and `motel-desk cabin reap --apply` exits 0 with a note when cabins were never set up). `--no-systemd` installs no timers: schedule `motel-desk reap --apply` and, with cabins,
`motel-desk cabin reap --apply` yourself.
