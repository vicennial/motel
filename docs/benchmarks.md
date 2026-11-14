# Benchmarks: method and results

This page says how cabin costs are measured and what is still missing. The list-price comparison is on
[docs/cost.md](cost.md). Everything needed to repeat a run is in [bench/](../bench/README.md).

## What is measured

One fixed workload: build CPython `v3.13.9` (commit `8183fa5e3f78ca6ab862de7fb8b14f3d929421e0`) with
`--enable-optimizations`, then run its full test suite, both at `-j<CPUs>`. The script is
[bench/run_workload.sh](../bench/run_workload.sh). It was chosen because it is CPU bound, deterministic, needs no
network after the fetch step, has a permissive licence and fits in 8 GiB. The reasons are in the
[bench README](../bench/README.md).

Per run, the script records: the CPU count, RAM, CPU model, OS and compiler; the seconds spent on fetch, build and
tests; the build and test exit statuses; and a status of `ok`, `tests_failed` or `build_failed`.

## How a cost is derived

1. Run the workload on a cabin of a known type. The cabin's cost is the type's list price for the started hours the
   run took, from `bench/prices.toml`. motel's own estimate, which also counts boot time, is printed beside it when
   you pass `--billed-eur`.
2. For every vendor, the calculator prices the same vCPU, RAM and minutes at that vendor's published rate under its
   billing rule (per second, per minute, active CPU). This second column is arithmetic, **not** a run on that vendor.
3. A vendor's real cost is measured only in the competitor runs described in
   [bench/competitors.md](../bench/competitors.md), and is read from the vendor's own usage page.

Two figures are reported for each platform: the cost of the run as billed, and the cost of a full hour of the same
machine. The workload is shorter than an hour, so Hetzner's started-hour rule dominates the first figure and the
ratio to a per-second service is small. The second figure is the one that matters for sustained work. Both are
needed.

## What this does not show

- List prices are not what a customer with a discount, a contract or free credits pays.
- A vCPU on one platform is not the same hardware as on another. The CPU model is recorded; it is not controlled.
- One workload is one workload. A job that waits on a network or a model spends less CPU, and cost per CPU-second
  stops being the right measure for it.
- Boot time, snapshots and isolation are real differences that a build-and-test run does not price.

## Results

**Measured runs: pending.** No measured run has been published yet. The table below is a template and holds no
data. Do not fill it with list-price arithmetic.

| Platform | Size | Run | Cold start | Build + test (s) | Whole script (s) | Status | Billed amount | Source of the amount |
|---|---|---|---|---|---|---|---|---|
| Cabin, CPX42 | 8 vCPU / 16 GiB | 1 | pending | pending | pending | pending | pending | pending |
| Cabin, CPX42 | 8 vCPU / 16 GiB | 2 | pending | pending | pending | pending | pending | pending |
| Cabin, CPX42 | 8 vCPU / 16 GiB | 3 | pending | pending | pending | pending | pending | pending |
| Cabin, CCX33 | 8 vCPU / 32 GiB | 1 to 3 | pending | pending | pending | pending | pending | pending |
| Managed sandbox A | 8 vCPU / 8 GiB | 1 to 3 | pending | pending | pending | pending | pending | pending |
| Managed sandbox B | 8 vCPU / 8 GiB | 1 to 3 | pending | pending | pending | pending | pending | pending |

When runs exist, this page will carry the median and every raw value for each row, the CPU model for each, every
failed or aborted run, and the exact commit of this repository that produced them. Sandbox A and B are named in
[bench/competitors.md](../bench/competitors.md), which also sets the fairness rules.
