# Providers

A provider adapts one cloud (or loop files) to the small interface in
[`providers/base.py`](../src/motel/providers/base.py): `whoami`, `visible`, `price_gb_month`, `get_volume`,
`create_volume`, `attach`, `detach`, `resize`, `delete`, `device_path`, `expected_serial` and `list_owned`.
Providers make no policy decisions: caps, the allowlist guard, locking and lifecycle live in the desk.

## Which clouds

`[host] provider` takes one of these names. Beta means written against the API documentation and a fake API;
nobody has run it against a real account yet, so treat the first live run as a supervised test on a scratch project.

| `provider =` | Cloud | Status | Credentials | Volumes arrive | Price list |
|---|---|---|---|---|---|
| `hetzner` | Hetzner Cloud | primary, live-verified | token, `credentials/hetzner.token` | formatted (ext4) | EUR |
| `digitalocean` | DigitalOcean Volumes | **beta** | token, `credentials/digitalocean.token` | blank | USD, `rooms.eur_per_usd` |
| `linode` | Linode Block Storage | **beta** | token, `credentials/linode.token` | blank | USD, `rooms.eur_per_usd` |
| `vultr` | Vultr Block Storage | **beta** | token, `credentials/vultr.token`; `[provider] block_type` | blank | USD, `rooms.eur_per_usd` |
| `scaleway` | Scaleway Block Storage | **beta** | token, `credentials/scaleway.token` | blank | EUR |
| `aws` | AWS EBS | **beta** | instance role (or `credentials/aws.credentials`) | blank | USD, read as the `_eur` amounts |
| `gcp` | Google Persistent Disk | **beta** | VM service account | blank | USD, read as the `_eur` amounts |
| `azure` | Azure Managed Disks | **beta** | VM managed identity; `[provider] sku`, `client_id` | blank | EUR, billed in tiers |
| `local` | loop files | for testing | none | formatted | none |

`sudo motel-desk setup <name>` asks for the token on a terminal (hidden), or reads `--token-file PATH`; clouds
that identify the machine themselves ask for nothing. Each page under [`providers/`](providers/) lists what is
unverified. Cabins run on Hetzner Cloud whatever the room provider is.

**Blank volumes.** The desk makes the ext4 filesystem itself on a new volume from a cloud that hands out blank
devices (`formats_on_create = False`), and only after positive proof that it is the right device and empty:

- the serial matches (where the cloud has one);
- nothing mounts it and nothing is stacked on it (`/sys/class/block/<dev>/holders` is empty);
- `blkid -p` reads it and finds no signature of any kind (a read error is a refusal, not "blank");
- an exclusive open reads the first and last 4 MiB as all zeros (any read error is a refusal);
- its size is the volume's, and the serial and size are checked again right before `mkfs.ext4` (the exclusive
  descriptor cannot stay open across mkfs, because mke2fs opens the device exclusively itself).

Any doubt is a refusal (exit 6). **Assumption:** a new cloud volume reads as zeros. A cloud that does not zero its
volumes fails closed, with a hint to check the device and format it by hand. A device that already holds the ext4
filesystem this desk made (label `motel-<floor id>`) is left alone, also when it is mounted at the floor's own
mount point (an earlier run died after mounting). A cloud with no device serial (Azure) is also checked by that
label on every mount. Before a grow the guest is rescanned until the device reaches the smallest size the formatting
accepted; a grow that cannot finish never keeps an empty floor from being retired.

```toml
[host]
provider = "vultr"

[provider]               # optional, only the keys that cloud understands
block_type = "storage_opt"      # vultr: "high_perf" (default) or "storage_opt"
# sku = "StandardSSD_LRS"       # azure
# client_id = "..."             # azure: a user-assigned identity

[rooms]
eur_per_usd = 0.92       # clouds with a dollar price list; default 1, which overestimates the euro cost
```

Server and never-touch ids may contain letters, digits, `.`, `_` and `-` (UUIDs, `i-0abc...`, VM names).

## Hetzner Cloud

- `setup hetzner` reads this server's id and location from `http://169.254.169.254/hetzner/v1/metadata`,
  asks for a Read & Write project token on a terminal with echo off (never from a pipe or argument; for automation
  `--token-file PATH` accepts a root-owned, non-symlink, regular file of mode 0600 or stricter),
  checks that the token can see this server, then records **every volume already in the project** as
  never-touch in `/etc/motel/setup-receipt.json`. Re-running setup keeps old entries and adds new ones, and
  never freezes volumes motel itself created.
- Volumes are created with `automount=false` and `format=ext4`, attached in the same call, in the server's
  location, labelled `managed-by=motel`, `role=floor`, `host=<server id>`, `floor=<floor id>`, and named
  `motel-<floor id>-<suffix>`. The device is `/dev/disk/by-id/scsi-0HC_Volume_<id>`; its serial must equal the id.
- Reads retry on 429/5xx; writes retry only on 429. A create is never retried after a lost response:
  the floor record stays as an intent and is reconciled by label and name.
- Redirects are refused, so the token cannot follow one to another host.
- The price comes from `/pricing` (gross) unless `rooms.price_gb_month_eur` is set.
- Limit: the token sees the whole project. Hetzner only attaches volumes inside the server's project, so
  motel cannot ask for a narrower token. The guard is what keeps it away from other resources.
- **Live-verified** by `tests/live/run_live.py` against real Hetzner projects: create/attach in one call, online
  `resize2fs`, the device serial check, delete after detach, reboot recovery and the full room and cabin lifecycle. The
  harness is the checklist; run it in a dedicated, empty project before a release.

