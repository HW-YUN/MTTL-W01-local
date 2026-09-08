#!/usr/bin/env python3
"""
MTTL-W01 -> Home Assistant local MQTT bridge (v1).

Independent process.  Does NOT reimplement the smartrelay wire protocol:
  - authoritative telemetry / outlet state  = devices.json written by the feed rule
  - authoritative liveness                   = relay observer  {"list":true}
  - outlet command                           = relay observer inject (existing path)

It mirrors that to a Home Assistant Mosquitto broker via HA MQTT Discovery.

v1 entities (9): switch Outlet 1-4, sensor Power total + Power outlet 1-4 (W).

Key policies:
  - HA switch state comes from real telemetry, never from command echo
    (discovery optimistic=false).  No blind POWER retry.
  - 45s availability grace lives entirely here (DeviceRegistry / relay / dashboard
    are NOT touched).
  - bridge periodic STATUS_GET default OFF (MTTL_HA_STATUS_INTERVAL=0). When it is
    enabled, the dashboard's own periodic STATUS_GET should be turned off so only
    one owner reconciles.
  - post-command settled STATUS_GET default OFF for individual outlets
    (MTTL_HA_POST_CMD_STATUS_DELAY=0). The "All" switch DOES get one one-shot
    targeted STATUS_GET ~MTTL_HA_ALL_CMD_STATUS_DELAY s after a POWER_SET, to
    pull the per-outlet 1..4 state fast; the 30s periodic reconcile is unchanged.
  - power sensors have NO expire_after.
  - QMS: NOT handled here.  Only availability transitions are logged.

Standard library only.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from typing import Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from ha_discovery import build_discovery, clean_mac, device_slug  # noqa: E402
from mttl_mqtt_client import MqttClient  # noqa: E402

log = logging.getLogger("mttl-ha-bridge")


# --------------------------------------------------------------------------- env

def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _env_f(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


class Config:
    def __init__(self) -> None:
        self.ha_host = _env("MTTL_HA_MQTT_HOST")
        self.ha_port = int(_env("MTTL_HA_MQTT_PORT", "1883"))
        self.ha_user = _env("MTTL_HA_MQTT_USERNAME")
        self.ha_pw = _env("MTTL_HA_MQTT_PASSWORD")
        self.ha_tls = _env("MTTL_HA_MQTT_TLS", "0") not in ("0", "false", "no", "")
        self.ha_ca = _env("MTTL_HA_MQTT_CA")
        self.data_prefix = _env("MTTL_HA_MQTT_PREFIX", "mttl")
        self.discovery_prefix = _env("MTTL_HA_DISCOVERY_PREFIX", "homeassistant")
        self.ha_client_id = _env("MTTL_HA_MQTT_CLIENT_ID", "mttl-ha-bridge")

        obs = _env("MTTL_OBSERVER", "127.0.0.1:9883")
        h, _, p = obs.rpartition(":")
        self.obs_host = h or "127.0.0.1"
        self.obs_port = int(p or "9883")

        self.state_file = _env("MTTL_STATE_FILE", "/srv/mttl-lab/relay/state/devices.json")

        # FIX-1 sticky client_id->MAC map. systemd sets STATE_DIRECTORY when the
        # unit has StateDirectory=mttl-ha-bridge (-> /var/lib/mttl-ha-bridge, rw,
        # owned by the service user, survives restart). Falls back to that literal
        # path; a test overrides it with MTTL_HA_SLUGMAP_FILE.
        _sd = (os.environ.get("STATE_DIRECTORY") or "/var/lib/mttl-ha-bridge").split(":")[0]
        self.slugmap_file = _env("MTTL_HA_SLUGMAP_FILE", os.path.join(_sd, "slugmap.json"))

        self.grace_s = _env_f("MTTL_HA_GRACE_SECONDS", 45.0)
        # bring-up default 0 = OFF.  bridge never sends periodic STATUS_GET at 0.
        self.status_interval = _env_f("MTTL_HA_STATUS_INTERVAL", 0.0)
        # FUTURE, default 0 = OFF.  bridge never sends a post-command STATUS_GET at 0.
        # Applies to individual outlet commands only.
        self.post_cmd_status_delay = _env_f("MTTL_HA_POST_CMD_STATUS_DELAY", 0.0)
        # "All" switch ONLY: one-shot targeted STATUS_GET this many seconds after
        # an outlet-0 (POWER_SET) command, to pull the per-outlet 1..4 state fast.
        # Does NOT affect individual outlet commands or the 30s periodic reconcile.
        self.all_cmd_status_delay = _env_f("MTTL_HA_ALL_CMD_STATUS_DELAY", 1.5)
        self.confirm_timeout = _env_f("MTTL_HA_CONFIRM_TIMEOUT", 10.0)

        # cadences (test overridable; not part of the design's public knobs)
        self.list_interval = _env_f("MTTL_HA_LIST_INTERVAL", 4.0)
        self.state_interval = _env_f("MTTL_HA_STATE_INTERVAL", 1.5)
        self.list_stale_s = _env_f("MTTL_HA_LIST_STALE_SECONDS", 15.0)

    def ha_ready(self) -> bool:
        return bool(self.ha_host and self.ha_user and self.ha_pw)


# ------------------------------------------------------------------- state file

class StateReader:
    """Tolerant reader for the feed rule's devices.json.  Never raises; keeps the
    last good snapshot on a missing / partial / invalid file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._last: dict = {}

    def read(self) -> dict:
        try:
            with open(self.path, "r") as f:
                blob = json.load(f)
        except FileNotFoundError:
            return self._last
        except (ValueError, OSError):
            return self._last  # partial write / garbage -> reuse last, retry next poll
        devs = blob.get("devices") if isinstance(blob, dict) else None
        if not isinstance(devs, dict):
            return self._last
        clean = {k: v for k, v in devs.items()
                 if isinstance(v, dict) and not str(k).startswith("ip:")}
        self._last = clean
        return clean


