#!/bin/sh
# Apply the QMS-sink dnsmasq drop-in.  Run on the relay host as root.
#
#   ADDITIVE: installs exactly ONE new file  /etc/dnsmasq.d/mttl-qms-sink.conf .
#   It never reads, copies, edits or removes /etc/dnsmasq.d/mttl-relay.conf or
#   any other file. dev.toi.ommeq.com is left unchanged.
#   No wildcard / glob is used anywhere in this script.
#
# Effect: log.toi.ommeq.com + hdslog.lguplus.co.kr resolve to the relay, so a QMS
# HTTPS attempt is observed by rule relay/rules/16-qms-sink.py instead of being
# blackholed. Nothing is forwarded upstream (relay is decloud).
#
# The relay IP is NOT hard-coded. Provide it one of two ways:
#   1. MTTL_RELAY_HOST_IP=<ip>  sh apply_qms_sink_dns.sh
#      -> the <RELAY_HOST_IP> token in config/dns/mttl-qms-sink.conf.example is
#         substituted at install time.
#   2. sh apply_qms_sink_dns.sh /path/to/your-filled-in-mttl-qms-sink.conf
# With neither, the script prints usage and exits non-zero.
#
# This is a STOP-POINT action. Only run it when you are ready to observe QMS and
# 16-qms-sink.py is already deployed to the relay's rules directory.
set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
TEMPLATE="$ROOT/config/dns/mttl-qms-sink.conf.example"
LIVE="/etc/dnsmasq.d/mttl-qms-sink.conf"
BACKUP_DIR="/srv/mttl-lab/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"

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
  echo "   or: sh $0 /path/to/filled-in-mttl-qms-sink.conf" >&2
  exit 2
fi
if grep -q '<RELAY_HOST_IP>' "$WORK"; then
  echo "config still contains the <RELAY_HOST_IP> placeholder — refusing to install" >&2
  exit 2
fi

SRC_SIZE=$(wc -c < "$WORK")

if [ -e "$LIVE" ]; then
  # ---- target already exists: exact-filename backup, verify, then overwrite ----
  BK="$BACKUP_DIR/${STAMP}-qms-sink-dns/mttl-qms-sink.conf.pre"
  mkdir -p "$(dirname "$BK")"
  cp -p "$LIVE" "$BK"
  [ -f "$BK" ] || { echo "backup NOT created: $BK" >&2; exit 1; }
  OLD_SIZE=$(wc -c < "$LIVE")
  BK_SIZE=$(wc -c < "$BK")
  [ "$OLD_SIZE" = "$BK_SIZE" ] || { echo "backup size mismatch ($OLD_SIZE != $BK_SIZE)" >&2; exit 1; }
  echo "target exists — backed up:"
  echo "  $LIVE  ($OLD_SIZE B)  ->  $BK  ($BK_SIZE B)   [verified]"
  cp "$WORK" "$LIVE"
  echo "overwrote $LIVE ($SRC_SIZE B)"
else
  # ---- NEW FILE ----
  echo "target does not exist — NEW FILE: $LIVE"
  cp "$WORK" "$LIVE"
  echo "installed $LIVE ($SRC_SIZE B)"
fi

NEW_SIZE=$(wc -c < "$LIVE")
[ "$NEW_SIZE" = "$SRC_SIZE" ] || { echo "post-install size mismatch ($NEW_SIZE != $SRC_SIZE)" >&2; exit 1; }
echo "installed content:"
grep -v '^#' "$LIVE" | grep -v '^$' || true

dnsmasq --test
systemctl reload dnsmasq
sleep 1

# Query the LOCAL dnsmasq directly — the host's own resolver may bypass it, so
# `getent` here could be misleading.
_q() {
  if command -v dig >/dev/null 2>&1; then
    dig +short @127.0.0.1 "$1" A | head -1
  elif command -v nslookup >/dev/null 2>&1; then
    nslookup "$1" 127.0.0.1 2>/dev/null | awk '/^Address: /{print $2}' | head -1
  else
    echo "(no dig/nslookup — query from a client on the redirected network instead)"
  fi
}
echo
echo "verify via local dnsmasq (127.0.0.1) — QMS hosts should now point at the relay:"
for d in log.toi.ommeq.com hdslog.lguplus.co.kr; do printf '  %s -> ' "$d"; _q "$d"; done
echo "verify UNCHANGED (must NOT be redirected by this drop-in):"
for d in dev.toi.ommeq.com toi.ommeq.com mef.onem2m.uplus.co.kr; do printf '  %s -> ' "$d"; _q "$d"; done

echo
echo "rollback: sh $(dirname "$0")/rollback_qms_sink_dns.sh"
