#!/usr/bin/env python3
"""
MTTL-W01 firmware OTA rule — single-target, fail-closed.

Purpose
-------
Serve a single custom firmware image (e.g. 1.0.106) to ONE targeted unit via the
device's own `/mef/updateVersionCheck/firmware/...` -> offer -> `/mef/firmware.../*.fwr`
flow. Every other source — including a unit that is already up to date — gets an
explicit "no update" BLOCK.

The target device identity is NOT hard-coded. It is supplied at deploy time via
environment variables (see "Configuration" below). If the target is not
configured, the rule is fail-closed: every request is BLOCKed, nothing is offered.

Scope
-----
Only `on_http_request` is defined. No `on_message` / `on_session_start` /
`on_local_inject` / `on_tick` -> the MQTT / oneM2M / control / telemetry paths are
untouched. Loaded as `15-` so it runs after `10-dashboard-feed` (which returns
None for these paths) and before `99-default` (which never sees an OTA path
because this rule always returns non-None for one).

Safety model
------------
- FAIL-CLOSED: if the target IP / origin are not configured, every request BLOCKs.
- DENY-FIRST: a denied IP / entityId is rejected before any allow logic.
- OFFER requires ALL of: source IP == target  AND  X-M2M-Origin == target  AND  a
  valid, unexpired ARM file whose target_* match the configured target  AND
  sha256(firmware) == ARM.sha256 (re-checked every request).
- The firmware GET is re-validated (Case A: X-M2M-Origin present -> IP AND origin;
  Case B: header absent -> IP == target AND a fresh stored OFFER for the target
  within CORRELATION_TTL_SECONDS). Never IP-only-unconditional.
- State machine: OFF -> ARMED -> OFFERED -> TRANSFERRING -> SPENT.
  SPENT is terminal and survives a relay restart (the ARM file is renamed away).
- No ARM / expired / malformed / sha mismatch / unexpected path / any exception
  -> BLOCK (HTTP 200, empty body).

Configuration (environment variables)
-------------------------------------
  MTTL_OTA_TARGET_IP       source IP of the unit to update            (required)
  MTTL_OTA_TARGET_ORIGIN   X-M2M-Origin / entityId of that unit       (required)
  MTTL_OTA_DENY_IPS        comma-separated IPs to always BLOCK        (optional)
  MTTL_OTA_DENY_ORIGINS    comma-separated origins to always BLOCK    (optional)
  MTTL_OTA_ARM_FILE        override the ARM file path                 (optional)
  MTTL_OTA_STATE_FILE      override the progress-state file path      (optional)

Runtime files (defaults)
------------------------
  ARM (operator writes, this rule only reads): /etc/mttl/firmware-ota.arm
  progress state (this rule writes, atomic)  : /srv/mttl-lab/relay/state/firmware-ota.state

This rule does NOT create /etc or /srv paths on its own beyond the state file's
parent directory. It never fetches anything and never touches upstream.

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

from rules_engine import HttpResponse, StreamPlan

_log = logging.getLogger(__name__)


# --- target / deny identity (deploy-time config; the ARM file cannot redirect) ---

def _target_ip() -> str:
    return (os.environ.get("MTTL_OTA_TARGET_IP") or "").strip()


def _target_origin() -> str:
    return (os.environ.get("MTTL_OTA_TARGET_ORIGIN") or "").strip()


def _deny_ips() -> frozenset:
    return frozenset(x.strip() for x in (os.environ.get("MTTL_OTA_DENY_IPS") or "").split(",") if x.strip())


def _deny_origins() -> frozenset:
    return frozenset(x.strip() for x in (os.environ.get("MTTL_OTA_DENY_ORIGINS") or "").split(",") if x.strip())


def _target_configured() -> bool:
    return bool(_target_ip() and _target_origin())


# --- tuning constants ---
CORRELATION_TTL_SECONDS = 120.0   # Case B: max age of the stored OFFER for a header-less .fwr GET
PACE_SECONDS = 0.03               # sleep between 4096-byte chunks (af950833 known-working value)
CHUNK_BYTES = 4096

DEFAULT_ARM_FILE = "/etc/mttl/firmware-ota.arm"
DEFAULT_STATE_FILE = "/srv/mttl-lab/relay/state/firmware-ota.state"

_ARM_REQUIRED_KEYS = ("target_ip", "target_origin", "fwr", "sha256",
                      "offer_version", "fwnnam", "expires_epoch")
_RE_SHA256 = re.compile(r"\A[0-9a-f]{64}\Z")
_RE_VERSION = re.compile(r"\A[0-9][0-9.]{0,15}\Z")
_RE_FWNNAM = re.compile(r"\A[A-Za-z0-9._+-]{1,80}\Z")

_LOCK = threading.Lock()


# --------------------------------------------------------------------------- io

def _arm_file() -> str:
    return os.environ.get("MTTL_OTA_ARM_FILE") or DEFAULT_ARM_FILE


def _state_file() -> str:
    return os.environ.get("MTTL_OTA_STATE_FILE") or DEFAULT_STATE_FILE


def _ip_from_cid(cid: str) -> str:
    # relay.py cid format: ":<localport>-<ip>:<port>-<ts>"  (matches 10-dashboard-feed._ip_from_cid)
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


def _read_state():
    try:
        with open(_state_file(), "r") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def _write_state(st: dict) -> None:
    sf = _state_file()
    d = os.path.dirname(sf)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = sf + "." + str(os.getpid()) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, separators=(",", ":"))
    os.replace(tmp, sf)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def _mark_spent(arm_path: str) -> None:
    """Rename the exact ARM file away so the box is OFF again (terminal, survives restart)."""
    try:
        dst = arm_path + ".spent." + str(int(time.time()))
        os.replace(arm_path, dst)
        _log.info("firmware-ota: SPENT — arm -> %s", os.path.basename(dst))
    except OSError as e:
        # state file already says SPENT (terminal); log and move on — fail-closed.
        _log.warning("firmware-ota: SPENT arm rename 실패(%s) — state=SPENT 로 유지", e)


# ------------------------------------------------------------------------- ARM

def _arm_valid(now: float):
    """Return (arm_dict, 'ok') if fully ARMED, else (None, reason)."""
    ap = _arm_file()
    if not os.path.isfile(ap):
        return None, "no-arm"
    try:
        raw = open(ap, "r").read()
    except OSError as e:
        return None, "arm-read:%s" % e
    arm = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        arm[k.strip()] = v.strip()

    miss = [k for k in _ARM_REQUIRED_KEYS if not arm.get(k)]
    if miss:
        return None, "arm-missing:%s" % ",".join(miss)
    if arm["target_ip"] != _target_ip() or arm["target_origin"] != _target_origin():
        return None, "arm-target-mismatch"          # cannot retarget via the ARM file
    if not _RE_SHA256.match(arm["sha256"].lower()):
        return None, "arm-sha-format"
    if not _RE_VERSION.match(arm["offer_version"]):
        return None, "arm-version-format"
    if not _RE_FWNNAM.match(arm["fwnnam"]):
        return None, "arm-fwnnam-format"
    try:
        exp = float(arm["expires_epoch"])
    except ValueError:
        return None, "arm-exp-format"
    if now >= exp:
        return None, "arm-expired"
    if not os.path.isabs(arm["fwr"]) or not os.path.isfile(arm["fwr"]):
        return None, "arm-fwr-missing"
    arm["sha256"] = arm["sha256"].lower()
    return arm, "ok"


def _fw_sha_ok(arm: dict) -> bool:
    try:
        return _sha256_file(arm["fwr"]) == arm["sha256"]
    except OSError:
        return False


def _expected_fw_paths(arm: dict):
    v, n = arm["offer_version"], arm["fwnnam"]
    return {
        ("/mef/firmware%s/%s" % (v, n)).encode("ascii"),
        ("/mef/firmware/MTAP/20/D/%s/%s" % (v, n)).encode("ascii"),
    }


# ---------------------------------------------------------------------- BLOCK

def _block(reason: str) -> HttpResponse:
    _log.info("firmware-ota: BLOCK — %s", reason)
    return HttpResponse(
        status_line=b"HTTP/1.1 200 OK",
        headers={"Content-Type": "text/plain;charset=UTF-8", "Connection": "close"},
        body=b"",
    )


# ---------------------------------------------------------------- completion

def _on_stream_done(sent: int, filesize: int, arm_path: str) -> None:
    """Called once by relay._serve_stream after the transfer ends. Only a full
    transfer (sent == filesize, filesize > 0) burns the one-shot (SPENT)."""
    with _LOCK:
        st = _read_state() or {}
        tr = dict(st.get("transfer") or {})
        tr["sent"], tr["filesize"] = sent, filesize
        st["transfer"] = tr
        st["updated"] = time.time()
        if filesize > 0 and sent == filesize:
            st["phase"] = "SPENT"
            _write_state(st)
            _mark_spent(arm_path)
            _log.info("firmware-ota: transfer complete %s/%s -> SPENT", sent, filesize)
        else:
            st["phase"] = "TRANSFERRING"      # keep — the device may retry
            _write_state(st)
            _log.warning("firmware-ota: transfer INCOMPLETE %s/%s — TRANSFERRING 유지(재시도 허용)",
                         sent, filesize)


# ----------------------------------------------------------------- handlers

def _handle_version_check(ctx, method, p, headers):
    ip = _ip_from_cid(getattr(ctx, "cid", "") or "")
    origin = (_hdr(headers, "X-M2M-Origin") or "").strip()
    now = time.time()

    if not _target_configured():
        return _block("OTA target not configured (version-check)")
    if ip in _deny_ips() or origin in _deny_origins():
        return _block("deny-first ip=%s origin=%r (version-check)" % (ip, origin))
    if method != b"GET":
        return _block("method %r on version-check" % method)

    st = _read_state() or {}
    if st.get("phase") == "SPENT":
        return _block("phase=SPENT (version-check)")

    arm, why = _arm_valid(now)
    if arm is None:
        return _block("not armed: %s (version-check)" % why)
    if not _fw_sha_ok(arm):
        return _block("firmware sha256 mismatch (version-check)")
    if not (ip == _target_ip() and origin == _target_origin()):
        return _block("not target: ip=%s origin=%r (version-check)" % (ip, origin))

    # defensive: a denied identity must never reach here
    if ip in _deny_ips() or origin in _deny_origins():
        raise RuntimeError("denied identity reached OFFER path")

    v, fwnnam = arm["offer_version"], arm["fwnnam"]
    _write_state({
        "phase": "OFFERED",
        "offer": {"ip": ip, "origin": origin, "epoch": now},
        "updated": now,
    })
    payload = ("<vr>%s<url>%s<fwnnam>%s<chksum>12345678" % (v, v, fwnnam)).encode("ascii")
    _log.info("firmware-ota: OFFER ip=%s origin=%s version=%s", ip, origin, v)
    return HttpResponse(
        status_line=b"HTTP/1.1 200 OK",
        headers={"Content-Type": "text/plain;charset=UTF-8", "Connection": "close"},
        body=payload,
    )


def _handle_firmware_get(ctx, method, p, headers):
    ip = _ip_from_cid(getattr(ctx, "cid", "") or "")
    origin = (_hdr(headers, "X-M2M-Origin") or "").strip()
    now = time.time()

    if not _target_configured():
        return _block("OTA target not configured (firmware-get)")
    if ip in _deny_ips() or origin in _deny_origins():
        return _block("deny-first ip=%s origin=%r (firmware-get)" % (ip, origin))
    if method != b"GET":
        return _block("method %r on firmware-get" % method)

    st = _read_state() or {}
    if st.get("phase") == "SPENT":
        return _block("phase=SPENT (firmware-get)")

    arm, why = _arm_valid(now)
    if arm is None:
        return _block("not armed: %s (firmware-get)" % why)
    if not _fw_sha_ok(arm):
        return _block("firmware sha256 mismatch (firmware-get)")

    if p not in _expected_fw_paths(arm):
        return _block("unexpected firmware path %r" % p)

    phase = st.get("phase")
    offer = st.get("offer") or {}
    if phase not in ("OFFERED", "TRANSFERRING"):
        return _block("no active offer (phase=%r)" % phase)
    if offer.get("ip") != _target_ip() or offer.get("origin") != _target_origin():
        return _block("stored offer is not the target")

    has_origin = bool(origin)
    if has_origin:
        # Case A — X-M2M-Origin present
        if not (ip == _target_ip() and origin == _target_origin()):
            return _block("caseA not target ip=%s origin=%r" % (ip, origin))
    else:
        # Case B — no X-M2M-Origin: IP + fresh stored target OFFER only, never IP-only-unconditional
        if ip != _target_ip():
            return _block("caseB ip!=target (%s)" % ip)
        try:
            age = now - float(offer.get("epoch") or 0)
        except (TypeError, ValueError):
            return _block("caseB bad offer epoch")
        if age > CORRELATION_TTL_SECONDS:
            return _block("caseB correlation TTL 초과 (%.0fs > %ss)" % (age, CORRELATION_TTL_SECONDS))

    # defensive: a denied identity must never reach here
    if ip in _deny_ips() or origin in _deny_origins():
        raise RuntimeError("denied identity reached TRANSFER path")

    fwr = arm["fwr"]
    try:
        filesize = os.path.getsize(fwr)
    except OSError as e:
        return _block("firmware getsize: %s" % e)

    _write_state({
        "phase": "TRANSFERRING",
        "offer": offer,
        "transfer": {"sent": 0, "filesize": filesize, "epoch": now},
        "updated": now,
    })

    arm_path = _arm_file()

    def _finalize(sent, fsize, _arm=arm_path):
        _on_stream_done(sent, fsize, _arm)

    _log.info("firmware-ota: DOWNLOAD ip=%s origin=%s match=%s path=%s",
              ip, origin or "(none)", "A" if has_origin else "B",
              p.decode("ascii", "replace"))
    return HttpResponse(
        status_line=b"HTTP/1.1 200 OK",
        headers={"Content-Type": "application/octet-stream", "Connection": "close"},
        body=b"",
        stream=StreamPlan(path=fwr, chunk=CHUNK_BYTES, pace=PACE_SECONDS, on_complete=_finalize),
    )


# --------------------------------------------------------------------- hook

def on_http_request(ctx, method, path, headers, body):
    try:
        p = (path or b"").split(b"?", 1)[0]
        is_version_check = p.startswith(b"/mef/updateVersionCheck/firmware/")
        is_firmware_get = p.startswith(b"/mef/firmware")
        if not (is_version_check or is_firmware_get):
            return None                       # not an OTA path -> 99-default handles it
        with _LOCK:
            if is_version_check:
                return _handle_version_check(ctx, method, p, headers)
            return _handle_firmware_get(ctx, method, p, headers)
    except Exception:
        _log.exception("firmware-ota: 예외 — BLOCK (fail-closed)")
        return _block("internal exception")
