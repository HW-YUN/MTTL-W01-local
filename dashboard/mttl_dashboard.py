#!/usr/bin/env python3
"""
MTTL-W01 local dashboard.

  GET  /                -> HTML (polls /api/state every N seconds)
  GET  /api/state       -> JSON  {server_time, updated, devices:{key:state}, observer:{...}}
  GET  /api/live        -> JSON  the relay's live MQTT session list (debug)
  GET  /healthz         -> "ok"
  POST /api/control     -> {"key":..., "outlet":0..4, "on":true|false}
                           or {"key":..., "action":"status"}  (STATUS_GET diagnostic)
                           only when --enable-control; publishes the command to the
                           relay observer. "power" is confirmed via telemetry;
                           "status" is fire-and-forget (STATUS_REPORT is async).

Data:
  - device state (telemetry / mef / identity) = the JSON file written by
    relay/rules/10-dashboard-feed.py
  - liveness (is the MQTT session actually up right now) = asked from the relay's
    observer port with {"list":true}; the feed rule cannot see disconnects.

Standard library only. No framework. Vendored smartrelay is not modified.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.normpath(os.path.join(_HERE, "..", "vendor", "smartrelay"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
try:
    import mqtt_wire as mw
except Exception:
    mw = None
try:
    import mttl_telemetry as T
except Exception:
    T = None

DEFAULT_STATE_FILE = os.environ.get("MTTL_STATE_FILE") or "/srv/mttl-lab/relay/state/devices.json"
DEFAULT_OBSERVER = os.environ.get("MTTL_OBSERVER") or "127.0.0.1:9883"
DEFAULT_PORT = int(os.environ.get("MTTL_DASHBOARD_PORT") or "8080")
DEFAULT_REFRESH = 3
ACTIVITY_FALLBACK_WINDOW = 180.0  # only used when the observer is unreachable
# periodic STATUS_GET so meter_watts does not go stale between manual reads.
# 0 disables; env override keeps the synthetic test from waiting real seconds.
AUTO_STATUS_INTERVAL = float(os.environ.get("MTTL_STATUS_INTERVAL") or "15.0")

CFG = {"state_file": DEFAULT_STATE_FILE, "observer": DEFAULT_OBSERVER,
       "refresh": DEFAULT_REFRESH, "enable_control": False}


# ===========================================================================
# ObserverLink — one persistent MQTT client to the relay observer port.
#   * receives every device PUBLISH the relay taps (real-time, unused for state
#     since the feed rule already writes the file, but proves the link is alive)
#   * every few seconds sends {"list":true} and reads the relay's authoritative
#     live-session list back on topic  mtap/devices
#   * outbound: publish_inject() sends an outlet command
# ===========================================================================

class ObserverLink:
    def __init__(self, observer: str):
        self.host, self.port = observer.rsplit(":", 1)
        self.host = self.host or "127.0.0.1"
        self.port = int(self.port)
        self._lock = threading.Lock()
        self.connected = False
        self.live_ids: set = set()
        self.latest = None
        self.live_ts = 0.0            # when live_ids was last refreshed
        self.last_tap_ts = 0.0        # last time any tapped PUBLISH arrived
        self._sock = None
        self._framer = None
        self._pid = 0
        self._stop = False
        self._thr = threading.Thread(target=self._run, name="observer-link", daemon=True)

    def start(self):
        self._thr.start()

    def snapshot(self) -> dict:
        with self._lock:
            fresh = self.connected and (time.time() - self.live_ts) < 15.0
            return {
                "connected": self.connected,
                "fresh": fresh,
                "live_ids": sorted(self.live_ids),
                "latest": self.latest,
                "live_age": round(time.time() - self.live_ts, 1) if self.live_ts else None,
                "tap_age": round(time.time() - self.last_tap_ts, 1) if self.last_tap_ts else None,
            }

    # ---- outbound -------------------------------------------------------

    def _send(self, pkt):
        with self._lock:
            if not self._sock:
                raise RuntimeError("observer link not connected")
            self._sock.sendall(pkt.bytes())

    def _next_pid(self):
        self._pid = (self._pid % 0xFFFF) + 1
        return self._pid

    def request_list(self):
        self._send(mw.build_publish(b"mtap/req", b'{"list":true}', qos=1, packet_id=self._next_pid()))

    def publish_inject(self, cmd: dict):
        self._send(mw.build_publish(b"mttl/dashboard/inject",
                                    json.dumps(cmd, separators=(",", ":")).encode(),
                                    qos=1, packet_id=self._next_pid()))

    def _auto_status(self):
        """Periodic STATUS_GET for live devices. Fire-and-forget, reuses the inject
        path, no _confirm. No-op when the relay reports no live MQTT session."""
        ids = sorted(self.live_ids)
        if not ids:
            return
        if len(ids) == 1:
            self.publish_inject({"action": "status"})
        else:
            for cid in ids:
                self.publish_inject({"action": "status", "device_client_id": cid})

    # ---- the loop -----------------------------------------------------

    def _connect(self):
        s = socket.create_connection((self.host, self.port), timeout=6)
        s.settimeout(6)
        cb = (mw._write_str(b"MQTT") + bytes([4, 0x02]) + b"\x00\x1e"
              + mw._write_str(b"mttl-dashboard"))
        s.sendall(mw.Packet(kind=mw.CONNECT, flags=0, body=cb).bytes())
        fr = mw.Framer()
        deadline = time.time() + 6
        while time.time() < deadline:
            data = s.recv(4096)
            if not data:
                raise RuntimeError("observer closed during CONNECT")
            for pkt in fr.push(data):
                if pkt.kind == mw.CONNACK and (len(pkt.body) < 2 or pkt.body[1] == 0):
                    s.sendall(mw.Packet(kind=mw.SUBSCRIBE, flags=2,
                                        body=b"\x00\x01" + mw._write_str(b"#") + b"\x00").bytes())
                    return s, fr
        raise RuntimeError("no CONNACK from observer")

    def _run(self):
        if mw is None:
            sys.stderr.write("[dash] observer link disabled (mqtt_wire missing)\n")
            return
        while not self._stop:
            try:
                s, fr = self._connect()
                with self._lock:
                    self._sock, self._framer = s, fr
                    self.connected = True
                sys.stderr.write("[dash] observer link up (%s:%d)\n" % (self.host, self.port))
                last_list = 0.0
                last_status = time.time()   # don't fire immediately on connect
                s.settimeout(1.0)
                while not self._stop:
                    now = time.time()
                    if now - last_list > 4.0:
                        try:
                            self.request_list()
                        except Exception:
                            break
                        last_list = now
                    if AUTO_STATUS_INTERVAL > 0 and now - last_status > AUTO_STATUS_INTERVAL:
                        last_status = now
                        try:
                            self._auto_status()
                        except Exception:
                            break
                    try:
                        data = s.recv(65536)
                    except socket.timeout:
                        continue
                    if not data:
                        break
                    for pkt in fr.push(data):
                        self._on_packet(pkt)
            except Exception as exc:
                sys.stderr.write("[dash] observer link error: %r — retry in 3s\n" % exc)
            finally:
                with self._lock:
                    self.connected = False
                    try:
                        if self._sock:
                            self._sock.close()
                    except Exception:
                        pass
                    self._sock = None
            for _ in range(30):
                if self._stop:
                    return
                time.sleep(0.1)

    def _on_packet(self, pkt):
        if pkt.kind == mw.PINGREQ:
            try:
                self._send(mw.build_pingresp())
            except Exception:
                pass
            return
        if pkt.kind != mw.PUBLISH:
            return
        info = mw.parse_publish(pkt)
        if info.qos > 0 and info.packet_id is not None:
            try:
                self._send(mw.build_puback(info.packet_id))
            except Exception:
                pass
        with self._lock:
            self.last_tap_ts = time.time()
        if info.topic == b"mtap/devices":
            try:
                d = json.loads(info.payload)
            except Exception:
                return
            with self._lock:
                self.live_ids = set(d.get("devices") or [])
                self.latest = d.get("latest")
                self.live_ts = time.time()


LINK: "ObserverLink|None" = None


# ===========================================================================
# state
# ===========================================================================

def read_state() -> dict:
    try:
        with open(CFG["state_file"], "r") as f:
            blob = json.load(f)
        if isinstance(blob, dict) and "devices" in blob:
            return blob
        return {"updated": None, "devices": blob if isinstance(blob, dict) else {}}
    except FileNotFoundError:
        return {"updated": None, "devices": {}}
    except Exception as exc:
        return {"updated": None, "devices": {}, "error": repr(exc)}


def _num(d, n):
    if not isinstance(d, dict):
        return None
    return d.get(str(n), d.get(n))


def decorate(blob: dict) -> dict:
    now = time.time()
    obs = LINK.snapshot() if LINK else {"connected": False, "fresh": False, "live_ids": [], "latest": None}
    out = {"server_time": now, "updated": blob.get("updated"), "refresh": CFG["refresh"],
           "control_enabled": CFG["enable_control"], "observer": obs, "devices": {}}
    total_w = 0.0
    n_online = 0
    for key, st0 in (blob.get("devices") or {}).items():
        st = dict(st0)
        cid = st.get("client_id")
        ls = st.get("last_seen")
        lm = st.get("last_msg_ts")

        if obs["connected"] and obs["fresh"]:
            session_live = bool(cid) and cid in obs["live_ids"]
            online = session_live
            online_basis = "relay session registry"
        else:
            session_live = None
            online = bool(lm or ls) and (now - (lm or ls)) <= ACTIVITY_FALLBACK_WINDOW
            online_basis = "recent telemetry (observer unreachable)"

        st["online"] = online
        st["session_live"] = session_live
        st["online_basis"] = online_basis
        st["last_seen_ago"] = round(now - ls, 1) if ls else None
        st["last_msg_ago"] = round(now - lm, 1) if lm else None
        st["session_age"] = round(now - st["session_start_ts"], 1) if st.get("session_start_ts") else None

        mw_ = st.get("meter_watts") or {}
        tot = _num(mw_, 0)
        if tot is None:
            per = [(_num(mw_, i) or 0.0) for i in (1, 2, 3, 4) if _num(mw_, i) is not None]
            tot = sum(per) if per else None
        st["total_watts"] = tot
        if online and isinstance(tot, (int, float)):
            total_w += tot
        if online:
            n_online += 1
        out["devices"][key] = st

    out["summary"] = {"devices": len(out["devices"]), "online": n_online,
                      "total_watts": round(total_w, 2) if out["devices"] else None}
    return out


# ===========================================================================
# control
# ===========================================================================

def _target_selection(key: str):
    """Return (device_client_id or None, note). Omitting the id targets the relay's
    most-recent session, which is robust to client_id churn on reconnect."""
    obs = LINK.snapshot() if LINK else {"connected": False, "live_ids": [], "latest": None}
    live = obs.get("live_ids") or []
    devs = read_state().get("devices") or {}
    want_cid = (devs.get(key) or {}).get("client_id") if key else None

    if not obs.get("connected"):
        return want_cid, "observer link down — sending best-effort"
    if len(live) == 0:
        return None, "relay reports no live MQTT session — will target latest (may fail)"
    if len(live) == 1:
        return None, "single live device — targeting the relay's active session"
    # multiple live devices: must be specific
    if want_cid and want_cid in live:
        return want_cid, "targeting selected device"
    return "__AMBIGUOUS__", "multiple live devices — select one (its session must be live)"


def _confirm(key: str, outlet: int, on: bool, deadline: float) -> dict:
    """Poll the state file for the physical result of the command."""
    devs0 = read_state().get("devices") or {}
    base_report = (devs0.get(key) or {}).get("last_cmd_report")
    while time.time() < deadline:
        time.sleep(0.4)
        st = (read_state().get("devices") or {}).get(key) or {}
        pw = st.get("power") or {}
        targets = range(1, 5) if outlet == 0 else [outlet]
        if all(_num(pw, n) is on for n in targets):
            return {"result": "applied", "power": {str(n): _num(pw, n) for n in targets}}
        rep = st.get("last_cmd_report")
        if rep and rep != base_report and rep.get("result") == 0:
            return {"result": "acked", "cmd_report": rep,
                    "note": "device accepted the command; waiting for POWER event to confirm"}
    st = (read_state().get("devices") or {}).get(key) or {}
    return {"result": "sent",
            "note": "published; no confirming telemetry within the wait window",
            "power_now": {str(n): _num(st.get("power") or {}, n) for n in (range(1, 5) if outlet == 0 else [outlet])},
            "last_cmd_report": st.get("last_cmd_report")}


def do_control(req: dict):
    if not CFG["enable_control"]:
        return 403, {"error": "control disabled (start dashboard with --enable-control)"}
    if mw is None or LINK is None:
        return 500, {"error": "observer link unavailable"}

    action = req.get("action") or "power"
    if action == "status":
        # diagnostic path: relay rules already map {"action":"status"} -> oneM2M
        # device_control STATUS_GET. No outlet/on, no _confirm (STATUS_REPORT is async).
        key = req.get("key") or ""
        target, note = _target_selection(key)
        if target == "__AMBIGUOUS__":
            return 409, {"error": note, "live": (LINK.snapshot()["live_ids"] if LINK else [])}
        cmd = {"action": "status"}
        if target:
            cmd["device_client_id"] = target
        try:
            LINK.publish_inject(cmd)
        except Exception as exc:
            return 502, {"error": "observer publish failed: %r" % exc}
        return 202, {"sent": cmd, "targeting": note, "result": "sent",
                     "note": "STATUS_GET requested; the device STATUS_REPORT returns "
                             "asynchronously via telemetry and is not awaited here"}
    if action != "power":
        return 400, {"error": "action must be 'power' or 'status'"}

    try:
        outlet = int(req.get("outlet"))
        on = bool(req.get("on"))
    except (TypeError, ValueError):
        return 400, {"error": "outlet (0..4) and on (bool) required"}
    if outlet not in range(0, 5):
        return 400, {"error": "outlet must be 0..4 (0 = all)"}
    key = req.get("key") or ""

    target, note = _target_selection(key)
    if target == "__AMBIGUOUS__":
        return 409, {"error": note, "live": (LINK.snapshot()["live_ids"] if LINK else [])}

    cmd = {"action": "power", "outlet": outlet, "on": on}
    if target:
        cmd["device_client_id"] = target
    try:
        LINK.publish_inject(cmd)
    except Exception as exc:
        return 502, {"error": "observer publish failed: %r" % exc}

    result = _confirm(key, outlet, on, time.time() + 8.0)
    code = 200 if result["result"] in ("applied", "acked") else 202
    return code, {"sent": cmd, "targeting": note, **result}


# ===========================================================================
# HTML
# ===========================================================================

PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>MTTL-W01 Devices</title>
<style>
 :root{color-scheme:light dark}
 *{box-sizing:border-box}
 body{font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1115;color:#e7e9ee}
 header{padding:12px 18px;border-bottom:1px solid #262a33;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}
 h1{font-size:16px;margin:0}
 .muted{color:#8b93a3;font-size:12px}
 .sum{display:flex;gap:18px;flex-wrap:wrap;font-size:13px}
 .sum b{color:#e7e9ee}
 main{padding:16px 18px;display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(360px,1fr))}
 .card{background:#161922;border:1px solid #262a33;border-radius:10px;padding:12px 14px}
 .chd{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:8px}
 .chd .name{font-weight:700}
 .grp{margin:8px 0}
 .grp h4{margin:0 0 4px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#8b93a3}
 .row{display:flex;justify-content:space-between;gap:10px;padding:2px 0}
 .k{color:#8b93a3}
 .badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:700}
 .on{background:#123d1d;color:#6fe08c}.off{background:#3d1212;color:#ed9a9a}.unk{background:#2a2d36;color:#9aa2b1}
 .warn{background:#3d3312;color:#e5cf8f}
 .allctl{display:flex;gap:6px;margin-bottom:6px}
 .allctl button{flex:1;font:inherit;font-size:12px;font-weight:600;background:#222634;color:#e7e9ee;border:1px solid #333a49;border-radius:6px;padding:4px 0;cursor:pointer}
 .outlets{display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
 .outlet{border:1px solid #2c313c;border-radius:8px;padding:6px 4px;text-align:center}
 .outlet .st{font-weight:700;margin:2px 0}
 .outlet .w{font-size:12px;color:#9aa2b1}
 .outlet .b{display:flex;gap:3px;margin-top:5px}
 .outlet button{flex:1;font:inherit;font-size:12px;background:#222634;color:#e7e9ee;border:1px solid #333a49;border-radius:5px;padding:2px 0;cursor:pointer}
 button:disabled{opacity:.35;cursor:not-allowed}
 .bars{display:inline-flex;gap:1px;align-items:flex-end;height:12px;vertical-align:middle}
 .bars i{width:3px;background:#3a4150;display:inline-block}
 .bars i.f{background:#6fe08c}
 .res{margin-top:8px;font-size:12px;padding:6px 8px;border-radius:6px;background:#0d0f14;border:1px solid #22262f;white-space:pre-wrap;word-break:break-word}
 .empty{grid-column:1/-1;color:#8b93a3;padding:44px;text-align:center;line-height:1.7}
</style></head><body>
<header>
 <h1>MTTL-W01 Devices</h1>
 <span class="sum" id="sum">loading&hellip;</span>
 <span class="muted" id="meta"></span>
</header>
<main id="main"><div class="empty">loading&hellip;</div></main>
<script>
const g=(o,n)=>o==null?null:(o[n]!=null?o[n]:o[''+n]);
const esc=(s)=>{const d=document.createElement('div');d.textContent=''+s;return d.innerHTML;};
const A=(s)=>s==null||s===''?'<span class="muted">unknown</span>':esc(s);
function b3(v){return v===true?'<span class="badge on">ON</span>':v===false?'<span class="badge off">OFF</span>':'<span class="badge unk">?</span>';}
function bars(rssi){if(rssi==null)return '<span class="muted">unknown</span>';
 const q=Math.max(0,Math.min(4,Math.round((rssi+95)/12)));
 let h='';for(let i=0;i<4;i++)h+=`<i class="${i<q?'f':''}" style="height:${(i+1)*3}px"></i>`;
 return `<span class="bars">${h}</span> ${esc(rssi)} dBm`;}
function rowsHtml(rows){return rows.map(([k,v])=>`<div class="row"><span class="k">${k}</span><span>${v}</span></div>`).join('');}
async function ctl(key,outlet,on,btn){
 const card=btn.closest('.card'); const res=card.querySelector('.res');
 card.querySelectorAll('button').forEach(b=>b.disabled=true);
 res.textContent='sending outlet '+outlet+' -> '+(on?'ON':'OFF')+' …';
 try{
  const r=await fetch('/api/control',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify({key,outlet,on})});
  const j=await r.json();
  res.textContent='['+r.status+'] '+(j.result||j.error||'')+
    (j.targeting?'  ('+j.targeting+')':'')+'\n'+JSON.stringify(j,null,0);
 }catch(e){res.textContent='request error: '+e;}
 load();
}
function outletCell(key,n,st,canCtl){
 const on=g(st.power,n); const w=g(st.meter_watts,n);
 const ek=g(st.energy_kwh,n); const tc=g(st.temperature_c,n);
 const sb=g(st.standby,n); const al=g(st.alarms,n);
 const cf=g(st.configuration,n);
 const dis=canCtl?'':'disabled';
 let sub='';
 if(sb&&sb.standby)sub+='<div class="muted">standby</div>';
 if(al&&al.state&&al.state!=='normal')sub+='<div class="badge warn">'+esc(al.state)+'</div>';
 if(cf&&cf.enabled)sub+='<div class="muted">cut '+esc(cf.threshold_watts)+'W</div>';
 return `<div class="outlet"><div class="k">#${n}</div><div class="st">${b3(on)}</div>
  <div class="w">${w==null?'– W':(+w).toFixed(2)+' W'}</div>
  <div class="muted">${ek==null?'– kWh':(+ek).toFixed(3)+' kWh'}</div>
  <div class="muted">${tc==null?'– °C':(+tc).toFixed(1)+' °C'}</div>${sub}
  <div class="b"><button ${dis} onclick="ctl('${key}',${n},true,this)">on</button>
  <button ${dis} onclick="ctl('${key}',${n},false,this)">off</button></div></div>`;
}
function card(key,st,canCtlGlobal){
 const id=st.device_id||st.client_id||st.mac||key;
 const canCtl=canCtlGlobal && st.online!==false;
 const sess = st.session_live===true?'<span class="badge on">live</span>'
   : st.session_live===false?'<span class="badge off">none</span>'
   : '<span class="badge unk">'+esc(st.online_basis||'unknown')+'</span>';
 const onl = st.online===true?'<span class="badge on">online</span>'
   : st.online===false?'<span class="badge off">offline</span>'
   : '<span class="badge unk">unknown</span>';
 const wf=st.wifi||{};
 const diag=rowsHtml([
  ['MQTT session', sess + (st.session_age!=null?` <span class="muted">${st.session_age}s</span>`:'') ],
  ['reconnects', esc(st.reconnects||0)],
  ['mef bootstrap', st.mef_bootstrap_seen?'<span class="badge on">seen</span>':'<span class="badge unk">no</span>'],
  ['IP', A(st.ip)],
  ['client id', A(st.client_id)],
  ['MAC', A(st.mac)],
  ['model', A(st.model)],
 ['firmware', st.vtpe_seen?'1.0.106':'<span class="muted">unknown</span>'],
  ['last telemetry', st.last_msg_ago!=null?st.last_msg_ago+' s ago':'<span class="muted">never</span>'],
  ['last seen', st.last_seen_ago!=null?st.last_seen_ago+' s ago':'<span class="muted">never</span>'],
  ['last topic', A(st.last_topic)],
  ['Wi-Fi SSID', A(wf.ssid)],
  ['Wi-Fi signal', bars(wf.rssi_dbm!=null?wf.rssi_dbm:null)],
 ]);
 const tot=st.total_watts;
 const en=g(st.energy_raw,0);
 const vv=st.voltage_v;
 const power=rowsHtml([
  ['total power', tot!=null?('<b>'+(+tot).toFixed(2)+' W</b>'):'<span class="muted">unknown</span>'],
  ['energy total', en!=null?esc(en)+' Wh (raw)':'<span class="muted">unknown</span>'],
  ['voltage', vv!=null?(+vv).toFixed(0)+' V':'<span class="muted">unknown</span>'],
 ]);
 const outlets=[1,2,3,4].map(n=>outletCell(key,n,st,canCtl)).join('');
 const outletUnknown = st.online!==true || [1,2,3,4].some(n=>g(st.power,n)==null);
 const dall=canCtl?'':'disabled';
 const allctl=`<div class="allctl">`
  +`<button ${dall} onclick="ctl('${key}',0,true,this)">ALL ON</button>`
  +`<button ${dall} onclick="ctl('${key}',0,false,this)">ALL OFF</button></div>`;
 return `<div class="card">
   <div class="chd"><span class="name">${esc(id)}</span><span>${onl}</span></div>
   <div class="grp"><h4>outlets ${outletUnknown?'<span class="muted">(unknown)</span>':''}</h4>
     ${allctl}<div class="outlets">${outlets}</div></div>
   <div class="grp"><h4>power</h4>${power}</div>
   <div class="grp"><h4>diagnostics</h4>${diag}</div>
   <div class="res" data-key="${esc(key)}">no command sent yet</div>
 </div>`;
}
async function load(){
 try{
  const j=await (await fetch('/api/state')).json();
  const ds=j.devices||{}; const s=j.summary||{}; const o=j.observer||{};
  document.getElementById('sum').innerHTML =
   `<span>devices <b>${s.devices||0}</b></span>`+
   `<span>online <b>${s.online||0}</b></span>`+
   `<span>total power <b>${s.total_watts!=null?s.total_watts+' W':'—'}</b></span>`+
   `<span>observer <b>${o.connected?(o.fresh?'live':'stale'):'down'}</b></span>`+
   (j.control_enabled?'<span class="badge on">control ON</span>':'<span class="badge unk">control off</span>');
  document.getElementById('meta').textContent =
   'server '+new Date(j.server_time*1000).toLocaleTimeString()+
   ' · feed '+(j.updated?new Date(j.updated*1000).toLocaleTimeString():'never')+
   ' · refresh '+j.refresh+'s';
  const keys=Object.keys(ds);
  const m=document.getElementById('main');
  if(!keys.length){
   m.innerHTML='<div class="empty">No devices yet.<br>relay is in decloud mode; apply the mef/brk2 DNS redirect and power on the device.<br>'+
    'observer: '+(o.connected?'connected':'not connected')+'</div>';
   return;
  }
  // preserve any result text already shown
  const prev={}; m.querySelectorAll('.res').forEach(r=>prev[r.dataset.key]=r.textContent);
  m.innerHTML=keys.map(k=>card(k,ds[k],j.control_enabled)).join('');
  m.querySelectorAll('.res').forEach(r=>{if(prev[r.dataset.key]&&prev[r.dataset.key]!=='no command sent yet')r.textContent=prev[r.dataset.key];});
 }catch(e){document.getElementById('main').innerHTML='<div class="empty">state fetch error: '+e+'</div>';}
}
load(); setInterval(load, %REFRESH% * 1000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "mttl-dashboard/2"
    protocol_version = "HTTP/1.1"

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[dash] %s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        p = self.path.split("?", 1)[0]
        if p == "/":
            return self._send(200, PAGE.replace("%REFRESH%", str(CFG["refresh"])).encode(),
                              "text/html; charset=utf-8")
        if p == "/api/state":
            return self._send(200, json.dumps(decorate(read_state())).encode())
        if p == "/api/live":
            return self._send(200, json.dumps(LINK.snapshot() if LINK else {"error": "no link"}).encode())
        if p == "/healthz":
            return self._send(200, b"ok", "text/plain")
        return self._send(404, b'{"error":"not found"}')

    do_HEAD = do_GET

    def do_POST(self):
        if self.path.split("?", 1)[0] != "/api/control":
            return self._send(404, b'{"error":"not found"}')
        try:
            n = int(self.headers.get("Content-Length", "0"))
            req = json.loads(self.rfile.read(n) or b"{}")
        except Exception as exc:
            return self._send(400, json.dumps({"error": "bad json: %r" % exc}).encode())
        code, resp = do_control(req)
        return self._send(code, json.dumps(resp).encode())


def main(argv=None):
    global LINK
    p = argparse.ArgumentParser(description="MTTL-W01 local dashboard")
    p.add_argument("--host", default=os.environ.get("MTTL_DASHBOARD_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--state-file", default=DEFAULT_STATE_FILE)
    p.add_argument("--observer", default=DEFAULT_OBSERVER, help="relay observer HOST:PORT (plaintext MQTT)")
    p.add_argument("--refresh", type=int, default=DEFAULT_REFRESH)
    p.add_argument("--enable-control", action="store_true",
                   help="allow POST /api/control to publish outlet commands to the observer")
    p.add_argument("--self-check", action="store_true", help="validate config + template, then exit")
    args = p.parse_args(argv)

    CFG.update(state_file=args.state_file, observer=args.observer,
               refresh=max(1, args.refresh), enable_control=args.enable_control)

    assert "%REFRESH%" in PAGE and 'id="main"' in PAGE, "template sanity"
    _ = decorate(read_state())  # must not raise
    if mw is None:
        sys.stderr.write("[dash] WARNING: mqtt_wire not importable — observer link + control disabled\n")
    if T is None:
        sys.stderr.write("[dash] WARNING: mttl_telemetry not importable\n")
    if args.self_check:
        print("SELF-CHECK: PASS (state_file=%s observer=%s control=%s)"
              % (CFG["state_file"], CFG["observer"], CFG["enable_control"]))
        return 0

    if mw is not None:
        LINK = ObserverLink(CFG["observer"])
        LINK.start()

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("dashboard on http://%s:%d/  (state=%s, observer=%s, control=%s)"
          % (args.host, args.port, CFG["state_file"], CFG["observer"],
             "ENABLED" if CFG["enable_control"] else "disabled"))
    sys.stdout.flush()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
