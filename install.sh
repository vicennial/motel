#!/bin/sh
# Install the motel front desk. Run as root from a ROOT-OWNED checkout (sudo git clone into /usr/local/src; docs/install.md):
#
#   sudo ./install.sh                      # users: allow_users of an existing config, else $SUDO_USER
#   sudo ./install.sh --users alice,@agents --provider hetzner
#   ./install.sh --prefix /tmp/stage --dry-run     # show the plan; nothing is changed
#
# From a wheel, `sudo motel install` does the same. Options: --prefix DIR  --dry-run  --users LIST  --provider NAME  --cabins  --upgrade  --cron  --no-systemd  --no-visudo
set -eu
here=$(cd "$(dirname "$0")" && pwd)
exec /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from motel.installer import main; raise SystemExit(main(sys.argv[2:], prog="install.sh"))' "$here/src" "$@"
