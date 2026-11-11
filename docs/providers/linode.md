# Linode (Akamai Cloud) Block Storage

> **Beta: not verified against the live API.** Written from the current official documentation (read on 2026-11-09)
> and tested against a fake API on a loopback socket. No Linode account was available for a live run.
> See [What needs live verification](#what-needs-live-verification).

Module: `motel.providers.linode.LinodeProvider` (standard library only). It is registered in the desk (`host.provider = "linode"`, token in `credentials/linode.token`, typed once by `sudo motel-desk setup linode`). Set `rooms.eur_per_usd` to convert the dollar price list.

## Capability table

| Topic | Behaviour |
| --- | --- |
| Labels | Volumes have a `label` (the name) and a list of `tags`, no key/value labels. Labels are stored as tags `m_<key>_<value>` (below). |
| Ownership checks | Same strictness as Hetzner: decoded labels, the `motel-` name and the attachment are compared on a fresh read before every write. |
| Create | One call: `POST /volumes` with `linode_id` creates and attaches (no `region` is sent; the Linode's region decides, and the returned region is checked against the host's). The provider then polls until `status` is `active` and `io_ready` is true. |
| Formatting | The API cannot format. Volumes are blank (`formats_on_create = False`); the desk formats it with `mkfs.ext4` only after proving the device blank (no signature, mounted nowhere, size matches). |
| Size | The create schema states no minimum; the product pages say 10 GB to 16 TB, the API response schema says at most 10,240 GB. Whole GB. Resize grows only and needs status `active`. |
| Volumes per Linode | Per configuration: RAM in GB, at least 8 and at most 64 (a rescue configuration allows 8). Account: 250 volumes and 100 TB; 100 TB per region. |
| Labels (names) | 1 to 32 characters matching `^[a-zA-Z]((?!--\|__)[a-zA-Z0-9-_])+$` (so at least 2 characters, no `--` or `__`). |
| Async | Create, attach and resize return the volume at once; the provider polls `GET /volumes/{id}` (`status` `creating`/`active`/`resizing`, `io_ready`, `linode_id`) with a 240 s limit, then `indeterminate`. Detach returns `{}` and is polled the same way. |
| Pagination | `page` and `page_size` (25 to 500, motel uses 100); the response has `pages` and `results`. A list that ends short of `results` raises `InventoryIncomplete`. |
| Rate limits | Paginated GET: 200 per minute; other calls 1,600 per minute; volume create 100 per minute. HTTP 429 is retried, honouring `Retry-After`. |
| Region matching | Region ids such as `us-iad` are the same in the API and the Metadata service. Volume and Linode must share a data center; block storage is not available in distributed compute regions. |
| Guest device | The API reports `filesystem_path` (`/dev/disk/by-id/scsi-0Linode_Volume_<label>`); the provider checks it against that exact shape, and builds it from the label otherwise. `expected_serial` is the label (see risks). |
| Whoami | Metadata service: `PUT http://169.254.169.254/v1/token` with `Metadata-Token-Expiry-Seconds`, then `GET /v1/instance` with `Metadata-Token` and `Accept: application/json` (`id`, `region`). |
| Delete protection | The `locks` field exists only on `v4beta`. This provider uses `v4`, so `Volume.delete_protected` stays false unless a lock is reported; a locked volume makes the delete call fail. |
| Encryption | The `encryption` field is not sent; the account default (enabled) applies. |
| Currency | USD. `price_gb_month` returns euro: the live region price times `eur_per_usd` (constructor argument, default `1`). |

## How labels are stored

A label `key=value` becomes the tag `m_<key>_<value>`, using only `a-z 0-9 - _` because the character set of a Linode
tag is not documented (only 3 to 50 characters).

* The key is lower-case letters, digits and `-` and never holds `_`, so the first `_` after the prefix is the separator.
* In the value, `a-z 0-9 -` stay as they are. Every other byte (including `_` and upper case) is written `_xx` in
  lower-case hex.
* Decoding re-encodes the result and accepts the tag only if it is identical, so look-alikes are ignored. A key with two
  different values is dropped and matches nothing. Both fail closed.
* A label that would make a tag longer than 50 characters is an error, never truncated. With motel's own labels the
  longest tag is `m_created_20261109t1200z` (24 characters); a host id may be up to 43 characters.

The `X-Filter` rules for tag arrays are not documented, so `list_owned` lists all volumes (paged) and matches here.

## Pricing

The live price is read from `GET /volumes/types` (the region's `region_prices` entry, else the default `price`) and
turned into euro with `eur_per_usd`. If that call fails or has an unexpected shape, the table price is used:
`$0.10` per GB per month, USD, "Volumes start at $0.10/GB per month" on
<https://www.akamai.com/cloud/pricing/block-storage> (read 2026-11-09; the page says price varies by data center and
shows only a Jakarta table). The table lives in `linode.PRICE_USD_GB_MONTH`. `rooms.price_gb_month_eur` overrides
everything.

## Token and what it can see

Use a personal access token with the OAuth scopes `volumes:read_write` and `linodes:read_write` (attach and detach
need both; see the attach operation page). `linodes:read_write` is wider than motel needs, but attach requires it.
Without extra restrictions the token sees every Linode and volume of the account. Linode also has restricted users
and role-based access (IAM) that can limit which entities a user can act on; a token for such a user is the
narrowest option of the three clouds, but it has **not** been tried with motel. The guard, the receipt and the label
checks remain the main protection.

## Sources

* Create volume: <https://techdocs.akamai.com/linode-api/reference/post-volume>
* Attach: <https://techdocs.akamai.com/linode-api/reference/post-attach-volume>
* Detach: <https://techdocs.akamai.com/linode-api/reference/post-detach-volume>
* Resize: <https://techdocs.akamai.com/linode-api/reference/post-resize-volume>
* List volumes: <https://techdocs.akamai.com/linode-api/reference/get-volumes>
* Volume types (prices): <https://techdocs.akamai.com/linode-api/reference/get-volume-types>
* Get and delete volume: <https://techdocs.akamai.com/linode-api/reference/get-volume>
* Tags: <https://techdocs.akamai.com/linode-api/reference/post-tag>
* Rate limits: <https://techdocs.akamai.com/linode-api/reference/rate-limits>
* Authentication and scopes: <https://techdocs.akamai.com/linode-api/reference/get-started>
* Block Storage limits: <https://techdocs.akamai.com/cloud-computing/docs/block-storage>
* Metadata service: <https://techdocs.akamai.com/cloud-computing/docs/metadata-service-api>
* Pricing: <https://www.akamai.com/cloud/pricing/block-storage>

## What needs live verification

1. That `lsblk -dno SERIAL` on the by-id device prints the label. The path is reported by the API; the serial equality is
   inferred. If it differs, motel refuses to mount; `serial_check=False` turns the check off.
2. That tags with `_xx` escapes are accepted, and the character set of tags in practice.
3. Create with `linode_id` and no `region` (the docs say `region` is only required without `linode_id`), the config that
   the volume is attached to (`persist_across_boots` is left at its default), and that attach and detach on a running
   Linode do not need a reboot (full-virtualization Linodes do: the block-storage page says to power them off).
4. The Metadata service in the host's region (the docs disagree on regional availability), and the token handshake.
5. Whether attach and detach finish before the 200 answer; the provider polls either way.
6. The size limits (10 GB minimum, 10,240 GB or 16 TB maximum), and that a `locks` array never appears on `v4`.
