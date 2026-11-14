# bench: check the cost claim yourself

This directory lets you reproduce the numbers on [docs/cost.md](../docs/cost.md) and put your own measurement
beside them. It has no secrets and calls no vendor. Cost numbers are list prices. Timings come from you running the
workload.

| File | What it does |
|---|---|
| `prices.toml` | List prices with vendor, unit, billing rule, limits, source URL, date read, and a `verified` flag. The EUR to USD rate is stated once, as an assumption. |
| `cost.py` | Calculator. Prices one workload under each vendor's real billing rule. Standard library only, Python 3.11 or newer. |
| `run_workload.sh` | A real, fixed workload for a cabin or any other machine. Writes a JSON result. |
| `competitors.md` | Plan for running the same workload on two managed sandboxes. A plan only: nothing here calls them. |

Method and the (still empty) results table are in [docs/benchmarks.md](../docs/benchmarks.md).

## 1. Price a workload

```
python bench/cost.py --vcpu 8 --ram 16 --minutes 60
python bench/cost.py --vcpu 4 --ram 8 --minutes 45 --count 10
python bench/cost.py --vcpu 8 --ram 16 --minutes 60 --cpu-util 0.1     # idle-heavy session, for active-CPU billing
python bench/cost.py --workload A                                      # workloads A to D from docs/cost.md
python bench/cost.py --vcpu 8 --ram 16 --minutes 60 --json
```

For each vendor the table shows the cost under that vendor's own rule (per second, per minute, started hours, or
active CPU), the size it billed, the ratio against the nearest orderable Hetzner type, and the monthly plan fee
beside the cost. It prints `not offered` where the size or the session length is over the vendor's limit. Below the
table it lists every unverified input (rows marked `*`) and the break-even duration: the run length below which a
per-second vendor is cheaper than one started Hetzner hour.

Useful flags: `--location nbg1` (Hetzner hourly prices there are estimates), `--hetzner-type cpx42`,
`--hypothetical` (price a vendor even when its limits do not allow the size, and mark the row), `--monthly`
(add plan fees to a total), `--only daytona,e2b`.

## 2. The workload

`run_workload.sh` clones CPython at tag `v3.13.9` (commit `8183fa5e3f78ca6ab862de7fb8b14f3d929421e0`), builds it
with `./configure --enable-optimizations` and `make -j<CPUs>`, then runs its whole test suite with
`python -m test -j<CPUs>`. It refuses to run if the tag does not point at that commit.

Why this one:

- **CPU bound and parallel.** Compiling C and running a test suite use the CPU, not the network or the disk. The
  build and the test run both scale with the CPU count. The profile-guided build includes one serial profile run of
  a few minutes, so average CPU use is below 100% on 8 vCPU. That is true of real builds, and every machine pays for
  all its vCPUs for the whole time either way.
- **Deterministic input.** A pinned commit and a fixed command line. The compiler comes from the image, so the
  result file records the OS and the compiler version.
- **No network after the fetch step.** `apt-get` and `git clone` run first and are timed separately
  (`fetch_seconds`). The build and tests need no network and no secrets.
- **Permissive licence, well known.** CPython is under the PSF licence, which is permissive. Anyone can inspect it.
- **Expected length: about 15 to 25 minutes on 8 vCPU.** This is an estimate from the size of the build, not a
  measurement. The first live run replaces this sentence. A run this long also makes the billing rules matter:
  a 25 minute run on Hetzner pays a whole started hour.
- **Small memory.** It fits in 8 GiB, so it can run on sandboxes that cap RAM at 8 GiB.

Considered and rejected: a full LLVM build (hours, and more than 8 GiB of RAM at `-j8`), the Linux kernel (GPL, and no
self-contained test suite), and Rust or Go tools that run for under 10 minutes or fetch from the network during their tests.

Test failures do not stop the recording. The build exit status and the test exit status are stored separately, and
`status` is one of `ok`, `tests_failed`, `build_failed` or `dry_run`. A run on a cabin with IPv6 disabled or a
firewalled network may fail a few tests. Publish those runs as they are.

## 3. Run it on a cabin

You need a working cabin setup ([docs/cabins.md](../docs/cabins.md)) whose `[cabins] types` allows the type you
pick, and a price cap that allows it. `motel cabin run` syncs the current git checkout to `~/work` on the cabin and
runs the command there, so run it from this directory:

```
cd bench
motel cabin run --type cpx42 --for 2h --task bench -- ./run_workload.sh --type cpx42 | tee run.log
```

`--type` for motel picks the server type. `--type` for the script is only a label stored in the result. Use the same
string for both. Good first picks, both 8 vCPU: `cpx42` (shared, 16 GiB) and `ccx33` (dedicated vCPU, 32 GiB).

The command's stdout comes back to you, and the script prints one line that starts with `MOTEL_BENCH_RESULT`
followed by the JSON. `tee` keeps it in `run.log`. The cabin is deleted at checkout, so its `result.json` is gone;
the log line is your copy. To keep the file instead, use `--keep` and copy it out before you check out:

```
motel cabin run --type cpx42 --for 2h --keep -- ./run_workload.sh --type cpx42
motel cabin ssh <cabin-id> -- cat work/result.json > result.json
motel cabin checkout <cabin-id>
```

With `motel cabin run --json` the last stdout line is motel's own summary, including `cost_estimate_net_eur`. Pass
that number to the calculator with `--billed-eur` to print it beside the list price.

## 4. Run it on any other machine

On Debian or Ubuntu with sudo, from this directory:

```
./run_workload.sh --type my-laptop
```

Without apt or sudo, install a C toolchain, git, and the `-dev` packages for OpenSSL, zlib, bzip2, readline,
SQLite, ncurses, libffi, xz, uuid, gdbm and zstd first, then add `--skip-deps`. The script deletes only a work
directory (`./bench-work` by default) that it created itself.

Check the recording path in a second, without building anything: `./run_workload.sh --dry-run`.

## 5. Turn the result into a cost comparison

```
python cost.py --from-result run.log          # or result.json
python cost.py --from-result run.log --billed-eur 0.0953
python cost.py --from-result run.log --timed-only    # build and tests only, without apt and clone time
```

The output starts with sentences of this form (values come from your file):

> This run took N minutes on cpx42.
> At Hetzner list price that is K started hour(s) of CPX42 (HEL1): EUR X, USD X.
> The same vCPU, RAM and minutes at each vendor's list price are in the table below.
> This is a list-price calculation, not a run on those vendors.

and then the usual table. Read the second column as arithmetic: "if a machine with this many vCPUs and this much RAM
ran for the same minutes at that vendor's published rate". It is not a measurement on that vendor. Their CPUs may be
faster or slower, so the real minutes there could differ. To measure instead, follow [competitors.md](competitors.md).

The minutes are the whole script by default, including `apt-get` and `git clone`, because a cabin bills for them. The
list-price cost for Hetzner counts started hours of the type you ran; motel's own bill also includes boot time.

## Updating prices

Edit `prices.toml` and keep the source URL and `read_date` in step with what you read. Keep `verified = false`, with
the reason in `unverified_items`, for anything you could not confirm from the vendor's page. The tests in
`tests/bench/` re-derive the four workloads from the research note and fail if a rate or a rule drifts. Run them with:

```
uv run --extra dev pytest tests/bench -q
```
