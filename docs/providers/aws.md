# AWS EBS provider

> **Beta: not verified against the live API.** Written from the AWS documentation and tested against a fake EC2
> API and a fake IMDS on a loopback socket (`tests/providers/test_aws.py`). Nobody has run it against a real
> account yet. Treat the first live run on a scratch account as part of the setup.

Module: [`providers/aws.py`](../../src/motel/providers/aws.py), standard library only. It talks to the
[EC2 Query API](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/Welcome.html) (`Version=2016-11-15`) and signs
requests with [Signature Version 4](https://docs.aws.amazon.com/IAM/latest/UserGuide/reference_sigv.html) implemented
with `hmac`/`hashlib`. The signer is checked against AWS's published
[test suite](https://docs.aws.amazon.com/general/latest/gr/signature-v4-test-suite.html) values.

## Why this provider is easier to lock down than Hetzner

A Hetzner token sees the whole project and cannot be narrowed. AWS lets you scope the credentials with **tag
conditions**: the policy below allows attach, detach, resize and delete **only on volumes tagged
`managed-by = motel`**, and allows tagging **only at creation**, so the credentials cannot retag someone else's
volume to make it theirs. Even a stolen or misused motel credential cannot touch your root volumes, databases or
backups. The allowlist guard inside motel stays on; IAM is the second wall behind it.

## What it does

| Interface method | EC2 call(s) | Notes |
|---|---|---|
| `whoami` | IMDSv2: `instance-id`, `placement/availability-zone`, `placement/region` | Token `PUT` first; there is no IMDSv1 fallback. `location` is the **availability zone** (`us-east-1a`). |
| `visible` | `DescribeInstances` (this instance), `DescribeVolumes` (all pages) | Every volume in the region the credentials can see goes into never-touch at setup. |
| `get_volume` | `DescribeVolumes` with `VolumeId.1` | |
| `list_owned` | `DescribeVolumes` with `tag:<key>` filters, then re-checked client side | Paginated with `NextToken`; failure raises `InventoryIncomplete`. |
| `create_volume` | `CreateVolume` (tags in `TagSpecification`, `ClientToken` = volume name), wait `available`, `AttachVolume` | Created in this instance's AZ only. If anything after the create fails, the error is `indeterminate` so the desk finds the volume by label and name and attaches it later instead of forgetting a billed volume. |
| `attach` | `AttachVolume`, wait `attached` | Picks the first free `/dev/sd[f-p]` then `/dev/sd[q-z]` name from the instance's current attachments; retries the next name if EC2 says it is in use. |
| `detach` | `DetachVolume`, wait `available` | No call if already detached. The desk unmounts first. |
| `resize` | `ModifyVolume`, poll `DescribeVolumesModifications` until `optimizing` or `completed` | Grow only. See limits. |
| `delete` | `DeleteVolume` | `VolumeInUse` is a definite error. |
| `device_path` | n/a | `/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_vol0123456789abcdef0` |
| `expected_serial` | n/a | The volume id without the dash (`vol0123456789abcdef0`), which is what `lsblk -o SERIAL` shows for EBS on NVMe. |
| `price_gb_month` | static table | USD. See Pricing. |

Labels are EBS tags: `managed-by`, `role`, `host`, `floor`, `suite`, `created`. The motel volume name is the `Name`
tag. `delete_protected` is always false: EBS has no delete protection (use the tag policy instead).

**Instances must be Nitro (NVMe) instances.** The stable device link above exists only where the EBS volume appears
as an NVMe device. On older Xen instances there is no such link and this provider is not usable. See
[identify NVMe EBS devices](https://docs.aws.amazon.com/ebs/latest/userguide/identify-nvme-ebs-device.html) and
[device naming](https://docs.aws.amazon.com/ebs/latest/userguide/device_naming.html). On NVMe the `/dev/sdX` name given
to `AttachVolume` is only a hint, which is why the desk mounts by the by-id link and checks the serial.

## Credentials

Preferred: **the instance role.** Attach an instance profile whose role carries the policy below. The provider reads
short-lived keys from `iam/security-credentials/<role>` through IMDSv2 and refreshes them five minutes before they
expire. Nothing long-lived is written to disk. See
[IAM roles for EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html) and
[configuring IMDS](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/configuring-instance-metadata-service.html).

Alternative: a root-owned credentials file (for a host outside EC2 networking rules, or if you must not expose the role
to local processes). `/etc/motel/credentials/aws.credentials`, owned by root, mode 0600 or stricter, not a symlink:

```
aws_access_key_id = AKIA...
aws_secret_access_key = ...
# aws_session_token = ...   (optional)
```

`load_credentials_file()` checks owner, mode and file type on the open descriptor and never prints the content.
This keeps a long-lived key on disk, so give that IAM user only the policy below.

**Local-process exposure of the instance role.** Any local process can reach IMDS and use the role's credentials
directly, including the agents motel serves. With the tag-scoped policy the damage is limited to motel-tagged
volumes, but if you want only root to reach IMDS, block it for everyone else, for example:
`iptables -A OUTPUT -d 169.254.169.254 -m owner ! --uid-owner 0 -j REJECT`. Setting the IMDS hop limit to 1 (the
default) also keeps containers off it. Use the file mode instead if you cannot do this.

## Least-privilege IAM policy

Replace `REGION`, `ACCOUNT` and the instance id. This is a policy for the role or user motel runs as.

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ReadOnly",
      "Effect": "Allow",
      "Action": ["ec2:DescribeVolumes", "ec2:DescribeVolumesModifications", "ec2:DescribeInstances"],
      "Resource": "*"
    },
    {
      "Sid": "CreateOnlyTaggedSmallGp3",
      "Effect": "Allow",
      "Action": "ec2:CreateVolume",
      "Resource": "arn:aws:ec2:REGION:ACCOUNT:volume/*",
      "Condition": {
        "StringEquals": {"aws:RequestTag/managed-by": "motel", "ec2:VolumeType": "gp3"},
        "NumericLessThanEquals": {"ec2:VolumeSize": 500},
        "ForAllValues:StringEquals": {"aws:TagKeys": ["managed-by", "role", "host", "floor", "suite", "created", "Name"]}
      }
    },
    {
      "Sid": "TagOnlyAtCreation",
      "Effect": "Allow",
      "Action": "ec2:CreateTags",
      "Resource": "arn:aws:ec2:REGION:ACCOUNT:volume/*",
      "Condition": {"StringEquals": {"ec2:CreateAction": "CreateVolume"}}
    },
    {
      "Sid": "ChangeOnlyMotelVolumes",
      "Effect": "Allow",
      "Action": ["ec2:AttachVolume", "ec2:DetachVolume", "ec2:ModifyVolume", "ec2:DeleteVolume"],
      "Resource": "arn:aws:ec2:REGION:ACCOUNT:volume/*",
      "Condition": {"StringEquals": {"aws:ResourceTag/managed-by": "motel"}}
    },
    {
      "Sid": "AttachToThisInstanceOnly",
      "Effect": "Allow",
      "Action": ["ec2:AttachVolume", "ec2:DetachVolume"],
      "Resource": "arn:aws:ec2:REGION:ACCOUNT:instance/i-0123456789abcdef0"
    }
  ]
}
```

What each statement buys you, and what it does not:

- The two `Attach` statements are separate on purpose: `AttachVolume` is authorized against both the volume and the
  instance, and a tag condition on a shared statement would also be evaluated against the instance.
- `ec2:VolumeSize` and `ec2:VolumeType` repeat motel's own caps as a hard IAM ceiling. Match them to
  `rooms.caps.max_floor_gb` and `volume_type`. A monthly-cost cap cannot be expressed in IAM; motel enforces it.
- The Describe calls do not support resource-level permissions, so the credentials can still **read** every volume and
  instance in the region (this is also why setup freezes them as never-touch). They cannot change them.
- Encrypted volumes: with the AWS managed `aws/ebs` key no extra permission is needed. A customer managed key needs
  `kms:CreateGrant`, `kms:GenerateDataKeyWithoutPlaintext` and `kms:DescribeKey` on that key for the instance role.
- Policy references: [EC2 API permissions](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-permissions.html) and
  [tagging on create](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/supported-iam-actions-tagging.html).
  Condition keys and the CreateTags-on-create pattern were written from these pages; confirm with the IAM policy
  simulator in your account.

## Setup steps

1. Launch (or pick) a Nitro instance with IMDSv2 required (`HttpTokens=required`) and a hop limit of 1.
2. Create the policy above, a role with it, and attach the role as the instance profile. (File mode: create an IAM
   user with the policy and write the credentials file.)
3. Choose `volume_type` (default `gp3`; `iops` is needed for `io1`/`io2`). Defaults need no other settings.
4. Set `[host] provider = "aws"` in `/etc/motel/motel.toml`, set `rooms.price_gb_month_eur` if your region has no
   built-in price (the number is read as USD for this provider), then run the setup command and `motel doctor`.
   `sudo motel-desk setup aws` needs no token: identity and credentials come from the instance.

## Limits and sharp edges

- **Resize cool-down.** EBS allows one modification per volume every 6 hours, and only an increase. A second grow of
  the same floor inside that window fails with a clear, definite error (`VolumeModificationRateExceeded`), and the
  desk keeps the old size. See [modifying volumes](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-modify-volume.html).
  The provider waits only for the `optimizing` state, when the new size is already visible to the instance; the volume
  keeps optimizing in the background.
- **Eventual consistency.** A volume just created can be missing from `DescribeVolumes` for a moment; the waiter
  tolerates `InvalidVolume.NotFound` for 30 seconds after create.
- **Throttling.** Reads retry on `RequestLimitExceeded` and the other throttling codes, on 5xx and on network errors,
  with a doubling delay (1 s up to 20 s, 6 attempts). Writes retry only on throttling codes (the request was rejected). Any
  other lost or 5xx write is `indeterminate`. See [API throttling](https://docs.aws.amazon.com/ec2/latest/devguide/ec2-api-throttling.html).
- **Idempotent create.** `CreateVolume` carries a `ClientToken` equal to the motel volume name, so repeating the exact
  same request cannot create a second volume.
- Sizes are GiB. One region, one AZ: the instance's. Cross-AZ volumes are refused before any call.
- XML responses are parsed with `xml.etree` from the TLS endpoint chosen by the region; redirects are refused so
  credentials cannot follow one to another host.

## Pricing

`price_gb_month()` returns a **USD** figure for the instance's region from a small static table
(`PRICES_GB_MONTH_USD` in the module), source <https://aws.amazon.com/ebs/pricing/>, dated 2026-11-09. The table holds
only gp3 in `us-east-1`, `us-east-2` and `us-west-2` (USD 0.08 per GB-month). The pricing page is rendered by script
and could not be read automatically, so other regions are deliberately absent: a region without an entry raises an
error that points to `rooms.price_gb_month_eur`, and the constructor accepts a `prices` dict to extend the table.
Prices are list prices before tax and ignore provisioned IOPS/throughput above the gp3 baseline.

**Currency.** The desk's cap settings are named `*_eur` and the Hetzner price is a euro gross figure. This provider
reports `currency = "USD"`. Motel never converts: with `provider = "aws"` read `rooms.price_gb_month_eur` and
`rooms.caps.monthly_eur` as USD amounts. A neutral `currency` setting is proposed but not implemented.

## Wiring

`host.provider = "aws"` is registered in `motel.providers.registry`. `setup aws` asks for nothing and freezes every volume the credentials can see as never-touch. `EBS` volumes are blank: the desk formats a new one only after proving it blank. Server and never-touch ids may contain `-`.
