# Azure Managed Disks

> **Beta: not verified against the live API.** This provider was written from the published ARM specifications and a
> loopback fake (`tests/providers/test_azure.py`). No request has been sent to a real subscription. Treat every
> "unverified" item below as a checklist for the first live run.

`provider = "azure"` stores floors on **managed data disks** and attaches them to the VM motel runs on. It talks to
Azure Resource Manager (ARM) with the **managed identity** of that VM. There is no password, key or token file: the
token comes from the Instance Metadata Service (IMDS), and subscription, resource group, VM name and region come
from the IMDS compute metadata. The provider can only act inside the resource group of its own VM.

## What it maps to

| motel | Azure |
|---|---|
| server id | VM name, **lower-cased** (ARM names are case-insensitive and `managedBy` can differ in case) |
| location | region name, e.g. `westeurope` |
| volume id | managed **disk name** (unique inside the resource group) |
| labels | native resource **tags** (up to 50; value up to 256 characters) |
| size | `diskSizeGB`, which is **GiB**; motel's `size_gb` is GiB for this provider |
| device | `/dev/disk/azure/scsi1/lun<N>` (SCSI VMs) or `/dev/disk/azure/data/by-lun/<N>` (NVMe VMs) |
| expected serial | none (Azure data disks expose no serial the guest can compare) |
| price | Azure retail price API, 128 GiB tier of the sku, divided by 128 |
| delete protection | none read; use a `CanNotDelete` lock on anything else in the group |

### Identity on the guest

Azure gives no per-disk serial, so identity rests on the LUN. `device_path()`:

1. reads the VM from ARM **every time** (never from memory) and finds the data-disk entry by disk name, giving the LUN;
2. refuses if the entry is marked `toBeDetached`;
3. cross-checks the VM's **own** IMDS view (`storageProfile.dataDisks`): if IMDS lists the same disk at a different LUN
   the call fails with "Azure disagrees with itself". An IMDS list that has not caught up yet is tolerated;
4. picks the SCSI or NVMe link from the VM's `diskControllerType`.

The udev links need either WALinuxAgent's rules (SCSI) or the `azure-vm-utils` package (NVMe and newer images).
motel also checks that the device holds ext4 and that its filesystem label is `motel-<floor id>` (the label motel wrote when it made the filesystem).

## Setup

1. **Resource group.** Use a resource group that holds only this VM and motel's disks if you can. Put nothing else in
   it that you would mind motel being able to read.
2. **Managed identity.** Give the VM an identity:
   `az vm identity assign -g <group> -n <vm>` (system-assigned), or attach a user-assigned identity and set
   `identity = { client_id = "..." }` for the provider. If the VM has several identities, one must be named.
3. **Two custom roles** (least privilege, below) and two assignments:
   the disk role on the **resource group**, the attach role on the **VM** only.
4. In `/etc/motel/motel.toml` set `provider = "azure"`, leave `server_id` and `location` as `auto`.
   `setup azure` needs no token; it checks that the identity can list the group's VMs and disks, then freezes every
   disk already in the group as never-touch.

### Least-privilege roles

Disk role, assigned at the resource group (everything except changing a VM):

```json
{
  "Name": "motel disk operator",
  "Description": "Create, read, resize and delete managed disks in one resource group; read VMs.",
  "Actions": [
    "Microsoft.Compute/disks/read",
    "Microsoft.Compute/disks/write",
    "Microsoft.Compute/disks/delete",
    "Microsoft.Compute/virtualMachines/read",
    "Microsoft.Compute/locations/DiskOperations/read",
    "Microsoft.Compute/locations/operations/read"
  ],
  "NotActions": [],
  "AssignableScopes": ["/subscriptions/<subscription-id>/resourceGroups/<group>"]
}
```

Attach role, assigned at the **VM resource** (so the identity can rewrite the disk list of this VM and no other):

```json
{
  "Name": "motel disk attacher",
  "Description": "Change the data disks of one virtual machine.",
  "Actions": ["Microsoft.Compute/virtualMachines/read", "Microsoft.Compute/virtualMachines/write"],
  "NotActions": [],
  "AssignableScopes": ["/subscriptions/<subscription-id>/resourceGroups/<group>/providers/Microsoft.Compute/virtualMachines/<vm>"]
}
```

Create each with `az role definition create --role-definition @file.json`, then
`az role assignment create --assignee-object-id <identity principal id> --assignee-principal-type ServicePrincipal --role "..." --scope ...`.
If `networkAccessPolicy` or disk encryption sets are in use in your subscription, extra actions may be needed.

