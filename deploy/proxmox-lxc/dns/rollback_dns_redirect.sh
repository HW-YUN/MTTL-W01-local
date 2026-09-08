#!/bin/sh
# Remove the dnsmasq redirect (mef/brk2 -> relay) and return to normal resolution.
# Run on the relay host as root.
set -eu

LIVE="/etc/dnsmasq.d/mttl-relay.conf"
BACKUP_ROOT="/srv/mttl-lab/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

if [ -f "$LIVE" ]; then
  mkdir -p "$BACKUP_ROOT/$STAMP-dns-removed"
  cp -p "$LIVE" "$BACKUP_ROOT/$STAMP-dns-removed/mttl-relay.conf"
  rm -f "$LIVE"           # exact single filename, no glob
  echo "removed $LIVE (copy kept in $BACKUP_ROOT/$STAMP-dns-removed/)"
else
  echo "$LIVE not present — nothing to remove"
fi

dnsmasq --test
systemctl reload dnsmasq
sleep 1

echo
echo "verify (should now resolve to the real LG U+ IPs again):"
for d in mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr dev.toi.ommeq.com; do
  printf '  %s -> ' "$d"; getent hosts "$d" | awk '{print $1}' | head -1
done
echo
echo "compare against the real upstream values from an un-redirected resolver, e.g.:"
echo "  dig +short mef.onem2m.uplus.co.kr @1.1.1.1"
