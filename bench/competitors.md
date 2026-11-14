# Plan: run the same workload on two managed sandboxes

This is a plan, not code. Nothing in this repository calls these services. The goal is a measured comparison to put
next to the list-price one: the same pinned workload ([run_workload.sh](run_workload.sh)) on a cabin and on two
managed sandboxes, with the bill read from each vendor's own usage page.

## Which two, and why

| Vendor | List price read 2026-11-14 | Free credit (same date) | Size limits that matter |
|---|---|---|---|
| E2B, <https://e2b.dev/pricing> | $0.000014 per vCPU-second, $0.0000045 per GiB-second, per second | $100, one time | 1 to 8 vCPU and 1 to 8 GiB. Hobby session cap 1 hour. Pro ($150 a month) 24 hours. |
| Daytona, <https://www.daytona.io/pricing> | $0.0504 per vCPU-hour, $0.0162 per GiB-hour, per second | $200 | Maximum sandbox size is not published. The limits page lists 10 vCPU with 10 or 20 GiB for a Tier 1 organisation and contradicts itself. |

They are the obvious pair: both are per-second sandboxes with the same list rates, so the comparison is between two
products, not two price lists. Both advertise fast cold starts, which is a claim worth testing here too.

Not chosen: Modal (its maximum sandbox size is not confirmed), Fly.io Machines (a VM, closer to a cabin than to a
sandbox), and the hosted CI runners (a different product).

## The fair common denominator

E2B stops at **8 vCPU and 8 GiB**. Hetzner has no 8 vCPU / 8 GiB type: the nearest orderable ones are CPX42
(8 vCPU, 16 GiB, shared) and CCX33 (8 vCPU, 32 GiB, dedicated). So:

- **Primary comparison: 8 vCPU on every platform.** E2B and Daytona at 8 vCPU / 8 GiB; the cabin on CPX42 and,
  as a second line, CCX33. The workload needs far less than 8 GiB, so the RAM gap does not change the speed. It does
  change the price, so compute list prices at `--ram 8` for the vendors and with the cabin's real type:
  `python bench/cost.py --vcpu 8 --ram 8 --minutes <N> --hetzner-type cpx42`. Say in the write-up that Hetzner gives
  more RAM for its price here. That choice makes the vendors look cheaper, not dearer.
- **Optional second point: 4 vCPU / 8 GiB.** This is an exact match of vCPU and RAM on E2B, Daytona and Hetzner CPX32.
  The run takes about twice as long, so it may pass E2B's 1 hour Hobby cap. Do not buy Pro for it. If the cap stops
  a run, publish that.

Neither size is 16 vCPU / 32 GiB. Those larger sizes are not offered on E2B, so the large-machine claims on
[docs/cost.md](../docs/cost.md) stay list-price calculations.

## What the owner must create

- An E2B account and one API key (the dashboard issues it).
- A Daytona account and one API key.
- Nothing new on Hetzner: the cabin setup already exists.

Check at sign-up whether each vendor wants a payment card. Do not add a card unless sign-up refuses to go on without
one, and then set the lowest spend limit the dashboard offers. Keep keys in environment variables on the machine that
drives the runs. Never put them in the repository, in a result file, or in a command line that shell history keeps.

## Expected spend

Estimates at list price, assuming the workload takes 20 to 25 minutes on 8 vCPU (not yet measured):

| Item | Estimate |
|---|---|
| One E2B or Daytona run, 8 vCPU / 8 GiB, 25 minutes | about $0.22 (list rate $0.533 an hour at that size) |
| 3 measured runs, plus 1 smoke run, plus image or template builds, per vendor | about $1 to $3 |
| Both vendors | about $2 to $6, inside the $100 and $200 free credits if the credits apply as listed |
| Cabin, CPX42, 3 runs (one started hour each) | about EUR 0.29 |
| Cabin, CCX33, 3 runs | about EUR 0.57 |

Out of pocket: roughly EUR 1 on Hetzner and nothing on the vendors, if the credits work as listed. Stop rule: kill any
run still going after 90 minutes, delete its sandbox, and look at both vendors' dashboards afterwards for sandboxes
that are still running.

## Steps

The vendors' SDKs and flags change. Read each vendor's current quickstart before you start, and write down which
version of the SDK you used.

