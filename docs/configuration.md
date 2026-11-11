# Configuration

`/etc/motel/motel.toml` (root, not writable by others). The file is validated strictly: unknown keys,
wrong types, impossible combinations and unsafe mount roots stop every command with a message that names
the file and the problem. `motel doctor` reports the same error.

```toml
[host]
provider    = "hetzner"        # or digitalocean, linode, vultr, scaleway, aws, gcp, azure, local (docs/providers.md)
server_id   = "auto"           # frozen from the metadata service by `setup`
location    = "auto"
allow_users = ["alice", "@agents"]   # users and @groups; also written to the sudoers rule by install.sh
# never_touch = [123456]       # extra volume ids that must never be touched; can add, never remove

[rooms]
mount_root     = "/mnt/motel"  # a dedicated directory, at least two path components
filesystem     = "ext4"        # xfs with quotas arrives in a later release
default_ttl    = "6h"
max_ttl_call   = "7d"          # per checkin and per extend
max_lifetime   = "14d"         # a room can never be extended past this
grace          = "6h"          # after checkout time; extend revives the room
lost_and_found = "24h"         # 0h purges at checkout
min_floor_gb   = 50            # shared floors start at this size
warn_before    = "2h"          # status/ls warn on stderr this long before checkout
retire_after   = "15m"         # an empty floor is deleted this long after its last room ...
release_margin = "6m"          # ... but on a cloud that bills per started hour, only in the last 10 minutes before this margin
                               # ahead of the next billed hour (4m to 30m); see lifecycle.md, "Floors"
# price_gb_month_eur = 0.05    # overrides the provider's live price in cap calculations
# eur_per_usd = 0.92           # clouds with a dollar price list (digitalocean, linode, vultr); default 1

[rooms.caps]
max_floors   = 3
max_floor_gb = 500
max_total_gb = 1000
monthly_eur  = 30              # projected: provisioned GB x price per GB-month

[local]                        # only for provider = "local"
dir = "/var/lib/motel/local"
```

Durations are `<number>m|h|d`. Sizes in the CLI are `200G`, `1T` (1024 GB) or a plain number of GB.
`[cabins]` is the cabin table (cabins run on Hetzner Cloud whatever `host.provider` is; see [cabins.md](cabins.md)). `[provider]` holds the few options a cloud understands: `block_type` (vultr), `sku` and `client_id` (azure). Unknown keys are errors.

`[tours]` holds the ceilings for [tours](tours.md) and is off until `enabled = true`; its keys are validated by the
concierge's parser ([concierge.md](concierge.md)) plus `[tours.own_login]`:

```toml
[tours]
enabled = true
max_concurrent_guests = 4      # per host, across tours
max_guests_per_tour = 20
max_eur_per_tour = 5.0         # cloud spend reserved per tour
max_usd_model_per_guest = 2.0  # model budget per attempt
max_ttl = "4h"                 # wall clock per attempt
max_result_mb = 64
max_active_tours_per_user = 3  # unfinished tours of one user
max_staged_mb_per_user = 1024  # staged repository bundles of one user's unfinished tours
concierge_port = 7781          # the fixed port on the guest
concierge_port_range = "17800-17999"   # controller loopback ports, one listener per live attempt
egress_allow = ["github.com"]  # hosts a tour may ask for
default_image = "ubuntu-24.04"

[tours.bindings.anthropic]     # a named upstream; the key is a file under /etc/motel/llm-keys/
key_file = "/etc/motel/llm-keys/anthropic.key"
profile = "claude-code"        # the beta headers `claude -p` sends
# upstream, auth, paths, pricing, betas, allow: see concierge.md

[tours.own_login]              # opt-in, off by default: see "Using your own Claude login" in tours.md
enabled = false
allow_users = []               # who may use it; empty means nobody
claude_token_file = "/etc/motel/credentials/own-login/claude.token"
max_concurrent = 2
max_ttl = "2h"
```

## Files

| path | owner / mode | content |
|---|---|---|
| `/etc/motel/motel.toml` | root, 0644 | this file |
| `/etc/motel/setup-receipt.json` | root, 0644 | nonsecret: provider, server id, location, never-touch volumes |
| `/etc/motel/credentials/hetzner.token` | root, 0600 (dir 0700) | the API token |
| `/var/lib/motel/` | root, 0700 | state, lock, guest book |
| `/etc/sudoers.d/motel` | root, 0440 | written by `install.sh` from `allow_users` |

Changing `allow_users`: edit the file, then run `sudo ./install.sh` again (it regenerates the sudoers rule
from the file). The desk enforces `allow_users` itself as well.

## Environment

The installed desk ignores the environment (and `sudo` resets it). There is no override for paths or
credentials; tests construct the desk in Python with explicit paths and fakes.
