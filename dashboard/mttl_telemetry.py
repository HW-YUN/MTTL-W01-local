#!/usr/bin/env python3
"""
MTTL-W01 oneM2M / telemetry parsing for the local dashboard.

Field names and decode rules are derived from the smartrelay reference
implementation (`vendor/smartrelay/rules/99-default.py`). Re-implemented here —
trimmed to what the dashboard shows — so the feed rule and the dashboard share
one parser.

Notes:
  - Any value produced for a device stays UNCONFIRMED until that device actually
    sends the field.
  - Fields the wire never carries (voltage, current) are reported as "unknown".

Standard library only.
"""

from __future__ import annotations

import base64
import json
import re
import time

# --- selectors / regexes (from 99-default._parse_telemetry_params) -----------

_POWER_RE = re.compile(r"^POWER([1-4]?)_(?:EVENT|SET)$")
_STATUS_REPORT_RE = re.compile(r"^STATUS([1-4]?)_REPORT$")
_STATUS_EVENT_RE = re.compile(r"^STATUS([1-4]?)_EVENT$")
_CONFIG_RE = re.compile(r"^CONFIGURATION([1-4]?)_(?:EVENT|SET|REPORT)$")

_ALARM_EVENT_TABLE = {
    0x00: "normal", 0x42: "overheat_trip", 0x44: "overheat_warning",
    0x46: "overheat_recovery", 0x80: "overload_recovery", 0x86: "overload_trip",
    0x88: "overload_warning",
}
_ALARM_STATE_TABLE = {
    0x00: "normal", 0x42: "overheat_trip", 0x44: "overheat_warning",
    0x46: "normal", 0x80: "normal", 0x86: "overload_trip", 0x88: "overload_warning",
}


def _hex_int(value):
    if value is None:
        return None
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


# --- 1.0.106 new fields v/t/p/e (content.cmd_report siblings) --------
# These are NOT inside parameters[] — they sit directly on content.cmd_report.
# Strict comma-separated hex, exact count, non-destructive on any malformation.

_HEXTOK_RE = re.compile(r"\A[0-9A-Fa-f]+\Z")


def _parse_hex_list(raw, expected: int):
    """'0A,00,1F,...' -> [int,...] of length exactly `expected`, else None.
    Any non-string / wrong count / bad hex token -> None (caller keeps old state)."""
    if not isinstance(raw, str):
        return None
    parts = [x.strip() for x in raw.split(",")]
    if len(parts) != expected or not all(_HEXTOK_RE.match(x) for x in parts):
        return None
    try:
        return [int(x, 16) for x in parts]
    except ValueError:
        return None


def _remap5_outlets_total(vals):
    """RAW p/e order is [outlet1, outlet2, outlet3, outlet4, total].
    Return the project-canonical {0: total, 1: outlet1, 2: outlet2, 3: outlet3, 4: outlet4}."""
    return {0: vals[4], 1: vals[0], 2: vals[1], 3: vals[2], 4: vals[3]}


def _switch_bool(value):
    raw = str(value or "").strip().upper()
    if raw == "00":
        return False
    if raw == "FF":
        return True
    return None


def b64_json(con):
    if not con:
        return None
    try:
        return json.loads(base64.b64decode(con))
    except Exception:
        return None


# --- mef bootstrap POST body ------------------------------------------------

_MAC_RE = re.compile(rb"<mac>([^<]*)</mac>")
_SERIAL_RE = re.compile(rb"<deviceSerialNo>([^<]*)</deviceSerialNo>")
_MODEL_RE = re.compile(rb"<deviceModel>([^<]*)</deviceModel>")


def parse_mef_post(body: bytes) -> dict:
    """Extract identifiers from a POST /mef <auth> XML body. Values are the device's own."""
    def g(rx):
        m = rx.search(body or b"")
        return m.group(1).decode(errors="replace") if m else None
    return {
        "mac": g(_MAC_RE),
        "serial": g(_SERIAL_RE),
        "model": g(_MODEL_RE),
    }


# --- oneM2M message -> telemetry delta --------------------------------------

