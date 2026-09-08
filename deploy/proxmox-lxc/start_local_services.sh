#!/bin/sh
# Start the local relay + dashboard on the relay host. Safe to run with the
# device OFF and NO dnsmasq redirect in place — the relay just sits idle with no
# traffic.
#
#   sh deploy/proxmox-lxc/start_local_services.sh            # relay + dashboard (control OFF)
#   sh deploy/proxmox-lxc/start_local_services.sh --control  # dashboard with outlet control ENABLED
#
# Run as root (the relay needs :80/:443/:18831).
set -eu
DEST=/srv/mttl-lab
CONTROL=0
[ "${1:-}" = "--control" ] && CONTROL=1

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -d "$DEST/relay" ] || { echo "$DEST not deployed — run deploy_to_lxc.sh first" >&2; exit 1; }

echo "[ca] ensure Root CA + leaves"
sh "$DEST/relay/gen_local_ca.sh" "$DEST/relay/certs"

echo "[relay] start"
systemctl restart mttl-relay
sleep 2
systemctl --no-pager is-active mttl-relay || { journalctl -u mttl-relay -n 20 --no-pager; exit 1; }

if [ "$CONTROL" = 1 ]; then
  echo "[dashboard] start WITH --enable-control"
  mkdir -p /etc/systemd/system/mttl-dashboard.service.d
  cat > /etc/systemd/system/mttl-dashboard.service.d/control.conf <<'EOF'
[Service]
ExecStart=
ExecStart=/usr/bin/python3 /srv/mttl-lab/dashboard/mttl_dashboard.py --host 0.0.0.0 --port 8080 --state-file /srv/mttl-lab/relay/state/devices.json --observer 127.0.0.1:9883 --refresh 3 --enable-control
EOF
  systemctl daemon-reload
else
  rm -f /etc/systemd/system/mttl-dashboard.service.d/control.conf 2>/dev/null || true
  systemctl daemon-reload
fi
systemctl restart mttl-dashboard
sleep 1
systemctl --no-pager is-active mttl-dashboard || { journalctl -u mttl-dashboard -n 20 --no-pager; exit 1; }

echo
echo "listeners:"
ss -tlnp 2>/dev/null | grep -E ':(80|443|18831|8080|9883)\b' || true
echo
echo "dashboard: http://<this-host-ip>:8080/"
echo "relay log: journalctl -u mttl-relay -f"
[ "$CONTROL" = 1 ] && echo "OUTLET CONTROL IS ENABLED — watch the first toggle on the device."
