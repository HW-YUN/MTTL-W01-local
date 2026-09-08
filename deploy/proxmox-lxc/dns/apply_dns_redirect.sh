#!/bin/sh
# Apply the dnsmasq redirect (mef/brk2 -> relay).  Run on the relay host as root.
#
# This is a STOP-POINT action: it makes the device's next cloud query land on the
# relay. Only run it when the relay is already up and you are ready to power the
# device on.
#
# The redirect target IP is NOT hard-coded. Provide it one of two ways:
#   1. MTTL_RELAY_HOST_IP=<ip>  sh apply_dns_redirect.sh
#      -> the <RELAY_HOST_IP> token in config/dns/mttl-relay.conf.example is
#         substituted at install time.
#   2. sh apply_dns_redirect.sh /path/to/your-filled-in-mttl-relay.conf
#      -> the file you pass is installed as-is (you already replaced the token).
# With neither, the script prints usage and exits non-zero.
#
# Backs up the current /etc/dnsmasq.d/ state first (NOT inside /etc/dnsmasq.d/).
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TEMPLATE="$ROOT/config/dns/mttl-relay.conf.example"
LIVE="/etc/dnsmasq.d/mttl-relay.conf"
BACKUP_ROOT="/srv/mttl-lab/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$BACKUP_ROOT/$STAMP-dns"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

# ---- resolve the config to install ----------------------------------------
WORK="$(mktemp)"
trap 'rm -f "$WORK"' EXIT
if [ "$#" -ge 1 ] && [ -f "$1" ]; then
  cp "$1" "$WORK"
elif [ -n "${MTTL_RELAY_HOST_IP:-}" ]; then
  [ -f "$TEMPLATE" ] || { echo "template missing: $TEMPLATE" >&2; exit 1; }
  sed "s/<RELAY_HOST_IP>/${MTTL_RELAY_HOST_IP}/g" "$TEMPLATE" > "$WORK"
else
  echo "usage: MTTL_RELAY_HOST_IP=<ip> sh $0" >&2
  echo "   or: sh $0 /path/to/filled-in-mttl-relay.conf" >&2
  exit 2
fi
if grep -q '<RELAY_HOST_IP>' "$WORK"; then
  echo "config still contains the <RELAY_HOST_IP> placeholder — refusing to install" >&2
  exit 2
fi

mkdir -p "$BACKUP"
# snapshot every existing dnsmasq.d file by exact name (no globs into a delete)
if [ -d /etc/dnsmasq.d ]; then
  for f in /etc/dnsmasq.d/*; do
    [ -e "$f" ] || continue
    cp -p "$f" "$BACKUP/$(basename "$f")"
  done
fi
echo "no redirect was present" > "$BACKUP/PRE_STATE.txt"
[ -f "$LIVE" ] && echo "WARNING: $LIVE already existed (see copy in this dir)" > "$BACKUP/PRE_STATE.txt"

cp "$WORK" "$LIVE"
echo "installed $LIVE :"
cat "$LIVE"

dnsmasq --test
systemctl reload dnsmasq
sleep 1

echo
echo "verify (mef/brk2 should now resolve to the relay host):"
for d in mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr; do
  printf '  %s -> ' "$d"; getent hosts "$d" | awk '{print $1}' | head -1
done
echo "verify dev.toi is UNCHANGED (real IP via CNAME):"
printf '  dev.toi.ommeq.com -> '; getent hosts dev.toi.ommeq.com | awk '{print $1}' | head -1

echo
echo "backup of previous state: $BACKUP"
echo "rollback: sh $(dirname "$0")/rollback_dns_redirect.sh"