**The identity is not secret on the VM.** Any process on the VM can ask IMDS for the same token (Microsoft documents
the VM as the security boundary). Unlike the root-only Hetzner token file, a local user can use these rights, so keep
the roles as small as above and do not put unrelated resources in the group.

## Behaviour

- **Create** is a `PUT` of an empty disk (`createOption: Empty`) with the tags, the `sku` (default `StandardSSD_LRS`), the
  VM's **zone** (read from the VM; none for `_ZRS` skus) and `networkAccessPolicy: DenyAll` (no SAS export; set
  `deny_export=False` to drop it). It follows the long-running operation, then attaches. A failed attach after a created
  disk raises `indeterminate`, so the desk finds the disk by label and name and does not create a second one.
- **Attach** is a read-modify-write of `storageProfile.dataDisks`:
  1. `GET` the VM and take its **ETag**. No ETag, no write.
  2. If the disk is already listed, stop (idempotent). If the disk is held by another VM or is in another zone, refuse.
  3. Choose the lowest free LUN (0 to 63).
  4. `PATCH` the **whole** array (existing entries sent back untouched, one new entry) with `If-Match: <etag>`.
  5. On **HTTP 412** somebody changed the VM: read it again and recompute. Up to six rounds, then give up with an error;
     the VM is never overwritten blindly.
  6. Follow the operation, then read the VM and confirm the disk sits at that LUN.
- **Detach** is the same loop with the entry removed. It refuses a holder outside this resource group.
- **Resize** is `PATCH diskSizeGB` (grow only), then a read to confirm the size.
- **Delete** is `DELETE`; ARM refuses an attached disk.
- **Long-running operations:** the `Azure-AsyncOperation` URL is polled (`Succeeded` / `Failed` / `Canceled`), else the
  `Location` URL until it stops answering 202. `Retry-After` is honoured. A timeout is `indeterminate`.
- **Safe links:** every `nextLink`, `Azure-AsyncOperation` and `Location` URL must stay on the ARM host before it is
  followed, so the bearer token cannot be sent elsewhere. Redirects are refused. IMDS is called without proxies, with
  `Metadata: true` and no `X-Forwarded-For`.
- **Retries:** reads retry on 429/5xx and network errors; writes retry only on 429. A lost write is `indeterminate`.
- IMDS allows about 5 requests per second per VM (20 per second and 5 at once for tokens). The token is cached until five
  minutes before it expires.

## Limits and caveats

- Disk size 1 to 32,767 GiB. Disks only grow. A disk is **billed at the smallest tier that fits** (below).
- Number of data disks depends on the VM size; the provider stops at LUN 63 and lets ARM refuse sooner.
- **Online resize:** ARM allows growing a data disk without deallocating the VM, with limits (for Standard HDD, Standard
  SSD and Premium SSD of 4 TiB or less, growing past 4 TiB needs the VM deallocated and the disk detached). The guest
  then needs a rescan (`echo 1 > /sys/class/block/<dev>/device/rescan`) before `resize2fs` sees the space; the desk does this (rescan, then wait for the size, then `resize2fs`).
  If ARM refuses, the provider reports the error as an ordinary failure.
- The provider assumes **raw disks**: the desk creates the filesystem on first use, after proving the disk blank.
- Disks and the VM must be in the same region, and the same zone when the VM is zonal.
- `device_path()` of a disk that is not attached raises an error, so the desk reads it after attaching.

## Pricing and why caps are estimates

Source: Azure Retail Prices API, euro, **West Europe**, Standard SSD, locally redundant, queried **2026-11-09**
(`https://prices.azure.com/api/retail/prices?currencyCode='EUR'&$filter=serviceName eq 'Storage' and armRegionName eq 'westeurope' and productName eq 'Standard SSD Managed Disks' and priceType eq 'Consumption'`).

| Tier | Size (GiB) | EUR per month | EUR per GiB-month |
|---|---|---|---|
| E1 | 4 | 0.2693 | 0.0673 |
| E2 | 8 | 0.5386 | 0.0673 |
| E3 | 16 | 1.0772 | 0.0673 |
| E4 | 32 | 2.1545 | 0.0673 |
| E6 | 64 | 4.3090 | 0.0673 |
| E10 | 128 | 8.6180 | 0.0673 |
| E15 | 256 | 17.2360 | 0.0673 |
| E20 | 512 | 34.4719 | 0.0673 |
| E30 | 1,024 | 68.9438 | 0.0673 |
| E40 | 2,048 | 137.8877 | 0.0673 |
| E50 | 4,096 | 275.7754 | 0.0673 |
| E60 | 8,192 | 551.5508 | 0.0673 |
| E70 | 16,384 | 1,103.1016 | 0.0673 |
| E80 | 32,767 | 2,206.2032 | 0.0673 |

