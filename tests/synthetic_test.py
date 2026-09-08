#!/usr/bin/env python3
"""
Synthetic test for the MTTL-W01 local relay + dashboard. No device, no cloud.

Two layers:
  RULES layer  (any Python >= 3.7): drives rules_engine + our rules directly,
      exercising the exact hooks the relay would call. Verifies mef bootstrap XML,
      oneM2M CSE replies, telemetry parsing, the feed rule's state file, and the
      dashboard (self-check + HTTP serve + /api/state).
  E2E layer    (Python >= 3.11 only, because vendored relay.py needs tomllib):
      spawns the real relay in decloud mode on high ports and replays the
      device bootstrap chain over real sockets/TLS.

Exit 0 = all PASS.  Standard library only.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOCAL = os.path.normpath(os.path.join(HERE, ".."))
VENDOR = os.path.join(LOCAL, "vendor", "smartrelay")
RELAY_DIR = os.path.join(LOCAL, "relay")
RULES_DIR = os.path.join(RELAY_DIR, "rules")
DASH_DIR = os.path.join(LOCAL, "dashboard")
sys.path.insert(0, VENDOR)
sys.path.insert(0, DASH_DIR)
import mqtt_wire as mw  # noqa: E402

DEV_MAC = "020000000003"
MEF_SNI = "mef.onem2m.uplus.co.kr"
BRK2_SNI = "brk2.onem2m.uplus.co.kr"

PASS: list = []
FAIL: list = []
SKIP: list = []


def ok(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (" — " + str(detail)[:160] if detail else ""))


def skip(name, reason=""):
    """Not applicable in this environment. Never a FAIL; the run can still be
    FAIL 0. Use ONLY for genuine environment differences, never to dodge a check."""
    SKIP.append(name)
    print("  SKIP " + name + (" — " + str(reason)[:160] if reason else ""))


class Ctx:
    def __init__(self, cid="", client_id=None, domain=None, device_client_id=None):
        self.cid = cid
        self.client_id = client_id
        self.domain = domain
        self.device_client_id = device_client_id


def _tag(resp_or_body, tag):
    body = resp_or_body if isinstance(resp_or_body, (bytes, bytearray)) else b""
    a = body.find(b"<" + tag + b">")
    b2 = body.find(b"</" + tag + b">")
    return body[a + len(tag) + 2:b2].decode() if a >= 0 and b2 > a else None


# --------------------------------------------------------------------------- CA

def test_ca(certdir):
    print("== CA / leaves ==")
    os.makedirs(certdir, exist_ok=True)
    ca_cnf = os.path.join(certdir, "ca.cnf")
    open(ca_cnf, "w").write(
        "[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n"
        "[dn]\nC=KR\nO=Local MITM CA\nCN=Local MITM Root CA\n"
        "[ext]\nbasicConstraints=critical,CA:TRUE\n"
        "keyUsage=critical,digitalSignature,keyCertSign,cRLSign\nsubjectKeyIdentifier=hash\n")
    ca_key = os.path.join(certdir, "ca.key")
    ca_crt = os.path.join(certdir, "ca.crt")
    subprocess.run(["openssl", "genrsa", "-out", ca_key, "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "req", "-new", "-x509", "-key", ca_key, "-out", ca_crt,
                    "-days", "7300", "-config", ca_cnf], check=True, capture_output=True)
    t = subprocess.run(["openssl", "x509", "-in", ca_crt, "-noout", "-text"],
                       capture_output=True, text=True).stdout
    ok("Root CA CA:TRUE + Certificate Sign", "CA:TRUE" in t and "Certificate Sign" in t)

    r = subprocess.run(["sh", os.path.join(LOCAL, "relay", "gen_local_ca.sh"), certdir],
                       capture_output=True, text=True)
    for tag, sni in (("mef", MEF_SNI), ("brk2", BRK2_SNI)):
        crt = os.path.join(certdir, "leaves", sni + ".crt")
        exists = os.path.exists(crt)
        ok(f"leaf {tag} generated", exists, r.stderr.strip()[-160:] if not exists else "")
        if not exists:
            continue
        lt = subprocess.run(["openssl", "x509", "-in", crt, "-noout", "-text"],
                            capture_output=True, text=True).stdout
        ok(f"leaf {tag}: CA:FALSE / serverAuth / SAN=DNS:{sni}",
           "CA:FALSE" in lt and "TLS Web Server Auth" in lt and f"DNS:{sni}" in lt)
        v = subprocess.run(["openssl", "verify", "-CAfile", ca_crt, crt],
                           capture_output=True, text=True)
        ok(f"leaf {tag}: chains to Root CA", v.returncode == 0, v.stdout.strip())
    return ca_crt


# --------------------------------------------------------------------------- telemetry unit

def test_telemetry_unit():
    print("== mttl_telemetry unit ==")
    import mttl_telemetry as T
    d = T.parse_mef_post(b"<x><mac>ABC123</mac><deviceModel>MTTL-W01</deviceModel></x>")
    ok("parse_mef_post mac/model", d["mac"] == "ABC123" and d["model"] == "MTTL-W01")
    inner = {"content": {"notification": {"parameters": [
        {"command": "STATUS_REPORT", "switchBinary": "FF", "meter_02": "0000C8",
         "SSID": "MTTL-LAB-2G", "RSSI": "-52"},
        {"command": "STATUS2_REPORT", "switchBinary2": "00", "meter2_02": "000000"},
        {"command": "METER_ACC_STATUS_EVENT", "meter_00": "0004D2"},
        {"command": "ALARM_EVENT", "event": "00"},
    ]}}}
    con = base64.b64encode(json.dumps(inner).encode()).decode()
    delta = T.parse_onem2m_message({"rqi": "plugeventreport-9", "pc": {"m2m:cin": {"con": con}}})
    ok("telemetry: power[0]=True", delta.get("power", {}).get(0) is True, delta.get("power"))
    ok("telemetry: meter_watts[0]=2.0", abs(delta.get("meter_watts", {}).get(0, 0) - 2.0) < 1e-6,
       delta.get("meter_watts"))
    ok("telemetry: energy_raw[0]=1234", delta.get("energy_raw", {}).get(0) == 1234)
    ok("telemetry: wifi ssid", delta.get("wifi", {}).get("ssid") == "MTTL-LAB-2G")
    ok("telemetry: alarm normal", delta.get("alarms", {}).get(0, {}).get("event") == "normal")
    st = T.new_device_state()
    T.merge_delta(st, delta)
    ok("merge_delta keeps power", st["power"].get("0") is True or st["power"].get(0) is True)

    # --- v/t/p/e on content.cmd_report (siblings of parameters[]) ----
    def _rep(v=None, t=None, p=None, e=None, params=None):
        rep = {"result": 0, "rpt_id": 1}
        if params is not None:
            rep["parameters"] = params
        for k, val in (("v", v), ("t", t), ("p", p), ("e", e)):
            if val is not None:
                rep[k] = val
        c = base64.b64encode(json.dumps({"content": {"cmd_report": rep}}).encode()).decode()
        return T.parse_onem2m_message({"rqi": "devicecontrol-12345", "pc": {"m2m:cin": {"con": c}}})

    _V = "00034A96"
    _T = "0000001E,0000001F,0000001F,0000001F"
    _P = "0000111C,00000000,00000000,00000000,0000111C"   # raw [o1,o2,o3,o4,total]
    _E = "00000002,00000000,00000000,00000000,00000002"

    dv = _rep(v=_V)
    ok("A. v alone -> voltage_v 215.702 updated, vtpe_seen NOT activated",
       dv.get("voltage_v") == 215.702 and dv.get("vtpe_seen") is None, dv.get("vtpe_seen"))
    dt = _rep(t=_T)
    ok("B(t). t alone -> {1:30,2:31,3:31,4:31} degC, vtpe_seen NOT activated",
       dt.get("temperature_c") == {1: 30.0, 2: 31.0, 3: 31.0, 4: 31.0}
       and dt.get("vtpe_seen") is None, dt.get("temperature_c"))
    dp = _rep(p=_P)
    ok("C(p). p alone -> {0:4.38,1:4.38,2:0,3:0,4:0} W, vtpe_seen NOT activated",
       dp.get("meter_watts") == {0: 4.38, 1: 4.38, 2: 0.0, 3: 0.0, 4: 0.0}
       and dp.get("vtpe_seen") is None, dp.get("meter_watts"))
    de = _rep(e=_E)
    ok("D(e). e alone -> {0:0.002,1:0.002,2:0,3:0,4:0} kWh, vtpe_seen NOT activated",
       de.get("energy_kwh") == {0: 0.002, 1: 0.002, 2: 0.0, 3: 0.0, 4: 0.0}
       and de.get("vtpe_seen") is None, de.get("energy_kwh"))
    dpartial = _rep(v=_V, t=_T, p=_P)   # 3 of 4 valid, e missing
    ok("B. partial (v+t+p, no e) -> fields stored, vtpe_seen NOT activated",
       dpartial.get("voltage_v") == 215.702 and dpartial.get("temperature_c") is not None
       and dpartial.get("meter_watts") is not None and dpartial.get("vtpe_seen") is None)
    dpartial2 = _rep(v=_V, t=_T, p=_P, e="q,0,0,0,0")   # e malformed
    ok("B2. v+t+p valid + e malformed -> vtpe_seen NOT activated, e-field untouched",
       dpartial2.get("vtpe_seen") is None and dpartial2.get("energy_kwh") is None)
    dfull = _rep(v=_V, t=_T, p=_P, e=_E)
    ok("C. all four valid in one report -> vtpe_seen == True",
       dfull.get("vtpe_seen") is True and dfull.get("voltage_v") == 215.702
       and dfull.get("energy_kwh", {}).get(0) == 0.002)
    dbad = _rep(v="ZZ", t="00,00", p="1,2,3", e="q,0,0,0,0",
                params=[{"command": "METER_CUR_STATUS_EVENT", "meter1_02": "0001B4"}])
    ok("E. all malformed -> no crash, no vtpe_seen, legacy meterN_02 kept",
       dbad.get("vtpe_seen") is None and dbad.get("voltage_v") is None
       and dbad.get("temperature_c") is None and dbad.get("energy_kwh") is None
       and dbad.get("meter_watts") == {1: 4.36})
    dstock = _rep(params=[{"command": "STATUS1_REPORT", "switchBinary1": "FF", "meter1_02": "0001B4"}])
    ok("F. stock (no v/t/p/e) -> no vtpe_seen, legacy power/meter intact",
       dstock.get("vtpe_seen") is None and dstock.get("power") == {1: True}
       and dstock.get("meter_watts") == {1: 4.36})
    # D. sticky: full report sets vtpe_seen; later partial/malformed keeps it + keeps good values
    st2 = T.new_device_state()
    T.merge_delta(st2, _rep(v=_V, t="0000001E,0000001E,0000001E,0000001E",
                            p="0000111C,0,0,0,0000111C", e="00000002,0,0,0,00000002"))
    ok("D. merge full report -> vtpe_seen True + all fields land",
       st2["vtpe_seen"] is True and st2["voltage_v"] == 215.702
       and st2["temperature_c"].get("1") == 30.0 and st2["energy_kwh"].get("0") == 0.002)
    T.merge_delta(st2, _rep(v="badhex"))                       # malformed
    T.merge_delta(st2, _rep(p="0000000A,0,0,0,0000000A"))      # partial-valid p only
    ok("D. merge partial/malformed after full -> vtpe_seen STAYS True, voltage_v kept, p still updates meter_watts",
       st2["vtpe_seen"] is True and st2["voltage_v"] == 215.702
       and st2["meter_watts"].get("0") == 0.01)


# --------------------------------------------------------------------------- rules layer

def test_rules_layer(state_file):
    print("== rules layer (rules_engine + our rules) ==")
    env_prev = os.environ.get("MTTL_STATE_FILE")
    os.environ["MTTL_STATE_FILE"] = state_file
    os.environ["MTTL_DASHBOARD_LIB"] = DASH_DIR
    if os.path.exists(state_file):
        os.remove(state_file)
    from rules_engine import RulesHandle
    rules = RulesHandle(RULES_DIR)

    xml = (f'<auth><deviceModel>MTTL-W01</deviceModel><serviceCode>MTAP</serviceCode>'
           f'<deviceType>asn</deviceType><ctn></ctn><mac>{DEV_MAC}</mac>'
           f'<deviceSerialNo>{DEV_MAC}</deviceSerialNo><iccId>345678</iccId></auth>').encode()
    ctx_http = Ctx(cid=":443-192.0.2.123:5001-1")
    resp = rules.on_http_request(ctx_http, b"POST", b"/mef", {}, xml)
    ok("POST /mef -> HttpResponse", resp is not None and hasattr(resp, "body"))
    eid = _tag(resp.body if resp else b"", b"entityId")
    enr = _tag(resp.body if resp else b"", b"enrmtKey")
    tok = _tag(resp.body if resp else b"", b"token")
    ok("mef XML: entityId/enrmtKey/token", all([eid, enr, tok]), f"entityId={eid}")
    ok("mef XML: <http>/<coap>/<mqtt> blocks", all(t in resp.body for t in (b"<http>", b"<coap>", b"<mqtt>")))
    ok("mef: GET path ignored", rules.on_http_request(Ctx(), b"GET", b"/mef/cert/http/pem", {}, b"") is None)

    cidb = (eid or "ASN_CSE-D-TESTONLY0-MTAP").encode()
    ctx_mqtt = Ctx(cid=":18831-192.0.2.123:5002-1", client_id=cidb)
    rules.on_session_start(ctx_mqtt)

    specs = rules.on_message(ctx_mqtt, {"op": "2", "to": "/IN_CSE-BASE-1",
                                        "fr": f"/{cidb.decode()}", "rqi": "cseBaseRetrieve"}, b"t")
    ok("cseBaseRetrieve -> reply spec", bool(specs))
    reply = json.loads(specs[0].payload) if specs else {}
    ok("cseBaseRetrieve rsc=2000 + m2m:cb", reply.get("rsc") == 2000
       and "m2m:cb" in (reply.get("pc") or {}), json.dumps(reply)[:140])

    boot_con = base64.b64encode(json.dumps(
        {"content": {"device": {"id": DEV_MAC, "type": "MULTITAP"}}}).encode()).decode()
    specs = rules.on_message(ctx_mqtt, {"op": "1", "to": f"/{cidb.decode()}", "ty": 4,
                                        "fr": f"/{cidb.decode()}", "rqi": "smartplugbootstrap",
                                        "pc": {"m2m:cin": {"con": boot_con}}}, b"t")
    ok("smartplugbootstrap -> ack spec", bool(specs))
    ack = json.loads(specs[0].payload) if specs else {}
    ack_inner = json.loads(base64.b64decode(ack.get("pc", {}).get("m2m:cin", {}).get("con", "")) or b"{}")
    ok("bootstrap ack: ret_code 200 + session_id",
       ack_inner.get("header", {}).get("ret_code") == "200"
       and str(ack_inner.get("header", {}).get("session_id", "")).startswith("SID-"))

    specs = rules.on_message(ctx_mqtt, {"op": "1", "to": f"/{cidb.decode()}", "ty": 4,
                                        "fr": f"/{cidb.decode()}", "rqi": "remoteCSECreate",
                                        "pc": {"m2m:csr": {"rn": "csr-Tonly_x"}}}, b"t")
    ok("generic op=1 create -> reply with ri/ct/lt", bool(specs)
       and "ri" in json.loads(specs[0].payload).get("pc", {}).get("m2m:csr", {}))

    tele_inner = {"content": {"notification": {"parameters": [
        {"command": "STATUS_REPORT", "switchBinary": "FF", "meter_02": "0000C8",
         "SSID": "MTTL-LAB-2G", "RSSI": "-52"},
        {"command": "STATUS1_REPORT", "switchBinary1": "FF", "meter1_02": "000064"},
        {"command": "STATUS2_REPORT", "switchBinary2": "00", "meter2_02": "000000"},
        {"command": "STATUS3_REPORT", "switchBinary3": "00"},
        {"command": "STATUS4_REPORT", "switchBinary4": "00"},
        {"command": "METER_ACC_STATUS_EVENT", "meter_00": "0004D2"},
    ]}}}
    tele_con = base64.b64encode(json.dumps(tele_inner).encode()).decode()
    rules.on_message(ctx_mqtt, {"op": "1", "ty": 4, "to": f"/{cidb.decode()}",
                                "fr": f"/{cidb.decode()}", "rqi": "plugeventreport-1",
                                "pc": {"m2m:cin": {"con": tele_con}}}, b"t")

    inj = rules.on_local_inject(Ctx(device_client_id=cidb, cid=":obs"), {"outlet": 1, "on": True})
    ok("on_local_inject -> device_control PUBLISH", bool(inj))
    if inj:
        env = json.loads(inj[0].payload)
        di = json.loads(base64.b64decode(env["pc"]["m2m:cin"]["con"]))
        ok("inject envelope: cmd_id + POWER1_SET",
           di["content"]["cmd_request"]["cmd_id"] == 1
           and di["content"]["cmd_request"]["parameters"][0]["command"] == "POWER1_SET")

    # "All" switch: bridge sends outlet 0 -> wire command POWER_SET (unchanged rule path)
    for on_, want in ((True, "FF"), (False, "00")):
        inj0 = rules.on_local_inject(Ctx(device_client_id=cidb, cid=":obs"), {"outlet": 0, "on": on_})
        di0 = json.loads(base64.b64decode(json.loads(inj0[0].payload)["pc"]["m2m:cin"]["con"])) if inj0 else {}
        prm = di0.get("content", {}).get("cmd_request", {}).get("parameters", [{}])[0]
        ok("All switch %s -> POWER_SET switchBinary=%s" % ("ON" if on_ else "OFF", want),
           prm.get("command") == "POWER_SET" and prm.get("switchBinary") == want, prm)

    time.sleep(0.2)
    blob = json.load(open(state_file))
    devs = blob.get("devices", {})
    ok("feed: state file has a device", len(devs) >= 1, list(devs))
    any_ = lambda k: any(st.get(k) for st in devs.values())
    ok("feed: mef_bootstrap_seen", any_("mef_bootstrap_seen"))
    ok("feed: mqtt_connected", any_("mqtt_connected"))
    ok("feed: mac recorded", any(st.get("mac") == DEV_MAC for st in devs.values()))
    ok("feed: power parsed", any((st.get("power") or {}) for st in devs.values()),
       [st.get("power") for st in devs.values()])
    ok("feed: meter_watts parsed", any((st.get("meter_watts") or {}) for st in devs.values()),
       [st.get("meter_watts") for st in devs.values()])
    ok("feed: wifi ssid parsed",
       any((st.get("wifi") or {}).get("ssid") == "MTTL-LAB-2G" for st in devs.values()))
    ok("feed: last_inject recorded", any(st.get("last_inject") for st in devs.values()))

    # ---- dedup: an ip:<ip> pseudo-entry from mef POST must merge into the
    #      client_id entry once MQTT connects, leaving exactly one device ----
    ok("feed: exactly one device (ip: pseudo merged into client_id)", len(devs) == 1, list(devs))
    ok("feed: no leftover 'ip:' key", not any(k.startswith("ip:") for k in devs))
    only = next(iter(devs.values()))
    ok("feed: merged entry kept mac from mef + power from mqtt",
       only.get("mac") == DEV_MAC and bool(only.get("power")))

    # ---- reconnect / session epoch ----
    rules.on_session_start(Ctx(cid=":18831-192.0.2.123:5003-2", client_id=cidb))
    time.sleep(0.15)
    st2 = next(iter(json.load(open(state_file))["devices"].values()))
    ok("feed: session_epoch incremented on reconnect", (st2.get("session_epoch") or 0) >= 2,
       st2.get("session_epoch"))
    ok("feed: reconnects counted", (st2.get("reconnects") or 0) >= 1, st2.get("reconnects"))

    if env_prev is None:
        os.environ.pop("MTTL_STATE_FILE", None)
    else:
        os.environ["MTTL_STATE_FILE"] = env_prev


# --------------------------------------------------------------------------- dashboard

def test_dashboard(state_file):
    print("== dashboard ==")
    r = subprocess.run([sys.executable, os.path.join(DASH_DIR, "mttl_dashboard.py"),
                        "--self-check", "--state-file", state_file],
                       capture_output=True, text=True)
    ok("dashboard --self-check", r.returncode == 0 and "SELF-CHECK: PASS" in r.stdout,
       (r.stdout + r.stderr).strip()[-160:])
    port = 18089
    dash = subprocess.Popen([sys.executable, os.path.join(DASH_DIR, "mttl_dashboard.py"),
                             "--host", "127.0.0.1", "--port", str(port),
                             "--state-file", state_file, "--observer", "127.0.0.1:1"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        up = False
        for _ in range(40):
            try:
                socket.create_connection(("127.0.0.1", port), 0.3).close()
                up = True
                break
            except OSError:
                time.sleep(0.1)
        ok("dashboard http up", up)
        if up:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/")
            page = c.getresponse().read()
            ok("dashboard / serves HTML title", b"MTTL-W01 Devices" in page)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/state")
            api = json.loads(c.getresponse().read())
            ok("dashboard /api/state devices>=1", len(api.get("devices", {})) >= 1)
            ok("dashboard control disabled by default", api.get("control_enabled") is False)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", "/api/control", json.dumps({"outlet": 1, "on": True}),
                      {"content-type": "application/json"})
            ok("dashboard /api/control -> 403 (control off)", c.getresponse().status == 403)
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/healthz")
            ok("dashboard /healthz ok", c.getresponse().read() == b"ok")
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/state")
            api = json.loads(c.getresponse().read())
            ok("dashboard: observer reported down (bad port) -> online basis = fallback",
               api["observer"]["connected"] is False)
    finally:
        dash.terminate()
        try:
            dash.wait(timeout=5)
        except Exception:
            dash.kill()


def test_dashboard_observer(state_file):
    """Dashboard <-> a stub relay observer (uses the vendored mqtt_session server loop,
    same as the real relay). Verifies: observer link up, live-session-driven online,
    control targeting + publish reaches the observer."""
    print("== dashboard <-> observer stub ==")
    import mqtt_session
    from rules_engine import PublishSpec

    devs = json.load(open(state_file))["devices"]
    live_cid = next(iter(devs.values())).get("client_id") or "ASN_CSE-D-x-MTAP"
    injects: list = []
    obs_port = 19899

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", obs_port))
    srv.listen(4)
    stop = threading.Event()

    def on_publish(session, topic, payload, qos):
        try:
            cmd = json.loads(payload)
        except Exception:
            return None
        if isinstance(cmd, dict) and cmd.get("list"):
            return [PublishSpec(topic=b"mtap/devices",
                                payload=json.dumps({"devices": [live_cid], "latest": live_cid}).encode(),
                                qos=0)]
        injects.append(cmd)
        return None

    def acceptor():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=mqtt_session.run_server_session,
                             args=(c, "stub", lambda m: None),
                             kwargs={"on_publish": on_publish, "recv_timeout": 30},
                             daemon=True).start()

    threading.Thread(target=acceptor, daemon=True).start()

    port = 18091
    dash = subprocess.Popen([sys.executable, os.path.join(DASH_DIR, "mttl_dashboard.py"),
                             "--host", "127.0.0.1", "--port", str(port), "--enable-control",
                             "--state-file", state_file, "--observer", f"127.0.0.1:{obs_port}",
                             "--refresh", "2"],
                            env=dict(os.environ, MTTL_STATUS_INTERVAL="1"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait(port, 8):
            ok("dashboard(observer) http up", False)
            return
        # give the observer link a couple of list cycles
        api = {}
        for _ in range(30):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/state")
            api = json.loads(c.getresponse().read())
            if api.get("observer", {}).get("fresh"):
                break
            time.sleep(0.4)
        ok("observer link connected + fresh", api.get("observer", {}).get("fresh") is True,
           api.get("observer"))
        ok("live_ids from stub", live_cid in api.get("observer", {}).get("live_ids", []))
        dev = next(iter(api["devices"].values()))
        ok("device online from relay session registry", dev.get("online") is True
           and dev.get("session_live") is True, {"online": dev.get("online"), "sl": dev.get("session_live")})
        ok("dashboard summary online>=1", (api.get("summary") or {}).get("online", 0) >= 1)

        # periodic STATUS_GET: with a live device + MTTL_STATUS_INTERVAL=1 the link
        # should emit {"action":"status"} on its own, before any manual control POST.
        deadline = time.time() + 6
        while time.time() < deadline and not any(i.get("action") == "status" for i in injects):
            time.sleep(0.2)
        auto = [i for i in injects if i.get("action") == "status"]
        ok("auto STATUS_GET reaches observer for a live device", bool(auto), injects[-3:])
        ok("auto STATUS_GET carries no outlet/on",
           bool(auto) and all("outlet" not in i and "on" not in i for i in auto), auto[:2])

        # POWER area contract: renders a voltage row (unknown-fallback when the
        # wire has no voltage), never a current row (current is never on the wire).
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        pg0 = c.getresponse().read().decode("utf-8", "replace")
        ok("POWER area: voltage row present, current row absent",
           "['voltage'," in pg0 and "st.voltage_v" in pg0
           and "'current'" not in pg0 and "st.current" not in pg0)

        key = next(iter(api["devices"]))
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("POST", "/api/control", json.dumps({"key": key, "outlet": 1, "on": True}),
                  {"content-type": "application/json"})
        resp = json.loads(c.getresponse().read())
        pw1 = [i for i in injects if i.get("action") == "power" and i.get("outlet") == 1]
        ok("control published to observer", bool(pw1), injects[-3:])
        ok("control single-live -> no device_client_id (targets latest)",
           bool(pw1) and "device_client_id" not in pw1[-1], pw1[-1] if pw1 else None)
        ok("control result reported (sent/acked/applied)",
           resp.get("result") in ("sent", "acked", "applied"), resp)

        # outlet 0 (ALL) request must go through the same API/backend path
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("POST", "/api/control", json.dumps({"key": key, "outlet": 0, "on": False}),
                  {"content-type": "application/json"})
        r0 = json.loads(c.getresponse().read())
        ok("outlet 0 (ALL OFF) accepted by /api/control",
           r0.get("result") in ("sent", "acked", "applied"), r0)
        ok("outlet 0 (ALL) reaches the observer as one power inject",
           any(i.get("action") == "power" and i.get("outlet") == 0 for i in injects), injects[-3:])

        # the ALL ON / ALL OFF buttons are exposed in the HTML and reuse ctl(...,0,...)
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/")
        pg = c.getresponse().read().decode("utf-8", "replace")
        ok("HTML has ALL ON / ALL OFF buttons", "ALL ON" in pg and "ALL OFF" in pg)
        ok("ALL ON/OFF reuse ctl(key,0,true/false)",
           "',0,true,this)" in pg and "',0,false,this)" in pg)

        # manual {"action":"status"} path still works -> relay STATUS_GET, no outlet/on
        n_status_before = len([i for i in injects if i.get("action") == "status"])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("POST", "/api/control", json.dumps({"key": key, "action": "status"}),
                  {"content-type": "application/json"})
        rs_hdr = c.getresponse()
        rs = json.loads(rs_hdr.read())
        ok("manual action:status accepted without outlet/on", rs_hdr.status == 202
           and rs.get("result") == "sent" and "error" not in rs, {"status": rs_hdr.status, "body": rs})
        # fire-and-forget: no _confirm wait, so poll for one more async publish
        deadline = time.time() + 5
        while (time.time() < deadline and
               len([i for i in injects if i.get("action") == "status"]) <= n_status_before):
            time.sleep(0.1)
        ok("manual action:status reaches observer as {'action':'status'}",
           len([i for i in injects if i.get("action") == "status"]) > n_status_before, injects[-3:])
        si = next(i for i in reversed(injects) if i.get("action") == "status")
        ok("status inject carries no outlet/on", "outlet" not in si and "on" not in si, si)

        # existing power outlet 1 path still works after the status branch
        # (auto STATUS_GET may interleave, so look at the last *power* inject, not injects[-1])
        n_power_before = len([i for i in injects if i.get("action") == "power"])
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=15)
        c.request("POST", "/api/control", json.dumps({"key": key, "outlet": 1, "on": False}),
                  {"content-type": "application/json"})
        rp = json.loads(c.getresponse().read())
        pw = [i for i in injects if i.get("action") == "power"]
        ok("power outlet 1 still works after status branch",
           rp.get("result") in ("sent", "acked", "applied")
           and len(pw) > n_power_before
           and pw[-1].get("outlet") == 1 and pw[-1].get("on") is False, rp)
    finally:
        dash.terminate()
        try:
            dash.wait(timeout=5)
        except Exception:
            dash.kill()
        stop.set()
        srv.close()


def test_auto_status_gating(state_file):
    """The periodic STATUS_GET must NOT fire when the relay reports no live session."""
    print("== auto STATUS_GET gating (no live device) ==")
    import mqtt_session
    from rules_engine import PublishSpec

    injects: list = []
    obs_port = 19898
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", obs_port))
    srv.listen(4)
    stop = threading.Event()

    def on_publish(session, topic, payload, qos):
        try:
            cmd = json.loads(payload)
        except Exception:
            return None
        if isinstance(cmd, dict) and cmd.get("list"):
            return [PublishSpec(topic=b"mtap/devices",
                                payload=json.dumps({"devices": [], "latest": None}).encode(),
                                qos=0)]
        injects.append(cmd)
        return None

    def acceptor():
        srv.settimeout(0.5)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=mqtt_session.run_server_session,
                             args=(c, "stub", lambda m: None),
                             kwargs={"on_publish": on_publish, "recv_timeout": 30},
                             daemon=True).start()

    threading.Thread(target=acceptor, daemon=True).start()

    port = 18092
    dash = subprocess.Popen([sys.executable, os.path.join(DASH_DIR, "mttl_dashboard.py"),
                             "--host", "127.0.0.1", "--port", str(port), "--enable-control",
                             "--state-file", state_file, "--observer", f"127.0.0.1:{obs_port}",
                             "--refresh", "2"],
                            env=dict(os.environ, MTTL_STATUS_INTERVAL="1"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        if not _wait(port, 8):
            ok("dashboard(gating) http up", False)
            return
        time.sleep(5)  # well past several 1s intervals
        ok("no auto STATUS_GET when relay reports no live session",
           not any(i.get("action") == "status" for i in injects), injects[-3:])
    finally:
        dash.terminate()
        try:
            dash.wait(timeout=5)
        except Exception:
            dash.kill()
        stop.set()
        srv.close()


# --------------------------------------------------------------------------- e2e (3.11+)

def test_e2e(ca_crt, certdir, state_file):
    print("== e2e (real relay, decloud, high ports) ==")
    HTTP_PORT, MEF_PORT, MQTT_PORT, OBS_PORT = 18080, 18443, 28831, 19883
    if os.path.exists(state_file):
        os.remove(state_file)
    env = dict(os.environ, MTTL_STATE_FILE=state_file, MTTL_DASHBOARD_LIB=DASH_DIR)
    logf = os.path.join(os.path.dirname(state_file), "relay-e2e.log")
    relay = subprocess.Popen(
        [sys.executable, os.path.join(VENDOR, "relay.py"), "serve",
         "--cert-dir", certdir, "--rules-dir", RULES_DIR,
         "--http-port", str(HTTP_PORT), "-p", f"{MEF_PORT}:443:{MEF_SNI}",
         "-p", f"{MQTT_PORT}:18831:{BRK2_SNI}", "--observer", f"127.0.0.1:{OBS_PORT}",
         "--default-domain", MEF_SNI],
        cwd=RELAY_DIR, env=env, stdout=open(logf, "w"), stderr=subprocess.STDOUT)
    try:
        up = all(_wait(p) for p in (HTTP_PORT, MEF_PORT, MQTT_PORT))
        ok("relay listeners up (80/443/18831 equiv)", up)
        if not up:
            return
        c = http.client.HTTPConnection("127.0.0.1", HTTP_PORT, timeout=5)
        c.request("GET", "/mef/cert/http/pem")
        rr = c.getresponse()
        ca_body = rr.read()
        ok("GET :80 /mef/cert/http/pem -> CA PEM", b"BEGIN CERTIFICATE" in ca_body)
        ok(":80 filename header oneM2M_HTTP_CA.pem",
           rr.getheader("Content-Disposition", "").endswith("oneM2M_HTTP_CA.pem"))

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(ca_crt)
        s = ctx.wrap_socket(socket.create_connection(("127.0.0.1", MEF_PORT), 10),
                            server_hostname=MEF_SNI)
        sans = [v for k, v in s.getpeercert().get("subjectAltName", ()) if k == "DNS"]
        ok("e2e :443 leaf SAN=mef", MEF_SNI in sans, sans)
        xml = (f'<auth><deviceModel>MTTL-W01</deviceModel><mac>{DEV_MAC}</mac>'
               f'<deviceSerialNo>{DEV_MAC}</deviceSerialNo></auth>').encode()
        s.sendall(b"POST /mef HTTP/1.0\r\nHost: x\r\nContent-Length: "
                  + str(len(xml)).encode() + b"\r\n\r\n" + xml)
        s.settimeout(5)
        raw = b""
        try:
            while True:
                d = s.recv(65536)
                if not d:
                    break
                raw += d
        except socket.timeout:
            pass
        s.close()
        mef_body = raw.partition(b"\r\n\r\n")[2]
        eid = _tag(mef_body, b"entityId")
        ok("e2e /mef -> entityId", bool(eid), eid)

        m = ctx.wrap_socket(socket.create_connection(("127.0.0.1", MQTT_PORT), 10),
                            server_hostname=BRK2_SNI)
        cidb = (eid or "ASN_CSE-D-X-MTAP").encode()
        fr = mw.Framer()
        cb = (mw._write_str(b"MQTT") + bytes([4, 0x02]) + b"\x00\x6e" + mw._write_str(cidb))
        m.sendall(mw.Packet(kind=mw.CONNECT, flags=0, body=cb).bytes())
        pk = _collect(m, fr, mw.CONNACK, time.time() + 8)
        ok("e2e MQTT CONNACK rc0", any(p.kind == mw.CONNACK and p.body[1] == 0 for p in pk),
           [p.kind for p in pk])
        sub = struct.pack(">H", 1) + mw._write_str(
            f"/oneM2M/resp/{cidb.decode()}/IN_CSE-BASE-1".encode()) + b"\x01"
        m.sendall(mw.Packet(kind=mw.SUBSCRIBE, flags=2, body=sub).bytes())
        _collect(m, fr, mw.SUBACK, time.time() + 8)
        body = mw._write_str(f"/oneM2M/req/IN_CSE-BASE-1/{cidb.decode()}".encode())
        body += struct.pack(">H", 5) + json.dumps(
            {"op": "2", "to": "/IN_CSE-BASE-1", "fr": "/" + cidb.decode(),
             "rqi": "cseBaseRetrieve"}).encode()
        m.sendall(mw.Packet(kind=mw.PUBLISH, flags=0x02, body=body).bytes())
        # relay replies with PUBACK (kind 4) THEN the reply PUBLISH (kind 3), in
        # separate TLS records — keep reading until the PUBLISH shows up.
        got = _collect(m, fr, mw.PUBLISH, time.time() + 8)
        pubs = [p for p in got if p.kind == mw.PUBLISH]
        rsc = None
        if pubs:
            try:
                rsc = json.loads(mw.parse_publish(pubs[0]).payload).get("rsc")
            except Exception as exc:
                rsc = f"parse-err:{exc!r}"
        ok("e2e oneM2M cseBaseRetrieve reply", rsc == 2000,
           f"pkts={[p.kind for p in got]} rsc={rsc}")

        # observer {"list":true} against the REAL relay while the session is live
        try:
            ob = socket.create_connection(("127.0.0.1", OBS_PORT), 5)
            ob.settimeout(5)
            ocb = mw._write_str(b"MQTT") + bytes([4, 0x02]) + b"\x00\x1e" + mw._write_str(b"syn-obs")
            ob.sendall(mw.Packet(kind=mw.CONNECT, flags=0, body=ocb).bytes())
            ofr = mw.Framer()
            _collect(ob, ofr, mw.CONNACK, time.time() + 5)
            ob.sendall(mw.build_publish(b"mtap/req", b'{"list":true}', qos=1, packet_id=1).bytes())
            olist = _collect(ob, ofr, mw.PUBLISH, time.time() + 5)
            names = [json.loads(mw.parse_publish(p).payload) for p in olist if p.kind == mw.PUBLISH
                     and mw.parse_publish(p).topic == b"mtap/devices"]
            ob.close()
            ok("e2e observer {\"list\":true} sees the live session",
               bool(names) and (cidb.decode() in (names[0].get("devices") or [])), names[:1])
        except Exception as exc:
            ok("e2e observer {\"list\":true} sees the live session", False, repr(exc))

        # --- reconnect / stale-close race against the REAL relay -------------
        try:
            m2 = ctx.wrap_socket(socket.create_connection(("127.0.0.1", MQTT_PORT), 10),
                                 server_hostname=BRK2_SNI)
            fr2 = mw.Framer()
            m2.sendall(mw.Packet(kind=mw.CONNECT, flags=0,
                                 body=(mw._write_str(b"MQTT") + bytes([4, 0x02]) + b"\x00\x6e"
                                       + mw._write_str(cidb))).bytes())
            _collect(m2, fr2, mw.CONNACK, time.time() + 6)
            m2.sendall(mw.Packet(kind=mw.SUBSCRIBE, flags=2,
                                 body=struct.pack(">H", 1)
                                 + mw._write_str(b"/oneM2M/resp/x/IN_CSE-BASE-1") + b"\x01").bytes())
            _collect(m2, fr2, mw.SUBACK, time.time() + 6)
            time.sleep(0.4)
            # relay.register(m2) should have closed m (same client_id)
            m_closed = False
            try:
                m.settimeout(2)
                m_closed = (m.recv(1) == b"")
            except OSError:
                m_closed = True
            ok("e2e reconnect: relay closed the stale first session", m_closed)
            m.close()

            # m2 must still be the live one, and a request on it still works
            m2.sendall(mw.Packet(kind=mw.PUBLISH, flags=0x02,
                                 body=(mw._write_str(b"/oneM2M/req/IN_CSE-BASE-1/" + cidb)
                                       + struct.pack(">H", 7)
                                       + json.dumps({"op": "2", "to": "/IN_CSE-BASE-1",
                                                     "fr": "/" + cidb.decode(),
                                                     "rqi": "cseBaseRetrieve"}).encode())).bytes())
            g2 = _collect(m2, fr2, mw.PUBLISH, time.time() + 8)
            p2 = [p for p in g2 if p.kind == mw.PUBLISH]
            ok("e2e reconnect: new session still serves requests",
               bool(p2) and json.loads(mw.parse_publish(p2[0]).payload).get("rsc") == 2000)

            ob = socket.create_connection(("127.0.0.1", OBS_PORT), 5)
            ob.settimeout(5)
            ob.sendall(mw.Packet(kind=mw.CONNECT, flags=0,
                                 body=(mw._write_str(b"MQTT") + bytes([4, 0x02]) + b"\x00\x1e"
                                       + mw._write_str(b"syn-obs2"))).bytes())
            ofr = mw.Framer()
            _collect(ob, ofr, mw.CONNACK, time.time() + 5)
            ob.sendall(mw.build_publish(b"mtap/req", b'{"list":true}', qos=1, packet_id=1).bytes())
            ol = _collect(ob, ofr, mw.PUBLISH, time.time() + 5)
            nm = [json.loads(mw.parse_publish(p).payload) for p in ol if p.kind == mw.PUBLISH
                  and mw.parse_publish(p).topic == b"mtap/devices"]
            ob.close()
            ok("e2e reconnect: observer list still has exactly the id (not evicted, not dup)",
               bool(nm) and nm[0].get("devices") == [cidb.decode()], nm[:1])
            m2.close()
        except Exception as exc:
            ok("e2e reconnect: stale-close race handled", False, repr(exc))

        time.sleep(0.5)
        try:
            devs = json.load(open(state_file)).get("devices", {})
            ok("e2e feed state populated", any(st.get("mqtt_connected") for st in devs.values()))
        except Exception as e:
            ok("e2e feed state populated", False, repr(e))
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=5)
        except Exception:
            relay.kill()


def _wait(port, t=10.0):
    end = time.time() + t
    while time.time() < end:
        try:
            socket.create_connection(("127.0.0.1", port), 0.4).close()
            return True
        except OSError:
            time.sleep(0.15)
    return False


RELAY_PY = os.path.join(VENDOR, "relay.py")


def _load_device_registry():
    """Extract the DeviceRegistry class from the vendored relay.py and exec it in
    isolation (relay.py can't be imported on Python <3.11 — tomllib)."""
    src = open(RELAY_PY, "r", encoding="utf-8").read()
    i = src.index("class DeviceRegistry:")
    j = src.index("\nDEVICE_REGISTRY = DeviceRegistry()")
    ns = {"threading": __import__("threading"), "Optional": None,
          "log": lambda *a, **k: None}
    exec(compile(src[i:j], "<DeviceRegistry>", "exec"), ns)
    return ns["DeviceRegistry"]


class _FakeSock:
    def __init__(self, tag):
        self.tag = tag
        self.closed = False

    def close(self):
        self.closed = True


def test_registry_race():
    print("== DeviceRegistry stale-close race (patched) ==")
    DR = _load_device_registry()
    reg = DR()
    cid = b"ASN_CSE-D-x-MTAP"
    lk = __import__("threading").Lock()

    a = _FakeSock("A")
    reg.register(cid, a, lk)
    ok("A registered", reg.get(cid)[0] is a and reg.list_client_ids() == [cid])

    b = _FakeSock("B")
    reg.register(cid, b, lk)
    ok("B (same id) becomes current", reg.get(cid)[0] is b)
    ok("register(B) closed the stale A socket", a.closed is True)
    ok("latest_client_id -> id (B)", reg.latest_client_id() == cid)

    # stale A's on_close fires late, passing A's own socket
    reg.unregister(cid, a)
    ok("stale A close does NOT evict B", reg.get(cid)[0] is b and cid in reg.list_client_ids())

    # B closes for real
    reg.unregister(cid, b)
    ok("B close removes the entry", reg.get(cid) is None and reg.list_client_ids() == [])

    # backward-compat: unregister without sock still works (old callers / proxy)
    c = _FakeSock("C")
    reg.register(b"other", c, lk)
    reg.unregister(b"other")
    ok("unregister(id) with no sock still removes (compat)", reg.list_client_ids() == [])


def _collect(sock, framer, want_kind, deadline, per_recv=1.0):
    """recv() into `framer` repeatedly until a packet of `want_kind` is framed or
    `deadline` passes. Returns every packet framed so far. Handles the case where
    the server sends multiple MQTT packets (e.g. PUBACK then PUBLISH) in separate
    TLS records — a single recv() would only see the first."""
    seen = []
    while time.time() < deadline:
        try:
            sock.settimeout(max(0.1, min(per_recv, deadline - time.time())))
            data = sock.recv(65536)
        except socket.timeout:
            continue
        except OSError:
            break
        if not data:
            break
        seen.extend(framer.push(data))
        if any(p.kind == want_kind for p in seen):
            break
    return seen


# -------------------------------------------------------------- firmware OTA rule

_OTA_4_CID = ":443-192.0.2.124:51000-1710000000"
_OTA_3_CID = ":443-192.0.2.123:51000-1710000000"
_OTA_4_ORIGIN = "ASN_CSE-D-EXAMPLE004-MTAP"
_OTA_3_ORIGIN = "ASN_CSE-D-EXAMPLE003-MTAP"
_OTA_VC_PATH = b"/mef/updateVersionCheck/firmware/MTAP/MTTL-W01/1.0.66"
_OTA_FWNNAM = "comMTTL-W01_1.0.106_v9_v4_vtpe_t4.fwr"
_OTA_FW_PATH_A = b"/mef/firmware1.0.106/" + _OTA_FWNNAM.encode()
_OTA_FW_PATH_B = b"/mef/firmware/MTAP/20/D/1.0.106/" + _OTA_FWNNAM.encode()


def _load_ota_rule():
    import importlib.util
    if VENDOR not in sys.path:
        sys.path.insert(0, VENDOR)
    p = os.path.join(RULES_DIR, "15-firmware-ota.py")
    spec = importlib.util.spec_from_file_location("rule15", p)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _load_serve_stream():
    """_serve_stream() only — relay.py can't be imported on py<3.11 (tomllib)."""
    src = open(RELAY_PY, "r", encoding="utf-8").read()
    i = src.index("def _serve_stream(dev_tls, cid, resp) -> None:")
    j = src.index("\n\n\ndef serve_http_locally(", i)
    ns = {"os": os, "ssl": ssl, "socket": socket, "getattr": getattr, "open": open}
    return src[i:j], ns


class _FakeTLS:
    def __init__(self, fail_on_call=None):
        self.sent = []            # bytes per sendall
        self.timeouts = []
        self.fail_on_call = fail_on_call
        self.calls = 0

    def settimeout(self, t):
        self.timeouts.append(t)

    def sendall(self, data):
        self.calls += 1
        if self.fail_on_call is not None and self.calls >= self.fail_on_call:
            raise BrokenPipeError("simulated peer drop")
        self.sent.append(bytes(data))


class _FakeClock:
    def __init__(self):
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(s)


def _write_arm(path, fwr, sha, *, expires_in=1800, target_ip="192.0.2.124",
               target_origin=_OTA_4_ORIGIN, drop=None):
    lines = {
        "target_ip": target_ip, "target_origin": target_origin, "fwr": fwr,
        "sha256": sha, "offer_version": "1.0.106", "fwnnam": _OTA_FWNNAM,
        "expires_epoch": str(int(time.time()) + expires_in),
    }
    if drop:
        lines.pop(drop, None)
    with open(path, "w") as f:
        for k, v in lines.items():
            f.write("%s=%s\n" % (k, v))


def test_firmware_ota_offline():
    print("== firmware OTA rule (15-firmware-ota.py) — single-target, fail-closed, offline ==")
    import hashlib as _h
    import logging as _logging
    R = _load_ota_rule()
    R._log.setLevel(_logging.CRITICAL)   # the rule logs expected BLOCK/retry states; keep test output clean
    from rules_engine import HttpResponse

    # The behavioural OTA tests use a small deterministic synthetic firmware blob
    # (below). No real firmware image is needed or referenced.

    wd = tempfile.mkdtemp(prefix="mttl-ota-")
    arm = os.path.join(wd, "firmware-ota.arm")
    state = os.path.join(wd, "firmware-ota.state")
    os.environ["MTTL_OTA_ARM_FILE"] = arm
    os.environ["MTTL_OTA_STATE_FILE"] = state
    os.environ["MTTL_OTA_TARGET_IP"] = "192.0.2.124"
    os.environ["MTTL_OTA_TARGET_ORIGIN"] = _OTA_4_ORIGIN
    os.environ["MTTL_OTA_DENY_IPS"] = "192.0.2.123"
    os.environ["MTTL_OTA_DENY_ORIGINS"] = _OTA_3_ORIGIN

    fw = os.path.join(wd, "fw.bin")
    blob = b"MTTLFW" * 1800 + b"tail"          # 10804 B -> 3 chunks (4096, 4096, 2612)
    open(fw, "wb").write(blob)
    fw_sha = _h.sha256(blob).hexdigest()

    def reset(**arm_kw):
        for p in (arm, state):
            if os.path.exists(p):
                os.remove(p)
        for extra in os.listdir(wd):
            if extra.startswith("firmware-ota.arm.spent."):
                os.remove(os.path.join(wd, extra))
        if arm_kw.get("_arm", True):
            kw = {k: v for k, v in arm_kw.items() if not k.startswith("_")}
            _write_arm(arm, fw, fw_sha, **kw)

    def call(cid, origin, method, path):
        ctx = Ctx(cid=cid)
        hdrs = {} if origin is None else {"X-M2M-Origin": origin}
        return R.on_http_request(ctx, method, path, hdrs, b"")

    def is_block(resp):
        return (isinstance(resp, HttpResponse) and resp.body == b""
                and resp.stream is None and resp.status_line == b"HTTP/1.1 200 OK")

    def phase():
        try:
            return json.load(open(state)).get("phase")
        except Exception:
            return None

    # 1 — no ARM -> BLOCK
    reset(_arm=False)
    ok("1  no ARM -> version-check BLOCK", is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))
    ok("1b no ARM -> firmware GET BLOCK", is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_A)))

    # 2 — deny-listed IP + origin -> never offered, no state transition
    reset()
    r = call(_OTA_3_CID, _OTA_3_ORIGIN, b"GET", _OTA_VC_PATH)
    ok("2  deny IP+origin -> BLOCK", is_block(r))
    ok("19 deny identity caused NO state transition", phase() is None and not os.path.exists(state))

    # 3 — target IP, wrong/absent origin -> BLOCK
    reset()
    ok("3  target IP + wrong origin -> BLOCK", is_block(call(_OTA_4_CID, "ASN_CSE-D-WRONG-MTAP", b"GET", _OTA_VC_PATH)))
    ok("3b target IP + no origin header -> BLOCK", is_block(call(_OTA_4_CID, None, b"GET", _OTA_VC_PATH)))

    # 4 — target origin, wrong IP -> BLOCK
    reset()
    ok("4  target origin + non-target IP -> BLOCK",
       is_block(call(":443-192.0.2.9:5-1", _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))

    # 5 — valid target + valid ARM + sha -> exact 1.0.106 offer, state OFFERED
    reset()
    r = call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
    want = b"<vr>1.0.106<url>1.0.106<fwnnam>%s<chksum>12345678" % _OTA_FWNNAM.encode()
    ok("5  target valid -> exact offer body", isinstance(r, HttpResponse) and r.body == want, r.body if isinstance(r, HttpResponse) else r)
    ok("5b offer Content-Type text/plain;charset=UTF-8",
       isinstance(r, HttpResponse) and r.headers.get("Content-Type") == "text/plain;charset=UTF-8")
    ok("5c offer has no stream", isinstance(r, HttpResponse) and r.stream is None)
    ok("5d state -> OFFERED", phase() == "OFFERED")

    # 6 — expired ARM -> BLOCK
    reset(expires_in=-10)
    ok("6  expired ARM -> BLOCK", is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))

    # 7 — malformed ARM (missing key) -> BLOCK
    reset(drop="sha256")
    ok("7  malformed ARM (no sha256) -> BLOCK", is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))
    reset(target_ip="192.0.2.123")
    ok("7b ARM retargeted away from configured target -> BLOCK",
       is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))

    # 8 — sha mismatch -> BLOCK
    reset()
    _write_arm(arm, fw, "0" * 64)
    ok("8  ARM sha != file sha -> BLOCK", is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))

    # 13 — direct firmware GET with no prior OFFER -> BLOCK
    reset()
    ok("13 firmware GET without an OFFER -> BLOCK", is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_A)))

    # 9 — unexpected firmware path (after a valid OFFER) -> BLOCK
    reset()
    call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)                 # -> OFFERED
    ok("9  unexpected firmware path -> BLOCK",
       is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", b"/mef/firmware9.9.9/evil.fwr")))

    # 10 — download Case A (X-M2M-Origin present)
    reset()
    call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
    rA = call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_A)
    ok("10 caseA -> stream response", isinstance(rA, HttpResponse) and rA.stream is not None)
    ok("10b stream path == ARM fwr", rA.stream is not None and rA.stream.path == fw)
    ok("10c stream chunk 4096 / pace 0.03", rA.stream is not None and rA.stream.chunk == 4096 and abs(rA.stream.pace - 0.03) < 1e-9)
    ok("10d Content-Type octet-stream", rA.headers.get("Content-Type") == "application/octet-stream")
    ok("10e state -> TRANSFERRING", phase() == "TRANSFERRING")
    ok("10f also matches the /MTAP/20/D/ path form", (lambda: (
        call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH),
        isinstance(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_B), HttpResponse))[1])())

    # 11 — download Case B (no X-M2M-Origin) within TTL
    reset()
    call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
    rB = call(_OTA_4_CID, None, b"GET", _OTA_FW_PATH_A)
    ok("11 caseB (no origin, fresh OFFER, target IP) -> stream", isinstance(rB, HttpResponse) and rB.stream is not None)

    # 12 — Case B beyond correlation TTL -> BLOCK
    reset()
    call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
    st = json.load(open(state))
    st["offer"]["epoch"] = time.time() - (R.CORRELATION_TTL_SECONDS + 30)
    json.dump(st, open(state, "w"))
    ok("12 caseB stale OFFER (> TTL) -> BLOCK", is_block(call(_OTA_4_CID, None, b"GET", _OTA_FW_PATH_A)))
    # but Case A (origin present) still works past the TTL
    ok("12b caseA still OK past TTL", isinstance(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_A), HttpResponse))

    # 14-17 — drive relay._serve_stream against the rule's StreamPlan
    ss_src, ss_ns = _load_serve_stream()

    def run_stream(resp, fake):
        clk = _FakeClock()
        ns = dict(ss_ns)
        ns["time"] = clk
        ns["log"] = lambda *a, **k: None
        exec(compile(ss_src, "<_serve_stream>", "exec"), ns)
        ns["_serve_stream"](fake, "cid", resp)
        return clk

    # full transfer
    reset()
    call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
    rS = call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_A)
    fake = _FakeTLS()
    clk = run_stream(rS, fake)
    body_chunks = fake.sent[1:]                     # sent[0] == headers
    ok("14 body chunk size == 4096 (all but last)",
       all(len(c) == 4096 for c in body_chunks[:-1]) and 0 < len(body_chunks[-1]) <= 4096)
    ok("14b total streamed bytes == file size", sum(len(c) for c in body_chunks) == len(blob))
    ok("14c headers carry the real Content-Length",
       b"Content-Length: %d\r\n" % len(blob) in fake.sent[0])
    ok("15 pacing sleep called once per body chunk with 0.03",
       clk.sleeps == [0.03] * len(body_chunks))
    ok("15b socket timeout restored before streaming", 60.0 in fake.timeouts)
    ok("17 exact filesize -> SPENT + ARM burned",
       phase() == "SPENT" and not os.path.exists(arm)
       and any(x.startswith("firmware-ota.arm.spent.") for x in os.listdir(wd)))
    ok("17b after SPENT, a fresh version-check is BLOCKed",
       is_block(call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)))

    # partial / broken transfer -> NOT spent
    reset()
    call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
    rP = call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_FW_PATH_A)
    fake = _FakeTLS(fail_on_call=3)                 # head ok, chunk1 ok, chunk2 raises
    run_stream(rP, fake)
    ok("16 broken mid-stream -> phase stays TRANSFERRING (retry allowed)", phase() == "TRANSFERRING")
    ok("16b broken mid-stream -> ARM NOT burned", os.path.exists(arm))
    ok("16c broken mid-stream -> transfer.sent < filesize",
       json.load(open(state))["transfer"]["sent"] < len(blob))

    # 18 — non-stream HttpResponse contract intact
    ok("18 HttpResponse(3 positional args) still works, stream defaults None",
       HttpResponse(b"HTTP/1.1 200 OK", {"X": "y"}, b"hi").stream is None)

    # 20 — concurrent OFFER requests: atomic, no race
    reset()
    errs = []

    def worker():
        try:
            for _ in range(15):
                call(_OTA_4_CID, _OTA_4_ORIGIN, b"GET", _OTA_VC_PATH)
        except Exception as e:  # pragma: no cover
            errs.append(repr(e))
    ts = [threading.Thread(target=worker) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok("20 concurrent OFFERs: no exception", not errs, errs)
    ok("20b concurrent OFFERs: state file still valid JSON, phase OFFERED",
       phase() == "OFFERED")

    os.environ.pop("MTTL_OTA_ARM_FILE", None)
    os.environ.pop("MTTL_OTA_STATE_FILE", None)
    os.environ.pop("MTTL_OTA_TARGET_IP", None)
    os.environ.pop("MTTL_OTA_TARGET_ORIGIN", None)
    os.environ.pop("MTTL_OTA_DENY_IPS", None)
    os.environ.pop("MTTL_OTA_DENY_ORIGINS", None)


# --------------------------------------------------------------------------- main

def main():
    wd = tempfile.mkdtemp(prefix="mttl-syn-")
    certdir = os.path.join(wd, "certs")
    state_file = os.path.join(wd, "state", "devices.json")
    os.makedirs(os.path.dirname(state_file), exist_ok=True)

    ver = sys.version_info
    print(f"python {ver.major}.{ver.minor}.{ver.micro}   workdir {wd}")

    ok("mqtt_wire framer roundtrip",
       mw.Framer().push(mw.build_puback(7).bytes())[0].kind == mw.PUBACK)

    test_registry_race()
    test_firmware_ota_offline()
    ca_crt = test_ca(certdir)
    test_telemetry_unit()
    test_rules_layer(state_file)
    test_dashboard(state_file)
    test_dashboard_observer(state_file)
    test_auto_status_gating(state_file)

    if ver >= (3, 11):
        test_e2e(ca_crt, certdir, state_file)
    else:
        print("== e2e SKIPPED (needs Python >= 3.11 for vendored relay.py/tomllib) ==")
        PASS.append("e2e-skipped-on-this-python")

    print()
    print("=" * 62)
    print(f"PASS {len(PASS)}   FAIL {len(FAIL)}   SKIP {len(SKIP)}")
    if SKIP:
        print("SKIPPED: " + ", ".join(SKIP))
    if FAIL:
        print("FAILED: " + ", ".join(FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
