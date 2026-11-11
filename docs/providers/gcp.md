# Google Cloud Persistent Disk / Hyperdisk provider

> **Beta: not verified against the live API.** Written from the Google Cloud documentation and tested against a
> fake Compute Engine API and a fake metadata server on a loopback socket (`tests/providers/test_gcp.py`). Nobody has
> run it against a real project yet. Treat the first live run on a scratch project as part of the setup.

Module: [`providers/gcp.py`](../../src/motel/providers/gcp.py), standard library only. It uses the
[Compute Engine REST API v1](https://cloud.google.com/compute/docs/reference/rest/v1) with an access token from the
metadata server for the VM's service account. There is no key file and no key-file support, on purpose (signing a
service-account JWT needs RSA, which the standard library does not have, and a key on disk is what this design avoids).

## What it does

| Interface method | API call(s) | Notes |
|---|---|---|
| `whoami` | metadata: `project/project-id`, `instance/zone`, `instance/name` | Header `Metadata-Flavor: Google`. `server_id` is the **instance name** and `location` the **zone**, because disks list their users by name. |
| `visible` | `instances.get` (this VM), `aggregatedList` disks (all pages) | Every disk in the project goes into never-touch at setup. A location reported `UNREACHABLE` makes the listing incomplete. |
| `get_volume` | `disks.get` | The volume id is the **disk name**. |
| `list_owned` | `disks.list` in this zone, paginated by `pageToken`, labels matched client side | Failure raises `InventoryIncomplete`. |
| `create_volume` | `disks.insert` (`labels` set at creation, `requestId`), wait, `instances.attachDisk` with `deviceName` = disk name | Created in this VM's zone only. If anything after the insert fails the error is `indeterminate`, so the desk finds the disk by label and name and attaches it later. |
| `attach` | `instances.attachDisk` (`READ_WRITE`, `autoDelete=false`) | |
| `detach` | `instances.get` (to read the real `deviceName`), `instances.detachDisk` | No call if no user. Refuses a disk with several users. |
| `resize` | `disks.resize` | Grow only. |
| `delete` | `disks.delete` | The operation error (for example "in use") surfaces. |
| `device_path` | n/a | `/dev/disk/by-id/google-<disk name>`, stable because `deviceName` is the disk name ([disk symlinks](https://cloud.google.com/compute/docs/disks/disk-symlinks)). |
| `expected_serial` | n/a | The disk name, which Compute Engine reports as the device serial. |
| `price_gb_month` | static table | USD. See Pricing. |

Every write returns a zone operation. The provider polls `zoneOperations.get` every 2 s for up to 300 s and raises the
operation's error if it has one; a timeout is `indeterminate`.

Labels are native GCE labels, so keys and values must be lowercase letters, digits, `-` and `_` (motel's labels
already are). The disk name is `motel-<floor>-<suffix>` (17 characters, valid as a disk name and as a SCSI serial).
Disk types: `pd-balanced` (default), `pd-ssd`, `pd-standard`, `hyperdisk-balanced`, `hyperdisk-throughput`,
`hyperdisk-extreme`; `provisioned_iops` and `provisioned_throughput` are passed through for Hyperdisk. Hyperdisk is
available only for some machine series, see [Hyperdisk](https://cloud.google.com/compute/docs/disks/hyperdisks).

**Unverified guess to check first:** that `lsblk -o SERIAL` shows the disk name for both SCSI and NVMe attachments. If
it does not, the desk fails closed (refuses to mount) and `motel doctor` shows the serial it saw.

## Credentials and VM setup

The token comes from `instance/service-accounts/default/token` and is cached until one minute before it expires. It goes
only into the `Authorization` header of requests to the API host; redirects are refused.

1. Create a **dedicated service account** for the VM (not the default compute account) and bind the custom role below.
2. Create or edit the VM so it runs as that account **and has the access scope
   `https://www.googleapis.com/auth/compute`** (or `cloud-platform`). The default scopes are read only for Compute
   Engine, and scopes can only be changed while the VM is stopped. See
   [access scopes](https://cloud.google.com/compute/docs/access/service-accounts#accesscopesiam).
3. Set `[host] provider = "gcp"`, set `rooms.price_gb_month_eur` if your region or disk type has no built-in price (read
   as USD), then run `sudo motel-desk setup gcp` (no token: the VM's service account is the credential) and `motel doctor`.

**Local-process exposure.** Any local process can ask the metadata server for this service account's token. With
the scoped role the damage is limited, but if you want only root to reach it, block it for others, for example:
`iptables -A OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j REJECT`.

## Least-privilege IAM

Two roles, because IAM Conditions cannot be applied to list calls or to operation reads.

Custom role `motelRead` (bind **without** a condition):

```json
{
  "title": "motelRead",
  "description": "Read disks, this VM and operations",
  "stage": "GA",
  "includedPermissions": [
    "compute.disks.get", "compute.disks.list", "compute.instances.get", "compute.zoneOperations.get", "compute.diskTypes.get"
  ]
}
```

Custom role `motelDisks` (bind **with** the condition below):

```json
{
  "title": "motelDisks",
  "description": "Create, attach, resize, detach and delete motel disks",
  "stage": "GA",
  "includedPermissions": [
    "compute.disks.create", "compute.disks.delete", "compute.disks.resize", "compute.disks.use",
    "compute.instances.attachDisk", "compute.instances.detachDisk"
  ]
}
```

Condition for the `motelDisks` binding (replace PROJECT, ZONE and VM):

```json
{
  "title": "motel disks and this VM only",
  "expression": "(resource.type == 'compute.googleapis.com/Disk' && resource.name.startsWith('projects/PROJECT/zones/ZONE/disks/motel-')) || (resource.type == 'compute.googleapis.com/Instance' && resource.name == 'projects/PROJECT/zones/ZONE/instances/VM')"
}
```

The honest limits:

- **Labels are not an IAM condition attribute.** Unlike AWS, you cannot say "only disks labelled managed-by=motel". The
  name prefix `motel-` is the scoping handle, and motel's own guard still checks labels. A user with the same role could
  create a disk with a `motel-` name and no labels, but could not touch any disk whose name does not start with it.
- `motelRead` lets the credentials **read** all disks in the project (this is also how setup freezes pre-existing disks
  as never-touch). It cannot change them.
- Conditions are evaluated only for resource types that support them. Check
  [conditions overview](https://cloud.google.com/iam/docs/conditions-overview) and
  [resource attributes](https://cloud.google.com/iam/docs/conditions-resource-attributes) for Compute Engine disks and
  instances. Whether `disks.create` evaluates the name condition on a not-yet-existing disk, and whether
  `attachDisk` needs a permission on a resource type the condition does not name, was not tested. If the first live run
  reports a denied permission, fix the role, not the code. Permission list source:
  [Compute Engine IAM roles and permissions](https://cloud.google.com/compute/docs/access/iam).
- Creating a disk may also check `compute.diskTypes.get`; it is in `motelRead` for that reason.
- Where the policy cannot be tightened, the caps (`max_floors`, `max_floor_gb`, `max_total_gb`) and the allowlist guard
  are what stop damage.

## Limits and sharp edges

- **Zonal disks.** The disk must be in the VM's zone; other zones are refused before any call. A same-named disk in
  another zone would share an id with ours in the project-wide listing; motel's names carry random suffixes, so this
  cannot happen with disks motel creates.
- **Resize.** Persistent Disk grows online. Hyperdisk applies a cool-down between modifications (hours; see
  [modify Hyperdisk](https://cloud.google.com/compute/docs/disks/modify-hyperdisks) for the current figure, not verified here).
  A failure comes back as a definite operation error and the desk keeps the old size.
- **Rate limits and retries.** Reads retry on 429, 5xx and network errors; writes retry only on 429 and a 403 whose
  reason is `rateLimitExceeded`. The delay doubles from 1 s to 20 s over 6 attempts. Any other lost or 5xx write is `indeterminate`.
- **Idempotent create.** `disks.insert` carries a `requestId` (a UUID derived from project, zone and disk name), so
  repeating the exact request cannot create a second disk ([request id](https://cloud.google.com/compute/docs/reference/rest/v1/disks/insert)).
- Sizes are GiB (`sizeGb`). Regional (replicated) disks are not created; they would show up in the project listing.

## Pricing

`price_gb_month()` returns a **USD** figure from a small static table (`PRICES_GB_MONTH_USD` in the module), source
<https://cloud.google.com/compute/disks-image-pricing>, dated 2026-11-09. It holds only `pd-balanced` in `us-central1`,
`us-east1` and `us-west1` (USD 0.10 per GiB-month) and is unverified: the pricing page is rendered by script and could not
be read automatically, so every other disk type or region is deliberately absent and raises an error pointing to
`rooms.price_gb_month_eur`. The constructor accepts a `prices` dict to extend the table. Prices are list prices before
tax and ignore Hyperdisk provisioned IOPS and throughput charges.

**Currency.** This provider reports `currency = "USD"`. Motel never converts: with `provider = "gcp"` read
`rooms.price_gb_month_eur` and `rooms.caps.monthly_eur` as USD amounts. The neutral `currency` setting is proposed but
not implemented.

## Wiring

`host.provider = "gcp"` is registered in `motel.providers.registry`. `setup gcp` asks for nothing and freezes every volume the credentials can see as never-touch. `Persistent disk` volumes are blank: the desk formats a new one only after proving it blank. Server and never-touch ids may contain `-`.
