#!/bin/sh
# Synthetic tests wrapper. Runs the localhost end-to-end harness + a few extra
# static checks. No device, no cloud, no privileged ports.
#
#   sh tests/synthetic_tests.sh
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

echo "### static checks"
PY="$(command -v python3)"
"$PY" --version
"$PY" -m py_compile \
  "$ROOT/relay/rules/10-dashboard-feed.py" \
  "$ROOT/relay/rules/15-firmware-ota.py" \
  "$ROOT/relay/rules/16-qms-sink.py" \
  "$ROOT/relay/rules/99-default.py" \
  "$ROOT/dashboard/mttl_telemetry.py" \
  "$ROOT/dashboard/mttl_dashboard.py" \
  "$ROOT/tests/synthetic_test.py" \
  "$ROOT/bridge/tests/ha_bridge_test.py" \
  "$ROOT/bridge/mttl_ha_bridge.py" \
  "$ROOT/bridge/mttl_mqtt_client.py" \
  "$ROOT/bridge/ha_discovery.py" \
  "$ROOT/vendor/smartrelay/relay.py" \
  "$ROOT/vendor/smartrelay/rules_engine.py" \
  "$ROOT/vendor/smartrelay/mqtt_wire.py" \
  "$ROOT/vendor/smartrelay/mqtt_session.py"
echo "  py_compile: OK"

echo "### relay/rules/99-default.py is a verbatim copy of vendor/smartrelay/rules/99-default.py"
a=$( (shasum -a 256 "$ROOT/vendor/smartrelay/rules/99-default.py" 2>/dev/null || sha256sum "$ROOT/vendor/smartrelay/rules/99-default.py") | awk '{print $1}')
b=$( (tail -n +5 "$ROOT/relay/rules/99-default.py" | (shasum -a 256 2>/dev/null || sha256sum)) | awk '{print $1}')
[ "$a" = "$b" ] && echo "  ok (body identical after the 4-line provenance header)" || { echo "  MISMATCH"; exit 1; }

echo "### config sanity: dns must be unset (decloud)"
if grep -Eq '^[[:space:]]*dns[[:space:]]*=' "$ROOT/config/relay/smartrelay.toml.example"; then
  echo "  FAIL: 'dns =' is set in the example config — that enables upstream proxy / cloud egress"; exit 1
fi
echo "  ok (decloud)"

echo
echo "### end-to-end harness"
set +e
"$PY" "$ROOT/tests/synthetic_test.py"; rc=$?

echo
echo "### HA MQTT bridge harness (stub broker + stub observer, no real HA / container / device)"
"$PY" "$ROOT/bridge/tests/ha_bridge_test.py"; rc2=$?
set -e

echo
echo "### combined: synthetic_test rc=$rc   ha_bridge_test rc=$rc2"
[ "$rc" -eq 0 ] && [ "$rc2" -eq 0 ]