Other references (same date, region, 128 GiB tier): Standard SSD **ZRS** E10 12.9270 (0.1010 per GiB); Premium SSD P10
19.4623 (0.1520 per GiB); Standard HDD S10 5.2857 (0.0413 per GiB); LRS snapshots 0.1302 per GB-month; Standard SSD
operations 0.0018 per 10,000. The price page lists an extra "Disk Mount" meter; it is not counted here.

How motel approximates:

- `price_gb_month(region)` asks the retail API for the 128 GiB tier of the configured sku and divides by 128. For Standard
  SSD LRS that is exactly linear across all tiers (0.067328 EUR per GiB-month in West Europe). Premium and HDD are close to
  linear. Prices differ by region, so the lookup uses the host's own region. If it fails, motel tells you to set
  `rooms.price_gb_month_eur`.
- Azure does **not** bill per GiB. A 130 GiB disk is billed as 256 GiB. Motel's caps multiply requested GiB by the rate, so
  they can **under-count by up to about 2x** (just above a tier boundary). `AzureProvider.billable_gb(n)` returns the tier a
  size is billed at. The desk uses it for the monthly cap.
- Transactions, snapshots, bursting and egress are not included. **Treat every cap as an estimate, not a bill.**

## Wiring

`host.provider = "azure"` is registered. Optional `[provider] sku = "Premium_LRS"` and `client_id = "..."` (a user-assigned identity). The desk uses `billable_gb` so the monthly cap counts the billed tier.

## Not verified against the live API

- That a VM `PATCH` with a stale `If-Match` returns **412** (the specification lists `If-Match` and an `etag` property;
  the behaviour was not observed), and that a `PATCH` of `dataDisks` replaces the array as assumed.
- That existing `dataDisks` entries returned by `GET` are accepted back unchanged in a `PATCH` for every VM shape.
- That attach and detach complete on a running VM with only the roles above, and that `disks/write` is the only disk
  right the attach needs.
- `networkAccessPolicy: DenyAll` on create, and the zone handling for ZRS and non-zonal VMs.
- The `Azure-AsyncOperation` / `Location` behaviour of disk `PUT`, `PATCH` and `DELETE` in practice.
- The IMDS fields (`storageProfile.dataDisks[].lun` and `.name`) at `api-version=2025-04-07`, and how long IMDS lags.
- `diskControllerType` from ARM for the NVMe link choice, and the link names on each distribution image.
- Online resize, the 4 TiB rule, and how fast the guest sees new space.
- The retail API filter (note: `productName` values for Premium and HDD were read once, not from documentation).
- The custom role action list is enough (for example `locations/DiskOperations/read` for operation polling).

## Sources

Read on 2026-11-09.

- Disks REST specification, api-version 2025-01-02 (create, update, delete, list, sku, `diskSizeGB`, `managedBy`, `diskState`, resize rule): https://github.com/Azure/azure-rest-api-specs/blob/main/specification/compute/resource-manager/Microsoft.Compute/Compute/stable/2025-01-02/DiskRP.json
- Virtual machines REST specification, api-version 2025-04-01 (`etag`, `If-Match`, `dataDisks`, `lun`, `toBeDetached`, `diskControllerType`, long-running options): https://github.com/Azure/azure-rest-api-specs/blob/main/specification/compute/resource-manager/Microsoft.Compute/Compute/stable/2025-04-01/ComputeRP.json
- Disks REST reference: https://learn.microsoft.com/rest/api/compute/disks
- Virtual machines update: https://learn.microsoft.com/rest/api/compute/virtual-machines/update
- Long-running operations and `Azure-AsyncOperation`: https://learn.microsoft.com/azure/azure-resource-manager/management/async-operations
- Managed identity token from IMDS (request, response, errors, retry): https://learn.microsoft.com/entra/identity/managed-identities-azure-resources/how-to-use-vm-token
- Instance Metadata Service (compute fields, storage profile, `Metadata: true`, no `X-Forwarded-For`, rate limits): https://learn.microsoft.com/azure/virtual-machines/instance-metadata-service
- Device links `/dev/disk/azure/scsi1/lun<N>` and `/dev/disk/azure/data/by-lun/<N>`: https://learn.microsoft.com/azure/virtual-machines/linux/azure-virtual-machine-utilities and https://learn.microsoft.com/troubleshoot/azure/virtual-machines/linux/troubleshoot-device-names-problems
- Expanding disks, online expansion limits, Linux rescan: https://learn.microsoft.com/azure/virtual-machines/linux/expand-disks
- Retail price API: https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
- Price page (tiers, snapshots): https://azure.microsoft.com/pricing/details/managed-disks/
