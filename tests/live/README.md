# Live test harness

Runs motel against real Hetzner Cloud projects and proves the things the fake provider cannot: real volumes attach, format,
mount, resize and delete; nothing outside motel's own volumes is touched; reboots and crashes recover.

It builds its own fixture (a throwaway server), runs the checks over SSH, and **always** destroys everything it made.

```
operator machine (you)                   ROOMS project (dedicated, empty)
 run_live.py ──hcloud wrapper──────────> server  motel-live-<run-id>   (labelled motel-live=<run-id>)
             ──ssh/scp─────────────────>   the test server = motel's "controller host"
                                           motel installed, user "alice" allowed
                                           bystander volume (pre-existing, labelled, attached)
                                           floors/volumes created by motel itself
                                                |
             ──cabins hcloud wrapper──┐         | `motel cabin ...` runs HERE, on the test server
                                      v         v
                                        CABINS project (second, dedicated, empty; only with --cabins)
                                           cabin servers, firewalls, disks created and deleted by motel
                                           (SSH allowed only from the test server's public address)
```

The word "controller" is motel's: the host that runs `motel-desk`, here the throwaway test server. The machine you run
`run_live.py` from is called the operator machine below. `cabin setup` refuses a project that holds the controller host or a server motel
did not create (`dedicated cabin project required`), so cabins cannot share the rooms project. That rule stays; the harness uses a second project.

## Safety model

- **Dedicated projects only.** The preflight refuses (exit 3) if the rooms project holds *any* server or volume that is not labelled
  `motel-live=<this run id>`. With `--cabins` the cabins project gets its own preflight, before anything is created anywhere: it must hold
  no server, volume, firewall, primary/floating IP or network that is not labelled for this run (SSH keys are allowed; the harness
  never creates anything in that project itself). It also refuses a cabins wrapper that reaches the same project as `--hcloud-cmd`.
  Permanent hosts are never touched because the harness will not start next to them.
