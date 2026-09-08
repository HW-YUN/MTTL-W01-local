#!/bin/sh
# Remove the QMS-sink dnsmasq drop-in.  Run on the relay host as root.
#
#   Deletes exactly ONE file by exact name: /etc/dnsmasq.d/mttl-qms-sink.conf .
#   Before deleting: verify it exists, make an exact-filename timestamp backup,
#   verify the backup exists and its size matches, THEN remove.
#   No wildcard / glob anywhere. mttl-relay.conf and every other file are untouched.
set -eu

LIVE="/etc/dnsmasq.d/mttl-qms-sink.conf"
BACKUP_DIR="/srv/mttl-lab/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

if [ ! -e "$LIVE" ]; then
  echo "$LIVE not present — nothing to remove"
  dnsmasq --test
  exit 0
fi

LIVE_SIZE=$(wc -c < "$LIVE")
BK="$BACKUP_DIR/${STAMP}-qms-sink-dns-rollback/mttl-qms-sink.conf"
mkdir -p "$(dirname "$BK")"
cp -p "$LIVE" "$BK"
[ -f "$BK" ] || { echo "backup NOT created: $BK" >&2; exit 1; }
BK_SIZE=$(wc -c < "$BK")
[ "$LIVE_SIZE" = "$BK_SIZE" ] || { echo "backup size mismatch ($LIVE_SIZE != $BK_SIZE)" >&2; exit 1; }
echo "backed up:  $LIVE ($LIVE_SIZE B)  ->  $BK ($BK_SIZE B)   [verified]"

rm "$LIVE"
[ ! -e "$LIVE" ] || { echo "remove failed: $LIVE still present" >&2; exit 1; }
echo "removed exactly: $LIVE"

dnsmasq --test
systemctl reload dnsmasq
sleep 1

_q() {
  if command -v dig >/dev/null 2>&1; then dig +short @127.0.0.1 "$1" A | head -1
  elif command -v nslookup >/dev/null 2>&1; then nslookup "$1" 127.0.0.1 2>/dev/null | awk '/^Address: /{print $2}' | head -1
  else echo "(no dig/nslookup)"; fi
}
echo
echo "verify via local dnsmasq (127.0.0.1) — QMS hosts no longer forced to the relay:"
for d in log.toi.ommeq.com hdslog.lguplus.co.kr; do printf '  %s -> ' "$d"; _q "$d"; done
echo "(mttl-relay.conf mef/brk2 redirect is untouched by this script.)"