def parse_onem2m_message(msg: dict) -> dict:
    """Given a device->server oneM2M envelope (already JSON-decoded), return a dict of
    dashboard-relevant deltas. Empty dict if the message carries no telemetry."""
    out: dict = {}
    rqi = msg.get("rqi") or ""
    pc = msg.get("pc") or {}

    if rqi == "smartplugbootstrap":
        cin = pc.get("m2m:cin") or {}
        con = b64_json(cin.get("con"))
        dev = (con or {}).get("content", {}).get("device", {})
        if dev:
            out["device_id"] = dev.get("id") or ""
            out["device_type"] = dev.get("type") or "MULTITAP"
        out["bootstrap"] = True
        return out

    is_report = (rqi.startswith("plugeventreport") or rqi.startswith("controlreport")
                 or rqi.startswith("devicecontrol-") or "control-report" in rqi)
    if not is_report:
        return out

    cin = pc.get("m2m:cin") or {}
    inner = b64_json(cin.get("con"))
    if not inner:
        return out
    content = inner.get("content") or {}
    notif = content.get("notification") if isinstance(content.get("notification"), dict) else None
    report = content.get("cmd_report") if isinstance(content.get("cmd_report"), dict) else None

    if report is not None:
        out["last_cmd_report"] = {"rqi": rqi, "result": report.get("result"),
                                  "rpt_id": report.get("rpt_id")}
    params = None
    if notif is not None:
        params = notif.get("parameters")
    elif report is not None:
        params = report.get("parameters")
    if isinstance(params, dict):
        params = [params]
    if not params:
        params = []          # no parameters[] -> still check cmd_report v/t/p/e below

    power, meter_w, energy_raw, alarms, cfg, standby, wifi = {}, {}, {}, {}, {}, {}, {}
    for p in params:
        if not isinstance(p, dict):
            continue
        cmd = str(p.get("command", ""))

        if "SSID" in p:
            wifi["ssid"] = p["SSID"]
        if "RSSI" in p:
            try:
                wifi["rssi_dbm"] = int(str(p["RSSI"]))
            except ValueError:
                wifi["rssi_raw"] = p["RSSI"]

        m = _POWER_RE.match(cmd)
        sr = _STATUS_REPORT_RE.match(cmd)
        se = _STATUS_EVENT_RE.match(cmd)
        sm = sr or se
        if m or sm:
            suffix = (m or sm).group(1) or ""
            n = int(suffix or 0)
            v = _switch_bool(p.get(f"switchBinary{suffix}"))
            if v is not None:
                power[n] = v
                if n == 0 and cmd == "POWER_EVENT":
                    for o in range(1, 5):
                        power[o] = v

        if cmd == "METER_CUR_STATUS_EVENT" or sm:
            for n in range(5):
                key = "meter_02" if n == 0 else f"meter{n}_02"
                raw = _hex_int(p.get(key))
                if raw is not None:
                    meter_w[n] = raw / 100.0
        if cmd == "METER_ACC_STATUS_EVENT":
            for n in range(5):
                key = "meter_00" if n == 0 else f"meter{n}_00"
                raw = _hex_int(p.get(key))
                if raw is not None:
                    energy_raw[n] = raw

        cm = _CONFIG_RE.match(cmd)
        if cm and cm.group(1) and f"configuration{cm.group(1)}" in p:
            s = str(p[f"configuration{cm.group(1)}"]).strip().upper()
            if re.fullmatch(r"[0-9A-F]{8}", s):
                cfg[int(cm.group(1))] = {
                    "threshold_watts": int(s[:6], 16) / 100.0,
                    "enabled": s[6:] == "01",
                }

        if cmd == "DEVICE_STATUS_EVENT":
            for n in range(1, 5):
                raw = _hex_int(p.get(f"event{n}"))
                if raw is not None:
                    standby[n] = {"standby": raw == 0, "active": raw == 1}

        if cmd == "ALARM_EVENT":
            for n in range(5):
                key = "event" if n == 0 else f"event{n}"
                raw = _hex_int(p.get(key))
                if raw is not None:
                    alarms[n] = {"raw": f"{raw:02X}",
                                 "event": _ALARM_EVENT_TABLE.get(raw, f"unknown_0x{raw:02X}"),
                                 "state": _ALARM_STATE_TABLE.get(raw, f"unknown_0x{raw:02X}")}

    # --- v/t/p/e on content.cmd_report (siblings of parameters[]) ----
    # Legacy parsing above is untouched. p (when valid) is the PREFERRED source
    # for meter_watts; meterN_02 stays as the fallback for any index p omits.
    #
    # Two independent things here:
    #  1. Each field's own valid value is stored non-destructively (v alone can
    #     update voltage_v, p alone can update meter_watts, ...).
    #  2. The persistent Discovery marker `vtpe_seen` is set ONLY when a SINGLE
    #     report carried ALL of v AND t AND p AND e valid. A partial report never
    #     activates the marker (and, per merge_delta, never clears an existing one).
    voltage_v = None
    temperature_c: dict = {}
    energy_kwh: dict = {}
    v_vals = t_vals = p_vals = e_vals = None
    if isinstance(report, dict):
        v_vals = _parse_hex_list(report.get("v"), 1)
        if v_vals is not None:
            voltage_v = v_vals[0] / 1000.0            # raw / 1000 = V
        t_vals = _parse_hex_list(report.get("t"), 4)
        if t_vals is not None:
            temperature_c = {i + 1: float(t_vals[i]) for i in range(4)}  # raw = degC
        p_vals = _parse_hex_list(report.get("p"), 5)
        if p_vals is not None:
            for k, raw in _remap5_outlets_total(p_vals).items():
                meter_w[k] = raw / 1000.0            # raw / 1000 = W  (p PREFERRED)
        e_vals = _parse_hex_list(report.get("e"), 5)
        if e_vals is not None:
            energy_kwh = {k: raw / 1000.0             # 1 count = 1 Wh = 0.001 kWh
                          for k, raw in _remap5_outlets_total(e_vals).items()}

    full_vtpe_valid = (v_vals is not None and t_vals is not None
                       and p_vals is not None and e_vals is not None)

    for name, d in (("power", power), ("meter_watts", meter_w), ("energy_raw", energy_raw),
                    ("energy_kwh", energy_kwh), ("temperature_c", temperature_c),
                    ("alarms", alarms), ("configuration", cfg), ("standby", standby), ("wifi", wifi)):
        if d:
            out[name] = d
    if voltage_v is not None:
        out["voltage_v"] = voltage_v
    if full_vtpe_valid:
        out["vtpe_seen"] = True          # marker: ONLY when v AND t AND p AND e all valid in this report
    return out


