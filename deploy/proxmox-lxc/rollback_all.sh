#!/bin/sh
# Full rollback of the MTTL-W01 local relay setup. Run on the relay host as root.
#
#   sh deploy/proxmox-lxc/rollback_all.sh
#
# Does, in order:
#   1. remove the dnsmasq redirect (mef/brk2 -> relay) + reload
#   2. stop + disable mttl-relay and mttl-dashboard
#   3. verify no relay ports are still bound
#   4. print the DNS state and next manual steps (device power-off, factory reset)
#
# Does NOT touch: firmware, the device hardware, your hypervisor/switch, cloud
# egress (stays OFF), or anything outside /srv/mttl-lab and
# /etc/dnsmasq.d/mttl-relay.conf.
set -u
DEST=/srv/mttl-lab
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

echo "== 1. DNS redirect =="
if [ -x "$DEST/deploy/proxmox-lxc/dns/rollback_dns_redirect.sh" ]; then
  sh "$DEST/deploy/proxmox-lxc/dns/rollback_dns_redirect.sh" || echo "  (dns rollback script returned non-zero)"
else
  LIVE=/etc/dnsmasq.d/mttl-relay.conf
  if [ -f "$LIVE" ]; then
    mkdir -p "$DEST/backups"
    cp -p "$LIVE" "$DEST/backups/$(date +%Y%m%d-%H%M%S)-mttl-relay.conf.removed"
    rm -f "$LIVE"
    dnsmasq --test && systemctl reload dnsmasq
    echo "  removed $LIVE"
  else
    echo "  no redirect present"
  fi
fi

echo "== 2. services =="
for u in mttl-dashboard mttl-relay; do
  systemctl stop "$u" 2>/dev/null && echo "  stopped $u" || echo "  $u not running"
  systemctl disable "$u" 2>/dev/null || true
done
rm -f /etc/systemd/system/mttl-dashboard.service.d/control.conf 2>/dev/null || true
systemctl daemon-reload 2>/dev/null || true

echo "== 3. ports =="
if ss -tlnp 2>/dev/null | grep -E ':(80|443|18831|18833|8080|9883)\b'; then
  echo "  WARNING: something is still bound above"
else
  echo "  ok — 80/443/18831/18833/8080/9883 all free"
fi

echo "== 4. DNS state =="
for d in mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr dev.toi.ommeq.com; do
  printf '  %s -> ' "$d"; getent hosts "$d" | awk '{print $1}' | head -1
done
echo "  (compare against an un-redirected resolver, e.g. dig +short <host> @1.1.1.1)"

cat <<'EOF'

== manual steps still required (not done by this script) ==
  - the device: power OFF.
  - the device's stored Wi-Fi credentials: factory reset (physical button
    long-press ~10s). Reset scope is device-specific — verify on your unit.
  - cloud egress: was never opened; leave outbound internet for the device's
    network segment = OFF.
  - certs under /srv/mttl-lab/relay/certs/ can stay (harmless) or be deleted.
EOF
