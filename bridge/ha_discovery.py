#!/usr/bin/env python3
"""
Home Assistant MQTT discovery payload builder for MTTL-W01 (bridge v1).

Lives in bridge/ (not dashboard/) so the HA runtime does not depend on the
dashboard package. Used by bridge/mttl_ha_bridge.py.

entity set per device:
  base (every device, 10):
    switch  outlet_1..4, all
    sensor  power_total, power_1..4  (W)
  vtpe=True (1.0.106 after a clean v/t/p/e telemetry, +10 => 20):
    sensor  voltage (V), temp_1..4 (degC), energy_total, energy_1..4 (kWh)

Still NOT emitted: wifi_rssi, binary_sensor "online", current (never on the wire).

Design decisions:
  - switch: optimistic=false, state from real telemetry, command via the BRIDGE
    topic  <data_prefix>/<slug>/outlet/<n>/set  (payload "ON"/"OFF").  The bridge
    translates that to the relay observer inject JSON; HA never talks to the
    observer directly.
  - every entity carries two availability topics with availability_mode "all":
      <data_prefix>/bridge/status       (bridge process alive; also its MQTT LWT)
      <data_prefix>/<slug>/availability (per-device, with the 45s grace machine)
  - power sensors carry NO expire_after (a load that stays constant produces no
    new publish; expire_after would then wrongly flip a healthy device to
    "unavailable"). Absence is expressed only through the availability topics.

unique_id / device identity are keyed by a stable slug (MAC, colon-stripped
lower-case; else a deterministic sha256 prefix of the MQTT client_id) so the HA
entities survive a bridge restart and client_id churn.  IP is never used.

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re

DISCOVERY_PREFIX_DEFAULT = "homeassistant"
DATA_PREFIX_DEFAULT = "mttl"

# strict MAC acceptance — ONLY these three shapes (after trimming whitespace):
#   02000000ABCD                bare 12-hex
#   02:00:00:00:AB:CD           colon-separated octets
#   02-00-00-00-AB-CD           hyphen-separated octets
# The separator must be consistent (no 02:00-00:...). Anything else -> None.
# We deliberately do NOT strip arbitrary non-hex characters from a free string,
# so a serial / IP / label can't be coerced into a 12-hex "MAC".
_MAC_BARE_RE = re.compile(r"\A[0-9a-fA-F]{12}\Z")
_MAC_SEP_RE = re.compile(r"\A[0-9a-fA-F]{2}([:-])(?:[0-9a-fA-F]{2}\1){4}[0-9a-fA-F]{2}\Z")


def clean_mac(value: "str | None") -> "str | None":
    """Return `value` as a canonical 12-hex lower-case MAC, or None.

    Accepts ONLY: bare 12-hex, or six 2-hex octets joined by a single consistent
    ':' or '-'. Leading/trailing whitespace is trimmed. A device_id that is
    itself a bare MAC (e.g. '02000000ABCD') passes. Everything else -> None.
    Single source of truth for "is this a real MAC" — used by device_slug and by
    the bridge's sticky-MAC logic."""
    if not value:
        return None
    s = str(value).strip()
    if _MAC_BARE_RE.match(s):
        return s.lower()
    if _MAC_SEP_RE.match(s):
        return s.replace(":", "").replace("-", "").lower()
    return None


def device_slug(client_id: str, mac: str | None) -> str:
    """Stable, deterministic per-device slug for HA unique_id / topic paths.

    MAC (colon/hyphen stripped, lower-cased) when it is a clean 12-hex string;
    otherwise 'd' + first 12 hex of sha256(client_id).  Never uses IP.  Never
    uses the builtin hash() (salted per process -> not stable across restarts).

    NOTE: the bridge now resolves a sticky MAC BEFORE calling this, so in normal
    operation `mac` is always a clean MAC and the hash branch is not taken. The
    hash branch is kept only as a last-resort identity for a device that has
    never once presented a MAC (the bridge defers HA discovery for such a device
    rather than publishing the hash slug — see mttl_ha_bridge).
    """
    c = clean_mac(mac)
    if c:
        return c
    h = hashlib.sha256((client_id or "").encode("utf-8", "replace")).hexdigest()
    return "d" + h[:12]


def _availability(data_prefix: str, slug: str) -> dict:
    return {
        "availability": [
            {"topic": f"{data_prefix}/bridge/status",
             "payload_available": "online", "payload_not_available": "offline"},
            {"topic": f"{data_prefix}/{slug}/availability",
             "payload_available": "online", "payload_not_available": "offline"},
        ],
        "availability_mode": "all",
    }


