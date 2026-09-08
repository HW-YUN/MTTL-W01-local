#!/bin/sh
# MTTL-W01 - Proxmox LXC relay/DNS readiness check.  READ-ONLY.
#
# Run on the Proxmox host (web Shell, root):
#   sh deploy/proxmox-lxc/lxc_readiness_readonly.sh <CTID> \
#       2>&1 | tee lxc_readiness_$(date +%Y%m%d_%H%M%S).txt
# then send the .txt back.
#
#   <CTID>   required. numeric id of the target container.
#
# This script changes NOTHING. It does not restart/start/stop/reload any service,
# enable/disable any unit, edit any file, install any package, change
# route/NIC/VLAN/firewall/DNS, start a relay, generate a certificate, redirect
# DNS, power any device, touch a SoftAP, or run any provisioning code.
# DNS lookups and a single ping/curl are read-only queries, not changes.

set -u

CT="${1:?usage: sh $0 <CTID>  — run on the Proxmox host}"
case "$CT" in *[!0-9]*) echo "CTID must be numeric: $CT" >&2; exit 2 ;; esac

sec() { printf '\n\n========== %s ==========\n' "$1"; }
sub() { printf '\n----- %s -----\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }

sec "0. HOST identity"
hostname
have pveversion && pveversion | head -1
date -Is

sec "1. CT $CT - status / config"
pct status "$CT"
pct config "$CT"

sec "2. CT $CT - inside identity (hostname / OS / kernel / net)"
pct exec "$CT" -- hostname
pct exec "$CT" -- cat /etc/os-release
pct exec "$CT" -- uname -a
pct exec "$CT" -- ip -br address
pct exec "$CT" -- ip route
pct exec "$CT" -- cat /etc/network/interfaces

sec "3. PORT availability inside CT $CT (need TCP 80 443 18831 18833, UDP+TCP 53)"
sub "all TCP listeners"
pct exec "$CT" -- ss -tlnp
sub "all UDP listeners"
pct exec "$CT" -- ss -ulnp
sub "just the ports of interest"
pct exec "$CT" -- sh -c 'ss -tlnp | grep -E ":(80|443|53|18831|18833)[[:space:]]" || echo "(no TCP listener on 80/443/53/18831/18833)"'
pct exec "$CT" -- sh -c 'ss -ulnp | grep -E ":53[[:space:]]" || echo "(no UDP :53 listener)"'
sub "unprivileged low-port bind threshold + current user"
pct exec "$CT" -- cat /proc/sys/net/ipv4/ip_unprivileged_port_start
pct exec "$CT" -- id

sec "4. dnsmasq - process / status / config"
pct exec "$CT" -- systemctl is-active dnsmasq
pct exec "$CT" -- systemctl is-enabled dnsmasq
pct exec "$CT" -- systemctl show dnsmasq -p FragmentPath -p ExecStart -p ExecReload -p MainPID
pct exec "$CT" -- sh -c 'dnsmasq --version 2>/dev/null | head -1'
pct exec "$CT" -- dnsmasq --test
pct exec "$CT" -- sh -c 'ps -o pid,args -C dnsmasq || true'
sub "/etc/dnsmasq.conf"
pct exec "$CT" -- cat /etc/dnsmasq.conf
sub "/etc/dnsmasq.d/ contents"
pct exec "$CT" -- ls -la /etc/dnsmasq.d/
pct exec "$CT" -- sh -c 'for f in /etc/dnsmasq.d/*; do [ -f "$f" ] && { echo "== $f =="; cat "$f"; }; done'
sub "recent query log"
pct exec "$CT" -- sh -c 'tail -n 30 /var/log/dnsmasq-mttl.log 2>/dev/null || echo "(no /var/log/dnsmasq-mttl.log)"'

sec "5. resolver state"
pct exec "$CT" -- cat /etc/resolv.conf
pct exec "$CT" -- cat /etc/hosts
pct exec "$CT" -- sh -c 'grep -E "^hosts:" /etc/nsswitch.conf || true'

sec "6. DNS lookups (dev.toi / log.toi / mef / brk2) - query only"
pct exec "$CT" -- sh -c 'for d in dev.toi.ommeq.com log.toi.ommeq.com mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr; do echo "== $d =="; getent hosts "$d" || echo "  getent: no result"; done'
pct exec "$CT" -- sh -c 'if command -v dig >/dev/null 2>&1; then for d in dev.toi.ommeq.com log.toi.ommeq.com mef.onem2m.uplus.co.kr brk2.onem2m.uplus.co.kr; do echo "== dig $d @127.0.0.1 =="; dig +short "$d" @127.0.0.1; done; else echo "(dig not installed)"; fi'
pct exec "$CT" -- sh -c 'command -v nslookup >/dev/null 2>&1 && nslookup dev.toi.ommeq.com 127.0.0.1 || echo "(nslookup not installed)"'

sec "7. upstream route / connectivity - query only, no toggling"
pct exec "$CT" -- ip route get 1.1.1.1
pct exec "$CT" -- sh -c 'ping -c 1 -W 2 1.1.1.1 || true'
pct exec "$CT" -- sh -c 'if command -v curl >/dev/null 2>&1; then curl -sS -m 5 -o /dev/null -w "curl https://1.1.1.1/ -> %{http_code}\n" https://1.1.1.1/ || echo "(curl failed / no route - expected if the segment has no internet)"; else echo "(curl not installed)"; fi'
sub "host-side interfaces / bridges (for a possible 2nd-NIC upstream path)"
ip -br address
cat /etc/network/interfaces

sec "8. tooling versions inside CT $CT"
pct exec "$CT" -- sh -c 'python3 --version; echo "path: $(command -v python3)"'
pct exec "$CT" -- python3 -c 'import ssl, socket, http.server, struct, hashlib, base64, selectors, threading, json; print("stdlib import OK; OpenSSL:", ssl.OPENSSL_VERSION)'
pct exec "$CT" -- python3 -c 'import cryptography; print("cryptography version:", cryptography.__version__)'
pct exec "$CT" -- sh -c 'python3 -m venv --help >/dev/null 2>&1 && echo "python3-venv: available" || echo "python3-venv: MISSING"'
pct exec "$CT" -- sh -c 'command -v pip3 && pip3 list 2>/dev/null || echo "(pip3 not installed)"'
pct exec "$CT" -- sh -c 'command -v openssl && openssl version -a'
pct exec "$CT" -- sh -c 'command -v tcpdump && tcpdump --version 2>&1 | head -2 || echo "(tcpdump not installed)"'
pct exec "$CT" -- sh -c 'command -v tshark && tshark --version 2>&1 | head -2 || echo "(tshark not installed)"'
pct exec "$CT" -- sh -c 'command -v socat && socat -V 2>&1 | head -1 || echo "(socat not installed)"'
pct exec "$CT" -- sh -c 'command -v git && git --version || echo "(git not installed)"'

sec "9. firewall state (nft / iptables) - inside CT $CT and on host"
sub "inside CT $CT"
pct exec "$CT" -- sh -c 'nft list ruleset 2>/dev/null || echo "(nft: no ruleset visible / not permitted in unprivileged CT)"'
pct exec "$CT" -- sh -c 'iptables -S 2>/dev/null || echo "(iptables -S: none / not permitted)"'
pct exec "$CT" -- sh -c 'ip6tables -S 2>/dev/null || echo "(ip6tables -S: none / not permitted)"'
sub "host"
have pve-firewall && pve-firewall status || echo "(pve-firewall: n/a)"
nft list ruleset 2>/dev/null | head -n 120 || echo "(nft: none)"
iptables -S 2>/dev/null | head -n 80 || echo "(iptables: none)"

sec "10. running services + listeners inside CT $CT"
pct exec "$CT" -- systemctl list-units --type=service --state=running --no-pager --no-legend
sub "full socket table with process"
pct exec "$CT" -- ss -tulnp

sec "11. existing relay / smartrelay / mttl artifacts inside CT $CT"
pct exec "$CT" -- sh -c 'ls -la /opt /srv /root 2>/dev/null || true'
pct exec "$CT" -- sh -c 'find /opt /srv /root /home /usr/local /etc -maxdepth 3 \( -iname "*relay*" -o -iname "*smartrelay*" -o -iname "*mttl*" \) 2>/dev/null || echo "(none found)"'
pct exec "$CT" -- sh -c 'ls -la /var/log/ | grep -iE "relay|mttl" || echo "(no relay/mttl logs)"'
pct exec "$CT" -- sh -c 'ls -la /etc/ssl/private/ 2>/dev/null; ls -la /root/certs 2>/dev/null; echo "(cert dir scan done)"'

sec "12. capacity headroom inside CT $CT"
pct exec "$CT" -- sh -c 'df -h /'
pct exec "$CT" -- sh -c 'free -m'
pct exec "$CT" -- sh -c 'nproc'

sec "13. production-impact surface"
pct list
have qm && qm list
pct listsnapshot "$CT"
sub "which guests share a bridge / a VLAN tag"
grep -HnE "net[0-9]+:.*(bridge=vmbr|tag=)" /etc/pve/lxc/*.conf /etc/pve/qemu-server/*.conf 2>/dev/null || echo "(no guest net config matched)"
sub "does the HOST itself bind a relay/DNS port ?"
ss -tulnp 2>/dev/null | grep -E ":53[[:space:]]|:(80|443|18831|18833)[[:space:]]" || echo "(host binds none of these)"

sec "DONE - send the full tee'd .txt back"