1. **Pick one base image for all three platforms**, Ubuntu 24.04, the same as the cabin image. A different compiler
   changes the build time. The result file records `os` and `compiler`, so a mismatch shows up.
2. **Decide how dependencies are installed, once, for all three.** Either every platform runs `apt-get` inside the
   timed script (default; `fetch_seconds` is recorded), or the dependencies are baked into the image everywhere and
   the script runs with `--skip-deps`. The sandboxes may not give you root or sudo; if so, bake them in. Compare
   `build_seconds + test_seconds` (the `--timed-only` figure) for speed, and the vendor's billed amount for cost.
3. **Create the sandbox at 8 vCPU / 8 GiB** with at least 10 GiB of disk. On E2B the CPU and RAM are properties of the
   template, so build a template at that size first. On Daytona set the resources when the sandbox is created. Record
   the exact size the vendor reports back.
4. **Write a driver of about 30 lines per vendor, outside this repository** (or in a separate results repository). It
   does, in order: note the time; create the sandbox; run `true` and note the time again (cold start); upload
   `run_workload.sh`; run `./run_workload.sh --type e2b-8vcpu-8gib` (use a different label for each platform); download
   `result.json`; delete the sandbox; note the time. Give the remote command a timeout of 90 minutes.
5. **Run in rounds, not blocks.** Round 1: cabin, E2B, Daytona. Round 2: the same. Round 3: the same. This spreads
   time-of-day and noisy-neighbour effects across the platforms.
6. **Read the bill from the vendor's own usage page** after each round, not from the driver's arithmetic. Wait for the
   page to update, and record the amount and the time you read it. If the page lists per-sandbox usage, record that
   figure too.
7. **For the cabin:**
   `cd bench && motel cabin run --type cpx42 --for 2h --task bench -- ./run_workload.sh --type cpx42 | tee run-cpx42-1.log`.
   motel prints the cabin's "ready in" time on stderr; copy it as the cold start. Repeat with `ccx33`.
8. **Feed every result through the calculator:** `python bench/cost.py --from-result <file>`. For a cabin it uses the
   type's real size. A sandbox label such as `e2b-8vcpu-8gib` is not a Hetzner type, so the calculator falls back to
   the CPU count and RAM the machine reported. Check that it printed 8 vCPU and 8 GiB. If the RAM rounding is off,
   run `python bench/cost.py --vcpu 8 --ram 8 --minutes <N> --hetzner-type cpx42` by hand. Compare the vendor's row
   with the amount on the vendor's usage page. A gap between the two is a finding. Report it.

## What to record for each run

| Field | Source |
|---|---|
| Platform, size as reported by the platform, region | Vendor dashboard or API |
| Cold start: from the create call to the first command returning | Driver clock |
| `fetch_seconds`, `build_seconds`, `test_seconds`, `wall_seconds`, `status`, `tests_result`, `tests_run` | `result.json` |
| `cpu_model`, `cpus`, `mem_total_kib`, `os`, `compiler` | `result.json` |
| Billed amount, and the time it was read | The vendor's own usage page, as a screenshot or copied text |
| Failures, retries, timeouts, anything unusual | Driver log |

## Fairness rules

1. Same commit: every run uses `run_workload.sh` at one commit of this repository, and the script checks the CPython
   commit itself.
2. Same vCPU count everywhere. Same RAM where the platform offers it; where it does not, say so next to the number.
3. Same base image and the same dependency step on all platforms (step 1 and 2).
4. Three runs per platform and size, at least. Report the median **and every raw value**. No run is dropped, and no run
   is repeated until it looks good.
5. Publish failures: a run that fails, hits a session cap, runs out of memory, or cannot get the size is a result. A
   test failure is a result too, and counts the same on every platform.
6. State what was not controlled: a vCPU is a different slice of a different CPU on each platform, and the CPU model
   is recorded for that reason.
7. Money comes from the vendor's usage page. The calculator's list-price arithmetic is shown beside it, never in
   place of it.
8. Report the run as billed and also the cost per full hour. The workload is shorter than an hour, so Hetzner's
   started-hour rule dominates the as-billed ratio. The honest headline is both numbers.
9. No credits, discounts or promo codes in the headline price. Say where credits were used.
10. Do not run the workload on a sandbox you do not own, and stay inside each vendor's terms of service.