def _dev_block(slug: str, model: str | None, mac: str | None,
               sw_version: str | None = None) -> dict:
    block = {
        "identifiers": [f"mttl_w01_{slug}"],
        "name": f"MTTL-W01 {slug}",
        "model": model or "MTTL-W01",
        "manufacturer": "LG U+ / TONLY",
    }
    if mac:
        block["connections"] = [["mac", str(mac)]]
    if sw_version:
        block["sw_version"] = str(sw_version)
    return block


def build_discovery(slug: str, mac: str | None = None, model: str | None = None,
                    sw_version: str | None = None,
                    data_prefix: str = DATA_PREFIX_DEFAULT,
                    discovery_prefix: str = DISCOVERY_PREFIX_DEFAULT,
                    vtpe: bool = False) -> list[dict]:
    """Return a list of {"topic": str, "payload": str} discovery configs for one
    device.  The caller publishes each with retain=True.  Nothing here connects.

    Base set (every device): 10 entries
      switch  outlet_1..4, all            (5)
      sensor  power_total, power_1..4     (5)   -- object_id/unique_id UNCHANGED from v1
    vtpe=True adds (only for a device that has sent a clean v/t/p/e telemetry):
      sensor  voltage                     (1)
      sensor  temp_1..4                   (4)
      sensor  energy_total, energy_1..4   (5)
    => 10 (stock) or 20 (1.0.106)."""
    dev = _dev_block(slug, model, mac, sw_version)
    node = f"mttl_{slug}"
    avail = _availability(data_prefix, slug)
    out: list[dict] = []

    def add(component: str, obj_id: str, payload: dict) -> None:
        payload.setdefault("device", dev)
        payload["unique_id"] = f"{node}_{obj_id}"
        payload.update(avail)
        out.append({
            "topic": f"{discovery_prefix}/{component}/{node}/{obj_id}/config",
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
        })

    def _switch(obj_id: str, name: str, chan: str) -> None:
        add("switch", obj_id, {
            "name": name,
            "state_topic": f"{data_prefix}/{slug}/outlet/{chan}/state",
            "command_topic": f"{data_prefix}/{slug}/outlet/{chan}/set",
            "payload_on": "ON", "payload_off": "OFF",
            "state_on": "ON", "state_off": "OFF",
            "optimistic": False,
        })

    for n in (1, 2, 3, 4):
        _switch(f"outlet_{n}", f"Outlet {n}", str(n))
    # All: reuses the existing wire command POWER_SET via bridge topic outlet/0.
    _switch("all", "All", "0")

    add("sensor", "power_total", {
        "name": "Power total",
        "state_topic": f"{data_prefix}/{slug}/power/total",
        "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
    })
    for n in (1, 2, 3, 4):
        add("sensor", f"power_{n}", {
            "name": f"Power outlet {n}",
            "state_topic": f"{data_prefix}/{slug}/power/{n}",
            "unit_of_measurement": "W", "device_class": "power", "state_class": "measurement",
        })

    if vtpe:
        add("sensor", "voltage", {
            "name": "Voltage",
            "state_topic": f"{data_prefix}/{slug}/voltage",
            "unit_of_measurement": "V", "device_class": "voltage", "state_class": "measurement",
        })
        for n in (1, 2, 3, 4):
            add("sensor", f"temp_{n}", {
                "name": f"Internal temperature {n}",
                "state_topic": f"{data_prefix}/{slug}/temp/{n}",
                "unit_of_measurement": "°C", "device_class": "temperature",
                "state_class": "measurement",
            })
        add("sensor", "energy_total", {
            "name": "Energy total",
            "state_topic": f"{data_prefix}/{slug}/energy/total",
            "unit_of_measurement": "kWh", "device_class": "energy",
            "state_class": "total_increasing",
        })
        for n in (1, 2, 3, 4):
            add("sensor", f"energy_{n}", {
                "name": f"Energy outlet {n}",
                "state_topic": f"{data_prefix}/{slug}/energy/{n}",
                "unit_of_measurement": "kWh", "device_class": "energy",
                "state_class": "total_increasing",
            })

    return out


if __name__ == "__main__":
    import pprint
    slug = device_slug("ASN_CSE-D-EXAMPLE003-MTAP", "02:00:00:00:00:03")
    print("slug:", slug)
    pprint.pp(build_discovery(slug, mac="02:00:00:00:00:03", model="MTTL-W01"))
