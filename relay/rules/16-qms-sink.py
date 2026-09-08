#!/usr/bin/env python3
"""
MTTL-W01 QMS sink rule — observe-only local sink for the device diagnostic-log
("QMS", POST /read_iot_wifi) HTTPS upload. NO upstream, NO forward.

Why
---
The V1.0.105 firmware's QMS sender uploads to:
    host  log.toi.ommeq.com : 443
    req   POST /read_iot_wifi
    body  M_Producer=TONLY&M_Model=MTTL-W01&M_SoftVer=..&M_MacAddr=..&M_SerialNo=..
          &M_IPAddr=..&DiagLog=|<diagnostic bytes>
A separate 1.0.66 runtime note shows `hdslog.lguplus.co.kr` for the same purpose;
whether it is the same service is NOT established, so BOTH hosts are sinked.

This rule lets a real QMS attempt be *seen* locally — source IP, timestamp, host,
method, path, Content-Length, and a few non-sensitive body markers — while:
  - the diagnostic payload is NEVER stored whole (no PII / DiagLog persisted),
  - nothing is forwarded to any real LG / Ommeq / Voltra endpoint,
  - the device gets a harmless `200 OK` so a mistimed observation does not by
    itself arm the firmware's QMS retry-backoff timer.

Scope / safety
--------------
- `on_http_request` ONLY. MQTT / oneM2M / control / telemetry paths untouched.
- Returns non-None ONLY when the request is for a QMS host (TLS SNI == that host,
  OR the `Host:` header == that host). Every other request -> None, so the mef
  POST, the dashboard feed, the OTA rule (15-), telemetry and 99-default all keep
  working byte-for-byte as before.
- No socket, no upstream, no urllib/requests. Pure local `HttpResponse`.
- Loaded as `16-` : after 10-dashboard-feed / 15-firmware-ota, before 99-default.
  15-firmware-ota only matches `/mef/...` paths and 99-default only matches
  `POST /mef`, so there is no overlap with `/read_iot_wifi`.
- The relay is decloud (`smartrelay.toml` has no `dns=`) so `handle_tls` cannot
  proxy upstream even if it wanted to; this rule adds no path to one.

Runtime files (root-writable — same dir the OTA rule already uses):
  state : /srv/mttl-lab/relay/state/qms-sink.state   (JSON: counters + last hit)
  log   : /srv/mttl-lab/relay/state/qms-sink.log     (JSON Lines, one per hit, append-only)
Overridable for tests: MTTL_QMS_SINK_STATE_FILE, MTTL_QMS_SINK_LOG_FILE

Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import threading
import time

try:
    from rules_engine import HttpResponse
except ModuleNotFoundError:  # standalone selftest run directly from the repo tree
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                     "..", "..", "vendor", "smartrelay"))
    from rules_engine import HttpResponse

_log = logging.getLogger(__name__)

# --- the two diagnostic-upload hosts (exact leaf names, lowercase) -------------
QMS_HOSTS = frozenset({"log.toi.ommeq.com", "hdslog.lguplus.co.kr"})

# --- the QMS request path (V1.0.105, confirmed). Other paths on a QMS host are
#     answered 200-harmless too, but flagged as unexpected in the log. ----------
QMS_PATH = b"/read_iot_wifi"

# --- body markers we ARE allowed to record: non-personal product attributes.
#     Everything else in the body (M_MacAddr, M_SerialNo, M_IPAddr, DiagLog, ...)
#     is never extracted or stored — only its presence / length. ---------------
MARKER_KEYS = ("M_Producer", "M_Model", "M_SoftVer")
MARKER_DENY = ("M_MacAddr", "M_SerialNo", "M_IPAddr", "DiagLog")
_MARKER_MAX = 40

DEFAULT_STATE_FILE = "/srv/mttl-lab/relay/state/qms-sink.state"
DEFAULT_LOG_FILE = "/srv/mttl-lab/relay/state/qms-sink.log"

_LOCK = threading.Lock()
_RE_PRINTABLE = re.compile(rb"[^\x20-\x7e]")


# --------------------------------------------------------------------------- io

def _state_file() -> str:
    return os.environ.get("MTTL_QMS_SINK_STATE_FILE") or DEFAULT_STATE_FILE


def _log_file() -> str:
    return os.environ.get("MTTL_QMS_SINK_LOG_FILE") or DEFAULT_LOG_FILE


def _ip_from_cid(cid: str) -> str:
    # relay.py cid format: ":<localport>-<ip>:<port>-<ts>"  (matches the other rules)
    try:
        return cid.split("-", 2)[1].split(":", 1)[0]
    except Exception:
        return ""


def _hdr(headers, name: str):
    nl = name.lower()
    for k, v in (headers or {}).items():
        if str(k).lower() == nl:
            return v
    return None


def _host_of(ctx, headers) -> str:
    """Lowercase host with any :port stripped, from Host: header first, then SNI."""
    h = _hdr(headers, "Host") or ""
    if not h:
        h = getattr(ctx, "domain", None) or ""
    h = str(h).strip().lower()
    if h.startswith("["):            # IPv6 literal — not a QMS host, leave as-is
        return h
    return h.split(":", 1)[0]


def _read_state() -> dict:
    try:
        with open(_state_file(), "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(st: dict) -> None:
    sf = _state_file()
    d = os.path.dirname(sf)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = sf + "." + str(os.getpid()) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, sf)


def _append_log(rec: dict) -> None:
    lf = _log_file()
    d = os.path.dirname(lf)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(lf, "a") as f:
        f.write(json.dumps(rec, separators=(",", ":"), sort_keys=True) + "\n")


# ------------------------------------------------------------------- markers

def _markers(body: bytes) -> dict:
    """Pull ONLY the allow-listed non-personal keys, length-capped & sanitised."""
    out = {}
    if not body:
        return out
    for key in MARKER_KEYS:
        m = re.search((r"(?:^|&)" + re.escape(key) + r"=([^&]*)").encode("ascii"), body)
        if not m:
            continue
        val = m.group(1)[:_MARKER_MAX]
        val = _RE_PRINTABLE.sub(b"?", val)
        out[key] = val.decode("ascii", "replace")
    return out


def _body_shape(body: bytes) -> dict:
    """Non-content description of the body — length, a short digest to spot
    retries of the same payload, and whether a DiagLog blob is attached."""
    body = body or b""
    shape = {
        "len": len(body),
        "sha8": hashlib.sha256(body).hexdigest()[:8] if body else "",
        "has_diaglog": b"DiagLog=" in body,
        "diaglog_bytes": 0,
    }
    marker = b"DiagLog=|"
    i = body.find(marker)
    if i >= 0:
        shape["diaglog_bytes"] = len(body) - (i + len(marker))
    elif shape["has_diaglog"]:
        j = body.find(b"DiagLog=")
        shape["diaglog_bytes"] = max(0, len(body) - (j + len(b"DiagLog=")))
    return shape


# ------------------------------------------------------------------ responses

def _ok200(note_body: bytes = b"OK\n") -> HttpResponse:
    # status line carries "200" for the firmware's response check (callgraph step 13)
    return HttpResponse(
        status_line=b"HTTP/1.1 200 OK",
        headers={"Content-Type": "text/plain;charset=UTF-8", "Connection": "close"},
        body=note_body,
    )


# ----------------------------------------------------------------------- hook

def on_http_request(ctx, method, path, headers, body):
    host = ""
    try:
        host = _host_of(ctx, headers)
        if host not in QMS_HOSTS:
            return None                      # not a QMS host -> other rules handle it

        ip = _ip_from_cid(getattr(ctx, "cid", "") or "")
        unit = "src:" + ip if ip else "src:?"      # raw source IP only, no labels
        p = (path or b"").split(b"?", 1)[0]
        m = (method or b"").decode("ascii", "replace")
        clen = _hdr(headers, "Content-Length")
        expected = (m == "POST" and p == QMS_PATH)

        rec = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "ts_epoch": round(time.time(), 3),
            "src_ip": ip,
            "unit": unit,
            "sni": (getattr(ctx, "domain", None) or ""),
            "host": host,
            "method": m,
            "path": p.decode("ascii", "replace"),
            "content_length_hdr": clen,
            "expected_qms_request": expected,
            "body": _body_shape(bytes(body or b"")),
            "markers": _markers(bytes(body or b"")),
            "cid": getattr(ctx, "cid", ""),
        }

        with _LOCK:
            try:
                _append_log(rec)
            except OSError as e:
                _log.warning("qms-sink: log append 실패: %s", e)
            st = _read_state()
            st["total_hits"] = int(st.get("total_hits") or 0) + 1
            bh = dict(st.get("hits_by_host") or {})
            bh[host] = int(bh.get(host) or 0) + 1
            st["hits_by_host"] = bh
            bi = dict(st.get("hits_by_ip") or {})
            bi[ip or "?"] = int(bi.get(ip or "?") or 0) + 1
            st["hits_by_ip"] = bi
            st["last"] = {k: rec[k] for k in
                          ("ts", "src_ip", "unit", "host", "method", "path",
                           "expected_qms_request", "markers")}
            st["last"]["body_len"] = rec["body"]["len"]
            st["last"]["body_sha8"] = rec["body"]["sha8"]
            try:
                _write_state(st)
            except OSError as e:
                _log.warning("qms-sink: state write 실패: %s", e)

        _log.info("qms-sink: HIT unit=%s host=%s %s %s clen=%s markers=%s diaglog=%sB%s",
                  unit, host, m, rec["path"], clen, rec["markers"],
                  rec["body"]["diaglog_bytes"],
                  "" if expected else "  [UNEXPECTED path/method for a QMS host]")

        # POST /read_iot_wifi -> 200 + tiny body.  Any other path/method on a QMS
        # host -> still a harmless 200 (never a redirect, never a forward).
        return _ok200(b"OK\n" if expected else b"")

    except Exception:
        _log.exception("qms-sink: 예외")
        # if we already know it's a QMS host, still answer harmlessly so we don't
        # leave the device hanging; otherwise stay out of the way.
        return _ok200(b"") if host in QMS_HOSTS else None


# --------------------------------------------------------------------- selftest

def _selftest() -> int:
    import tempfile

    fails = []

    def check(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    wd = tempfile.mkdtemp(prefix="qms-sink-selftest-")
    os.environ["MTTL_QMS_SINK_STATE_FILE"] = os.path.join(wd, "qms-sink.state")
    os.environ["MTTL_QMS_SINK_LOG_FILE"] = os.path.join(wd, "qms-sink.log")

    class C:
        def __init__(self, cid, domain=None):
            self.cid = cid
            self.domain = domain

    real_body = (b"M_Producer=TONLY&M_Model=MTTL-W01&M_SoftVer=1.0.106"
                 b"&M_MacAddr=020000000004&M_SerialNo=020000000004"
                 b"&M_IPAddr=192.0.2.124&DiagLog=|somebinarydiagbytes-not-stored")

    # 1. non-QMS host -> None (mef POST style)
    r = on_http_request(C(":443-192.0.2.123:5000-1"),
                        b"POST", b"/mef", {"Host": "mef.onem2m.uplus.co.kr"}, b"x")
    check("non-QMS host POST /mef -> None (99-default still handles it)", r is None)

    # 2. QMS host by Host header, no SNI -> 200, hit recorded
    r = on_http_request(C(":443-192.0.2.124:5100-1", domain="mef.onem2m.uplus.co.kr"),
                        b"POST", QMS_PATH,
                        {"Host": "log.toi.ommeq.com", "Content-Length": str(len(real_body))},
                        real_body)
    check("QMS host via Host header -> 200", r is not None and b"200" in r.status_line)
    check("QMS 200 body is tiny", r is not None and len(r.body) <= 8)

    # 3. QMS host by SNI, Host header absent -> 200
    r = on_http_request(C(":443-192.0.2.123:5200-1", domain="hdslog.lguplus.co.kr"),
                        b"POST", b"/whatever", {}, b"")
    check("QMS host via SNI + unexpected path -> still harmless 200",
          r is not None and b"200" in r.status_line and r.body == b"")

    st = _read_state()
    check("state total_hits == 2", st.get("total_hits") == 2)
    check("state hits_by_host has both hosts",
          set(st.get("hits_by_host", {})) == {"log.toi.ommeq.com", "hdslog.lguplus.co.kr"})
    check("state hits_by_ip counts both source IPs",
          st.get("hits_by_ip", {}).get("192.0.2.124") == 1 and
          st.get("hits_by_ip", {}).get("192.0.2.123") == 1)

    with open(os.environ["MTTL_QMS_SINK_LOG_FILE"]) as f:
        lines = [json.loads(x) for x in f if x.strip()]
    check("log has 2 JSONL records", len(lines) == 2)
    rec0 = lines[0]
    check("markers captured M_Producer/M_Model/M_SoftVer",
          rec0["markers"] == {"M_Producer": "TONLY", "M_Model": "MTTL-W01", "M_SoftVer": "1.0.106"})
    # src_ip / cid legitimately hold the CONNECTION source IP (wanted). What must
    # never appear is body-derived content: MAC, serial, the DiagLog blob.
    blob = json.dumps(rec0)
    check("body MAC / serial / DiagLog bytes NOT in the record",
          not any(s in blob for s in ("020000000004", "somebinarydiagbytes", "DiagLog=|")))
    check("no M_MacAddr / M_SerialNo / M_IPAddr marker captured",
          not any(k in rec0["markers"] for k in MARKER_DENY))
    check("body shape: len + sha8 + has_diaglog + diaglog_bytes only",
          set(rec0["body"]) == {"len", "sha8", "has_diaglog", "diaglog_bytes"}
          and rec0["body"]["has_diaglog"] is True
          and rec0["body"]["diaglog_bytes"] == len(b"somebinarydiagbytes-not-stored"))
    check("unit field is the raw source IP (no label)", rec0["unit"] == "src:192.0.2.124")
    check("expected_qms_request True for POST /read_iot_wifi", rec0["expected_qms_request"] is True)

    # 4. never returns anything but a local HttpResponse (no stream, no upstream)
    check("response has no .stream (no file/proxy path)", getattr(r, "stream", None) is None)

    # 5. exception path still fails safe for a known QMS host
    r = on_http_request(C(":443-192.0.2.124:5300-1"),
                        b"POST", QMS_PATH, {"Host": "log.toi.ommeq.com"}, None)
    check("None body handled -> 200", r is not None and b"200" in r.status_line)

    print("\n%s  (%d checks, %d failed)" %
          ("PASS" if not fails else "FAIL", 16, len(fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest())