# ------------------------------------------------------------ FIX-1 sticky MAC

_CONFLICT_WARN_INTERVAL_S = 300.0   # rate-limit "conflicting MAC" warnings per client_id


class SlugMap:
    """Persistent, WRITE-ONCE client_id -> MAC memory.

    Once a device has presented a real MAC (devices.json 'mac' or 'device_id'),
    that mapping is remembered forever, so a later poll where the entry is
    momentarily MAC-less (devices.json recreated + MQTT-only reconnect, bridge
    restart, redeploy) still resolves to the SAME actual-MAC slug — never
    regressing to a client_id-hash slug.

    A client_id is bound to its MAC WRITE-ONCE: a later *different* clean MAC for
    the same client_id is NOT auto-applied. The existing binding is kept, a
    rate-limited WARNING is logged, and rebinding is left to an explicit admin
    action (edit/delete the slugmap file). This protects against a swapped/mis-
    provisioned unit silently taking over another unit's HA identity.

    Atomic writes (tmp + os.replace), deterministic JSON (sorted). No secrets.
    If the store is not writable (unit not yet migrated to StateDirectory=), it
    degrades to in-memory only + a one-time warning; the bridge keeps working."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._m: dict = {}              # client_id -> 12-hex mac  (write-once)
        self._save_warned = False
        self._conflict_warned: dict = {}   # client_id -> monotonic ts of last warn
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        src = d.get("client_id_mac") if isinstance(d, dict) else None
        if isinstance(src, dict):
            for k, v in src.items():
                c = clean_mac(v)
                if c:
                    self._m[str(k)] = c
        if self._m:
            log.info("slugmap: loaded %d client_id->MAC mapping(s) from %s",
                     len(self._m), self.path)

    def get(self, client_id: str):
        return self._m.get(client_id)

    def learn(self, client_id: str, mac) -> bool:
        """WRITE-ONCE bind of client_id -> mac.
          - no existing binding + clean MAC  -> learn + persist, return True
          - existing binding == this MAC      -> no-op, return False
          - existing binding != this MAC      -> KEEP the existing one, log a
            rate-limited conflict WARNING, return False (never auto-rebind)."""
        c = clean_mac(mac)
        if not c or not client_id:
            return False
        cur = self._m.get(client_id)
        if cur == c:
            return False
        if cur is not None:
            self._warn_conflict(client_id, cur, c)
            return False
        self._m[client_id] = c
        self._save()
        log.info("slugmap: learned client_id=%s MAC=%s", client_id, c)
        return True

    def _warn_conflict(self, client_id: str, kept: str, rejected: str) -> None:
        now = time.monotonic()
        if now - self._conflict_warned.get(client_id, -1e9) < _CONFLICT_WARN_INTERVAL_S:
            return
        self._conflict_warned[client_id] = now
        log.warning("slugmap: client_id=%s is already bound to MAC=%s; IGNORING a "
                    "conflicting MAC=%s (write-once sticky — rebinding is a manual "
                    "admin action: edit %s)", client_id, kept, rejected, self.path)

    def _save(self) -> None:
        try:
            d = os.path.dirname(self.path)
            if d:
                os.makedirs(d, exist_ok=True)
            tmp = self.path + "." + str(os.getpid()) + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"client_id_mac": self._m}, f,
                          separators=(",", ":"), sort_keys=True)
            os.replace(tmp, self.path)
        except OSError as e:
            if not self._save_warned:
                log.warning("slugmap: cannot persist to %s (%s) - sticky MAC is "
                            "in-memory only this run; deploy StateDirectory= to fix",
                            self.path, e)
                self._save_warned = True


def _bool_map(d) -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            try:
                out[int(k)] = bool(v)
            except (TypeError, ValueError):
                pass
    return out


def _num_map(d) -> dict:
    out = {}
    if isinstance(d, dict):
        for k, v in d.items():
            try:
                out[int(k)] = float(v)
            except (TypeError, ValueError):
                pass
    return out


def outlet_state(entry: dict) -> dict:
    return _bool_map(entry.get("power"))


def power_watts(entry: dict) -> dict:
    """{'total': float|None, 1..4: float|None} from the already-parsed meter_watts."""
    mw_ = _num_map(entry.get("meter_watts"))
    per = {n: mw_.get(n) for n in (1, 2, 3, 4)}
    if 0 in mw_:
        total = mw_[0]
    else:
        vals = [v for v in per.values() if isinstance(v, (int, float))]
        total = sum(vals) if vals else None
    return {"total": total, 1: per[1], 2: per[2], 3: per[3], 4: per[4]}


# ------------------------------------------------------------ availability grace

class AvailabilityMachine:
    """Per-slug UP / GRACE / DOWN machine.  All time comes in as an argument so
    tests can drive it with a fake clock.

    observe() -> list of (slug, event) with event in:
        'online'            first time up / grace-recovered while still 'up'
        'online_republish'  came back from DOWN -> also re-publish state+power
        'offline'           grace expired
    Log-only lifecycle events are on .log_events after each call.
    """

    def __init__(self, grace_seconds: float) -> None:
        self.grace = float(grace_seconds)
        self._st: dict = {}
        self.log_events: list = []

    def observe(self, live_slugs: set, known_slugs: set, now: float) -> list:
        self.log_events = []
        out = []
        for slug in known_slugs:
            cur = self._st.get(slug)
            if cur is None:
                cur = {"state": "init", "since": None}
                self._st[slug] = cur
            live = slug in live_slugs
            state = cur["state"]

            if state in ("init", "down"):
                if live:
                    cur["state"] = "up"
                    cur["since"] = None
                    out.append((slug, "online_republish" if state == "down" else "online"))
                    self.log_events.append((slug, "reconnected" if state == "down" else "first_online", 0.0))
            elif state == "up":
                if not live:
                    cur["state"] = "grace"
                    cur["since"] = now
                    self.log_events.append((slug, "entered_grace", 0.0))
            elif state == "grace":
                dur = now - (cur["since"] or now)
                if live:
                    cur["state"] = "up"
                    cur["since"] = None
                    self.log_events.append((slug, "reconnected_within_grace", dur))
                elif dur >= self.grace:
                    cur["state"] = "down"
                    cur["since"] = None
                    out.append((slug, "offline"))
                    self.log_events.append((slug, "grace_expired", dur))
        return out

    def state_of(self, slug: str) -> str:
        return self._st.get(slug, {}).get("state", "init")


# --------------------------------------------------------------------- observer

class ObserverLink:
    """Bridge's own minimal client to the relay observer (plaintext, no auth).
    Not derived from the dashboard module."""

    INJECT_TOPIC = "mttl/ha-bridge/inject"
    REQ_TOPIC = "mtap/req"

    def __init__(self, host: str, port: int) -> None:
        self._lock = threading.Lock()
        self._live: set = set()
        self._latest: Optional[str] = None
        self._live_ts = 0.0
        self.mqtt = MqttClient(
            host, port, "mttl-ha-bridge-obs",
            on_connect=lambda c: c.subscribe([("#", 0)]),
            on_message=self._on_msg, log=lambda lvl, m: log.log(getattr(logging, lvl, 20), m),
            name="observer",
        )

    def start(self) -> None:
        self.mqtt.start()

    def stop(self) -> None:
        self.mqtt.stop()

    @property
    def connected(self) -> bool:
        return self.mqtt.connected

    def _on_msg(self, topic: str, payload: bytes, qos: int, retain: bool, dup: bool) -> None:
        if topic != "mtap/devices":
            return
        try:
            d = json.loads(payload)
        except Exception:
            return
        with self._lock:
            self._live = set(d.get("devices") or [])
            self._latest = d.get("latest")
            self._live_ts = time.monotonic()

    def request_list(self) -> None:
        self.mqtt.publish(self.REQ_TOPIC, b'{"list":true}', qos=1)

    def live_ids(self, stale_after: float) -> set:
        with self._lock:
            fresh = self.mqtt.connected and (time.monotonic() - self._live_ts) < stale_after
            return set(self._live) if fresh else set()

    def inject_power(self, outlet: int, on: bool, device_client_id: Optional[str]) -> None:
        cmd = {"action": "power", "outlet": int(outlet), "on": bool(on)}
        if device_client_id:
            cmd["device_client_id"] = device_client_id
        self.mqtt.publish(self.INJECT_TOPIC, json.dumps(cmd).encode(), qos=1)

    def inject_status(self, device_client_id: Optional[str]) -> None:
        cmd = {"action": "status"}
        if device_client_id:
            cmd["device_client_id"] = device_client_id
        self.mqtt.publish(self.INJECT_TOPIC, json.dumps(cmd).encode(), qos=1)


# ----------------------------------------------------------------------- bridge

# outlet 0 == the "All" switch -> wire command POWER_SET (existing, unchanged)
_SET_TOPIC_RE = re.compile(r"^(?P<pfx>[^/]+)/(?P<slug>[^/]+)/outlet/(?P<n>[0-4])/set$")


class HaBridge:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.reader = StateReader(cfg.state_file)
        self.slugmap = SlugMap(cfg.slugmap_file)          # FIX-1 sticky client_id->MAC
        self.obs = ObserverLink(cfg.obs_host, cfg.obs_port)
        self.avail = AvailabilityMachine(cfg.grace_s)

        self._stop = threading.Event()
        self._ha_need_republish = threading.Event()
        self._pub_lock = threading.Lock()
        self._last_pub: dict = {}          # slug -> {"o":{n:bool}, "p":{k:val}, "v":str, "t":{}, "e":{}, "avail":str}
        self._pending: dict = {}           # (slug, outlet) -> {"want":bool, "deadline":float}
        self._slug2cid: dict = {}          # slug -> client_id (from last poll)
        self._slug_mac: dict = {}          # slug -> resolved MAC (for discovery connections)
        self._deferred_logged: set = set() # client_ids we've already logged a "no MAC yet" for
        self._vtpe_pub: dict = {}          # slug -> bool : did the last discovery publish include v/t/e entities

        self.ha: Optional[MqttClient] = None
        if cfg.ha_ready():
            self.ha = MqttClient(
                cfg.ha_host, cfg.ha_port, cfg.ha_client_id,
                username=cfg.ha_user, password=cfg.ha_pw, tls=cfg.ha_tls, ca=cfg.ha_ca,
                will_topic=f"{cfg.data_prefix}/bridge/status", will_payload="offline",
                will_retain=True, will_qos=1,
                on_connect=self._on_ha_connect,
                on_message=self._on_ha_message,
                log=lambda lvl, m: log.log(getattr(logging, lvl, 20), m),
                name="ha",
            )
        else:
            log.error("HA broker not configured (need MTTL_HA_MQTT_HOST/USERNAME/PASSWORD) "
                      "- bridge runs but publishes nothing")

        self._thr = threading.Thread(target=self._loop, name="ha-bridge", daemon=True)

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self.obs.start()
        if self.ha:
            self.ha.start()
        self._thr.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._thr.join(timeout=timeout)
        self.obs.stop()
        if self.ha:
            # graceful: mark offline (retained) before the LWT would
            try:
                self.ha.publish(f"{self.cfg.data_prefix}/bridge/status", b"offline", qos=1, retain=True)
            except Exception:
                pass
            self.ha.stop()

    # ---- HA callbacks --------------------------------------------------

    def _on_ha_connect(self, _c: MqttClient) -> None:
        # runs in the ha client thread; just flag, the loop does the work
        self._ha_need_republish.set()

    def _on_ha_message(self, topic: str, payload: bytes, qos: int, retain: bool, dup: bool) -> None:
        if retain:
            log.warning("ignoring RETAINED command on %s (broker replay guard)", topic)
            return
        m = _SET_TOPIC_RE.match(topic)
        if not m or m.group("pfx") != self.cfg.data_prefix:
            log.debug("unrecognised command topic %s", topic)
            return
        slug = m.group("slug")
        outlet = int(m.group("n"))
        val = payload.decode("utf-8", "replace").strip().upper()
        if val not in ("ON", "OFF"):
            log.warning("command %s: bad payload %r (want ON/OFF)", topic, payload[:16])
            return
        on = val == "ON"
        cid = self._slug2cid.get(slug)
        if not cid:
            log.warning("command for unknown device slug %s", slug)
            return
        multi = len(self._slug2cid) > 1
        self.obs.inject_power(outlet, on, cid if multi else None)
        log.info("command -> observer inject: outlet=%d on=%s slug=%s%s",
                 outlet, on, slug, " (targeted)" if multi else "")
        # "All" switch (outlet 0) uses its own one-shot delay; individual outlets
        # keep the global post_cmd_status_delay (0 = off) unchanged.
        is_all = (outlet == 0)
        delay = self.cfg.all_cmd_status_delay if is_all else self.cfg.post_cmd_status_delay
        self._pending[(slug, outlet)] = {"want": on, "deadline": time.monotonic() + self.cfg.confirm_timeout,
                                         "status_at": (time.monotonic() + delay) if delay > 0 else None,
                                         "cid": cid, "multi": multi, "is_all": is_all}
        # NOTE: no optimistic state publish here.  HA switch follows telemetry only.

    # ---- publish helpers ---------------------------------------------

    def _pub(self, topic: str, payload: str, qos: int = 0, retain: bool = False) -> None:
        if self.ha and self.ha.connected:
            self.ha.publish(topic, payload.encode(), qos=qos, retain=retain)

    def _publish_discovery(self, slug: str, mac: Optional[str], vtpe: bool = False) -> None:
        for item in build_discovery(slug, mac=mac, sw_version=("1.0.106" if vtpe else None), vtpe=vtpe,
                                    data_prefix=self.cfg.data_prefix,
                                    discovery_prefix=self.cfg.discovery_prefix):
            self._pub(item["topic"], item["payload"], qos=1, retain=True)
        self._vtpe_pub[slug] = bool(vtpe)

    @staticmethod
    def _rec(_d):
        _d.setdefault("o", {}); _d.setdefault("p", {}); _d.setdefault("t", {})
        _d.setdefault("e", {}); _d.setdefault("v", None); _d.setdefault("avail", None)
        return _d

    def _publish_device_state(self, slug: str, entry: dict, force: bool = False) -> None:
        rec = self._rec(self._last_pub.setdefault(slug, {}))
        outs = outlet_state(entry)
        for n in (0, 1, 2, 3, 4):                       # 0 = the "All" switch
            v = outs.get(n)
            if v is None:
                continue
            s = "ON" if v else "OFF"
            if force or rec["o"].get(n) != s:
                self._pub(f"{self.cfg.data_prefix}/{slug}/outlet/{n}/state", s, qos=1, retain=True)
                rec["o"][n] = s
        pw = power_watts(entry)                         # p-derived meter_watts, else meterN_02
        for key in ("total", 1, 2, 3, 4):
            val = pw.get(key)
            if val is None:
                continue
            topic_key = str(key)
            s = f"{float(val):.2f}"
            if force or rec["p"].get(topic_key) != s:
                self._pub(f"{self.cfg.data_prefix}/{slug}/power/{topic_key}", s, qos=0, retain=False)
                rec["p"][topic_key] = s
        # --- voltage / internal temperature / cumulative energy -----
        # gated on vtpe_seen: the same persistent marker that gates the discovery.
        # A partial report (some fields, marker not set) publishes neither.
        if not entry.get("vtpe_seen"):
            return
        vv = entry.get("voltage_v")
        if isinstance(vv, (int, float)):
            s = f"{float(vv):.1f}"
            if force or rec["v"] != s:
                self._pub(f"{self.cfg.data_prefix}/{slug}/voltage", s, qos=0, retain=False)
                rec["v"] = s
        for n, tv in _num_map(entry.get("temperature_c")).items():
            if n not in (1, 2, 3, 4):
                continue
            s = f"{float(tv):.1f}"
            if force or rec["t"].get(n) != s:
                # temperature: retain=True. It is a slowly-varying value and the
                # change-suppression below means it is published rarely; without
                # retain HA that (re)subscribes after the one publish would show
                # "unknown" forever. (matches the energy sensors.)
                self._pub(f"{self.cfg.data_prefix}/{slug}/temp/{n}", s, qos=1, retain=True)
                rec["t"][n] = s
        for k, ev in _num_map(entry.get("energy_kwh")).items():
            if k not in (0, 1, 2, 3, 4):
                continue
            topic_key = "total" if k == 0 else str(k)
            s = f"{float(ev):.3f}"
            if force or rec["e"].get(topic_key) != s:
                # energy: retain=True (total_increasing survives HA restart)
                self._pub(f"{self.cfg.data_prefix}/{slug}/energy/{topic_key}", s, qos=1, retain=True)
                rec["e"][topic_key] = s

    def _set_availability(self, slug: str, online: bool) -> None:
        rec = self._rec(self._last_pub.setdefault(slug, {}))
        s = "online" if online else "offline"
        if rec["avail"] != s:
            self._pub(f"{self.cfg.data_prefix}/{slug}/availability", s, qos=1, retain=True)
            rec["avail"] = s

    # ---- FIX-1 MAC resolution --------------------------------------

    def _resolve_mac(self, cid: str, entry: dict):
        """Best actual MAC for a client_id.

        A sticky binding ALWAYS wins once it exists: this method never returns a
        MAC that differs from the remembered one, and never rebinds it. It only
        *learns* a MAC for a client_id that has none yet.

          - sticky set  -> return sticky (a differing clean MAC in `entry` is
                           handed to slugmap.learn() only so it logs the conflict,
                           then ignored)
          - sticky unset -> learn from the first clean MAC in
                           entry['mac'] / entry['device_id'] and return it
          - neither      -> None  -> caller defers HA discovery (FIX-2)
        """
        sticky = self.slugmap.get(cid)
        for cand in (entry.get("mac"), entry.get("device_id")):
            c = clean_mac(cand)
            if not c:
                continue
            if sticky is None:
                self.slugmap.learn(cid, c)      # first sighting: learn + persist
                return c
            if c != sticky:
                self.slugmap.learn(cid, c)      # no-op + rate-limited conflict warn
            return sticky                        # sticky wins, unconditionally
        return sticky

    # ---- main loop --------------------------------------------------

    def _loop(self) -> None:
        last_list = last_state = last_status = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            wall = now  # monotonic is fine for grace maths; keeps tests hermetic

            if now - last_list >= self.cfg.list_interval:
                self.obs.request_list()
                last_list = now

            state = self.reader.read()
            self._slug2cid = {}
            self._slug_mac = {}
            slug_entry: dict = {}
            for cid, entry in state.items():
                mac = self._resolve_mac(cid, entry)
                if not mac:
                    # FIX-2: no real MAC yet (no mac/device_id field, nothing
                    # remembered) -> do NOT publish a client_id-hash discovery.
                    # Wait for the MAC. The device simply isn't in HA yet.
                    if cid not in self._deferred_logged:
                        log.info("HA discovery deferred: client_id=%s has no MAC yet "
                                 "(mac/device_id absent, none remembered) - waiting, "
                                 "NOT publishing a hash-fallback slug", cid)
                        self._deferred_logged.add(cid)
                    continue
                self._deferred_logged.discard(cid)
                slug = device_slug(cid, mac)          # always an actual-MAC slug
                self._slug2cid[slug] = cid
                self._slug_mac[slug] = mac
                slug_entry[slug] = entry
            known = set(slug_entry)

            live_cids = self.obs.live_ids(self.cfg.list_stale_s)
            live_slugs = set()
            for cid in live_cids:
                mac = self._resolve_mac(cid, state.get(cid, {}))
                if mac:
                    live_slugs.add(device_slug(cid, mac))
            live_slugs &= (known | live_slugs)  # keep even if not in state yet

            if self._ha_need_republish.is_set() and self.ha and self.ha.connected:
                self._ha_need_republish.clear()
                self._last_pub.clear()
                self._vtpe_pub.clear()
                self._pub(f"{self.cfg.data_prefix}/bridge/status", "online", qos=1, retain=True)
                for slug, entry in slug_entry.items():
                    self._publish_discovery(slug, self._slug_mac.get(slug),
                                            vtpe=bool(entry.get("vtpe_seen")))
                    online = self.avail.state_of(slug) == "up"
                    self._set_availability(slug, online)
                    if online:
                        self._publish_device_state(slug, entry, force=True)
                # subscribe to command topics LAST, after state is on the broker
                self.ha.subscribe([(f"{self.cfg.data_prefix}/+/outlet/+/set", 1)])
                log.info("HA (re)connected: discovery + current state republished for %d device(s)", len(slug_entry))

            # a device that has now sent a clean v/t/p/e telemetry gets its
            # voltage/temp/energy discovery published (once). Never removed here.
            for slug, entry in slug_entry.items():
                if entry.get("vtpe_seen") and not self._vtpe_pub.get(slug) and self.ha and self.ha.connected:
                    self._publish_discovery(slug, self._slug_mac.get(slug), vtpe=True)
                    log.info("vtpe telemetry seen slug=%s - discovery republished with voltage/temp/energy", slug)
                    if self.avail.state_of(slug) == "up":
                        self._publish_device_state(slug, entry, force=True)

            for slug, ev in self.avail.observe(live_slugs, known, wall):
                if ev in ("online", "online_republish"):
                    if slug not in self._last_pub:
                        self._publish_discovery(slug, self._slug_mac.get(slug),
                                                vtpe=bool(slug_entry.get(slug, {}).get("vtpe_seen")))
                    self._set_availability(slug, True)
                    # both first-online and DOWN->UP force a full state/power republish
                    self._publish_device_state(slug, slug_entry.get(slug, {}), force=True)
                elif ev == "offline":
                    self._set_availability(slug, False)
            for slug, kind, dur in self.avail.log_events:
                log.info("availability: %s slug=%s down_for=%.1fs", kind, slug, dur)

            if now - last_state >= self.cfg.state_interval:
                last_state = now
                for slug, entry in slug_entry.items():
                    if self.avail.state_of(slug) == "up":
                        self._publish_device_state(slug, entry, force=False)

            if self.cfg.status_interval > 0 and now - last_status >= self.cfg.status_interval:
                last_status = now
                if len(live_cids) == 1:
                    self.obs.inject_status(None)
                else:
                    for cid in live_cids:
                        self.obs.inject_status(cid)

            self._check_pending(state, now)
            self._stop.wait(0.2)

    def _check_pending(self, state: dict, now: float) -> None:
        for key in list(self._pending.keys()):
            slug, outlet = key
            p = self._pending[key]
            # one-shot post-command STATUS_GET. `status_at` is only ever set when a
            # positive delay was chosen (All -> all_cmd_status_delay; individual ->
            # post_cmd_status_delay, 0/off in production). Fires AT MOST ONCE per
            # command (status_at -> None).
            one_shot_done = p.get("status_at") is None
            if not one_shot_done and now >= p["status_at"]:
                self.obs.inject_status(p["cid"] if p["multi"] else None)
                p["status_at"] = None
                one_shot_done = True
                log.info("one-shot STATUS_GET after %s command: slug=%s outlet=%d",
                         "All" if p.get("is_all") else "outlet", slug, outlet)
            cid = self._slug2cid.get(slug)
            entry = state.get(cid, {}) if cid else {}
            cur = outlet_state(entry).get(outlet)
            confirmed = cur is not None and cur == p["want"]
            # For the All switch, keep the pending alive until its one-shot has
            # fired even when power[0] is already confirmed -- the one-shot's job
            # is to pull the per-outlet 1..4 state, not to confirm the aggregate.
            if confirmed and (one_shot_done or not p.get("is_all")):
                log.info("command applied: slug=%s outlet=%d -> %s", slug, outlet, "ON" if p["want"] else "OFF")
                self._pending.pop(key, None)
                continue
            if now >= p["deadline"]:
                log.warning("command timeout (NOT retried): slug=%s outlet=%d want=%s",
                            slug, outlet, "ON" if p["want"] else "OFF")
                self._pending.pop(key, None)


# --------------------------------------------------------------------------- main

def main(argv=None) -> int:
    logging.basicConfig(
        level=getattr(logging, _env("MTTL_HA_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = Config()
    log.info("starting: observer=%s:%d state=%s slugmap=%s data_prefix=%s grace=%.0fs "
             "status_interval=%.0f post_cmd_status_delay=%.0f",
             cfg.obs_host, cfg.obs_port, cfg.state_file, cfg.slugmap_file, cfg.data_prefix,
             cfg.grace_s, cfg.status_interval, cfg.post_cmd_status_delay)
    bridge = HaBridge(cfg)
    bridge.start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
