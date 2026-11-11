# DigitalOcean (Block Storage volumes)

> **Beta: not verified against the live API.** Written from the current official documentation (read on 2026-11-09)
> and tested against a fake API on a loopback socket. No DigitalOcean account was available for a live run.
> See [What needs live verification](#what-needs-live-verification).

Module: `motel.providers.digitalocean.DigitalOceanProvider` (standard library only). It is registered in the desk (`host.provider = "digitalocean"`, token in `credentials/digitalocean.token`, typed once by `sudo motel-desk setup digitalocean`). Set `rooms.eur_per_usd` to convert the dollar price list.

## Capability table

| Topic | Behaviour |
| --- | --- |
| Labels | Volumes have a name and a list of tags, no key/value labels. Labels are stored as tags `m_<key>_<value>` (below). |
| Ownership checks | Same strictness as Hetzner: the desk compares the decoded labels, the `motel-` name and the attachment on a fresh read before every write. |
| Create | Blank volume in the host's region, then an `attach` action (two calls; a failure between them is reported as unresolved so the desk finds the volume by label). |
| Formatting | The API can format (`filesystem_type`), but DigitalOcean then **auto-mounts** a pre-formatted volume on attach, which would clash with motel's own mount. motel creates blank volumes (`formats_on_create = False`); the desk formats it with `mkfs.ext4` only after proving the device blank (no signature, mounted nowhere, size matches). |
| Size | 1 GiB to 16,384 GiB, whole numbers. Sizes are GiB; motel's "GB" means the provider's unit. Resize grows only. |
| Volumes per Droplet | 15. Per account: 5,000 (support can raise it). |
| Names | Lower-case letters, digits and `-`, start with a letter, at most 64 characters, unique per region, cannot be renamed. |
| Async | `attach`, `detach` and `resize` return an action (`in-progress`, `completed`, `errored`) that the provider polls at `GET /v2/volumes/{id}/actions/{action_id}` (timeout 240 s, then `indeterminate`). Create and delete are synchronous. |
| Pagination | `per_page` (max 200, motel uses 200) and `page`; `links.pages.next` and `meta.total`. A list that ends short of `meta.total` raises `InventoryIncomplete`. |
| Rate limits | 5,000 requests per hour and 250 per minute; HTTP 429 is retried (reads and writes), honouring `Retry-After` when sent, otherwise 2 s doubling. |
| Region matching | The volume is created in the region slug read from the Droplet's metadata (for example `nyc3`); the API uses the same slugs. Volumes exist in all 16 regions listed on the availability page. |
| Guest device | `/dev/disk/by-id/scsi-0DO_Volume_<volume name>`. `expected_serial` is the volume name (see risks). |
| Whoami | `http://169.254.169.254/metadata/v1/id` (integer Droplet id) and `/metadata/v1/region`. |
| Delete protection | None exists for volumes, so `Volume.delete_protected` is always false and the guard's protection check cannot help. The never-touch set from setup is the protection. |
| Currency | USD. `price_gb_month` returns euro: the table price times `eur_per_usd` (constructor argument, default `1`). |

## How labels are stored

A label `key=value` becomes the tag `m_<key>_<value>`. Only `a-z 0-9 - _` are used, so the tag is valid whatever
else DigitalOcean allows (letters, digits, colons, dashes, underscores; 255 characters; case stable).

* The key is lower-case letters, digits and `-` and never holds `_`, so the first `_` after the prefix is always the
  separator.
* In the value, `a-z 0-9 -` stay as they are. Every other byte (including `_` and upper case) is written `_xx` in
  lower-case hex.
* Decoding re-encodes the result and accepts the tag only if it is identical, so a look-alike spelling
  (`M_managed-by_motel`, `m_managed-by_mo_74el`, a trailing `_`) is ignored. A key that carries two different values
  is dropped and matches nothing. Both fail closed: the desk then sees a volume without motel labels.
* A label that does not fit in 255 characters is an error, never truncated.

`GET /v2/volumes` filters only by name and region, so `list_owned` lists all volumes (paged) and matches tags here.

## Pricing

`$0.10` per GiB per month, USD, flat across regions: <https://docs.digitalocean.com/products/volumes/details/pricing/>
(page says "Last verified 26 Oct 2026"; hourly billing, no hourly rate is published). The table lives in
`digitalocean.PRICE_USD_GB_MONTH`. To override it, set `rooms.price_gb_month_eur` (the desk uses that before asking
the provider). To convert without overriding, pass `eur_per_usd`. With the default `1` the cost is overestimated
while the euro is worth more than the dollar, which keeps the monthly cap on the safe side.

## Token and what it can see

Create a personal access token with **custom scopes** (<https://docs.digitalocean.com/reference/api/scopes/>). The
set below follows the scope names in the reference; it has not been tried live:

| Need | Scopes |
| --- | --- |
| list, read, create, delete volumes | `block_storage:read`, `block_storage:create`, `block_storage:delete` |
| attach, detach, resize, poll actions | `block_storage_action:create`, `block_storage_action:read` |
| list Droplets (setup, doctor) | `droplet:read` |
| write and read tags | `tag:create`, `tag:read` |

A token cannot be restricted to one Droplet, volume or project: it sees every Droplet and volume of its team, and a
scope cannot be edited after the token is created. The guard, the receipt and the label checks are what keep motel
off other resources. `Full Access` also works. Tags that motel creates may remain in the team's tag list after the
volumes are gone (not verified).

## Sources

* Block Storage API: <https://docs.digitalocean.com/reference/api/reference/block-storage/>
* Volume actions: <https://docs.digitalocean.com/reference/api/reference/block-storage-actions/>
* Tags: <https://docs.digitalocean.com/reference/api/reference/tags/>
* Limits: <https://docs.digitalocean.com/products/volumes/details/limits/>
* Availability: <https://docs.digitalocean.com/products/volumes/details/availability/>
* Naming and device paths: <https://docs.digitalocean.com/products/volumes/details/naming-conventions/>
* Metadata: <https://docs.digitalocean.com/reference/api/metadata/droplet-properties>
* Scopes: <https://docs.digitalocean.com/reference/api/scopes/>

## What needs live verification

1. That `lsblk -dno SERIAL /dev/disk/by-id/scsi-0DO_Volume_<name>` prints the volume name. The by-id link is documented;
   the serial equality is inferred from how udev builds it. If it differs, motel refuses to mount (fail closed);
   `serial_check=False` turns the check off, leaving the by-id link as the identity proof.
2. That a blank, attached volume is not auto-mounted, and that the tag scopes above are enough.
3. That `tags` on create need no separate tag-creation call, and that a tag with an `_xx` escape is accepted.
4. `resize` with and without `region` in the body (it is sent), online `resize2fs`, delete after detach.
5. The `Retry-After` header on 429 (only `ratelimit-*` headers are documented).
