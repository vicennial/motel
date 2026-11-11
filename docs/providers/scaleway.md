# Scaleway Block Storage

> **Beta: not verified against the live API.** This provider was written from the published API sources and a
> loopback fake (`tests/providers/test_scaleway.py`). No request has been sent to a real Scaleway account. Treat
> every "unverified" item below as a checklist for the first live run.

`provider = "scaleway"` stores floors on Scaleway **Block Storage** (SBS) volumes. Volumes are created with the Block
API, and attached and detached through the Instance API. Everything is scoped to the **zone and project of the host
motel runs on**, both read from the Instance metadata service.

## What it maps to

| motel | Scaleway |
|---|---|
| server id | Instance id (UUID), from the metadata service |
| location | zone, e.g. `fr-par-1` (volumes and Instances are zonal) |
| volume id | Block volume UUID |
| labels | tags `key=value` (see below) |
| size | `size` in bytes; motel counts **GB = 10^9 bytes**, the API's own granularity |
| device | `/dev/disk/by-id/scsi-0SCW_sbs_volume-<volume uuid>` |
| expected serial | `volume-<volume uuid>` (what `lsblk -o SERIAL` shows) |
| price | public product catalogue, `Volume Low Latency 5K` (or `15K`), per GB-hour x 730 |
| delete protection | none exists; the guard relies on tags, the floor record and the never-touch list |

### Labels are tags

Scaleway tags are a list of strings. motel stores each label as `key=value` and splits on the **first** `=`.

- Keys are `[A-Za-z0-9][A-Za-z0-9_.-]*` (up to 63 characters). A key that does not fit is refused, not altered.
- Values must be non-empty, at most 120 characters, with no line breaks and no outer spaces.
- A tag without `=` is somebody else's tag and is ignored.
- If a key appears twice with different values (for example someone adds `host=other` next to ours), the label reads
  as a **conflict** and matches nothing. A foreign tag can therefore never make a volume look like one of ours, and
  `list_owned` and the allowlist guard both refuse it.
- The Block API's `tags` list filter returns volumes matching **any** of the tags, so motel does not use it. It lists
  the project's volumes in the zone and requires **all** labels client-side.

## Setup