## Local (loop files)

`provider = "local"`: each volume is a sparse file `l<N>.img` plus `l<N>.json` under `[local] dir`. The
provider runs `truncate`, `mkfs.ext4`, `losetup`. It needs root, `losetup`, and `mkfs.ext4`. There is no
network, no token and no price (set `rooms.price_gb_month_eur` to exercise the cost cap). Volume ids are monotonic and persisted (`.next-id`), never reused. Loop devices
do not survive a reboot; the desk re-attaches them when it remounts a floor.

## How each cloud bills

motel keeps one table of this in [`billing.py`](../src/motel/billing.py) (`BILLING`). A cloud that bills **per started hour from
the provider's own `created` time** is aligned: deleting an idle resource early inside a paid hour saves nothing, so motel
keeps it until a release window just before the next billed hour starts (see [Hourly billing](cabins.md#hourly-billing) for
cabins and [Lifecycle](lifecycle.md#floors) for floors). A cloud whose granularity could not be confirmed from its own
documentation is `unknown`, and unknown means no alignment at all: resources are released as soon as motel would release them
anyway. Sources were read 10 and 11 November 2026.

| `provider =` | servers (cabins run on Hetzner only) | volumes | aligned |
|---|---|---|---|
| `hetzner` | per started hour. "We always round up the hourly usage of a server." ([billing FAQ](https://docs.hetzner.com/cloud/billing/faq/)) | per started hour. "We round up partial hours." ([billing system](https://docs.hetzner.com/general/billing-and-account-management/billing-at-hetzner/billing-system-hetzner/); "Volumes ... are billed hourly", [volumes overview](https://docs.hetzner.com/cloud/volumes/overview)) | servers and volumes |
| `digitalocean` | per second, 60 s minimum ([Droplet pricing](https://docs.digitalocean.com/products/droplets/details/pricing/)) | unknown: "Charges accrue hourly for as long as the volume exists", rounding of a partial hour not stated ([Volume pricing](https://docs.digitalocean.com/products/volumes/details/pricing/)) | no |
| `linode` | per started hour: "rates get rounded up to the nearest hour" ([pricing](https://www.akamai.com/cloud/pricing)) | per started hour, same sentence ([Block Storage pricing](https://www.akamai.com/cloud/pricing/block-storage)) | servers and volumes |
| `vultr` | per started hour: "The minimum billing unit is one hour, and hourly charges begin when a server is deployed." ([billing doc](https://docs.vultr.com/support/platform/billing/how-am-i-billed-for-my-servers)) | unknown: the page does not address block storage | servers only |
| `scaleway` | unknown: hourly, rounding not readable ([instance pricing](https://www.scaleway.com/en/docs/instances/reference-content/understanding-instance-pricing/), HTTP 403 to automated reads) | unknown, same page | no |
| `aws` | per second, 60 s minimum ([EC2 on-demand](https://aws.amazon.com/ec2/pricing/on-demand/)) | per second, 60 s minimum ([EBS pricing](https://aws.amazon.com/ebs/pricing/)) | no |
| `gcp` | per second, one minute minimum ([announcement](https://cloud.google.com/blog/products/gcp/extending-per-second-billing-in-google)) | per second ([same page](https://cloud.google.com/blog/products/gcp/extending-per-second-billing-in-google)) | no |
| `azure` | unknown ([VM pricing](https://azure.microsoft.com/pricing/details/virtual-machines/linux/); no confirmed statement) | unknown: "billed on an hourly basis", "pro-rated on an hourly basis" ([managed disks](https://azure.microsoft.com/pricing/details/managed-disks/)) | no |
| `local`, fake | no bill | no bill | no |

Two parts of this are not stated by a provider. The Hetzner **volume** rule is inferred from its general billing page (it names
servers, and says partial hours are rounded up for its services), and that billing starts **from creation** is motel's
assumption. Both are labelled `inferred` in the table; if either is wrong the cost of being wrong is one billed hour held
for nothing, never an early delete.

The clock anchor is the **earliest** of: the provider's `created` time for the resource (for a cabin, the server's and the
disk's), and the time the create response reached motel. Taking the earliest keeps a provider clock ahead of motel's from
causing a late deletion. It is not exact: when the arrival time is the one used, it can be later than the real creation by
the API round trip (seconds), so a release can be that much late against the provider's own hour. A provider time more than 5 minutes before motel started the create is not
believed, and a time without a time zone is ignored; the record's own start time is used when nothing else is usable. A provider can declare its own volume rule (`volume_billing`, a `motel.billing.Rule`) and so override the
table; the in-memory fake does not, so its tests see no alignment unless they ask for it.

## Writing a provider

Implement the Protocol, return string ids, raise `ProviderError(indeterminate=True)` when a write may have
happened, `InventoryIncomplete` when a listing cannot be completed, and `NotFound` for a missing volume.
`motel.providers.fake.FakeProvider` is an in-memory reference with failure injection (`lose_next_create`,
`drop_next_create`, `fail_next`, `incomplete`). Add one `ProviderSpec` row to `motel.providers.registry.SPECS`: the config, `setup`, the doctor and the installer read it. Set `currency`, `formats_on_create` (False if the cloud hands out blank volumes), and optionally `gb_bytes` (bytes in one of its gigabytes, if not 2^30) and `billable_gb(size)` (the billed tier).
A provider is supported only after the live harness (`tests/live/`) passes on it.
