# Vultr Block Storage

> **Beta: not verified against the live API.** Written from the current official documentation and the official Go
> client (read on 2026-11-09) and tested against a fake API on a loopback socket. No Vultr account was available for a
> live run, and Vultr's API reference page could not be fetched (HTTP 403 to automated readers), so several request
> and response details come from the documentation pages and the official `govultr` client instead. See
> [What needs live verification](#what-needs-live-verification).

Module: `motel.providers.vultr.VultrProvider` (standard library only). It is registered in the desk (`host.provider = "vultr"`, token in `credentials/vultr.token`, typed once by `sudo motel-desk setup vultr`). Set `[provider] block_type = "high_perf"` (default) or `"storage_opt"` and `rooms.eur_per_usd`.

## Capability table

| Topic | Behaviour |
| --- | --- |
| Labels | A block has one free-text `label` and nothing else (no tags, no key/value labels). The volume name and all motel labels share that one label (below). |
| Ownership checks | Same strictness as Hetzner: decoded labels, the `motel-` name and the attachment are compared on a fresh read before every write. |
| Create | `POST /blocks` (`pending`), poll until `active`, then `POST /blocks/{id}/attach` with `"live": true`, poll until attached. A failure after the create is reported as unresolved so the desk finds the block by label. |
| Block type | `high_perf` (NVMe, default) or `storage_opt` (HDD), chosen with the `block_type` constructor argument. |
| Formatting | The API cannot format. Blocks are blank (`formats_on_create = False`); the desk formats it with `mkfs.ext4` only after proving the device blank (no signature, mounted nowhere, size matches). |
| Size | NVMe: 1 GB minimum; HDD: 40 GB minimum; the provisioning guide says up to 10 TB. Whole GB. Resize (`PATCH /blocks/{id}` with `size_gb`) grows only; shrinking in place is not possible. |
| Volumes per instance | 16. Bare metal servers cannot attach block storage. |
| Async | Create, attach, detach and resize are accepted at once; the provider polls `GET /blocks/{id}` (`status`, `size_gb`, `attached_to_instance`) with a 240 s limit, then `indeterminate`. |
| Pagination | `per_page` (motel uses 500) and `cursor`; the response has `meta.total` and `meta.links.next` (empty on the last page). A list that ends short of `meta.total` raises `InventoryIncomplete`. |
| Rate limits | About 30 requests per second per originating IP; above that HTTP 429, retried with back-off. |
| Region matching | Lower-case region ids such as `ewr`. The metadata service reports the code in upper case, which the provider lower-cases. A block attaches only to an instance in the same location. |
| Guest device | `/dev/disk/by-id/virtio-<first 20 characters of mount_id>`; `expected_serial` is the same 20 characters (virtio serials are at most 20 bytes). `mount_id` is only set while the block is attached. |
| Whoami | `http://169.254.169.254/v1.json`: `instance-v2-id` (the UUID the API uses) and `region.regioncode`; falls back to `/v1/instance-v2-id`. |
| Delete protection | None exists, so `Volume.delete_protected` is always false. The never-touch set from setup is the protection. |
| Currency | USD. `price_gb_month` returns euro: the table price for the block type times `eur_per_usd` (constructor argument, default `1`). |

## How labels are stored

The label is `<name>~<key>=<value>~<key>=<value>...`, for example
`motel-f-ab12-cd34~managed-by=motel~role=floor~host=<instance id>~floor=f-ab12~suite=0~created=20261109t1200z`.

* The name stays first and readable (letters, digits, `.` `_` `-`, at most 64 characters). Keys are lower-case letters,
  digits and `-`.
* In a value, `A-Za-z0-9._-` stay as they are; every other byte, including `~`, `=`, `%`, space and non-ASCII, is
  written `%xx` in lower-case hex. So `~` and `=` never appear raw inside a value.
* Decoding re-renders the result and accepts the label only if it is identical. Anything else (an extra `~`, upper-case
  hex, a duplicate key, a stray `%`, a plain label such as `Remote-Block-Storage`) gives **no labels**, so the guard
  refuses it. The name of such a block is the text before the first `~`.
* The API reference states no label length limit. motel refuses to write a label over 255 characters and never
  truncates. With motel's own labels the label is about 130 characters (a UUID host id is 36 of them).

The API has no label filter, so `list_owned` lists all blocks (paged) and matches here.

## Pricing

USD per GB per month by block type, in `vultr.PRICE_USD_GB_MONTH`: `high_perf` (NVMe) `0.10`, `storage_opt` (HDD) `0.04`.
**Source caveat:** Vultr's own pricing pages answered automated readers with HTTP 403, so these figures come from
third-party pricing summaries (read 2026-11-09), for example <https://onedollarvps.pages.dev/pricing/vultr-pricing>.
Confirm them at <https://www.vultr.com/products/block-storage/> and set `rooms.price_gb_month_eur` (which the desk uses
before asking the provider) if they differ. The API also returns a `cost` per block, which a later version could use to
learn the real rate from an existing block. With `eur_per_usd` left at `1`, the cost is overestimated while the euro is
worth more than the dollar, which keeps the monthly cap on the safe side.

## Token and what it can see

A Vultr API key **cannot be scoped to a product or a resource**: one key reaches every resource in the account
(<https://docs.vultr.com/support/platform/api/can-i-scope-api-keys-to-specific-products>). The only restriction is an
IP-based access control list: allow only this host's public address
(<https://docs.vultr.com/platform/other/api/manage-api-access-control>). Sub-users with API access were not
researched in detail. Treat the key as full account access; the guard, the receipt and the label checks are what keep
motel off other resources, and the key file stays root-owned with mode 0600.

## Sources

* Provisioning (create): <https://docs.vultr.com/products/storage/block-storage/provisioning>
* Attach and detach (`live`): <https://docs.vultr.com/products/storage/block-storage/management/attach-instances>
* Resize (`PATCH`): <https://docs.vultr.com/products/storage/block-storage/management/resize-block-storage>
* FAQ (sizes, 16 volumes): <https://docs.vultr.com/products/storage/block-storage/faq>
* Linux mount (`/dev/vdb` naming): <https://docs.vultr.com/products/storage/block-storage/mount/linux>
* `mount_id` and block fields: <https://docs.vultr.com/reference/terraform/resources/block_storage>
* Official Go client (fields, pagination): <https://github.com/vultr/govultr/blob/master/block_storage.go>
* Rate limits: <https://docs.vultr.com/support/platform/api/what-rate-limits-apply-to-the-vultr-api>
* API keys: <https://docs.vultr.com/support/platform/api/can-i-scope-api-keys-to-specific-products>
* API reference (not readable by the tools used): <https://www.vultr.com/api/#tag/block>
* Metadata API: <https://www.vultr.com/metadata/>

## What needs live verification

1. The whole request and response shape against the API reference (create, attach with `instance_id` and `live`,
   detach, `PATCH`, list pagination with `cursor`, `meta.links.next`), which was taken from the docs pages and `govultr`.
2. The maximum label length, and that a label of about 130 characters is accepted.
3. That the `virtio-<mount_id[:20]>` link exists and that `lsblk -dno SERIAL` prints the same 20 characters. On a
   fresh attach `mount_id` may not be filled in at once; the provider polls until the block is attached and `active`
   but then reads `mount_id` fresh on each call.
4. That a blank block attached with `live` appears without a reboot, and that NVMe blocks appear as virtio disks on Linux.
5. The metadata JSON keys (`instance-v2-id`, `region.regioncode`) and that `/v1.json` needs no header.
6. The prices above, and the HDD minimum of 40 GB (motel's default minimum is 50 GB).