- Every resource the harness creates carries `motel-live=<run-id>`. Teardown (`finally`, plus SIGINT/SIGTERM/SIGHUP handlers)
  deletes by that label: the server, the per-run SSH key, the bystander volume, and the volumes motel created on that server
  (`managed-by=motel` **and** a `host=` label naming this run's server). A motel volume that cannot be attributed is left alone and reported.
- Cabins are created by motel, not by the harness, so they carry motel's labels (`managed-by=motel,role=cabin,controller=<test server id>`),
  not `motel-live`. Teardown deletes cabin servers, firewalls, addresses and disks in the cabins project by that controller label. Anything it
  finds there is a motel leak: it is deleted (unless `--no-sweep`) and the run still FAILS.
- `--cleanup RUN_ID` repeats exactly that selection for an earlier run, in **both** projects when `--cabins-hcloud-cmd` is given (it also reads
  `<out-dir>/state.json` for server ids that no longer exist, which is how it finds the cabins of a dead test server). It never touches
  unlabelled resources, another run's, or cabins of another controller. If `state.json` says the run used a cabins project and you give no
  `--cabins-hcloud-cmd`, it says so and exits 4.
- After teardown the inventory of **each** project (servers, volumes, ssh keys, firewalls, primary/floating IPs, networks) must equal its
  initial inventory. Anything extra (for example a cabin remnant, or a cabin that landed in the rooms project) fails the run, is swept
  unless `--no-sweep` is given, and with `--no-sweep` exits 4.
- The tokens are read only to register them for redaction (logs and the report replace them with `[REDACTED]`). Each travels as a file:
  `scp` to a root-owned 0600 file on the test server, `motel-desk [cabin] setup hetzner --token-file`, then `rm`. The rooms token goes to
  `/root/.motel-live-token` for `setup`; the cabins token goes to `/root/.motel-live-cabins-token` for `cabin setup` and is stored by motel in
  its own `cabins.token`, so the two never share a file (the harness checks that `cabins.token` differs from `hetzner.token`, is root 0600, and
  that the temporary file is gone). Neither is on a command line, and the hcloud tokens are whatever your wrappers hold. Identical token files
  are refused (exit 2).
- No shell strings: `--hcloud-cmd`, `--ssh-cmd`, `--scp-cmd` are split with `shlex`; every command is an argv list.

## Run it

Needs: Python 3.11+, `git`, `ssh`, `scp`, `ssh-keygen` on the operator machine; an `hcloud` command (or wrapper) with **write** access to the
rooms test project and a file holding that project's Read & Write API token (the one motel will use on the server). For `--cabins` also a
**second, empty project**: a second wrapper with write access to it (`--cabins-hcloud-cmd`) and a file holding that project's Read & Write token
(`--cabins-token-file`). Both options are required with `--cabins`; if either is missing the cabins phase is skipped with a message (a SKIP in the
report, exit code unaffected).

```
# 1. rehearse: prints the plan and every command, executes nothing, writes nothing
python3 tests/live/run_live.py --dry-run --hcloud-cmd 'WRAPPER ROOMS_ARGS --' --cabins \
        --cabins-hcloud-cmd 'WRAPPER CABINS_ARGS --' --cabins-token-file CABINS_TOKEN_PATH

# 2. rooms only (M0 + M1 checks), about 35-45 minutes
python3 tests/live/run_live.py --hcloud-cmd 'WRAPPER ARGS --' --token-file TOKEN_PATH \
        --ref main --out-dir /path/to/run-output

# 3. rooms and cabins in two projects (cabins add about 60-90 minutes; the idle teardown waits out the 30 minute minimum)
python3 tests/live/run_live.py --hcloud-cmd 'WRAPPER ROOMS_ARGS --' --token-file ROOMS_TOKEN_PATH --ref main \
        --cabins --cabins-hcloud-cmd 'WRAPPER CABINS_ARGS --' --cabins-token-file CABINS_TOKEN_PATH --out-dir /path/to/run-output

# 4. tours (phase 3): implies --cabins, so the same two projects; about 20-30 minutes on top of the setup, four cabins, no model
python3 tests/live/run_live.py --hcloud-cmd 'WRAPPER ROOMS_ARGS --' --token-file ROOMS_TOKEN_PATH --ref main --skip-rooms \
        --tours --cabins-hcloud-cmd 'WRAPPER CABINS_ARGS --' --cabins-token-file CABINS_TOKEN_PATH --out-dir /path/to/run-output

# 5. omnigent (phase 4): implies --cabins, same two projects; one cabin, no model, no key. Add --tours to run both phases
python3 tests/live/run_live.py --hcloud-cmd 'WRAPPER ROOMS_ARGS --' --token-file ROOMS_TOKEN_PATH --ref main --skip-rooms \
        --omnigent --cabins-hcloud-cmd 'WRAPPER CABINS_ARGS --' --cabins-token-file CABINS_TOKEN_PATH --out-dir /path/to/run-output

# if a run died hard (power loss, kill -9) and left resources: sweeps both projects
python3 tests/live/run_live.py --cleanup RUN_ID --hcloud-cmd 'WRAPPER ROOMS_ARGS --' \
        --cabins-hcloud-cmd 'WRAPPER CABINS_ARGS --' [--out-dir /path/to/run-output]
```

`WRAPPER ROOMS_ARGS --` is whatever starts `hcloud` against the rooms test project (for example `hcloud-wrapper my-rooms-project --access write --`);
`WRAPPER CABINS_ARGS --` is the same for the cabins project (`hcloud-wrapper my-cabins-project --access write --`); the harness appends
`server list -o json` and so on. Use `--ref` for the commit to install (`git archive`d from `--repo`, default the
checkout the harness lives in; the ref must contain `install.sh` and `src/`). Put `--out-dir` outside the repository.

| option | default | meaning |
|---|---|---|
| `--location`, `--server-type`, `--image` | `nbg1`, `cx23`, `ubuntu-24.04` | the throwaway server (the type may be retired in a location: pick another) |
| `--min-floor-gb`, `--room-gb`, `--suite-gb`, `--bystander-gb` | 20, 8, 10, 10 | sizes (Hetzner volumes are at least 10 GB) |
| `--skip-rooms` | off | provision + setup only (use with `--cabins`) |
| `--cabins`, `--cabins-skip run,firewall,disk,idle,ttl` | off | phase 2 and which parts to drop |
| `--cabins-hcloud-cmd 'WRAPPER --'`, `--cabins-token-file PATH` | none | the second, dedicated cabins project: its hcloud wrapper and its token file (both required with `--cabins`) |
| `--tours`, `--tours-skip isolation,restart,failure` | off | phase 3 and which parts to drop. `--tours` implies `--cabins` (same two options) and, without an explicit `--cabins`, skips the cabin checks themselves (the cabin setup still runs) |
| `--tours-egress HOST` | `pypi.org` | the one host the tour guests may reach (the operator's `egress_allow` entry; it must answer HTTPS from Hetzner) |
| `--omnigent`, `--omnigent-skip shell,keepalive`, `--omnigent-version V`, `--omnigent-agent NAME` | off, none, `0.21.0`, `polly` | phase 4 and which parts to drop; the Omnigent release to install and pin on the cabin; the built-in agent the managed session binds. `--omnigent` implies `--cabins` (same two options) and, without an explicit `--cabins`, skips the cabin checks themselves |
| `--cabin-type`, `--cabin-idle` | `cx23`, `30m` | cabin server type; `idle_timeout` (cabins refuse less than 30m) |
| `--cabins-config FILE` | built in | replace the `[cabins]` table (`{cabin_type}`, `{idle}` are substituted) |
| `--no-probe` | off | skip the public port 22 probe from the operator machine |
| `--no-sweep` | off | report leftovers at teardown but do not delete them |

Output in `--out-dir` (default `./motel-live-<run-id>/`, mode 0700): `report.json` (every check with PASS/FAIL/SKIP, evidence, seconds,
initial inventory, spend estimate), `log.txt` (every command, redacted), `state.json` (ids for `--cleanup`), `known_hosts`. The per-run
private key is deleted at the end. A summary is printed.

Exit codes: 0 all passed (skips allowed) / 1 a check failed / 2 usage / 3 preflight refused / 4 **teardown incomplete, resources may
still bill** (the message prints the `--cleanup` command) / 130 interrupted (teardown still ran).

Spend: the server is billed per started hour (the estimate multiplies hours by `hcloud server-type describe`'s gross hourly price);
volumes are billed hourly too and are small (about 50 GB for under two hours); cabins add their own server-hours. A full rooms run is
cents.

## What each check proves

Install and setup
- **ship the git ref and run install.sh**: the README path works from a clean Ubuntu image (`install.sh --users alice --provider hetzner`).
- **setup with `--token-file`**: setup discovers the server id and location from the metadata service, binds to the right server, and
  records the pre-existing bystander volume in the receipt as never-touch. The token file is gone from the server afterwards.
- **housekeeping timer**: the systemd timer is enabled by install and the service runs (`reap --apply`); the harness then switches the
  timer off so that it controls when reaping happens.

Rooms (M0 install-to-purge; M1 live checks)
- **doctor ok**, **token file root-only, user cannot read it or run `setup`/`reap` through sudo**, **a user outside `allow_users` is refused**.
- **checkin creates a floor**: one new `managed-by=motel` volume of the minimum floor size, labelled with this server, attached,
  formatted and mounted `nodev,nosuid,noatime`; room dir is `alice` 0700, other users cannot read it; nothing in `/etc/fstab`.
- **ls/status JSON** has the documented shape, capacity matches, missing room exits 7.
- **second checkin packs on the same floor; `--suite` gets its own exact-size floor**.
- **extend** moves the checkout time; over-long checkin/extend are refused with exit 3.
- **caps refusal**: exit 3 and the provider's volume list is unchanged (no provider write).
- **busy refusal**: a process holding a file open in a room makes `checkout --now` exit 4 and lose nothing; after it closes, the purge works.
- **lifecycle walk** with minute-scale `grace`, `lost_and_found`, `retire_after`: dry-run reap changes nothing; active -> grace with data intact;
  **extend in grace** revives; grace -> lost & found (data moved out of the user's reach); **restore**; checkout -> lost & found -> purged, directory gone.
- **interrupted acquire**: the desk is SIGKILLed right after the provider created the volume; the next `reap` reconciles it by label
  (no orphan reported, doctor clean) and the empty floor is retired.
- **reboot**: floors are not mounted after boot (nothing is in fstab); the next `reap` remounts them, data and room state are intact,
  `doctor` is ok and `checkin` works again.
- **re-running setup** keeps `never_touch` to the bystander only (it does not freeze motel's own volumes).
- **checkout everything**: every room is checked out with `--now` (its data goes at once), then `reap --apply` retires every floor, no `managed-by=motel` volume remains at the provider, mount root empty, no orphans. Hetzner bills a volume per started hour, so an empty **shared** floor is held to its release window (checked against that volume's own `created` time with `core.release_bounds`; deleted earlier fails, deleted after its paid hour ends fails) and a `--suite` floor is retired as soon as `retire_after` is over. This waits up to about an hour.
- **bystander untouched**: id, name, size, labels, attachment and protection are identical to what the harness recorded before setup.

Cabins (phase 2, `--cabins`, in the second project; skipped cleanly if the ref has no cabins or `cabin setup` cannot take a token file)
Every `motel cabin ...` command runs over ssh **on the test server**, because the cabin firewall admits only that server's address. Two
connection checks make the direction explicit; the second is the only thing in the phase that runs on the operator machine.
- **setup**: the cabins token (not the rooms one) is copied root 0600, `motel-desk cabin setup hetzner --token-file` accepts the dedicated
  project, the temporary token file is gone, `cabins.token` is root 0600 and differs from the rooms `hetzner.token`, doctor is ok. The cabins
  wrapper must not see the test server.
- `cabin run -- uname -a` output and exit code pass through; `sh -c 'exit 7'` gives 7; an ephemeral run leaves nothing behind.
- **firewall**: every inbound source is a single host route, the IPv4 source set is exactly the test server's `/32`, port 22 is the only
  inbound rule, the view and the provider agree, and the firewall is applied to the cabin.
- **reachable from the test server**: a TCP connect to the cabin's port 22, made over ssh from the test server, succeeds (the allowlist works).
- **closed to the operator machine**: a TCP connect to the same address from the machine running `run_live.py` (not allowlisted) fails.
- **`--disk`** attaches exactly one new volume to the cabin and the checkout below removes it. The firewall, reachable, closed and `--disk`
  checks share **one** cabin (created with `--disk 10G` unless `disk` is skipped), so a pass takes one cabin for all four; TTL, idle,
  ephemeral-run and checkout-cleanup keep their own.
- **TTL and idle teardown** (run together to save time): a 5m-TTL cabin is hard-deleted by reap; the TTL cabin is not held for its paid hour. An idle cabin is sealed at `idle_timeout` but, because Hetzner bills every started hour, deleted only in its release window (the last 10 minutes before the 6 minute margin ahead of the end of its billed hour, so minute 44 to 54 for the default 30m); the harness reaps every 60 seconds and the product releases from just after minute 43 when the next pass would come too late, so the earliest accepted delete is `opens - 1 minute` (`core.release_bounds`); the check fails if it is deleted before that or after the hour ends. It waits up to about an hour.
- **nothing left**: no extra server, firewall, primary IP or volume remains in the cabins project, and none of them appeared in the
  rooms project (a cabin created in the wrong project is reported as "ROOMS project (wrong place)").

Tours (phase 3, `--tours`; implies the cabins setup, in the same second project)
Experimental `motel tour ...` on the real server as real root. No model is called and no real key exists: guests use the `command` adapter
(`argv`, `secrets = []`, `budget_usd = 0`), and the one guest that needs a concierge listener holds a **fake** key
(`sk-ant-LIVEFAKE-<hex>`, written to `/etc/motel/llm-keys/anthropic.key`, registered with the redactor) that the concierge would only
send to an upstream if a request reached it, and none does. Two tours, **four cabins** in all (a started hour each, cheapest type):
`live-main` (guests `alpha`, `bravo`: both commit a file) and `live-failures` (`fails` exits 3, `hangs` sleeps).

- **install**: `install.sh --cabins --tours --upgrade` after `[cabins]` and `[tours]` are in `motel.toml`; the `motel-inspect` user exists, is
  a system uid with no login shell, and `/var/lib/motel-inspect` is `root:root 0711`; both concierge units are active and
  `systemd-analyze verify` accepts them (a misspelled key or missing file fails it); the running service process, read from
  `/proc/<MainPID>/environ`, sees the marker `MOTEL_FDSTORE_MAX=512` and, on systemd 254 or newer, `$FDSTORE=512`, `MOTEL_FDSTORE_SIGNAL=1`,
  and `FileDescriptorStorePreserve=yes` (what the unit file says is not enough: systemd must have honoured it); `motel doctor` is ok.
- **a tour, no model**: `tour start` of two guests (two cabins at once); `tour wait` succeeds; both guests are `done` with cleanup `deleted`;
  `tour result` gives a verified bundle digest and size that equal `sha256sum`/`stat` of the stored bundle (read as root), a 40-hex head, and the
  changed files; `tour fetch` imports branch `motel/<tour>/<guest>` into the test repository at the verified head, with the guest's content
  and only its file changed, and does not move the checked-out branch.
- **inspection as `motel-inspect` under prlimit**: a transient unit samples the processes of that user every 20 ms (command line and
  `/proc/<pid>/limits`, because `prlimit` execs git). The evidence is git running as `motel-inspect` with `-c core.hooksPath=/dev/null` and
  finite address-space and process limits. A short git run could be missed; that fails the check loudly rather than passing it.
- **guest isolation, from inside the guest with commands only**: `example.com`, `api.anthropic.com` and `1.1.1.1` give no HTTP answer; the
  allowlisted host does; the placeholder token is refused by the concierge for a host it does not know (`/u/evil.example/...`), for another
  binding, for a path outside the binding (all 403) and a wrong token gets 401; the concierge counts at least three refusals for the guest; the fake
  key is not in the guest's environment, in any process's environment or command line, or in any file under `/etc /home /var /opt /srv /root /tmp /usr/local`.
  No probe ever sends the valid token on a valid path, so nothing reaches an upstream.
- **concierge restart**: while `alpha` runs (it asks the concierge every 10 s for four minutes) `systemctl restart motel-concierge` runs; the
  service pid changes, alpha is still `done` at the end, every probe before and after the restart (outside a 3 s window around it) was refused
  by the concierge with `motel_path_denied` (a `000` would mean the listener was lost), and `lost_listeners` is empty (the fc7fda6 fd store).
- **failure paths**: a guest that exits 3 is reported `failed` with the runner's exit code 3 and its cabin (label `lease=...`) is gone;
  `tour cancel` while the other guest runs ends it `cancelled` and no cabin, firewall, address or volume of the tour remains.
- **cost**: the phase counts the cabin servers it saw in the cabins project (first seen to gone, from the provider's `created` time), prints
  billed hours per cabin and in `report.json` under `tours_cabins`, and fails if the count is not the planned four (a retry or a stray cabin).

Omnigent (phase 4, `--omnigent`; implies the cabins setup, in the same second project)
The Omnigent sandbox provider (`docs/omnigent.md`) against one real cabin. Everything runs on the test server over the harness's ssh, as the
unprivileged cabin user; nothing runs on the operator's machine, and no model is called and no key exists (the server is started with a clean
environment: the check reads the variable *names* of its process and fails on any key, token or secret). **One cabin**, a started hour, cheapest type.

- **install**: apt `python3-venv`; the shipped ref is copied (the unpacked ref is root-only) and `pip install omnigent==VERSION <ref>` runs into a venv
  as the cabin user (this installs the provider files next to Omnigent's own); `omnigent sandbox create --help` lists `motel`.
- **a disposable server**: `omnigent server` on `127.0.0.1` and a kernel-picked free port (never 6767), with an explicit `--port` and `--database-uri`
  (a dedicated server that never consults a shared pid file), a throwaway `HOME` and `OMNIGENT_HARNESS_TMP_PARENT` under one directory, and the config
  `sandbox.provider: motel`, loopback `server_url`, `tunnel: auto`, `ttl: 20m`, `harness_packages: []`. `/health` answers and `/v1/info` lists `motel`.
- **provision**: Omnigent 0.21.0 has no standalone call that makes a managed host (and `omnigent sandbox create` needs an Omnigent source checkout), so
  the harness makes the server provision the way the web UI does: `POST /v1/sessions` with `host_type: managed` and `sandbox_provider: motel` for a
  built-in agent (`polly`, else the first listed; `--omnigent-agent` picks one). The create returns at once and the server checks the cabin in through
  the provider; the check passes when exactly one live cabin appears in `motel cabin ls` (the session's `sandbox_status` stage is logged on the way and
  a `failed` stage fails the check with the server's reason).
- **registration**: `GET /v1/hosts` lists a host with `sandbox_provider: motel` and status `online`. The cabin's firewall reaches only the public internet,
  so a host that dials the loopback `server_url` can only have come through the provider's relay (the server log line that announces the relay is counted
  as evidence).
- **a command in the sandbox**: the provider's own `run` in a second process of the venv (`MotelSandboxLauncher.run` on the live cabin returns the
  command's output), then the session's shell endpoint (`POST /v1/sessions/ID/resources/environments/ENV/shell`) through the server, the host and the
  runner. The second needs the session's runner to start on a bare cabin, which depends on the agent: if it fails for that reason, rerun with
  `--omnigent-skip shell` (the diagnostics say why).
- **keep_alive**: the provider's `keep_alive` in a second process (asking for `1h` on a `20m` lease) moves the checkout time (`expires_at` of
  `motel cabin status`) forward by at least 10 minutes. (A keep-alive that asks for the lease length it already has moves it by less than the
  5 minute minimum, which motel treats as success, so the probe asks for more.)
- **terminate**: `DELETE /v1/sessions/ID` is how the server ends a managed sandbox (it calls the launcher's `terminate`); the cabin must then reach
  `released` (or be gone). A cabin that stays `active` or `cleanup_pending` fails the check.
- **teardown, always**: the session is deleted, the server stopped by its recorded process group (the pid it wrote itself, checked against its command
  line before any signal; nothing else is signalled), any cabin still live is checked out and both projects must return to their initial inventory;
  the cost check counts the cabin servers it saw (first seen to gone) and fails unless it is exactly one. The server must stop answering.
- **on a failure**: scrubbed diagnostics in `<out-dir>/omnigent-diagnostics/NN-<check>/`: the server and provider log tail and the install log, the
  `/v1/info`, `/v1/hosts` and session documents, `motel cabin ls` and `status`, the cabin-side logs (`/tmp/omnigent-host.log`,
  `/tmp/omnigent-harness-install.log`) of every live cabin, the cabins project's servers and the phase's timeline. Known secrets, key shapes and
  `NAME_TOKEN=value` assignments are replaced.

Teardown: harness-owned resources deleted, no cabin remnants in the cabins project, final inventory of **each** project equals its initial
inventory, spend estimate.

## Unit tests for the harness

`test_harness.py` runs the whole flow against fake `hcloud`, `ssh`, `scp` and `ssh-keygen` scripts on a temporary `PATH`
(`fakes/fakeworld.py`: two fake cloud projects, `hcloud` for the rooms project and a `hcloud-cabins` wrapper for the cabins project, plus a
toy motel whose cabin setup applies motel's dedicated-project rule and whose cabins live in the cabins project). No network, no root.

```
python3 -m unittest discover -s tests/live -p 'test_harness.py' -v      # or: pytest tests/live
```

They assert: the full rooms scenario passes in the fake world; preflight refusal creates and deletes nothing; teardown after a
provisioning failure, a setup failure, `KeyboardInterrupt` and SIGTERM; a failed delete exits 4 and `--cleanup` finishes it;
leaked resources are flagged and swept (or kept with `--no-sweep`); `--cleanup` deletes only what is labelled for the run and leaves
unlabelled and other-run resources alone; every `create` carries the run label; the token appears nowhere in logs, report, console
output or hcloud arguments (the fake desk deliberately echoes both tokens); dry-run touches nothing; the cabins phase skips cleanly when
its options are missing or cabin setup cannot take a token file. For two projects: a full rooms-and-cabins run passes with both inventories
back to their initial state, each token reaches only its own setup, cabin commands run over ssh on the test server and only the negative
probe runs locally, the cabins preflight refuses a non-empty project (exit 3, nothing created), the rooms token in the cabins slot fails at
`cabin setup` (rc 6), a cabin created in the wrong project or a remnant left in the cabins project fails the run (and is swept, or exits 4
with `--no-sweep`), and `--cleanup` sweeps both projects without touching other controllers' cabins or unrelated servers.

For tours the toy motel also simulates `motel tour ...` with timelines (guests book for 15 s, then run for a fixed time, in the fake world's
virtual time), the concierge units and a restart, `systemd-analyze`, `id`, `getent`, `date`, a repository and its branches, and a sampler unit. The tests
assert: a full run passes with exactly four cabins (each at least one billed hour, none left) and the fake key in no log, report or console; `--tours` alone
runs the cabins setup but not the cabin checks, and `--cabins --tours` runs both; `--tours-skip` drops the checks and the cabins of the skipped parts; missing
cabins options skip the phase; an older systemd needs no `$FDSTORE`; and each injected fault (a bad unit, no store, a bad inspect dir, inspection as root or
without prlimit, a bundle that does not match its digest, a lost listener, an open egress, the key on the guest, a cabin left after cancel, a failed guest
reported done, a fifth cabin) fails exactly the check that proves it while the teardown still leaves both projects clean. The `[tours]` fragment and the
tour spec are also parsed by the real motel config and spec code when the checkout is importable.

For omnigent the toy world also simulates the server (`/health`, `/v1/info`, `/v1/agents`, `/v1/sessions` with a managed launch that creates one
cabin and a host that registers after 20 virtual seconds, `/v1/hosts`, the shell endpoint, the session delete that checks the cabin out), the venv install, the
provider in a second process (`run`, `keep_alive`) and `motel cabin status|extend|ssh`. The tests assert: a full run passes with exactly one cabin and a stopped
server, a throwaway HOME and `OMNIGENT_HARNESS_TMP_PARENT`, an unprivileged port that is not 6767, and no local `omnigent` command (a stand-in on the operator's
`PATH` would record one); a host that never registers fails that check, writes scrubbed diagnostics and still deletes everything; a launch that fails is
reported with the server's reason; an interrupt or SIGTERM mid-phase deletes the session, stops the server and removes the cabin; each injected fault (a failed
install, a missing provider, a key in the server's environment, a rejected session, a second cabin, a keep-alive that does not extend, a terminate that leaves
the cabin active, a session with no runner, a server that will not stop) fails exactly the check that proves it while the teardown still leaves both projects
clean. The start and stop scripts also run for real against a stand-in server (the whole process group goes, a pid that is not the server's is left alone).

`test_live_pass.py` is the real run as a test; `conftest.py` never collects it unless `MOTEL_LIVE=1` (and then it needs `MOTEL_LIVE_ARGS`).
The repository CI (`python -m unittest discover -s tests`) does not descend into this directory.

## Known gaps

- The fake world checks the harness logic, not motel or Hetzner. The first live pass will probably expose JSON-shape or timing
  differences (hcloud field names such as `applied_to`, `source_ips`; `hcloud server create` blocking behaviour; cx22 availability).
- Interrupted acquire kills the desk once a new `managed-by=motel` volume is visible. That is *after* the provider create, not in the
  tiny window before it; the "lost create response" path (intent without a volume) is covered only by motel's own unit tests.
  If the checkin finishes first twice, the check SKIPs.
- No `systemd` timer firing check (the harness runs the service once by hand), no XFS/quota check, no multi-user contention.
- The host key is trusted on first use into a run-local `known_hosts`; the throwaway server is reachable only with the per-run key.
- Cabin parts that need a tailnet (`access = "tailscale"`) are not covered. If the ref's `cabin setup` cannot take `--token-file`
  (it exits 2), the cabins phase skips at its first check.
- The cabins project must be empty at the start apart from SSH keys; motel itself also refuses a cabin project that holds anything it did not create.
- `--cleanup` finds the cabins of a run only through `<out-dir>/state.json` (the test server's id, which cabins carry as their `controller` label);
  without it, cabins of a vanished test server are listed as "not provably this run's" and left alone.
- Spend estimate covers the test server only; the tours phase reports its own cabins (billed hours, estimate) under `tours_cabins` in `report.json`.
- Omnigent: the live behaviour is untested until the first pass. Known soft spots: the managed session needs an agent whose runner can start on a bare
  cabin with `harness_packages: []` (if the server rolls the launch back, the registration or shell check fails with the server's reason; try
  `--omnigent-agent`, or `--omnigent-skip shell`); the host registers only after apt and pip have run on the cabin (a few minutes, 20 minute budget); the
  API shapes (`/v1/hosts`, `sandbox_status`, the shell reply) were read from Omnigent 0.21.0's source, not observed.
- Tours: the live behaviour is untested until the first pass. Known soft spots: the egress host must answer HTTPS from the guest (a DNS or address change
  there fails the allowlist check, not the filter); the 20 ms process sampler may miss a very short git run; the restart judges probes outside a 3 s
  window, so a larger clock skew between the cabin and the test server could blur it; the number of descriptors systemd holds right after the restart is
  noted, not judged.
