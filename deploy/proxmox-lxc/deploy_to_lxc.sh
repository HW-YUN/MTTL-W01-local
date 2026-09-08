#!/bin/sh
# Deploy the local relay + dashboard to a Proxmox LXC container under /srv/mttl-lab/.
#
# Run from the Proxmox host (it has `pct`).
#
#   sh deploy/proxmox-lxc/deploy_to_lxc.sh <CTID>            # push + verify, do NOT start
#   sh deploy/proxmox-lxc/deploy_to_lxc.sh <CTID> --start    # also install + start services
#   MTTL_LXC_CTID=<CTID> sh deploy/proxmox-lxc/deploy_to_lxc.sh [--start]
#
# <CTID> is the numeric container id. An explicit argument wins; otherwise the
# MTTL_LXC_CTID environment variable is used. With neither, the script exits with
# a usage message.
#
# What it does:
#   1. backs up any existing /srv/mttl-lab/ (NOT under /etc/dnsmasq.d/)
#   2. pushes the repo tree into /srv/mttl-lab/ via `pct exec` (tar stream)
#   3. copies vendor relay.py + deps next to smartrelay.toml, installs smartrelay.toml
#   4. installs systemd unit files (disabled)
#   5. runs the in-container synthetic tests
#   6. (--start only) generates the CA and starts mttl-relay + mttl-dashboard
set -eu

CT=""
START=0
for a in "$@"; do
  case "$a" in
    --start) START=1 ;;
    ''|*[!0-9]*) echo "unexpected argument: $a" >&2; exit 2 ;;
    *) CT="$a" ;;
  esac
done
[ -n "$CT" ] || CT="${MTTL_LXC_CTID:-}"
case "$CT" in
  ''|*[!0-9]*)
    echo "usage: sh $0 <CTID> [--start]   (or set MTTL_LXC_CTID)" >&2
    exit 2 ;;
esac

DEST=/srv/mttl-lab
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"     # repo root
STAMP="$(date +%Y%m%d-%H%M%S)"

command -v pct >/dev/null || { echo "pct not found — run this on the Proxmox host" >&2; exit 1; }

echo "[1/6] backup existing $DEST in CT $CT (if any)"
pct exec "$CT" -- sh -c '
  set -e
  if [ -d '"$DEST"' ]; then
    mkdir -p '"$DEST"'/backups
    tar czf '"$DEST"'/backups/'"$STAMP"'-predeploy.tgz \
      --exclude backups --exclude relay/certs --exclude relay/state -C '"$DEST"' . || true
    echo "  backed up to '"$DEST"'/backups/'"$STAMP"'-predeploy.tgz"
  else
    echo "  (no existing '"$DEST"')"
  fi
  mkdir -p '"$DEST"' '"$DEST"'/relay/certs '"$DEST"'/relay/state '"$DEST"'/backups
'

echo "[2/6] push repo tree -> $DEST"
tar czf - -C "$ROOT" \
  --exclude '.git' --exclude 'relay/certs' --exclude 'relay/state' --exclude '__pycache__' . \
  | pct exec "$CT" -- sh -c "tar xzf - -C $DEST"

echo "[3/6] place relay.py + deps + smartrelay.toml in $DEST/relay"
pct exec "$CT" -- sh -c '
  set -e
  cd '"$DEST"'/relay
  for f in relay.py mqtt_session.py mqtt_wire.py rules_engine.py; do
    cp -f ../vendor/smartrelay/$f ./$f
  done
  [ -f smartrelay.toml ] || cp ../config/relay/smartrelay.toml.example smartrelay.toml
  chmod +x ./gen_local_ca.sh ../deploy/proxmox-lxc/*.sh ../deploy/proxmox-lxc/dns/*.sh 2>/dev/null || true
  echo "  relay dir: $(ls)"
'

echo "[4/6] install systemd units (disabled)"
pct exec "$CT" -- sh -c '
  cp '"$DEST"'/deploy/proxmox-lxc/systemd/mttl-relay.service /etc/systemd/system/
  cp '"$DEST"'/deploy/proxmox-lxc/systemd/mttl-dashboard.service /etc/systemd/system/
  systemctl daemon-reload
  echo "  installed: mttl-relay.service, mttl-dashboard.service (not enabled)"
'

echo "[5/6] synthetic tests (in container)"
pct exec "$CT" -- sh -c "cd $DEST && sh tests/synthetic_tests.sh"

if [ "$START" = 1 ]; then
  echo "[6/6] --start: generate CA + start services"
  pct exec "$CT" -- sh -c "
    set -e
    cd $DEST
    sh relay/gen_local_ca.sh $DEST/relay/certs
    systemctl restart mttl-relay
    sleep 2
    systemctl restart mttl-dashboard
    sleep 1
    systemctl --no-pager --full status mttl-relay mttl-dashboard | sed -n '1,12p'
    echo
    echo 'dashboard: http://<this-container-ip>:8080/'
    ss -tlnp | grep -E ':(80|443|18831|8080|9883)\b' || true
  "
else
  echo "[6/6] deploy done (services NOT started). To start:"
  echo "   pct exec $CT -- sh -c 'cd $DEST && sh relay/gen_local_ca.sh \$PWD/relay/certs && systemctl start mttl-relay mttl-dashboard'"
fi
