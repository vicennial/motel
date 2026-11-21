# What cabins cost, and when a managed sandbox is cheaper

A cabin is a Hetzner Cloud server in your own account. Hetzner bills it per started hour. Managed sandbox services
bill per second but charge more per hour. The table below compares list prices read on 2026-11-14. For 3-run measured
CPython build-and-test timings across Hetzner CPX42, E2B and Daytona (2.4× cheaper per round as-billed on E2B, 3.7×
if the cabin's billed hour is filled), see [docs/benchmarks.md](benchmarks.md).

At list prices, for sustained 8 to 16 vCPU work, a Hetzner orderable type costs about 5x to 8x less than a
per-second sandbox, and the gap shrinks or reverses for short runs.

| List price, USD | A: one machine, 8 vCPU / 16 GiB, 1 hour | B: 8 vCPU / 16 GiB, 8 h a day, 22 days (one fresh server per day) | C: 10 parallel machines, 4 vCPU / 8 GiB, 45 minutes each | D: one machine, 16 vCPU / 32 GiB, always on for a 730 h month |
|---|---|---|---|---|
| Hetzner orderable type, HEL1 | CPX42 $0.105 | CPX42 $18.45 | CPX32 $0.538 | CPX62 $122.09 |
| Hetzner, NBG1 (hourly estimated) | $0.123 | $21.71 | $0.635 | $143.54 |
| Daytona sandbox* | $0.662 (6.3x) | $116.58 (6.3x) | $2.48 (4.6x) | $967.10 (7.9x) |
| E2B sandbox | not offered | not offered | $2.48 (4.6x) | not offered |
| Modal Sandboxes* | $0.952 (9.1x) | $167.52 (9.1x) | $3.57 (6.6x) | $1,389.69 (11.4x) |
| Fly.io Machines (performance, Ashburn) | $0.367 (3.5x) | $64.54 (3.5x) | $1.37 (2.6x) | $535.38 (4.4x) |
| Vercel Sandbox (Pro, iad1) | $1.36 (13.0x) | $239.92 (13.0x) | $5.11 (9.5x) | not offered |

Each cell is the list-price cost of the whole workload in USD, with the ratio to the Hetzner HEL1 row in brackets.
Workload B and D are monthly, and plan fees are not in the table. Free credits and included allowances are not
deducted. `not offered` means the size or the session length is over the vendor's published limit.

- Hetzner prices exclude VAT and include the primary IPv4 (EUR 0.50 a month). EUR is converted at 1.10 USD, an
  assumption: the rate Hetzner's block-storage page implies, not a market rate.
- The hourly price on hetzner.com is read as the HEL1 price. The pages do not say, but hourly times 624 matches the
  HEL1 monthly price for every type. NBG1 hourly prices are not published; the NBG1 row scales the HEL1 hourly price
  by the monthly ratio.
- Hetzner bills every started hour, and never more than the monthly price for one server.
- Workload C is 10 machines for 45 minutes: 7.5 machine-hours used, 10 billed by Hetzner. It is about $2 of
  difference in total, so a large ratio there is a small amount of money.
- Fly.io Machines is the closest to Hetzner (3.5x to 4.4x). Modal and Vercel are further away.
- \* Unverified. Daytona does not publish a maximum sandbox size, so we could not confirm that it offers 16 vCPU / 32 GiB.
  Modal's maximum is not confirmed either. `bench/prices.toml` lists every unverified input.

Sources, all read on 2026-11-14 (Vercel's page is dated 2026-09-10):
Hetzner: <https://www.hetzner.com/cloud/regular-performance>, <https://www.hetzner.com/cloud/general-purpose>,
<https://www.hetzner.com/cloud/cost-optimized>, billing rules <https://docs.hetzner.com/cloud/billing/faq/>.
Daytona: <https://www.daytona.io/pricing> ($0.0504 per vCPU-hour, $0.0162 per GiB-hour).
E2B: <https://e2b.dev/pricing> ($0.000014 per vCPU-second, $0.0000045 per GiB-second; 1 to 8 vCPU, 1 to 8 GiB).
Modal: <https://modal.com/pricing>. Fly.io Machines: <https://fly.io/pricing.md>.
Vercel Sandbox: <https://vercel.com/docs/sandbox/pricing> (active CPU billed at 100% use in this table).

## Prices moved in 2026

Hetzner raised prices on 15 June 2026. The shared CPX and dedicated CCX lines went up by about 144% to 176% on the
types we sampled. The date and size come from a search summary of Hetzner's price-adjustment page and a third-party
write-up; we could not open Hetzner's page, so treat them as unverified. On 2026-11-14 every type in the cheap CX line
carried the "not available" class on hetzner.com. CX43, the 8 vCPU / 16 GiB type, would be $0.029 an hour at its listed
price, and the gap to a sandbox would then be about 23x. We do not use it, because nobody can order it. Any
comparison made before June 2026, or one that used the CX line, is stale.

## Where a managed sandbox is cheaper or better

- **Runs under about 10 minutes.** Hetzner bills the whole started hour. For 8 vCPU / 16 GiB, one hour costs $0.105
  on CPX42. A per-second sandbox at the Daytona or E2B list rate costs the same at 9.5 minutes (11 minutes against the
  NBG1 estimate). A 5 minute run costs about $0.055 there.
- **Swarms of short jobs.** Each machine pays for a started hour, so 45 minute jobs waste a quarter of the hour.
- **Idle-heavy sessions on active-CPU billing.** Some services bill CPU only while it is busy. Vercel Sandbox at
  10% CPU use costs $0.44 an hour for 8 vCPU / 16 GiB at list price. That is 4.2x CPX42, not 6x or more, and the gap is
  smaller still on small types.
- **Cold starts in milliseconds.** Vendors advertise start times from tens to a few hundred milliseconds. We have not
  measured them. A new Hetzner server takes tens of seconds to boot.
- **Snapshot, fork, pause and resume** of a running machine's memory state, which a plain server does not do.
- **Isolation built for untrusted code**, and no operations work: no patching, firewall rules, IP management or
  capacity planning. A Hetzner location can run out of a type, as the CX line shows.
- **Free credits** that cover small usage completely. Daytona lists $200 and E2B lists $100 (pages above, read
  2026-11-14).

If you run few sandbox-hours a month, the cheaper hour does not matter and these points decide. Hetzner's saving
shows up on long, sustained, large jobs.

## Check it yourself

Everything above comes from [bench/prices.toml](../bench/prices.toml) and [bench/cost.py](../bench/cost.py):

```
python bench/cost.py --workload A     # also B, C, D
python bench/cost.py --vcpu 8 --ram 16 --minutes 60
```

[bench/README.md](../bench/README.md) has a fixed workload you can run on a cabin and on any other machine, and
turns your own timing into the same table. A test in `tests/bench/` fails if the table on this page drifts from the
calculator.
