#!/usr/bin/env python3
"""
Dashboard feed rule — PASSIVE. Every hook returns None so 99-default.py handles
all actual protocol responses. This rule only records what it sees into a JSON
state file that mttl_dashboard.py reads.

Not part of smartrelay. Loaded before 99-default.py (name "10-") so its hooks run
first, but since they all return None the engine falls through to 99-default.

State file shape:  {"updated": <ts>, "devices": {<key>: <device-state>}}
  key = the MQTT client_id once known; "ip:<addr>" only transiently, before the
  device's MQTT session appears (the mef POST on :443 has no client_id yet).
  On first MQTT contact the ip:<addr> entry is MERGED into the client_id entry
  and any leftover ip:<addr> whose ip already belongs to a client_id entry is
  pruned — so the dashboard never shows the same physical device twice.

Liveness (online/offline) is NOT decided here — the dashboard asks the relay's
observer for the live session list. This rule can't see MQTT disconnects
(rules_engine has no on_close hook), so `mqtt_connected` here means "has ever
connected", nothing more.

env:
  MTTL_STATE_FILE     path to the JSON state file (default: <rules_dir>/../state/devices.json)
  MTTL_DASHBOARD_LIB  directory containing mttl_telemetry.py (default: <rules_dir>/../../dashboard)

Standard library only.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_LIB = os.environ.get("MTTL_DASHBOARD_LIB") or os.path.normpath(os.path.join(_HERE, "..", "..", "dashboard"))
if _LIB not in sys.path:
    sys.path.insert(0, _LIB)

try:
    import mttl_telemetry as T
except Exception as exc:  # pragma: no cover
    T = None
    sys.stderr.write("[10-dashboard-feed] mttl_telemetry import failed: %r — feed disabled\n" % exc)

_STATE_FILE = os.environ.get("MTTL_STATE_FILE") or os.path.normpath(
    os.path.join(_HERE, "..", "state", "devices.json"))
_LOCK = threading.Lock()


def _ip_from_cid(cid: str) -> str:
    # relay.py cid format: ":<localport>-<ip>:<port>-<ts>"
    try:
        return cid.split("-", 2)[1].split(":", 1)[0]
    except Exception:
        return "?"


def _load() -> dict:
    try:
        with open(_STATE_FILE, "r") as f:
            blob = json.load(f)
        if isinstance(blob, dict) and isinstance(blob.get("devices"), dict):
            return blob["devices"]
        return blob if isinstance(blob, dict) else {}
    except Exception:
        return {}


def _save(devices: dict) -> None:
    os.makedirs(os.path.dirname(_STATE_FILE), exist_ok=True)
    tmp = _STATE_FILE + "." + str(os.getpid()) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"updated": time.time(), "devices": devices}, f, separators=(",", ":"))
    os.replace(tmp, _STATE_FILE)


def _prune(devices: dict) -> None:
    """Drop ip:<addr> pseudo-entries whose ip already belongs to a real client_id entry."""
    real_ips = {st.get("ip") for k, st in devices.items() if not k.startswith("ip:") and st.get("ip")}
    for k in [k for k in devices if k.startswith("ip:") and devices[k].get("ip") in real_ips]:
        devices.pop(k, None)


def _get_entry(devices: dict, client_id, ip: str):
    """Return (key, entry). Migrates+merges an ip:<ip> entry onto the client_id key."""
    if client_id:
        ipkey = "ip:" + ip
        if client_id not in devices:
            devices[client_id] = T.new_device_state()
        entry = devices[client_id]
        if ipkey in devices and ipkey != client_id:
            T.merge_entry(entry, devices.pop(ipkey))
        return client_id, entry
    ipkey = "ip:" + ip
    # if a real entry already has this ip, feed into it instead of a pseudo-entry
    for k, st in devices.items():
        if not k.startswith("ip:") and st.get("ip") == ip and ip != "?":
            return k, st
    if ipkey not in devices:
        devices[ipkey] = T.new_device_state()
    return ipkey, devices[ipkey]


def _record(client_id, cid, updater):
    if T is None:
        return
    ip = _ip_from_cid(cid or "")
    cids = client_id.decode(errors="replace") if isinstance(client_id, (bytes, bytearray)) else client_id
    with _LOCK:
        devices = _load()
        key, entry = _get_entry(devices, cids, ip)
        if ip != "?":
            entry["ip"] = ip
        if cids:
            entry["client_id"] = cids
        try:
            updater(entry)
        except Exception as exc:  # never let a feed bug touch the relay
            sys.stderr.write("[10-dashboard-feed] updater error: %r\n" % exc)
        _prune(devices)
        _save(devices)


# --- hooks (all return None; 99-default does the real work) ------------------

def on_http_request(ctx, method, path, headers, body):
    if method == b"POST" and path == b"/mef":
        ident = T.parse_mef_post(body) if T else {}

        def upd(e):
            e["mef_bootstrap_seen"] = True
            for k in ("mac", "serial", "model"):
                if ident.get(k):
                    e[k] = ident[k]
            e["last_seen"] = time.time()
            e.setdefault("first_seen", e["last_seen"])
        _record(None, getattr(ctx, "cid", ""), upd)
    return None


def on_session_start(ctx):
    now = time.time()

    def upd(e):
        if e.get("mqtt_connected") and (e.get("session_epoch") or 0) > 0:
            e["reconnects"] = int(e.get("reconnects") or 0) + 1
        e["session_epoch"] = int(e.get("session_epoch") or 0) + 1
        e["session_start_ts"] = now
        e["mqtt_connected"] = True
        e["last_seen"] = now
        e.setdefault("first_seen", now)
    _record(getattr(ctx, "client_id", None), getattr(ctx, "cid", ""), upd)
    return None


def on_message(ctx, msg, topic):
    delta = T.parse_onem2m_message(msg) if T else {}
    tp = topic.decode(errors="replace") if isinstance(topic, (bytes, bytearray)) else str(topic)
    now = time.time()

    def upd(e):
        e["mqtt_connected"] = True
        e["last_topic"] = tp
        e["last_msg_ts"] = now
        e["last_seen"] = now
        e.setdefault("first_seen", now)
        if delta:
            T.merge_delta(e, delta)
    _record(getattr(ctx, "client_id", None), getattr(ctx, "cid", ""), upd)
    return None


def on_local_inject(ctx, cmd):
    def upd(e):
        e["last_inject"] = {"ts": time.time(), "cmd": cmd}
    _record(getattr(ctx, "device_client_id", None), getattr(ctx, "cid", ""), upd)
    return None
