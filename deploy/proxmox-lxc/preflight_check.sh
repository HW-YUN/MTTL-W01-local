#!/bin/sh
# Pre-connect preflight for the MTTL-W01 local relay. Run on the relay host (root).
# Changes nothing — it just checks the state right before the STOP point
# (DNS redirect + device power-on).
set -u
DEST=/srv/mttl-lab
FAIL=0
# NOTE: must always return 0 — it is used as the RHS of `test && say PASS || say FAIL`.
say() { printf '%s %s\n' "$1" "$2"; [ "$1" = "FAIL" ] && FAIL=1; return 0; }

echo "=== MTTL-W01 relay preflight ==="

python3 -c 'import sys; sys.exit(0 if sys.version_info>=(3,11) else 1)' \
  && say "PASS" "python3 >= 3.11 ($(python3 -V 2>&1))" \
  || say "FAIL" "python3 < 3.11 — vendored relay.py needs tomllib"

command -v openssl >/dev/null && say "PASS" "openssl present ($(openssl version))" || say "FAIL" "openssl missing"

for f in relay/relay.py relay/smartrelay.toml relay/rules/99-default.py relay/rules/10-dashboard-feed.py \
         dashboard/mttl_dashboard.py dashboard/mttl_telemetry.py vendor/smartrelay/mqtt_wire.py; do
  [ -f "$DEST/$f" ] && say "PASS" "file $f" || say "FAIL" "missing $f"
done

if grep -Eq '^[[:space:]]*dns[[:space:]]*=' "$DEST/relay/smartrelay.toml" 2>/dev/null; then
  say "FAIL" "smartrelay.toml has dns= (upstream proxy / cloud egress)"
else
  say "PASS" "relay config = decloud (no dns=, no cloud egress)"
fi

[ -f "$DEST/relay/certs/ca.crt" ] && say "PASS" "Root CA present" || say "WARN" "Root CA not generated yet (run relay/gen_local_ca.sh)"

if ping -c1 -W2 1.1.1.1 >/dev/null 2>&1; then
  say "FAIL" "the device network segment has internet egress (expected OFF)"
else
  say "PASS" "internet egress blocked (ping 1.1.1.1 fails)"
fi

if [ -f /etc/dnsmasq.d/mttl-relay.conf ]; then
  say "WARN" "dnsmasq redirect ALREADY applied (mttl-relay.conf present)"
else
  say "PASS" "no dnsmasq redirect applied yet (example only)"
fi
for d in mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr dev.toi.ommeq.com; do
  printf '     %s -> %s\n' "$d" "$(getent hosts "$d" | awk '{print $1}' | head -1)"
done

echo "--- listeners ---"
if systemctl is-active --quiet mttl-relay; then
  say "PASS" "mttl-relay active"
  ss -tlnp 2>/dev/null | grep -E ':(80|443|18831)\b' | sed 's/^/     /'
else
  say "WARN" "mttl-relay not running (start_local_services.sh)"
fi
systemctl is-active --quiet mttl-dashboard && say "PASS" "mttl-dashboard active" || say "WARN" "mttl-dashboard not running"
ss -tlnp 2>/dev/null | grep -qE ':8080\b' && say "PASS" "dashboard :8080 listening" || say "WARN" "dashboard :8080 not listening"

echo
if [ "$FAIL" = 0 ]; then
  echo "PREFLIGHT: no hard failures. Remaining manual steps at the STOP point:"
  echo "  1) MTTL_RELAY_HOST_IP=<ip> sh $DEST/deploy/proxmox-lxc/dns/apply_dns_redirect.sh"
  echo "  2) power on the device ; watch: journalctl -u mttl-relay -f  +  the dashboard on :8080"
else
  echo "PREFLIGHT: FAILURES above — fix before connecting the device."
fi
exit $FAIL
