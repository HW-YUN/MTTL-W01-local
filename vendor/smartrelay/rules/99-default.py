#!/usr/bin/env python3
"""
MTTL-W01 기본(fallback) 응답 규칙 — 여기가 기기별 하드코딩이 전부 모이는 곳이다.
relay.py(엔진)는 이 파일 내용을 몰라도 동작해야 한다.

파일명이 `99-`라 항상 최저 우선순위(fallback) — 특정 기기만 다르게 다루고 싶으면 이 파일보다
앞서 정렬되는 이름(`10-xxx.py` 등)의 파일을 추가해서 override.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import re
import threading
import time

from rules_engine import HttpResponse, PublishSpec

_log = logging.getLogger(__name__)

# --- 이 기기 실측값 (ANALYSIS.md / captures 기준) ---
VENDOR_CODE = "0000564"
DEVICE_MODEL = "MTTL-W01"
FW_VERSION_DEFAULT = "0.1.60"

# 모든 캡처(다른 기기/다른 세션 포함)에서 동일하게 관측된 미들노드(MN-CSE) ID — 장치별이
# 아니라 인프라 쪽 고정값으로 보임. device_control 봉투의 fr에 그대로 쓴다.
MN_CSE_ID = "MN_CSE-S-7969586b38-OGSS"

_B62 = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _b62(data: bytes, length: int) -> str:
    n = int.from_bytes(data, "big")
    out = []
    while len(out) < length:
        n, r = divmod(n, 62)
        out.append(_B62[r])
    return "".join(reversed(out))


def _derive_credentials(device_key: str):
    """기기 고유값(mac/시리얼)마다 안정적으로(재부팅해도 동일) 다른 entityId/enrmtKey/token을
    만든다 — Decloud에선 이 값을 발급하는 게 우리 자신이므로, 형식만 맞으면 되고 원본 식별자를
    그대로 노출할 필요가 없다(해시로 감춤). 여러 대가 동시에 이 서버에 붙어도 서로 다른 MQTT
    client_id/password를 받게 되어 registry/topic 네임스페이스가 겹치지 않는다."""
    seed = device_key.strip().upper().encode()
    h = hashlib.sha256(seed).digest()
    entity_mid = _b62(h[:8], 10)
    enrmt_key = _b62(hashlib.sha256(seed + b"enrmt").digest(), 22)
    token = _b62(hashlib.sha256(seed + b"token1").digest()[:6], 7) + "-" + \
        _b62(hashlib.sha256(seed + b"token2").digest(), 35)
    entity_id = f"ASN_CSE-D-{entity_mid}-MTAP"
    return entity_id, enrmt_key, token


_MAC_RE = re.compile(rb"<mac>([^<]*)</mac>")
_SERIAL_RE = re.compile(rb"<deviceSerialNo>([^<]*)</deviceSerialNo>")


# --------------------------------------------------------------------------
# :443 POST /mef — MEF 인증 성공 응답 (구조는 captures/ 골든 그대로, 자격증명만 기기별로 발급)
# --------------------------------------------------------------------------

def on_http_request(ctx, method, path, headers, body):
    if method != b"POST" or path != b"/mef":
        return None
    m = _MAC_RE.search(body) or _SERIAL_RE.search(body)
    device_key = m.group(1).decode(errors="replace") if m else "unknown-device"
    entity_id, enrmt_key, token = _derive_credentials(device_key)
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        "<authdata>"
        f"<http><enrmtKey>{enrmt_key}</enrmtKey><entityId>{entity_id}</entityId>"
        f"<token>{token}</token></http>"
        f"<coap><enrmtKey>{enrmt_key}</enrmtKey><entityId>{entity_id}</entityId>"
        f"<token>{token}</token><encryptionMethod>TLS_PSK_WITH_AES_128_CCM_8</encryptionMethod></coap>"
        f"<mqtt><enrmtKey>{enrmt_key}</enrmtKey><entityId>{entity_id}</entityId>"
        f"<token>{token}</token></mqtt>"
        "</authdata>"
    ).encode()
    return HttpResponse(
        status_line=b"HTTP/1.1 200 OK",
        headers={"Content-Type": "application/xml;charset=UTF-8", "Connection": "close"},
        body=xml,
    )


# --------------------------------------------------------------------------
# 세션별 관측 상태 — 지금까지는 부트스트랩 이후 텔레메트리(STATUS/METER/...)를 전부
# 버렸었다. client_id별로 최소한의 상태(전원/전력/설정/알람 + device_control에 필요한
# device_id 메타)를 기억해둔다. DB가 아니라 이 프로세스가 살아있는 동안만의 캐시다.
# --------------------------------------------------------------------------

_STATE_LOCK = threading.Lock()
_DEVICE_STATE: dict[bytes, dict] = {}


def _device_state(client_id: bytes) -> dict:
    with _STATE_LOCK:
        return _DEVICE_STATE.setdefault(client_id, {})


# --------------------------------------------------------------------------
# :18831 — oneM2M CSE 부트스트랩 시퀀스 (captures/ 골든 구조 그대로, ri/ct/lt만 그때그때 생성)
# --------------------------------------------------------------------------

def _now() -> str:
    return time.strftime("%Y%m%dT%H%M%S", time.gmtime())


NEVER_EXPIRES = "99991231T000000"

_RI_LOCK = threading.Lock()
_RI_SEQ = random.randint(0, 89999)


def _new_ri(ty) -> str:
    """리소스 ID 생성기. random.randint를 매 호출마다 새로 뽑으면(구버전) 동시 요청 시
    이론상 충돌 가능 — 프로세스 전역 카운터로 바꿔서 충돌을 원천 차단한다."""
    global _RI_SEQ
    try:
        ty_text = str(int(ty))
    except (TypeError, ValueError):
        ty_text = str(ty)
    with _RI_LOCK:
        _RI_SEQ = (_RI_SEQ + 1) % 90000
        n = _RI_SEQ + 10000
    return f"ri_{ty_text}_{n:05d}"


def _reply_envelope(req: dict, pc: dict) -> dict:
    """요청의 rqi를 그대로 echo, to/fr을 뒤집어서 응답 봉투를 만든다.
    (captures 전수 조사로 확인된 패턴: 응답 fr = 요청 to의 첫 세그먼트, 응답 to = 요청 fr.)"""
    req_to = (req.get("to") or "").strip("/")
    base = "/" + req_to.split("/")[0] if req_to else ""
    return {
        "rsc": 2000 if req.get("rqi") == "cseBaseRetrieve" else 2001,
        "rqi": req.get("rqi"),
        "to": req.get("fr"),
        "fr": base,
        "pc": pc,
    }


def _session_id_for(client_id: bytes) -> str:
    """captures 관찰: SID-<entityId 중간 세그먼트 마지막 7글자>-MTAP 형식."""
    cid = client_id.decode(errors="replace")
    parts = cid.split("-")
    hashpart = parts[2] if len(parts) >= 4 else cid
    suffix = hashpart[-7:] if len(hashpart) >= 7 else hashpart
    return f"SID-{suffix}-MTAP"


def on_message(ctx, msg, topic):
    rqi = msg.get("rqi") or ""
    pc = msg.get("pc") or {}

    if rqi == "cseBaseRetrieve":
        reply_pc = {
            "m2m:cb": {
                "ri": "cb-1", "rn": "cb-1", "ty": 5, "csi": "/IN_CSE-BASE-1", "cst": 1, "pi": "",
                "ct": "20260101T000000", "lt": "20260101T000000",
                "srt": [1, 2, 3, 4, 5, 9, 13, 14, 16, 23], "poa": ["mqtt"],
            }
        }
        return [_envelope_to_pub(ctx, _reply_envelope(msg, reply_pc))]

    if rqi == "smartplugbootstrap":
        cin = pc.get("m2m:cin") or {}
        req_con = _b64_json_decode(cin.get("con"))
        device = (req_con or {}).get("content", {}).get("device", {})
        if ctx.client_id:
            st = _device_state(ctx.client_id)
            st["device_id"] = device.get("id") or st.get("device_id", "")
            st["device_type"] = device.get("type", "MULTITAP")
            st["device_model"] = DEVICE_MODEL
            # 새 접속의 부트스트랩 — 지난 접속에서 쌓인 완전성 판단 상태는 리셋.
            st["_status_report_seen"] = set()
            st["status_complete"] = False
            st["status_missing_selectors"] = sorted(_STATUS_REQUIRED_SELECTORS)
        ack = {
            "header": {
                "version": "v2", "vendor_code": VENDOR_CODE, "api_key": "device_bootstrap",
                "device_id": device.get("id", ""), "device_uuid": device.get("id", ""),
                "device_type": device.get("type", "MULTITAP"), "device_model": DEVICE_MODEL,
                "ret_code": "200", "message": "success",
                "session_id": _session_id_for(ctx.client_id or b""),
            }
        }
        reply_pc = {"m2m:cin": {"et": NEVER_EXPIRES, "cnf": cin.get("cnf", "text/plain: 0"),
                                 "con": _b64_json_encode(ack)}}
        return [_envelope_to_pub(ctx, _reply_envelope(msg, reply_pc))]

    if (rqi.startswith("plugeventreport") or rqi.startswith("controlreport")
            or rqi.startswith("devicecontrol-") or "control-report" in rqi):
        # 상태 리포트 / device_control ack(우리가 보낸 rqi를 기기가 그대로 echo — 실측
        # 확인됨: "controlreport"/"control-report" 문자열이 rqi에 들어있는 게 아니라
        # "devicecontrol-<우리가 붙인 번호>"를 그대로 돌려준다) — 응답 PUBLISH는 불필요
        # (PUBACK만으로 충분, 엔진이 이미 보냄). 내용을 해석해서 상태 캐시에 남겨둔다.
        _handle_telemetry(ctx, pc, rqi)
        return None

    try:
        op = int(msg.get("op"))
    except (TypeError, ValueError):
        op = None
    if op == 1 and pc:
        # 범용 리소스 생성 응답 — remoteCSECreate/accessControlPolicyCreate/nodeCreate/
        # firmwareCreate처럼 rqi별로 일일이 하드코딩하지 않고, "op=1(create) + pc 있음"이면
        # 전부 이 자리에서 처리한다. 새 create 타입이 와도 이 파일을 안 고쳐도 됨.
        now = _now()
        response_pc = {}
        for key, value in pc.items():
            if isinstance(value, dict):
                resource = dict(value)
                resource.update(ri=_new_ri(msg.get("ty")), ct=now, lt=now, et=NEVER_EXPIRES)
                response_pc[key] = resource
            else:
                response_pc[key] = value
        return [_envelope_to_pub(ctx, _reply_envelope(msg, response_pc))]

    _log.warning("미인식 rqi=%r op=%r (PUBACK만 보내고 응답 없음)", rqi, msg.get("op"))
    return None


def _envelope_to_pub(ctx, envelope: dict) -> PublishSpec:
    topic = f"/oneM2M/resp/{(ctx.client_id or b'').decode(errors='replace')}/IN_CSE-BASE-1".encode()
    payload = json.dumps(envelope).encode()
    return PublishSpec(topic=topic, payload=payload, qos=1)


def _b64_json_decode(con):
    if not con:
        return None
    try:
        return json.loads(base64.b64decode(con))
    except Exception:
        return None


def _b64_json_encode(obj) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


# --------------------------------------------------------------------------
# 텔레메트리(기기->서버 STATUS/METER/CONFIGURATION/ALARM 이벤트) 해석
#
# 이전엔 plugeventreport/controlreport가 오면 그냥 버렸다. 파싱 규칙 자체는 이 저장소
# captures/로 직접 확인한 필드명(command/switchBinaryN/meterN_02/...)에 근거한다.
# --------------------------------------------------------------------------

_POWER_RE = re.compile(r"^POWER([1-4]?)_(?:EVENT|SET)$")
_STATUS_REPORT_RE = re.compile(r"^STATUS([1-4]?)_REPORT$")
_STATUS_EVENT_RE = re.compile(r"^STATUS([1-4]?)_EVENT$")
_CONFIG_RE = re.compile(r"^CONFIGURATION([1-4]?)_(?:EVENT|SET|REPORT)$")

_STATUS_REQUIRED_SELECTORS = frozenset(range(5))  # 0(전체 집계) + 1~4번 출력

# 같은 알람 코드라도 "현재 상태"와 "방금 일어난 이벤트"의 의미가 다르다 — 0x46/0x80은
# state로 보면 이미 정상 복귀했지만, event로 보면 "방금 복구됐다"는 전이(transition)다.
# 하나로 합쳐버리면 이 구분이 사라져서, 지금 정상인지 방금 정상이 됐는지를 못 가른다.
_ALARM_STATE_TABLE = {
    0x00: "normal",
    0x42: "overheat_trip",
    0x44: "overheat_warning",
    0x46: "normal",
    0x80: "normal",
    0x86: "overload_trip",
    0x88: "overload_warning",
}
_ALARM_EVENT_TABLE = {
    0x00: "normal",
    0x42: "overheat_trip",
    0x44: "overheat_warning",
    0x46: "overheat_recovery",
    0x80: "overload_recovery",
    0x86: "overload_trip",
    0x88: "overload_warning",
}


def _decode_alarm(raw: int) -> dict:
    return {
        "raw": f"{raw:02X}",
        "state": _ALARM_STATE_TABLE.get(raw, f"unknown_0x{raw:02X}"),
        "event": _ALARM_EVENT_TABLE.get(raw, f"unknown_0x{raw:02X}"),
    }


def _hex_int(value):
    if value is None:
        return None
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return None


def _switch_bool(value):
    raw = str(value or "").strip().upper()
    if raw == "00":
        return False
    if raw == "FF":
        return True
    return None


def _decode_configuration(raw):
    s = str(raw or "").strip().upper()
    if not re.fullmatch(r"[0-9A-F]{8}", s):
        return None
    threshold = int(s[:6], 16)
    enable_byte = int(s[6:], 16)
    if enable_byte not in (0, 1):
        return None
    return {"raw": s, "threshold_centiwatt": threshold, "threshold_watts": threshold / 100.0,
            "enabled": enable_byte == 1}


def _parse_telemetry_params(params: list) -> dict:
    power, meter_watts, energy_raw, configuration, alarms, standby_state = {}, {}, {}, {}, {}, {}
    wifi = {}
    malformed_switches = []
    status_report_seen: set[int] = set()

    for p in params or []:
        if not isinstance(p, dict):
            continue
        cmd = str(p.get("command", ""))

        # SSID/RSSI는 특정 command 전용이 아니라 거의 모든 파라미터에 같이 실려온다.
        if "SSID" in p:
            wifi["ssid"] = p["SSID"]
        if "RSSI" in p:
            try:
                wifi["rssi_dbm"] = int(str(p["RSSI"]))
            except ValueError:
                wifi["rssi_raw"] = p["RSSI"]

        m = _POWER_RE.match(cmd)
        if m:
            suffix = m.group(1) or ""
            n = int(suffix or 0)
            key = f"switchBinary{suffix}"
            v = _switch_bool(p.get(key))
            if v is not None:
                power[n] = v
                # POWER_EVENT(접미사 없음)는 물리 ALL 버튼 — 출력 1~4 전체에 동시 반영.
                if n == 0 and cmd == "POWER_EVENT":
                    for outlet in range(1, 5):
                        power[outlet] = v
            elif key in p:
                malformed_switches.append({"command": cmd, "key": key, "value": p.get(key)})

        status_report_match = _STATUS_REPORT_RE.match(cmd)
        status_match = status_report_match or _STATUS_EVENT_RE.match(cmd)
        if status_match:
            suffix = status_match.group(1) or ""
            n = int(suffix or 0)
            if status_report_match:
                status_report_seen.add(n)
            key = f"switchBinary{suffix}"
            v = _switch_bool(p.get(key))
            if v is not None:
                power[n] = v
            elif key in p:
                malformed_switches.append({"command": cmd, "key": key, "value": p.get(key)})

        if cmd == "METER_CUR_STATUS_EVENT" or status_match:
            for n in range(5):
                key = "meter_02" if n == 0 else f"meter{n}_02"
                raw = _hex_int(p.get(key))
                if raw is not None:
                    meter_watts[n] = raw / 100.0

        if cmd == "METER_ACC_STATUS_EVENT":
            for n in range(5):
                key = "meter_00" if n == 0 else f"meter{n}_00"
                raw = _hex_int(p.get(key))
                if raw is not None:
                    energy_raw[n] = raw

        cm = _CONFIG_RE.match(cmd)
        if cm:
            suffix = cm.group(1) or ""
            if suffix and f"configuration{suffix}" in p:
                decoded = _decode_configuration(p[f"configuration{suffix}"])
                if decoded:
                    configuration[int(suffix)] = decoded

        if cmd == "DEVICE_STATUS_EVENT":
            # 출력별 대기전력(standby) 상태 — CONFIGURATION*_SET으로 컷오프를 걸어둔
            # 출력이 실제로 대기 상태로 전환됐는지는 이 이벤트로만 알 수 있다.
            for n in range(1, 5):
                raw = _hex_int(p.get(f"event{n}"))
                if raw is not None:
                    standby_state[n] = {
                        "raw": f"{raw:02X}",
                        "state": "standby" if raw == 0 else "active" if raw == 1 else f"unknown_0x{raw:02X}",
                        "active": raw == 1,
                        "standby": raw == 0,
                    }

        if cmd == "ALARM_EVENT":
            for n in range(5):
                key = "event" if n == 0 else f"event{n}"
                raw = _hex_int(p.get(key))
                if raw is not None:
                    alarms[n] = _decode_alarm(raw)

    # 주의: 완전성(status_complete) 판단은 여기서 못 한다 — 실측상 STATUS_REPORT는
    # 셀렉터(0~4)당 별도 PUBLISH로 하나씩 온다(한 이벤트에 5개가 다 안 들어있음). 그래서
    # status_report_selectors는 "이번 메시지 하나"의 관측치만 돌려주고, 여러 메시지에 걸친
    # 누적/완전성 판단은 호출자(_handle_telemetry, 세션별 상태를 들고 있음)가 한다.
    return {
        "power": power, "meter_watts": meter_watts, "energy_raw": energy_raw,
        "configuration": configuration, "alarms": alarms, "standby_state": standby_state,
        "wifi": wifi,
        "status_report_selectors": sorted(status_report_seen),
        "malformed_switches": malformed_switches,
    }


def _handle_telemetry(ctx, pc: dict, rqi: str = ""):
    cin = pc.get("m2m:cin") or {}
    inner = _b64_json_decode(cin.get("con"))
    if not inner:
        return
    content = inner.get("content") or {}
    notif = content.get("notification") if isinstance(content.get("notification"), dict) else None
    report = content.get("cmd_report") if isinstance(content.get("cmd_report"), dict) else None
    cid_str = (ctx.client_id or b"").decode(errors="replace")

    if report is not None:
        # 우리가 보낸 device_control의 ack — 실측 확인: parameters 없이 result/rpt_id만
        # 온다("성공했다"는 accept 신호일 뿐, 물리적으로 실제 바뀌었는지는 아니다 —
        # 그건 뒤따라오는 별도 POWERn_EVENT 텔레메트리로 확인해야 함).
        result = report.get("result")
        if ctx.client_id:
            _device_state(ctx.client_id)["last_cmd_report"] = {
                "rqi": rqi, "result": result, "rpt_id": report.get("rpt_id"),
            }
        if result == 0:
            _log.info("device_control 응답 %s: rqi=%s 성공", cid_str, rqi)
        else:
            _log.warning("device_control 응답 %s: rqi=%s 실패/미확인(result=%r)", cid_str, rqi, result)

    if notif is not None:
        params = notif.get("parameters")
    elif report is not None:
        params = report.get("parameters")
    else:
        params = None
    if isinstance(params, dict):
        params = [params]
    if not params:
        return

    parsed = _parse_telemetry_params(params)
    merge_keys = ("power", "meter_watts", "energy_raw", "configuration", "alarms", "standby_state", "wifi")

    if ctx.client_id:
        st = _device_state(ctx.client_id)
        changed = {}
        for key in merge_keys:
            new_values = parsed.get(key) or {}
            if not new_values:
                continue
            bucket = st.setdefault(key, {})
            if key == "wifi":
                # RSSI는 이벤트마다 몇 dBm씩 흔들리는 노이즈라 그대로 diff에 넣으면 다시
                # 스팸이 된다 — SSID(접속 AP)가 실제로 바뀔 때만 의미 있는 변화로 본다.
                diff = {k: v for k, v in new_values.items() if k == "ssid" and bucket.get(k) != v}
            else:
                diff = {k: v for k, v in new_values.items() if bucket.get(k) != v}
            if diff:
                changed[key] = diff
            bucket.update(new_values)
        if changed:
            _log.info("텔레메트리 변경 %s: %s", cid_str, changed)

        # STATUS_REPORT는 셀렉터당 별도 메시지로 오므로, 이번 접속에서 지금까지 본
        # 셀렉터 집합에 계속 합집합해서 5개가 다 모였는지를 세션 단위로 판단한다.
        # 이 집합은 smartplugbootstrap(재접속마다 다시 옴)에서 새로 초기화된다.
        seen = st.setdefault("_status_report_seen", set())
        seen.update(parsed["status_report_selectors"])
        st["status_complete"] = _STATUS_REQUIRED_SELECTORS.issubset(seen)
        st["status_missing_selectors"] = sorted(_STATUS_REQUIRED_SELECTORS - seen)

    if parsed["malformed_switches"]:
        _log.warning("이상값 감지 %s: %s", cid_str, parsed["malformed_switches"])

    # 값이 바뀌었든 아니든 매번 원본 파싱 결과 전체를 보고 싶을 때만(--log-level trace) 나옴 —
    # 기본(info)에서는 위의 "텔레메트리 변경"처럼 실제로 바뀐 것만 보인다.
    raw_summary = ", ".join(f"{k}={parsed[k]}" for k in merge_keys if parsed.get(k))
    if raw_summary:
        _log.trace("텔레메트리 원본 %s: %s", cid_str, raw_summary)


# --------------------------------------------------------------------------
# observer 로컬 주입 -> 실제 device_control oneM2M PUBLISH로 번역
#
# 이 봉투 구조는 이 저장소 자체 캡처가 아니라 MTTL-W01_Toolkit의 2026-08-28
# "LIVE-WIRE"(실기기 Voltra Cloud->device 트래픽 캡처 기반) 구현을 참고해서 맞춘 것이다.
# 예전 버전(순수 추론)과 달라진 점: cmd_id가 명령 문자열이 아니라 정수(1=제어,2=상태조회),
# inner에 header/notification 블록 필요, parameter의 command에 _SET 접미사 필요,
# 봉투 fr이 IN_CSE-BASE-1이 아니라 미들노드(MN_CSE_ID). POWER 제어(on/off)는 실기기
# 캡처로 성공 확인됨(2026-08-28, result:0 + 후속 POWERn_EVENT 물리 확인까지 일치) —
# status/configuration은 아직 이 저장소 자체 와이어로 검증 안 됐으니 처음 시험할 때는
# 반드시 사람이 지켜볼 것.
# --------------------------------------------------------------------------

def _control_header(ctx, session_id: str) -> dict:
    st = _device_state(ctx.device_client_id) if ctx.device_client_id else {}
    device_id = st.get("device_id")
    if not device_id:
        # 이 세션의 smartplugbootstrap을 못 봤다(예: relay 재시작 사이 재접속) — 실제 MAC
        # 기반 device_id를 모르니 entity 중간 세그먼트로 대체한다. 경고는 세션당 한 번만
        # (명령 낼 때마다 반복하면 로그만 시끄럽고 정보량은 없음).
        cid = (ctx.device_client_id or b"").decode(errors="replace")
        parts = cid.split("-")
        device_id = (parts[2] if len(parts) >= 4 else cid).upper()
        if not st.get("_device_id_fallback_warned"):
            _log.warning("device_id 미확보(부트스트랩 미관측, client=%s) — %s 로 추정해서 진행", cid, device_id)
            st["_device_id_fallback_warned"] = True
    return {
        "version": "v2", "vendor_code": VENDOR_CODE, "api_key": "device_control",
        "session_id": session_id, "device_id": device_id, "device_uuid": device_id,
        "device_type": st.get("device_type", "MULTITAP"), "device_model": st.get("device_model", DEVICE_MODEL),
    }, device_id


_CTRL_LOCK = threading.Lock()
_CTRL_SEQ = random.randint(0, 89999)


def _new_devicecontrol_rqi() -> str:
    """_new_ri와 같은 이유(동시 요청 충돌 방지)로 카운터 사용 — oneM2M rqi는 흔히
    idempotency key로도 쓰이므로, 겹치면 device가 두 번째 명령을 중복으로 보고 그냥
    무시할 위험이 있다(단순 로그 혼동보다 심각함)."""
    global _CTRL_SEQ
    with _CTRL_LOCK:
        _CTRL_SEQ = (_CTRL_SEQ + 1) % 90000
        n = _CTRL_SEQ + 10000
    return f"devicecontrol-{n}"


def _build_control_envelope(ctx, client_id: str, cmd_id: int, parameters: list) -> dict:
    session_id = _session_id_for(ctx.device_client_id or b"")
    header, device_id = _control_header(ctx, session_id)
    inner = {
        "header": header,
        "type": "control-request",
        "content": {"device": {"uuid": device_id}, "cmd_request": {"cmd_id": cmd_id, "parameters": parameters}},
        "notification": {"noti_type": "device_control_noti_event"},
    }
    return {
        "op": "1",
        "to": f"/{client_id}",
        "fr": f"/{MN_CSE_ID}",
        "ty": 4,
        "rqi": _new_devicecontrol_rqi(),
        "pc": {"m2m:cin": {"cnf": "text/plain: 0", "con": _b64_json_encode(inner)}},
    }


def on_local_inject(ctx, cmd):
    """observer로 들어온 JSON 커맨드를 device_control PUBLISH로 번역한다.

    지원하는 action:
      {"outlet": 1, "on": true}                          # action 생략시 기본 "power". outlet 0=전체
      {"action": "power", "outlet": 1, "on": true}        # 위와 동일, 명시형
      {"action": "status"}                                # STATUS_GET — 즉시 상태 보고 요청
      {"action": "configuration", "outlet": 1,
       "threshold_centiwatt": 500, "enabled": true}       # 대기전력 컷오프 임계값 설정(1~4번만)
    """
    if not ctx.device_client_id:
        _log.warning("주입 대상 기기 세션 없음 — 무시")
        return None

    action = cmd.get("action", "power")
    client_id = ctx.device_client_id.decode(errors="replace")

    if action == "status":
        cmd_id, parameters = 2, [{"command": "STATUS_GET"}]

    elif action == "configuration":
        outlet = int(cmd.get("outlet", 1))
        if outlet not in range(1, 5):
            _log.warning("configuration outlet은 1~4만 지원(받음: %d) — 무시", outlet)
            return None
        threshold = int(cmd.get("threshold_centiwatt", 0))
        if not 0 <= threshold <= 0xFFFFFF:
            _log.warning("threshold_centiwatt은 0~%d 범위(받음: %d) — 무시", 0xFFFFFF, threshold)
            return None
        enabled = bool(cmd.get("enabled", False))
        raw = f"{threshold:06X}{1 if enabled else 0:02X}"
        cmd_id = 1
        parameters = [{"command": f"CONFIGURATION{outlet}_SET", f"configuration{outlet}": raw}]

    elif action == "power":
        outlet = int(cmd.get("outlet", 1))
        if outlet not in range(0, 5):
            _log.warning("power outlet은 0(전체)~4만 지원(받음: %d) — 무시", outlet)
            return None
        on = bool(cmd.get("on", False))
        suffix = "" if outlet == 0 else str(outlet)
        cmd_id = 1
        parameters = [{"command": f"POWER{suffix}_SET", f"switchBinary{suffix}": "FF" if on else "00"}]

    else:
        _log.warning("알 수 없는 action=%r — 무시", action)
        return None

    envelope = _build_control_envelope(ctx, client_id, cmd_id, parameters)
    topic = f"/oneM2M/req/IN_CSE-BASE-1/{client_id}".encode()
    return [PublishSpec(topic=topic, payload=json.dumps(envelope, separators=(",", ":")).encode(), qos=1)]
