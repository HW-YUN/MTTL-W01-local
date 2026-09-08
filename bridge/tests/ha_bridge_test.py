#!/usr/bin/env python3
"""
Synthetic test for the MTTL-W01 -> Home Assistant MQTT bridge (v1).

No real HA, no real container, no real device, no cloud.  Everything runs on
localhost against two in-process minimal MQTT stub servers (one plays the HA
broker, one plays the relay observer) plus a temp devices.json fixture.

Standard library only.  Runnable on Python 3.9+.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, "..", ".."))
BRIDGE = os.path.join(ROOT, "bridge")
VENDOR = os.path.join(ROOT, "vendor", "smartrelay")
for p in (VENDOR, BRIDGE):
    if p not in sys.path:
        sys.path.insert(0, p)

import mqtt_wire as mw            # noqa: E402  (vendored, read-only)
import ha_discovery              # noqa: E402
import mttl_ha_bridge as B       # noqa: E402

PASS: list = []
FAIL: list = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (" — " + str(detail)[:200] if detail else ""))


def wait_for(pred, timeout=5.0, interval=0.05):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


# --------------------------------------------------------------------------- stub

class MiniServer:
    """Minimal MQTT 3.1.1 server for tests. Records everything the bridge sends
    (with retain flag); can push messages to the connected client; can drop the
    client connection; observer mode auto-answers {"list":true}."""

    def __init__(self, is_observer=False):
        self.is_observer = is_observer
        self.published: list = []       # dict(topic,payload,qos,retain,dup)
        self.injects: list = []         # observer: non-list JSON commands
        self.list_devices = ([], None)  # observer: (client_ids, latest)
        self.connect_bodies: list = []
        self.subs: list = []
        self._sock = None
        self._lock = threading.Lock()
        self._stop = False
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self.port = self._srv.getsockname()[1]
        self._srv.listen(4)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        self._srv.settimeout(0.2)
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        with self._lock:
            self._sock = c
        fr = mw.Framer()
        c.settimeout(0.2)
        try:
            while not self._stop:
                try:
                    data = c.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                for pkt in fr.push(data):
                    self._handle(c, pkt)
        finally:
            with self._lock:
                if self._sock is c:
                    self._sock = None
            try:
                c.close()
            except Exception:
                pass

    def _handle(self, c, pkt):
        if pkt.kind == mw.CONNECT:
            self.connect_bodies.append(bytes(pkt.body))
            self._send(c, mw.build_connack(False, 0))
        elif pkt.kind == mw.SUBSCRIBE:
            info = mw.parse_subscribe(pkt)
            self.subs += [t.decode("utf-8", "replace") for t, _ in info.topics]
            self._send(c, mw.build_suback(info.packet_id, [min(q, 1) for _, q in info.topics]))
        elif pkt.kind == mw.PUBLISH:
            info = mw.parse_publish(pkt)
            if info.qos == 1 and info.packet_id is not None:
                self._send(c, mw.build_puback(info.packet_id))
            self._on_publish(info)
        elif pkt.kind == mw.PINGREQ:
            self._send(c, mw.build_pingresp())

    def _on_publish(self, info):
        self.published.append(dict(
            topic=info.topic.decode("utf-8", "replace"), payload=bytes(info.payload),
            qos=info.qos, retain=bool(info.retain), dup=bool(info.dup)))
        if self.is_observer:
            try:
                cmd = json.loads(info.payload)
            except Exception:
                cmd = None
            if isinstance(cmd, dict):
                if cmd.get("list"):
                    devs, latest = self.list_devices
                    self.push("mtap/devices",
                              json.dumps({"devices": list(devs), "latest": latest}).encode())
                else:
                    self.injects.append(cmd)

    def _send(self, c, pkt):
        with self._lock:
            try:
                c.sendall(pkt.bytes())
            except Exception:
                pass

    def push(self, topic, payload, qos=0, retain=False):
        if isinstance(topic, str):
            topic = topic.encode()
        if isinstance(payload, str):
            payload = payload.encode()
        pkt = mw.build_publish(topic, payload, qos=qos,
                               packet_id=(9 if qos > 0 else None), retain=retain)
        with self._lock:
            if self._sock is None:
                return False
            try:
                self._sock.sendall(pkt.bytes())
                return True
            except Exception:
                return False

    def kill_client(self):
        with self._lock:
            s, self._sock = self._sock, None
        if s:
            try:
                s.close()
            except Exception:
                pass

    def stop(self):
        self._stop = True
        try:
            self._srv.close()
        except Exception:
            pass

    # query helpers
    def topics(self):
        return [p["topic"] for p in self.published]

    def by_topic(self, t):
        return [p for p in self.published if p["topic"] == t]

    def discovery_configs(self):
        return [p for p in self.published if p["topic"].endswith("/config")
                and p["topic"].split("/")[0] == "homeassistant"]

    def power_injects(self, outlet=None):
        return [i for i in self.injects if i.get("action") == "power"
                and (outlet is None or i.get("outlet") == outlet)]

    def status_injects(self):
        return [i for i in self.injects if i.get("action") == "status"]


def parse_will(body: bytes):
    off = 0
    _, off = mw._read_str(body, off)   # "MQTT"
    off += 1                           # protocol level
    cf = body[off]
    off += 1
    off += 2                           # keepalive
    _, off = mw._read_str(body, off)   # client id
    wt = wp = None
    if cf & 0x04:
        wt, off = mw._read_str(body, off)
        wp, off = mw._read_str(body, off)
    return cf, wt, wp


# --------------------------------------------------------------------------- fixt

DEV_A = "ASN_CSE-D-AAAA-MTAP"
MAC_A = "02:00:00:00:00:03"
SLUG_A = ha_discovery.device_slug(DEV_A, MAC_A)
DEV_B = "ASN_CSE-D-BBBB-MTAP"
MAC_B = "AA:BB:CC:DD:EE:FF"
SLUG_B = ha_discovery.device_slug(DEV_B, MAC_B)


def _entry(client_id, mac, power=None, watts=None):
    return {
        "client_id": client_id, "mac": mac,
        "power": power or {"1": False, "2": False, "3": False, "4": False},
        "meter_watts": watts or {},
    }


def write_state(path, devices: dict):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"updated": time.time(), "devices": devices}, f)
    os.replace(tmp, path)


def make_bridge(obs: MiniServer, broker: MiniServer, state_path: str, **over):
    env = dict(
        MTTL_HA_MQTT_HOST="127.0.0.1", MTTL_HA_MQTT_PORT=str(broker.port),
        MTTL_HA_MQTT_USERNAME="mttl-bridge", MTTL_HA_MQTT_PASSWORD="s3cr3t",
        MTTL_HA_MQTT_PREFIX="mttl", MTTL_HA_DISCOVERY_PREFIX="homeassistant",
        MTTL_OBSERVER="127.0.0.1:%d" % obs.port, MTTL_STATE_FILE=state_path,
        MTTL_HA_SLUGMAP_FILE=os.path.join(os.path.dirname(state_path),
                                          "slugmap-%d.json" % broker.port),
        MTTL_HA_GRACE_SECONDS="1.0", MTTL_HA_STATUS_INTERVAL="0",
        MTTL_HA_POST_CMD_STATUS_DELAY="0", MTTL_HA_CONFIRM_TIMEOUT="1.5",
        MTTL_HA_LIST_INTERVAL="0.25", MTTL_HA_STATE_INTERVAL="0.25",
        MTTL_HA_LIST_STALE_SECONDS="3", MTTL_HA_LOG_LEVEL="WARNING",
    )
    env.update({k: str(v) for k, v in over.items()})
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        bridge = B.HaBridge(B.Config())
    finally:
        for k, v in old.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    return bridge


# --------------------------------------------------------------------------- unit

def test_unit_slug():
    print("== unit: device_slug ==")
    a1 = ha_discovery.device_slug(DEV_A, MAC_A)
    a2 = ha_discovery.device_slug(DEV_A, MAC_A)
    ok("slug deterministic (mac)", a1 == a2 == "020000000003", a1)
    n1 = ha_discovery.device_slug("weird id", None)
    n2 = ha_discovery.device_slug("weird id", None)
    ok("slug deterministic (client_id fallback)", n1 == n2 and n1.startswith("d") and len(n1) == 13, n1)
    ok("slug not from builtin hash()", n1 != ("d" + format(hash("weird id") & 0xFFFFFFFFFFFF, "012x")))
    ip_slug = ha_discovery.device_slug(DEV_A, "192.0.2.123")
    ok("slug ignores non-MAC (IP-ish) value -> client_id fallback",
       ip_slug == ha_discovery.device_slug(DEV_A, None) and ip_slug.startswith("d"), ip_slug)


def test_unit_clean_mac_strict():
    print("== unit: clean_mac (strict) ==")
    cm = ha_discovery.clean_mac
    accept = {
        "020000000004": "020000000004",
        "020000000003": "020000000003",
        "02:00:00:00:00:04": "020000000004",
        "02:00:00:00:00:03": "020000000003",
        "02-00-00-00-00-04": "020000000004",
        "  020000000004  ": "020000000004",
        "020000000004": "020000000004",
    }
    for i, o in accept.items():
        ok(f"clean_mac accepts {i!r} -> {o}", cm(i) == o, cm(i))
    reject = ["192.0.2.123", "192.0.2.9", "not-a-mac", "", None,
              "ASN_CSE-D-EXAMPLE004-MTAP",
              "020000000004 (primary)", "MAC=020000000004",   # <- no arbitrary non-hex stripping
              "02:00-00:00:00:04",                            # <- mixed separator
              "02000000000", "0200000000049", "02:00:00:00:04"]  # <- wrong length
    for i in reject:
        ok(f"clean_mac rejects {i!r}", cm(i) is None, cm(i))
    ok("example MAC forms still resolve to their expected slugs",
       ha_discovery.device_slug("ASN_CSE-D-EXAMPLE003-MTAP", "02:00:00:00:00:03") == "020000000003"
       and ha_discovery.device_slug("ASN_CSE-D-EXAMPLE004-MTAP", "020000000004") == "020000000004")


def test_unit_discovery():
    print("== unit: build_discovery ==")
    items = ha_discovery.build_discovery(SLUG_A, mac=MAC_A)
    ok("base discovery 10 entities (4 outlet + All + 5 power)", len(items) == 10, len(items))
    sw = [i for i in items if "/switch/" in i["topic"]]
    se = [i for i in items if "/sensor/" in i["topic"]]
    ok("5 switch + 5 sensor", len(sw) == 5 and len(se) == 5, (len(sw), len(se)))
    ok("no expire_after anywhere", all("expire_after" not in i["payload"] for i in items))
    payloads = [json.loads(i["payload"]) for i in items]
    ok("every switch optimistic=false",
       all(p.get("optimistic") is False for p in payloads if "command_topic" in p))
    o1 = next(p for p in payloads if p.get("unique_id", "").endswith("_outlet_1"))
    ok("switch command_topic is the bridge topic",
       o1["command_topic"] == f"mttl/{SLUG_A}/outlet/1/set", o1["command_topic"])
    ok("switch payload ON/OFF", o1["payload_on"] == "ON" and o1["payload_off"] == "OFF")
    allsw = next(p for p in payloads if p.get("unique_id", "").endswith("_all"))
    ok("All switch -> outlet/0 topics, optimistic false",
       allsw["name"] == "All" and allsw["command_topic"] == f"mttl/{SLUG_A}/outlet/0/set"
       and allsw["state_topic"] == f"mttl/{SLUG_A}/outlet/0/state" and allsw["optimistic"] is False)
    ok("two availability topics + mode all",
       all(len(p["availability"]) == 2 and p["availability_mode"] == "all" for p in payloads))
    ok("availability topics are bridge/status + <slug>/availability",
       {t["topic"] for t in o1["availability"]} == {"mttl/bridge/status", f"mttl/{SLUG_A}/availability"})
    pt = next(p for p in payloads if p.get("unique_id", "").endswith("_power_total"))
    ok("power_total state_topic + unique_id unchanged",
       pt["state_topic"] == f"mttl/{SLUG_A}/power/total"
       and pt["unique_id"] == f"mttl_{SLUG_A}_power_total")
    ok("base set has NO voltage/temp/energy entity (vtpe=False)",
       not any(k in i["topic"] for i in items for k in ("voltage", "temp_", "energy")))
    ok("base set has NO wifi/binary_sensor entity",
       not any(k in i["topic"] for i in items for k in ("wifi", "binary_sensor")))
    # vtpe=True -> +10 = 20
    vitems = ha_discovery.build_discovery(SLUG_A, mac=MAC_A, vtpe=True)
    ok("vtpe discovery 20 entities", len(vitems) == 20, len(vitems))
    vuids = {json.loads(i["payload"])["unique_id"] for i in vitems}
    ok("vtpe adds voltage + temp_1..4 + energy_total + energy_1..4",
       {f"mttl_{SLUG_A}_{o}" for o in
        ("voltage", "temp_1", "temp_2", "temp_3", "temp_4",
         "energy_total", "energy_1", "energy_2", "energy_3", "energy_4")} <= vuids)
    vp = {json.loads(i["payload"])["unique_id"]: json.loads(i["payload"]) for i in vitems}
    ok("voltage: V / device_class voltage / measurement",
       vp[f"mttl_{SLUG_A}_voltage"]["unit_of_measurement"] == "V"
       and vp[f"mttl_{SLUG_A}_voltage"]["device_class"] == "voltage"
       and vp[f"mttl_{SLUG_A}_voltage"]["state_class"] == "measurement")
    ok("temp_1: 'Internal temperature 1' / degC / temperature / measurement",
       vp[f"mttl_{SLUG_A}_temp_1"]["name"] == "Internal temperature 1"
       and vp[f"mttl_{SLUG_A}_temp_1"]["unit_of_measurement"] == "°C"
       and vp[f"mttl_{SLUG_A}_temp_1"]["device_class"] == "temperature")
    ok("energy_total: kWh / energy / total_increasing",
       vp[f"mttl_{SLUG_A}_energy_total"]["unit_of_measurement"] == "kWh"
       and vp[f"mttl_{SLUG_A}_energy_total"]["device_class"] == "energy"
       and vp[f"mttl_{SLUG_A}_energy_total"]["state_class"] == "total_increasing")
    ok("base 10 unique_ids are a subset of vtpe 20 (no rename/removal)",
       {json.loads(i["payload"])["unique_id"] for i in items} <= vuids)


def test_unit_availability_machine():
    print("== unit: AvailabilityMachine (fake clock) ==")
    m = B.AvailabilityMachine(45.0)
    r = m.observe({"a"}, {"a"}, 0.0)
    ok("first sight -> online", r == [("a", "online")], r)
    r = m.observe(set(), {"a"}, 10.0)
    ok("gone -> no publish (enters grace)", r == [] and m.state_of("a") == "grace", (r, m.state_of("a")))
    r = m.observe(set(), {"a"}, 40.0)
    ok("still <45s -> no offline", r == [] and m.state_of("a") == "grace")
    r = m.observe({"a"}, {"a"}, 44.0)
    ok("reconnect within grace -> stays up, no publish", r == [] and m.state_of("a") == "up", r)
    ok("logged reconnected_within_grace", any(e[1] == "reconnected_within_grace" for e in m.log_events))
    m.observe(set(), {"a"}, 100.0)                       # grace
    r = m.observe(set(), {"a"}, 150.0)                   # 50s >= 45
    ok("grace expired -> offline", r == [("a", "offline")] and m.state_of("a") == "down", r)
    r = m.observe({"a"}, {"a"}, 160.0)
    ok("DOWN -> reconnect -> online_republish", r == [("a", "online_republish")], r)


def test_unit_state_reader(tmpdir):
    print("== unit: StateReader tolerance ==")
    p = os.path.join(tmpdir, "sr.json")
    sr = B.StateReader(p)
    ok("missing file -> {}", sr.read() == {})
    write_state(p, {DEV_A: _entry(DEV_A, MAC_A)})
    ok("valid file -> device", set(sr.read()) == {DEV_A})
    with open(p, "w") as f:
        f.write('{"devices": {"x": ')  # truncated / invalid
    ok("partial write -> keeps last good", set(sr.read()) == {DEV_A})
    write_state(p, {DEV_A: _entry(DEV_A, MAC_A), "ip:192.0.2.5": {"foo": 1}})
    ok("ip: pseudo-entries skipped", set(sr.read()) == {DEV_A})


# ---------------------------------------------------------------------- integ

def test_integration(tmpdir):
    print("== integration: bridge <-> stub HA broker + stub observer ==")
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices.json")
    write_state(sp, {DEV_A: _entry(DEV_A, MAC_A)})
    obs.list_devices = ([DEV_A], DEV_A)

    bridge = make_bridge(obs, broker, sp)
    bridge.start()
    try:
        ok("1. bridge MQTT CONNECT reached the broker",
           wait_for(lambda: len(broker.connect_bodies) >= 1, 6), broker.connect_bodies)
        ok("   observer link connected",
           wait_for(lambda: bridge.obs.connected, 6))
        ok("   HA client connected", wait_for(lambda: bridge.ha.connected, 6))

        cf, wt, wp = parse_will(broker.connect_bodies[0])
        ok("2. LWT set: mttl/bridge/status = offline, retained",
           (cf & 0x04) and wt == b"mttl/bridge/status" and wp == b"offline" and (cf & 0x20),
           (hex(cf), wt, wp))
        ok("   CONNECT carries username+password flags", (cf & 0x80) and (cf & 0x40))

        ok("3. discovery published",
           wait_for(lambda: len(broker.discovery_configs()) >= 10, 6),
           [d["topic"] for d in broker.discovery_configs()])
        ok("4. discovery publishes are retained",
           all(d["retain"] for d in broker.discovery_configs()))
        ok("5. exactly 10 base discovery entities for the stock device",
           len([d for d in broker.discovery_configs() if f"/mttl_{SLUG_A}/" in d["topic"]]) == 10)
        dp = [json.loads(d["payload"]) for d in broker.discovery_configs()]
        ok("6. no expire_after on any sensor discovery",
           all(b"expire_after" not in d["payload"] for d in broker.discovery_configs()))
        ok("7. switch discovery optimistic=false",
           all(p.get("optimistic") is False for p in dp if "command_topic" in p))

        def _chan(uid):
            tail = uid.split("_")[-1]
            return "0" if tail == "all" else tail
        ok("8. switch command_topic is the bridge topic (not the observer)",
           all(p["command_topic"] == f"mttl/{SLUG_A}/outlet/{_chan(p['unique_id'])}/set"
               for p in dp if "command_topic" in p))
        ok("8b. stock device has NO voltage/temp/energy discovery",
           not any(k in d["topic"] for d in broker.discovery_configs()
                   for k in ("/voltage/", "/temp_", "/energy_")))

        ok("16. live device -> availability online (retained)",
           wait_for(lambda: any(x["payload"] == b"online" and x["retain"]
                                for x in broker.by_topic(f"mttl/{SLUG_A}/availability")), 6))
        ok("21. bridge/status online published (retained)",
           any(x["payload"] == b"online" and x["retain"]
               for x in broker.by_topic("mttl/bridge/status")))

        # ---- power sensors (devices.json.meter_watts is already parsed to floats) ----
        write_state(sp, {DEV_A: _entry(DEV_A, MAC_A,
                                       watts={"0": 12.99, "1": 8.61, "2": 4.37})})
        ok("13. power total + per-outlet published",
           wait_for(lambda: broker.by_topic(f"mttl/{SLUG_A}/power/total")
                    and broker.by_topic(f"mttl/{SLUG_A}/power/1"), 6))
        ok("   power total value mirrors devices.json meter_watts",
           broker.by_topic(f"mttl/{SLUG_A}/power/total")[-1]["payload"] == b"12.99",
           broker.by_topic(f"mttl/{SLUG_A}/power/total")[-1]["payload"])
        ok("14. power publishes are NOT retained",
           all(not x["retain"] for x in broker.by_topic(f"mttl/{SLUG_A}/power/total")))

        # ---- command -> observer inject ---------------------------------
        n_state_on = len([x for x in broker.by_topic(f"mttl/{SLUG_A}/outlet/1/state")
                          if x["payload"] == b"ON"])
        broker.push(f"mttl/{SLUG_A}/outlet/1/set", "ON")
        ok("9. HA command ON -> observer inject power outlet 1 on",
           wait_for(lambda: any(i.get("outlet") == 1 and i.get("on") is True
                                for i in obs.power_injects()), 5), obs.power_injects())
        ok("   single device -> no device_client_id in inject",
           all("device_client_id" not in i for i in obs.power_injects()))
        broker.push(f"mttl/{SLUG_A}/outlet/1/set", "OFF")
        ok("10. HA command OFF -> observer inject on=false",
           wait_for(lambda: any(i.get("outlet") == 1 and i.get("on") is False
                                for i in obs.power_injects()), 5))
        time.sleep(0.8)
        ok("11. command echo alone does NOT change HA switch state",
           len([x for x in broker.by_topic(f"mttl/{SLUG_A}/outlet/1/state")
                if x["payload"] == b"ON"]) == n_state_on)

        # ---- telemetry change -> HA state ------------------------------
        write_state(sp, {DEV_A: _entry(DEV_A, MAC_A,
                                       power={"1": True, "2": False, "3": False, "4": False},
                                       watts={"0": 12.99, "1": 8.61})})
        ok("12. devices.json state change -> HA outlet state ON",
           wait_for(lambda: any(x["payload"] == b"ON"
                                for x in broker.by_topic(f"mttl/{SLUG_A}/outlet/1/state")[n_state_on:]), 6))

        # ---- retained command guard -----------------------------------
        n_pi2 = len(obs.power_injects(outlet=2))
        broker.push(f"mttl/{SLUG_A}/outlet/2/set", "ON", retain=True)
        time.sleep(0.8)
        ok("15. RETAINED command is ignored (no inject)",
           len(obs.power_injects(outlet=2)) == n_pi2, obs.power_injects(outlet=2))

        # ---- 45s grace (grace=1.0s in test) --------------------------
        ok("17/18. device gone < grace -> NO offline; reconnect -> stays online",
           _grace_short(obs, broker))
        ok("19. device gone >= grace -> availability offline",
           _grace_expire(obs, broker))
        ok("20. DOWN then reconnect -> online + state republished",
           _grace_recover(obs, broker, sp))

        # ---- STATUS_GET ownership defaults --------------------------
        pre = len(obs.status_injects())
        time.sleep(1.5)
        ok("28. MTTL_HA_STATUS_INTERVAL=0 -> bridge sends 0 periodic STATUS_GET",
           len(obs.status_injects()) == pre, obs.status_injects())
        ok("30. MTTL_HA_POST_CMD_STATUS_DELAY=0 -> no post-command STATUS_GET",
           len(obs.status_injects()) == pre)

        # ---- blind retry guard --------------------------------------
        obs.injects.clear()
        broker.push(f"mttl/{SLUG_A}/outlet/3/set", "ON")   # devices.json never confirms outlet 3
        ok("31. one inject, then command times out with NO retry",
           wait_for(lambda: len(obs.power_injects(outlet=3)) == 1, 3)
           and _stable(lambda: len(obs.power_injects(outlet=3)), 2.5))

        # ---- HA broker reconnect -> republish ----------------------
        broker.published.clear()
        broker.kill_client()
        ok("26. HA broker drop -> bridge reconnects",
           wait_for(lambda: not bridge.ha.connected, 4) and wait_for(lambda: bridge.ha.connected, 8))
        ok("27. after HA reconnect -> discovery + current state republished",
           wait_for(lambda: len(broker.discovery_configs()) >= 10
                    and broker.by_topic(f"mttl/{SLUG_A}/availability"), 6))

        # ---- observer reconnect -----------------------------------
        obs.kill_client()
        ok("25. observer drop -> bridge reconnects and resumes",
           wait_for(lambda: not bridge.obs.connected, 4) and wait_for(lambda: bridge.obs.connected, 8))

        ok("bridge threads still alive", bridge._thr.is_alive())
    finally:
        bridge.stop()
        obs.stop()
        broker.stop()


def _grace_short(obs, broker):
    tp = f"mttl/{SLUG_A}/availability"
    base = len(broker.by_topic(tp))
    obs.list_devices = ([], None)
    time.sleep(0.6)                       # < grace (1.0s)
    if any(x["payload"] == b"offline" for x in broker.by_topic(tp)[base:]):
        return False
    obs.list_devices = ([DEV_A], DEV_A)
    time.sleep(0.6)
    return not any(x["payload"] == b"offline" for x in broker.by_topic(tp)[base:])


def _grace_expire(obs, broker):
    tp = f"mttl/{SLUG_A}/availability"
    base = len(broker.by_topic(tp))
    obs.list_devices = ([], None)
    got = wait_for(lambda: any(x["payload"] == b"offline" for x in broker.by_topic(tp)[base:]), 4)
    return got


def _grace_recover(obs, broker, sp):
    tp = f"mttl/{SLUG_A}/availability"
    st = f"mttl/{SLUG_A}/outlet/1/state"
    base_a = len(broker.by_topic(tp))
    base_s = len(broker.by_topic(st))
    obs.list_devices = ([DEV_A], DEV_A)
    online = wait_for(lambda: any(x["payload"] == b"online" for x in broker.by_topic(tp)[base_a:]), 4)
    repub = wait_for(lambda: len(broker.by_topic(st)) > base_s, 4)
    return online and repub


def _stable(fn, seconds):
    v = fn()
    end = time.time() + seconds
    while time.time() < end:
        if fn() != v:
            return False
        time.sleep(0.1)
    return True


def test_integration_multidevice(tmpdir):
    print("== integration: multi-device identity + targeted inject ==")
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices2.json")
    write_state(sp, {DEV_A: _entry(DEV_A, MAC_A), DEV_B: _entry(DEV_B, MAC_B)})
    obs.list_devices = ([DEV_A, DEV_B], DEV_B)

    bridge = make_bridge(obs, broker, sp)
    bridge.start()
    try:
        ok("22a. discovery for both device slugs, no collision",
           wait_for(lambda: any(f"/mttl_{SLUG_A}/" in d["topic"] for d in broker.discovery_configs())
                    and any(f"/mttl_{SLUG_B}/" in d["topic"] for d in broker.discovery_configs()), 6)
           and SLUG_A != SLUG_B)
        u_ids = {json.loads(d["payload"])["unique_id"] for d in broker.discovery_configs()}
        ok("23. unique_id deterministic + slug-based (20 unique ids, 2 stock devices x 10)",
           len(u_ids) == 20 and all(uid.startswith(("mttl_%s_" % SLUG_A, "mttl_%s_" % SLUG_B)) for uid in u_ids),
           len(u_ids))
        broker.push(f"mttl/{SLUG_B}/outlet/2/set", "ON")
        ok("22b. command to device B -> inject targets B's client_id",
           wait_for(lambda: any(i.get("outlet") == 2 and i.get("device_client_id") == DEV_B
                                for i in obs.power_injects()), 5), obs.power_injects())
    finally:
        bridge.stop()
        obs.stop()
        broker.stop()


def test_integration_status_on(tmpdir):
    print("== integration: MTTL_HA_STATUS_INTERVAL > 0 sends STATUS_GET ==")
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices3.json")
    write_state(sp, {DEV_A: _entry(DEV_A, MAC_A)})
    obs.list_devices = ([DEV_A], DEV_A)
    bridge = make_bridge(obs, broker, sp, MTTL_HA_STATUS_INTERVAL="0.4")
    bridge.start()
    try:
        ok("29. interval>0 -> bridge emits periodic STATUS_GET",
           wait_for(lambda: len(obs.status_injects()) >= 1, 5), obs.status_injects())
    finally:
        bridge.stop()
        obs.stop()
        broker.stop()


def test_integration_bad_state(tmpdir):
    print("== integration: invalid/partial devices.json -> no crash ==")
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices4.json")
    write_state(sp, {DEV_A: _entry(DEV_A, MAC_A)})
    obs.list_devices = ([DEV_A], DEV_A)
    bridge = make_bridge(obs, broker, sp)
    bridge.start()
    try:
        wait_for(lambda: bridge.ha.connected and bridge.obs.connected, 6)
        with open(sp, "w") as f:
            f.write("{ this is not json ]")
        time.sleep(1.0)
        ok("24a. bridge survives garbage devices.json", bridge._thr.is_alive())
        write_state(sp, {DEV_A: _entry(DEV_A, MAC_A, power={"1": True, "2": False, "3": False, "4": False})})
        ok("24b. bridge recovers after valid write",
           wait_for(lambda: any(x["payload"] == b"ON"
                                for x in broker.by_topic(f"mttl/{SLUG_A}/outlet/1/state")), 6))
    finally:
        bridge.stop()
        obs.stop()
        broker.stop()


# ------------------------------------------------------- FIX-1 / FIX-2 (orphan guard)

# example device identities (distinct from the DEV_A/DEV_B fixtures) — condition #5
CID3 = "ASN_CSE-D-EXAMPLE003-MTAP"
MAC3 = "020000000003"                     # unit A: MAC only via mef POST <mac>
CID4 = "ASN_CSE-D-EXAMPLE004-MTAP"
MAC4 = "020000000004"                     # unit B: also reported as device_id
HASH3 = ha_discovery.device_slug(CID3, None)   # dfb10e36a16c7
HASH4 = ha_discovery.device_slug(CID4, None)   # de1ca49e10f23


def test_fix12_sticky_slug_defer(tmpdir):
    print("== FIX-1/FIX-2: sticky MAC + defer hash-fallback discovery ==")

    # ---- pure-unit facts (no network) --------------------------------------
    ok("5. unit A current actual-MAC slug unchanged",
       ha_discovery.device_slug(CID3, MAC3) == "020000000003")
    ok("5. unit B current actual-MAC slug unchanged",
       ha_discovery.device_slug(CID4, MAC4) == "020000000004")
    ok("5. unit B device_id (bare MAC) resolves to the SAME actual-MAC slug",
       ha_discovery.device_slug(CID4, "020000000004") == "020000000004")
    base = ha_discovery.build_discovery("020000000004", mac=MAC4)
    _base_uids = {json.loads(i["payload"])["unique_id"] for i in base}
    ok("6. the 9 pre-existing entity topics/unique_ids/identifiers are UNCHANGED",
       len(base) == 10                                 # +1 = the new All switch
       and {
           f"homeassistant/switch/mttl_020000000004/outlet_{n}/config" for n in (1, 2, 3, 4)
       } | {
           f"homeassistant/sensor/mttl_020000000004/{o}/config"
           for o in ("power_total", "power_1", "power_2", "power_3", "power_4")
       } <= {i["topic"] for i in base}
       and {
           f"mttl_020000000004_{s}" for s in
           ("outlet_1", "outlet_2", "outlet_3", "outlet_4",
            "power_total", "power_1", "power_2", "power_3", "power_4")
       } <= _base_uids
       and f"mttl_020000000004_all" in _base_uids
       and all(json.loads(i["payload"])["device"]["identifiers"] == ["mttl_w01_020000000004"]
               for i in base))

    # ---- SlugMap unit: atomic, deterministic, WRITE-ONCE sticky, degrade-safe ----
    smf = os.path.join(tmpdir, "sm_unit.json")
    sm = B.SlugMap(smf)
    ok("SlugMap.learn persists a clean MAC", sm.learn(CID3, "02:00:00:00:00:03") and sm.get(CID3) == "020000000003")
    ok("SlugMap.learn ignores a non-MAC", (not sm.learn(CID4, "192.0.2.9")) and sm.get(CID4) is None)
    ok("SlugMap.learn no-op when unchanged", not sm.learn(CID3, "020000000003"))
    # WRITE-ONCE: a different clean MAC for a bound client_id is refused, binding kept
    other_mac = "AA:BB:CC:DD:EE:FF"
    ok("SlugMap.learn REFUSES a conflicting MAC (write-once)",
       sm.learn(CID3, other_mac) is False and sm.get(CID3) == "020000000003")
    ok("SlugMap.learn conflicting again -> still kept, still False",
       sm.learn(CID3, "aabbccddeeff") is False and sm.get(CID3) == "020000000003")
    ok("SlugMap file never recorded the conflicting MAC",
       json.load(open(smf)) == {"client_id_mac": {CID3: "020000000003"}})
    ok("SlugMap conflict warning is rate-limited (<=1 warn tracked per client_id)",
       len(sm._conflict_warned) == 1 and CID3 in sm._conflict_warned)
    sm2 = B.SlugMap(smf)
    ok("4. SlugMap reload restores mapping (bridge restart survives)", sm2.get(CID3) == "020000000003")
    ok("4. reloaded map is still write-once (conflict refused after reload)",
       sm2.learn(CID3, other_mac) is False and sm2.get(CID3) == "020000000003")
    B.SlugMap("/proc/nonexistent-dir/x/slugmap.json").learn(CID3, MAC3)  # must not raise

    # ---- _resolve_mac: sticky ALWAYS wins over a conflicting entry MAC --------
    obs0 = MiniServer(is_observer=True)
    broker0 = MiniServer()
    sp0 = os.path.join(tmpdir, "devices_conflict.json")
    smf0 = os.path.join(tmpdir, "devices_conflict.slugmap.json")
    with open(smf0, "w") as f:
        json.dump({"client_id_mac": {CID4: "020000000004"}}, f)   # pre-bound sticky
    write_state(sp0, {CID4: {"client_id": CID4, "mac": other_mac, "device_id": "AABBCCDDEEFF",
                             "power": {"1": False, "2": False, "3": False, "4": False},
                             "meter_watts": {}}})
    obs0.list_devices = ([CID4], CID4)
    brc = make_bridge(obs0, broker0, sp0, MTTL_HA_SLUGMAP_FILE=smf0)
    ok("_resolve_mac returns the STICKY mac, not the entry's conflicting mac",
       brc._resolve_mac(CID4, {"mac": other_mac, "device_id": "AABBCCDDEEFF"}) == "020000000004")
    ok("_resolve_mac did not rebind the sticky map",
       brc.slugmap.get(CID4) == "020000000004"
       and json.load(open(smf0)) == {"client_id_mac": {CID4: "020000000004"}})
    brc.start()
    try:
        wait_for(lambda: brc.ha.connected, 6)
        ok("conflict: discovery slug stays 020000000004 (9 configs)",
           wait_for(lambda: len([c for c in broker0.discovery_configs()
                                 if "/mttl_020000000004/" in c["topic"]]) == 10, 6),
           [c["topic"] for c in broker0.discovery_configs()])
        ok("conflict: NO aabbccddeeff slug published",
           not any("mttl_aabbccddeeff" in c["topic"] for c in broker0.discovery_configs()))
        ok("conflict: NO hash-fallback slug published",
           not any(f"mttl_{HASH4}" in c["topic"] for c in broker0.discovery_configs()))
    finally:
        brc.stop()
        obs0.stop()
        broker0.stop()

    # ---- integration: the actual orphan scenario -------------------------
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices_fix12.json")
    smf_shared = os.path.join(tmpdir, "devices_fix12.slugmap.json")  # survives the restart
    # (1) devices.json freshly recreated: unit B present via MQTT only, NO mac / device_id yet
    write_state(sp, {CID4: {"client_id": CID4,
                            "power": {"1": False, "2": False, "3": False, "4": False},
                            "meter_watts": {}}})
    obs.list_devices = ([CID4], CID4)
    bridge = make_bridge(obs, broker, sp, MTTL_HA_SLUGMAP_FILE=smf_shared)
    bridge.start()
    try:
        wait_for(lambda: bridge.ha.connected and bridge.obs.connected, 6)
        time.sleep(1.2)  # several poll cycles

        def cfgs():
            return broker.discovery_configs()

        ok("1. no MAC yet -> ZERO discovery config published",
           len(cfgs()) == 0, [c["topic"] for c in cfgs()])
        ok("1. no hash-fallback slug on the broker at all",
           not any(f"mttl_{HASH4}" in c["topic"] for c in cfgs()))
        ok("1. no retained availability for the hash slug",
           broker.by_topic(f"mttl/{HASH4}/availability") == [])

        # (2) MAC learned — via device_id on the bootstrap message
        write_state(sp, {CID4: {"client_id": CID4, "device_id": MAC4,
                                "power": {"1": False, "2": False, "3": False, "4": False},
                                "meter_watts": {"0": 0.0}}})
        ok("2. after MAC learned -> discovery for the ACTUAL-MAC slug",
           wait_for(lambda: len([c for c in cfgs() if "/mttl_020000000004/" in c["topic"]]) == 10, 6),
           [c["topic"] for c in cfgs()])
        ok("2. still NOTHING under the hash slug",
           not any(f"mttl_{HASH4}" in c["topic"] for c in cfgs()))
        ok("2. discovery identifiers use the actual-MAC slug",
           all(json.loads(c["payload"])["device"]["identifiers"] == ["mttl_w01_020000000004"]
               for c in cfgs()))
        ok("2. availability online is under the actual-MAC slug",
           wait_for(lambda: any(x["payload"] == b"online"
                                for x in broker.by_topic("mttl/020000000004/availability")), 6))

        n_cfg_after_learn = len(cfgs())

        # (3) entry goes momentarily incomplete again (mac & device_id both gone)
        write_state(sp, {CID4: {"client_id": CID4,
                                "power": {"1": True, "2": False, "3": False, "4": False},
                                "meter_watts": {}}})
        time.sleep(1.0)
        ok("3. incomplete entry -> STILL the actual-MAC slug (sticky), no hash slug",
           not any(f"mttl_{HASH4}" in c["topic"] for c in cfgs())
           and len([c for c in cfgs() if "/mttl_020000000004/" in c["topic"]]) == 10)
        ok("3. actual-MAC device stays available (not knocked offline by the gap)",
           broker.by_topic("mttl/020000000004/availability")[-1]["payload"] == b"online")
        ok("3. outlet state still tracked on the actual-MAC slug",
           wait_for(lambda: any(x["payload"] == b"ON"
                                for x in broker.by_topic("mttl/020000000004/outlet/1/state")), 6))
        ok("3. no NEW discovery churn from the gap",
           len(cfgs()) == n_cfg_after_learn)

        # (4) bridge restart: sticky mapping reloaded from disk -> immediately actual-MAC
        bridge.stop()
        broker2 = MiniServer()
        obs2 = MiniServer(is_observer=True)
        obs2.list_devices = ([CID4], CID4)
        write_state(sp, {CID4: {"client_id": CID4,  # again NO mac/device_id
                                "power": {"1": False, "2": False, "3": False, "4": False},
                                "meter_watts": {}}})
        bridge2 = make_bridge(obs2, broker2, sp, MTTL_HA_SLUGMAP_FILE=smf_shared)
        bridge2.start()
        try:
            wait_for(lambda: bridge2.ha.connected, 6)
            ok("4. after restart + MAC-less devices.json -> actual-MAC discovery from sticky map",
               wait_for(lambda: len([c for c in broker2.discovery_configs()
                                     if "/mttl_020000000004/" in c["topic"]]) == 10, 6),
               [c["topic"] for c in broker2.discovery_configs()])
            ok("4. restart never emits the hash slug",
               not any(f"mttl_{HASH4}" in c["topic"] for c in broker2.discovery_configs()))
        finally:
            bridge2.stop()
            obs2.stop()
            broker2.stop()
    finally:
        try:
            bridge.stop()
        except Exception:
            pass
        obs.stop()
        broker.stop()


# ------------------------------------------------- v/t/p/e + All switch

def _vtpe_entry(client_id, mac, *, vtpe=True, power=None, partial=False):
    """A devices.json entry as the feed rule would write it.
      vtpe=True             -> full 1.0.106 report was parsed (vtpe_seen marker set)
      partial=True          -> some v/t/p/e fields present but NO vtpe_seen marker
                               (the parser stored the individual values but never
                                saw v AND t AND p AND e valid in one report)
    """
    e = _entry(client_id, mac, power=power or {"0": True, "1": True, "2": False, "3": False, "4": False})
    if vtpe and not partial:
        e.update({
            "vtpe_seen": True,
            "voltage_v": 215.702,
            "temperature_c": {"1": 30.0, "2": 31.0, "3": 31.0, "4": 31.0},
            "meter_watts": {"0": 4.38, "1": 4.38, "2": 0.0, "3": 0.0, "4": 0.0},
            "energy_kwh": {"0": 0.002, "1": 0.002, "2": 0.0, "3": 0.0, "4": 0.0},
        })
    elif partial:
        e.update({                       # values present, marker deliberately absent
            "voltage_v": 215.702,
            "temperature_c": {"1": 30.0, "2": 31.0, "3": 31.0, "4": 31.0},
        })
    return e


def test_stage4_vtpe(tmpdir):
    print("== v/t/p/e discovery gating + All switch ==")
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices_s4.json")
    # DEV_A = stock (no vtpe), DEV_B = 1.0.106 (vtpe telemetry already seen)
    write_state(sp, {DEV_A: _entry(DEV_A, MAC_A),
                     DEV_B: _vtpe_entry(DEV_B, MAC_B)})
    obs.list_devices = ([DEV_A, DEV_B], DEV_B)
    bridge = make_bridge(obs, broker, sp)
    bridge.start()
    try:
        wait_for(lambda: bridge.ha.connected and bridge.obs.connected, 6)

        def cfgs(slug):
            return [d for d in broker.discovery_configs() if f"/mttl_{slug}/" in d["topic"]]

        # K. entity counts
        ok("K. stock device -> exactly 10 discovery entities",
           wait_for(lambda: len(cfgs(SLUG_A)) == 10, 6), [d["topic"] for d in cfgs(SLUG_A)])
        ok("K. 1.0.106 (vtpe_seen) device -> exactly 20 discovery entities",
           wait_for(lambda: len(cfgs(SLUG_B)) == 20, 6), [d["topic"] for d in cfgs(SLUG_B)])

        # E. a device with PARTIAL v/t/p/e values but NO vtpe_seen marker -> still base 10
        write_state(sp, {DEV_A: _vtpe_entry(DEV_A, MAC_A, partial=True),
                         DEV_B: _vtpe_entry(DEV_B, MAC_B)})
        time.sleep(1.2)
        ok("E. partial telemetry (no vtpe_seen marker) -> discovery stays 10, NO voltage/temp/energy",
           len({d["topic"] for d in cfgs(SLUG_A)}) == 10
           and not any(k in d["topic"] for d in cfgs(SLUG_A) for k in ("/voltage/", "/temp_", "/energy_")))
        ok("E. partial device also publishes NO voltage/temp/energy STATE",
           not broker.by_topic(f"mttl/{SLUG_A}/voltage") and not broker.by_topic(f"mttl/{SLUG_A}/temp/1"))
        # restore for the rest of the test
        write_state(sp, {DEV_A: _entry(DEV_A, MAC_A), DEV_B: _vtpe_entry(DEV_B, MAC_B)})
        time.sleep(0.6)

        # F. stock has NO v/t/e discovery ; G. vtpe device DOES
        ok("F. stock device: NO voltage/temp/energy discovery, NO empty sensors",
           not any(k in d["topic"] for d in cfgs(SLUG_A)
                   for k in ("/voltage/", "/temp_", "/energy_")))
        vt_b = {d["topic"].split("/")[-2] for d in cfgs(SLUG_B)}
        ok("G. vtpe device: voltage + temp_1..4 + energy_total + energy_1..4 discovery present",
           {"voltage", "temp_1", "temp_2", "temp_3", "temp_4",
            "energy_total", "energy_1", "energy_2", "energy_3", "energy_4"} <= vt_b)

        # H. All switch on BOTH devices
        for slug in (SLUG_A, SLUG_B):
            allc = [json.loads(d["payload"]) for d in cfgs(slug) if d["topic"].endswith("/all/config")]
            ok(f"H. All switch discovery present for {slug[:8]}",
               len(allc) == 1 and allc[0]["name"] == "All"
               and allc[0]["command_topic"] == f"mttl/{slug}/outlet/0/set"
               and allc[0]["state_topic"] == f"mttl/{slug}/outlet/0/state"
               and allc[0]["optimistic"] is False)

        # H. All state reflects normalized total (power["0"])
        ok("H. All switch state = normalized total (power[0]=True -> ON)",
           wait_for(lambda: any(x["payload"] == b"ON" and x["retain"]
                                for x in broker.by_topic(f"mttl/{SLUG_B}/outlet/0/state")), 6))

        # H. All ON/OFF -> observer inject outlet 0 (bridge side; wire POWER_SET tested in synthetic_test)
        broker.push(f"mttl/{SLUG_B}/outlet/0/set", "ON")
        ok("H. HA All ON -> observer inject {action:power, outlet:0, on:true} targeted to DEV_B",
           wait_for(lambda: any(i.get("outlet") == 0 and i.get("on") is True
                                and i.get("device_client_id") == DEV_B
                                for i in obs.power_injects()), 5), obs.power_injects())
        broker.push(f"mttl/{SLUG_B}/outlet/0/set", "OFF")
        ok("H. HA All OFF -> observer inject outlet 0 on:false",
           wait_for(lambda: any(i.get("outlet") == 0 and i.get("on") is False
                                for i in obs.power_injects()), 5))

        # I. existing per-outlet switches unchanged
        broker.push(f"mttl/{SLUG_B}/outlet/2/set", "ON")
        ok("I. per-outlet switch 2 still injects outlet 2 (unchanged)",
           wait_for(lambda: any(i.get("outlet") == 2 and i.get("on") is True
                                for i in obs.power_injects()), 5))

        # G. vtpe values published
        ok("G. voltage state published (215.7)",
           wait_for(lambda: any(x["payload"] == b"215.7"
                                for x in broker.by_topic(f"mttl/{SLUG_B}/voltage")), 6))
        # temperature_c came through devices.json -> keys are the strings "1".."4"
        ok("G. Internal temperature 1..4 published as 30.0/31.0/31.0/31.0, RETAINED",
           all(wait_for(lambda n=n, v=v: any(x["payload"] == v.encode() and x["retain"]
                                             for x in broker.by_topic(f"mttl/{SLUG_B}/temp/{n}")), 6)
               for n, v in ((1, "30.0"), (2, "31.0"), (3, "31.0"), (4, "31.0"))))
        # Discovery state_topic MUST equal the topic the bridge actually publishes to
        _tdisc = {json.loads(d["payload"])["unique_id"]: json.loads(d["payload"])["state_topic"]
                  for d in cfgs(SLUG_B) if d["topic"].endswith(tuple(f"/temp_{n}/config" for n in (1, 2, 3, 4)))}
        _tpub = {t for t in broker.topics() if f"mttl/{SLUG_B}/temp/" in t}
        ok("G. temp discovery state_topic == bridge publish topic (1:1)",
           set(_tdisc.values()) == {f"mttl/{SLUG_B}/temp/{n}" for n in (1, 2, 3, 4)}
           and _tpub == set(_tdisc.values()))
        ok("G. energy total published (0.002 kWh, retained)",
           wait_for(lambda: any(x["payload"] == b"0.002" and x["retain"]
                                for x in broker.by_topic(f"mttl/{SLUG_B}/energy/total")), 6))
        ok("G. stock device publishes NO voltage/temp/energy state",
           not broker.by_topic(f"mttl/{SLUG_A}/voltage")
           and not broker.by_topic(f"mttl/{SLUG_A}/temp/1")
           and not broker.by_topic(f"mttl/{SLUG_A}/energy/total"))

        # J. sticky slug intact, no hash fallback anywhere
        ok("J. no hash-fallback slug on the broker (sticky/orphan guard preserved)",
           not any(("/mttl_d" + "9d785af473dc/" in d["topic"]) or ("/mttl_dfb10e36a16c7/" in d["topic"])
                   for d in broker.discovery_configs())
           and SLUG_A == "020000000003")

        # G(transition). a stock device that STARTS sending vtpe -> discovery grows to 20 UNIQUE topics
        # (MiniServer keeps every retained republish; HA/mosquitto would dedupe by topic).
        write_state(sp, {DEV_A: _vtpe_entry(DEV_A, MAC_A),
                         DEV_B: _vtpe_entry(DEV_B, MAC_B)})
        ok("G(transition). stock->vtpe: discovery grows to 20 unique topics, power sensors NOT duplicated",
           wait_for(lambda: len({d["topic"] for d in cfgs(SLUG_A)}) == 20, 6)
           and len({d["topic"] for d in cfgs(SLUG_A) if "power_" in d["topic"]}) == 5)
        _base10 = {f"mttl_{SLUG_A}_{o}" for o in
                   ("outlet_1", "outlet_2", "outlet_3", "outlet_4", "all",
                    "power_total", "power_1", "power_2", "power_3", "power_4")}
        ok("G(transition). base 10 unique_ids preserved after growth to 20 (no rename/removal)",
           _base10 <= {json.loads(d["payload"])["unique_id"] for d in cfgs(SLUG_A)})
    finally:
        bridge.stop()
        obs.stop()
        broker.stop()


def test_all_switch_oneshot_status(tmpdir):
    print("== All-switch one-shot STATUS_GET (outlet 0 only) ==")
    obs = MiniServer(is_observer=True)
    broker = MiniServer()
    sp = os.path.join(tmpdir, "devices_all1s.json")
    write_state(sp, {DEV_A: _entry(DEV_A, MAC_A), DEV_B: _entry(DEV_B, MAC_B)})
    obs.list_devices = ([DEV_A, DEV_B], DEV_B)
    # short one-shot delay + long confirm window so consecutive commands don't
    # race the deadline; periodic STATUS_GET stays OFF (default 0).
    bridge = make_bridge(obs, broker, sp,
                         MTTL_HA_ALL_CMD_STATUS_DELAY="0.4", MTTL_HA_CONFIRM_TIMEOUT="5.0")
    bridge.start()
    try:
        wait_for(lambda: bridge.ha.connected and bridge.obs.connected, 6)
        time.sleep(0.4)

        def cmd(chan, val):
            n = len(obs.status_injects())
            broker.push(f"mttl/{SLUG_B}/outlet/{chan}/set", val)
            time.sleep(1.3)
            return obs.status_injects()[n:]

        s_all_on = cmd("0", "ON")
        ok("All ON -> POWER_SET FF wire unchanged",
           any(i.get("action") == "power" and i.get("outlet") == 0 and i.get("on") is True
               for i in obs.injects))
        ok("All ON -> EXACTLY 1 targeted STATUS_GET (to DEV_B)",
           len(s_all_on) == 1 and s_all_on[0].get("device_client_id") == DEV_B, s_all_on)

        s_ind = cmd("2", "ON")
        ok("individual Outlet 2 command -> 0 STATUS_GET (unchanged)", len(s_ind) == 0, s_ind)

        s_all_off = cmd("0", "OFF")
        ok("All OFF -> POWER_SET 00 wire + EXACTLY 1 targeted STATUS_GET",
           any(i.get("action") == "power" and i.get("outlet") == 0 and i.get("on") is False for i in obs.injects)
           and len(s_all_off) == 1 and s_all_off[0].get("device_client_id") == DEV_B, s_all_off)

        s_again = cmd("0", "ON")
        ok("All command repeated -> still EXACTLY 1 (never 2 for the same command)",
           len(s_again) == 1)

        # spontaneous device event already reflects power[0] BEFORE the one-shot delay
        n = len(obs.status_injects())
        broker.push(f"mttl/{SLUG_B}/outlet/0/set", "OFF")
        time.sleep(0.1)
        write_state(sp, {DEV_A: _entry(DEV_A, MAC_A),
                         DEV_B: _entry(DEV_B, MAC_B,
                                       power={"0": False, "1": False, "2": False, "3": False, "4": False})})
        time.sleep(1.3)
        got = obs.status_injects()[n:]
        ok("All + fast spontaneous power[0] -> one-shot still fires exactly once (per-outlet sync)",
           len(got) == 1, got)

        ok("periodic reconciliation unchanged: status_interval == 0, no periodic STATUS_GET timer change",
           bridge.cfg.status_interval == 0.0)
        ok("individual-outlet post-cmd delay knob unchanged (still 0 = off)",
           bridge.cfg.post_cmd_status_delay == 0.0)
    finally:
        bridge.stop()
        obs.stop()
        broker.stop()


# --------------------------------------------------------------------------- main

def main():
    import tempfile
    wd = tempfile.mkdtemp(prefix="mttl-ha-")
    print(f"python {sys.version.split()[0]}   workdir {wd}")
    test_unit_slug()
    test_unit_clean_mac_strict()
    test_unit_discovery()
    test_unit_availability_machine()
    test_unit_state_reader(wd)
    test_integration(wd)
    test_integration_multidevice(wd)
    test_integration_status_on(wd)
    test_integration_bad_state(wd)
    test_fix12_sticky_slug_defer(wd)
    test_stage4_vtpe(wd)
    test_all_switch_oneshot_status(wd)

    print()
    print("=" * 62)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}")
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