1. **Use a dedicated Project** for motel (Console > Project). Block Storage does not support IAM resource-level
   conditions (see [limits](#limits-and-caveats)), so the key can see and delete every volume in its project.
2. **IAM application** (Console > IAM > Applications) with a **policy** scoped to that project only. Start with these
   permission sets and nothing else:
   - `BlockStorageFullAccess` (create, read, list, update and delete volumes)
   - `InstancesServerList` and `InstancesServerRead` (the provider lists Instances to prove the key can see this one)
   - `InstancesServerWrite` (attach and detach go through the Instance API)

   Not verified: whether attach also needs `InstancesVolumeWrite`. If the first attach returns HTTP 403, add exactly
   that one set and note it here.
3. **API key** for the application. Only the secret key is used. It is sent as `X-Auth-Token` to `api.scaleway.com`
   and nowhere else.
4. Put the secret key where the desk reads it: `/etc/motel/credentials/scaleway.token`, root-owned, mode `0600`.
   (`sudo motel-desk setup scaleway` prompts for it on a terminal and stores it there.)
5. In `/etc/motel/motel.toml` set `provider = "scaleway"`. Leave `server_id` and `location` as `auto`.

`perf_iops` defaults to 5000 (`sbs_5k`). 15000 (`sbs_15k`) needs an Instance type with at least 3 GiB/s block bandwidth.

## Behaviour

- **Create** is two calls: create the volume (`from_empty`, `perf_iops`, `project_id`, tags), wait for `available`, then
  attach it. If the volume exists but is not ready or not attached, the error is `indeterminate`, so the desk finds it
  by its labels and carries on rather than creating a second one.
- **Attach** is `POST /instance/v1/zones/{zone}/servers/{id}/attach-volume` with `volume_type = sbs_volume`, then a wait
  until the volume is `in_use` and every reference is `attached` to that server. It is idempotent, and refuses a volume
  held by anything else. A holder that is not an Instance (for example a Kubernetes node) shows up as
  `<type>:<id>` and never equals this host.
- **Detach** reads the holder from the volume's references, calls `detach-volume`, and waits for `available`.
- **Resize** is `PATCH` with a larger `size` in bytes. Grow only. It waits until the volume leaves `resizing`.
- **Delete** is `DELETE`; the API refuses a volume that is `in_use`.
- **Retries:** reads retry on 429/5xx and network errors. Writes retry only on 429 (the request was rejected). A write
  whose answer is lost, or that gets a 5xx, raises `ProviderError(indeterminate=True)` and is never repeated blindly.
- **Redirects are refused**, so the key cannot follow a redirect to another host. The key is never sent to the
  metadata service or to the public catalogue.

## Limits and caveats

- Volume size 5 GB to 15 TB (15,000 GB), 1 GB steps; growing only. (Raised from 10 TB in March 2026.)
- At most 15 Block volumes per Instance (16 including the boot volume).
- A volume attaches only to an Instance in the **same zone**; motel refuses any other location.
- 5K and 15K volumes are `sbs_5k` / `sbs_15k`. The legacy `b_ssd` type and the Instance volume API are not used.
- The provider assumes **raw volumes**: Scaleway cannot format at creation, so the desk must run `mkfs.ext4` on first use
 ; the desk makes it after proving the device blank.
- After an online resize the guest may need a SCSI rescan before `resize2fs` sees the new size; the desk rescans before `resize2fs`.
- `include_deleted` is off, and a volume in status `deleted` is treated as gone.

## Pricing

Source: Scaleway's public product catalogue, queried on **2026-11-09** (no key needed):
`https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?product_types=block_storage&zone=fr-par-1`.
Prices are per GB-hour in euros, before VAT, and identical in every zone listed.

| Product | EUR per GB-hour | EUR per GB-month (x 730 h) |
|---|---|---|
| Block Storage 5K (`sbs_5k`) | 0.000129999 | 0.0949 |
| Block Storage 15K (`sbs_15k`) | 0.000177 | 0.1292 |
| Snapshot (not used by motel) | 0.000049 | 0.0358 |

Scaleway announced the 5K increase (0.000118 to 0.00013 per GB-hour) for 1 June 2026. A month is taken as 730 hours,
which is motel's convention here; Scaleway bills by the hour for the size, not the data stored. `price_gb_month()` reads
the catalogue each time it is asked. If that fails, motel reports an error that tells you to set
`rooms.price_gb_month_eur` yourself. Caps are estimates built from this rate.

## Wiring

`host.provider = "scaleway"` is registered (token file `credentials/scaleway.token`). Sizes are decimal gigabytes, which the desk knows from the provider's `gb_bytes`.

## Not verified against the live API

- Tag character set: whether `=` and the `20261109t0000z` timestamp style are accepted in tags.
- The `attach-volume` / `detach-volume` bodies, and whether attach to a running Instance completes without a stop.
- The metadata JSON key for the zone (`zone`, with `location.zone_id` as a fallback) and for the project.
- That `lsblk -o SERIAL` shows exactly `volume-<uuid>` for `sbs` disks, and the by-id link name on every image.
- The reference type string `instance_server` and the status values in practice.
- The IAM permission sets above are enough, and 403 vs 404 behaviour for a key from another project.
- The error body shape (`type`, `message`) and the `Retry-After` header on 429.
- Online resize of an attached volume, and how long the guest takes to see it.
- The `v2alpha1` catalogue stays stable; it is an alpha API.

## Sources

Read on 2026-11-09. The developer reference pages under `scaleway.com/en/developers/api` returned HTTP 403 to the
fetcher used, so the wire contract below comes from Scaleway's own generated SDK and CSI driver.

- Block Storage API reference: https://www.scaleway.com/en/developers/api/block-storage
- Block API (v1) SDK source, paths, fields, enums, limits: https://github.com/scaleway/scaleway-sdk-go/blob/master/api/block/v1/block_sdk.go
- Instance API SDK source (`attach-volume`, `detach-volume`, servers list, `sbs_volume`): https://github.com/scaleway/scaleway-sdk-go/blob/master/api/instance/v1/instance_sdk.go
- Instance metadata structure and `/conf?format=json`: https://github.com/scaleway/scaleway-sdk-go/blob/master/api/instance/v1/instance_metadata_sdk.go
- Metadata service addresses: https://docs.cloud-init.io/en/26.1/reference/datasources/scaleway.html
- Reference implementation of attach, detach, wait, resize (CSI driver): https://github.com/scaleway/scaleway-csi/blob/master/pkg/scaleway/instance.go and https://github.com/scaleway/scaleway-csi/blob/master/pkg/scaleway/block.go
- Volume types, 16 volumes per Instance, 15 TB: https://github.com/scaleway/docs-content/blob/main/pages/block-storage/faq.mdx and https://github.com/scaleway/docs-content/blob/main/macros/block-storage/block-storage-volume-types.mdx
- 15 TB change (2026-03-09): https://github.com/scaleway/docs-content/blob/main/changelog/march2026/2026-03-09-block-storage-changed-increased-volume-capacity-to-15tb.mdx
- Attach rules, 15K bandwidth note: https://github.com/scaleway/docs-content/blob/main/pages/block-storage/how-to/attach-a-volume.mdx
- Device identity (`SCW`, `sbs`, serial `volume-<uuid>`, by-id link): https://github.com/scaleway/docs-content/blob/main/pages/instances/reference-content/identify-devices.mdx
- Growing a volume: https://github.com/scaleway/docs-content/blob/main/pages/block-storage/api-cli/increasing-volume-size.mdx
- IAM permission sets (`BlockStorageFullAccess`, `Instances*`): https://github.com/scaleway/docs-content/blob/main/pages/iam/policies-permissions/permission-sets.mdx
- Resource-level conditions are not offered for Block Storage: https://github.com/scaleway/docs-content/blob/main/pages/iam/policies-permissions/supported-products-resource-level.mdx
- Price catalogue: https://api.scaleway.com/product-catalog/v2alpha1/public-catalog/products?product_types=block_storage
- Price update notice (5K and snapshot, 1 June 2026): https://www.scaleway.com/en/blog/a-transparent-update-on-scaleway-pricing/