# --- state merge -----------------------------------------------------------

_MERGE_KEYS = ("power", "meter_watts", "energy_raw", "energy_kwh", "temperature_c",
               "alarms", "configuration", "standby", "wifi")


def new_device_state() -> dict:
    return {
        "device_id": None, "device_type": None, "ip": None, "client_id": None,
        "mac": None, "serial": None, "model": None,
        "first_seen": None, "last_seen": None,
        "mef_bootstrap_seen": False, "mqtt_connected": False,
        "session_epoch": 0, "reconnects": 0, "session_start_ts": None,
        "power": {}, "meter_watts": {}, "energy_raw": {},
        "alarms": {}, "configuration": {}, "standby": {}, "wifi": {},
        "last_cmd_report": None, "last_topic": None, "last_msg_ts": None,
        # 1.0.106 v/t/p/e. Populated ONLY after a clean parse (vtpe_seen);
        # stock devices never send these and keep the defaults.
        "voltage_v": None, "temperature_c": {}, "energy_kwh": {}, "vtpe_seen": False,
        # legacy string placeholders (dashboard hides these rows; never assigned
        # from telemetry — voltage now lives in voltage_v):
        "voltage": "not reported", "current": "not reported",
    }


_SCALAR_NEWER = ("device_id", "device_type", "ip", "client_id", "mac", "serial", "model",
                 "last_topic", "last_cmd_report", "pending_control", "last_control_result")


def merge_entry(dst: dict, src: dict) -> dict:
    """Fold `src` device state into `dst` (used when an ip:<ip> pseudo-entry and a
    real client_id entry turn out to be the same device). `dst` wins on identity;
    the freshest timestamps and any non-empty telemetry buckets are kept."""
    for k in ("first_seen",):
        a, b = dst.get(k), src.get(k)
        if b is not None and (a is None or b < a):
            dst[k] = b
    for k in ("last_seen", "last_msg_ts", "session_start_ts"):
        a, b = dst.get(k), src.get(k)
        if b is not None and (a is None or b > a):
            dst[k] = b
    for k in _SCALAR_NEWER:
        if not dst.get(k) and src.get(k):
            dst[k] = src[k]
    for k in ("mef_bootstrap_seen", "mqtt_connected", "vtpe_seen"):
        dst[k] = bool(dst.get(k)) or bool(src.get(k))
    for k in ("session_epoch", "reconnects"):
        dst[k] = max(int(dst.get(k) or 0), int(src.get(k) or 0))
    if dst.get("voltage_v") is None and src.get("voltage_v") is not None:
        dst["voltage_v"] = src["voltage_v"]
    for key in _MERGE_KEYS:
        b = src.get(key)
        if isinstance(b, dict) and b:
            bucket = dst.setdefault(key, {})
            for kk, vv in b.items():
                bucket.setdefault(str(kk), vv)
    return dst


def merge_delta(state: dict, delta: dict) -> dict:
    now = time.time()
    state["last_seen"] = now
    if state.get("first_seen") is None:
        state["first_seen"] = now
    for k in ("device_id", "device_type", "mac", "serial", "model"):
        if delta.get(k):
            state[k] = delta[k]
    if delta.get("bootstrap"):
        state["mef_bootstrap_seen"] = True
        state["mqtt_connected"] = True
    if delta.get("last_cmd_report"):
        state["last_cmd_report"] = delta["last_cmd_report"]
    # v/t/p/e. vtpe_seen is sticky (never unset). voltage_v is a scalar.
    if delta.get("vtpe_seen"):
        state["vtpe_seen"] = True
    if delta.get("voltage_v") is not None:
        state["voltage_v"] = delta["voltage_v"]
    for key in _MERGE_KEYS:
        d = delta.get(key)
        if isinstance(d, dict):
            bucket = state.setdefault(key, {})
            for kk, vv in d.items():
                bucket[str(kk)] = vv
    return state


def recent_activity(state: dict, window: float) -> bool:
    """Fallback liveness signal when the relay observer cannot be reached:
    did any oneM2M message arrive from this device within `window` seconds?
    The authoritative signal is the relay's live session registry, not this."""
    ts = state.get("last_msg_ts") or state.get("last_seen")
    return bool(ts) and (time.time() - ts) <= window
