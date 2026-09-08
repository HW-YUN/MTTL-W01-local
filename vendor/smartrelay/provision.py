#!/usr/bin/env python3
"""
SoftAP 프로비저닝 — LGAPMODE 바이너리 프로토콜 클라이언트 (범용).

순수 표준 라이브러리 소켓만 사용 — OS/네트워크 관리자(nmcli 등)에 의존하지 않음,
Linux/macOS/Windows 어디서나 동작. AP 접속은 이 스크립트가 절대 대신 하지 않는다 —
아래 안내대로 사용자가 자기 OS의 WiFi 설정에서 직접 SoftAP에 연결한 뒤 실행할 것.

검증된 사실 (README.md 기준, 실기기 실측):
  - 프로토콜은 HTTP가 아니라 raw TCP 위의 "LGAPMODE" 바이너리 헤더 프로토콜.
    호스트:포트 192.168.1.1:30300. 헤더 20바이트
    ("LGAPMODE"+"0010"+code(int32 LE)+bodysize(int32 LE)) 뒤에 body.
  - 헤더와 body는 반드시 별도의 TCP write로 나눠 보내야 한다. 하나로 합쳐 보내면
    기기가 응답 없이 무한 타임아웃된다(실측 확인된 특성) — send_request()가 이미 처리.
  - code=103 (Device Info): body 없음. 응답 code=20301(스펙상 203 아님 — strict
    equality 금지), body 70바이트: MAC(12)+Serial(20)+FW(16)+Model(16)+extra(2)+result(4).
  - code=101 (WiFi Setting): body 128바이트, SSID_EUCKR(32)+PW_EUCKR(32)+
    SSID_UTF8(32)+PW_UTF8(32), 널 패딩. 응답 code=201, result=0(성공).
  - code=102 (Reset): body 없음. 응답 code=202, result=0(성공). 직후 SoftAP가
    내려가고 기기가 지정된 홈 와이파이로 전환을 시도한다.

사용 순서:
  1) 사용자가 직접 OS WiFi 설정에서 기기 SoftAP에 접속
  2) python3 provision.py info                                    # 기기 확인(선택)
  3) python3 provision.py inject-wifi --home-ssid ... --home-password ... --yes
  4) python3 provision.py reset --yes                              # 홈 와이파이로 전환

각 명령은 실행 전에 192.168.1.1:30300 도달 가능 여부를 짧게 확인하고, 안 되면
수동 연결 방법을 안내한 뒤 종료한다(자동으로 AP에 붙으려 하지 않음).
"""

import argparse
import socket
import struct
import sys
import time

DEVICE_HOST = "192.168.1.1"
DEVICE_PORT = 30300
DEFAULT_TIMEOUT = 10

MAGIC = b"LGAPMODE"
VERSION = b"0010"

CODE_DEVICE_INFO = 103
CODE_WIFI_SET = 101
CODE_RESET = 102

RESP_DEVICE_INFO = 20301  # 스펙상 203이 아니라 실측상 20301 — strict check 금지
RESP_WIFI_SET = 201
RESP_RESET = 202


# ---------------------------------------------------------------------------
# SoftAP 접속 (실행하지 않음 — 안내만)
# ---------------------------------------------------------------------------

def print_manual_connect_hint(ssid_hint: str = "TONLY_TAP_XXXXXXX"):
    print("기기 SoftAP에 연결되어 있지 않은 것 같습니다.")
    print("OS의 WiFi 설정에서 직접 아래 네트워크에 연결한 뒤 이 명령을 다시 실행하세요:")
    print(f"  SSID     : {ssid_hint} (기기 뒷면/라벨에서 실제 SSID 확인)")
    print("  Password : 기기 뒷면/라벨이나 설명서에서 확인")
    print("이 스크립트는 AP 연결을 대신 해주지 않습니다 — 수동 연결 후 재시도하세요.")


# ---------------------------------------------------------------------------
# LGAPMODE 프로토콜 코어
# ---------------------------------------------------------------------------

def build_header(code: int, bodysize: int) -> bytes:
    return MAGIC + VERSION + struct.pack("<ii", code, bodysize)


def parse_header(raw: bytes):
    if len(raw) != 20:
        return None
    name, ver, code, bodysize = struct.unpack("<8s4sii", raw)
    return {"name": name, "version": ver, "code": code, "bodysize": bodysize}


def recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            break
        buf += chunk
    return buf


def device_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def wait_for_device(host: str, port: int, wait_timeout: int) -> bool:
    """wait_timeout<=0: 한 번만 확인. >0: 그 시간(초) 동안 재시도(사용자가 그동안
    수동으로 AP에 붙는 걸 기다려줌 — 이 함수가 연결을 시도하지는 않는다)."""
    if device_reachable(host, port):
        return True
    if wait_timeout <= 0:
        return False
    print(f"({host}:{port} 대기 중 — 최대 {wait_timeout}초, 그동안 수동으로 SoftAP에 연결하세요)")
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        time.sleep(2)
        if device_reachable(host, port):
            return True
    return False


def send_request(code: int, body: bytes, timeout: int, host: str, port: int):
    """헤더와 body를 반드시 별도의 write()로 전송 (합쳐 보내면 기기가 무응답
    타임아웃 — 실측 확인됨)."""
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.sendall(build_header(code, len(body)))
        if body:
            s.sendall(body)
        header_raw = recv_exact(s, 20)
        header = parse_header(header_raw)
        if header is None:
            return None, header_raw
        body_resp = recv_exact(s, header["bodysize"]) if header["bodysize"] > 0 else b""
        return header, body_resp


def preflight(args) -> bool:
    if wait_for_device(args.host, args.port, getattr(args, "wait", 0)):
        return True
    print_manual_connect_hint()
    return False


# ---------------------------------------------------------------------------
# code=103 Device Info
# ---------------------------------------------------------------------------

def parse_device_info(body: bytes):
    if len(body) < 70:
        return None
    mac, serial, fw, model, extra, result = struct.unpack_from("<12s20s16s16s2s i", body, 0)

    def decode(b: bytes) -> str:
        return b.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    return {
        "mac": decode(mac),
        "serial": decode(serial),
        "fw": decode(fw),
        "model": decode(model),
        "extra": extra.hex(),
        "result": result,
    }


def cmd_info(args):
    if not preflight(args):
        return 1
    try:
        header, body = send_request(CODE_DEVICE_INFO, b"", args.timeout, args.host, args.port)
    except Exception as e:
        print(f"연결/통신 실패: {e}")
        return 1
    if header is None:
        print(f"응답 헤더 파싱 실패: {body!r}")
        return 1
    print(f"응답: code={header['code']} bodysize={header['bodysize']}")
    if header["code"] != RESP_DEVICE_INFO:
        print(f"  (참고: 예상 code={RESP_DEVICE_INFO}와 다름 — 기기/FW에 따라 달라질 수 있음, strict check 안 함)")
    info = parse_device_info(body)
    if info is None:
        print(f"  body 파싱 실패(길이 {len(body)} < 70): {body!r}")
        return 1
    for k, v in info.items():
        print(f"  {k}: {v}")
    return 0


# ---------------------------------------------------------------------------
# code=101 WiFi Setting
# ---------------------------------------------------------------------------

def pad_field(s: str, size: int, encoding: str) -> bytes:
    b = s.encode(encoding)
    if len(b) > size:
        raise ValueError(f"필드 값이 {size}바이트를 초과: {s!r} ({len(b)} bytes, encoding={encoding})")
    return b + b"\x00" * (size - len(b))


def build_wifi_body(ssid: str, password: str) -> bytes:
    # 실기기 실측 레이아웃: SSID_EUCKR(32)+PW_EUCKR(32)+SSID_UTF8(32)+PW_UTF8(32)
    return (
        pad_field(ssid, 32, "euc-kr")
        + pad_field(password, 32, "euc-kr")
        + pad_field(ssid, 32, "utf-8")
        + pad_field(password, 32, "utf-8")
    )


def cmd_inject_wifi(args):
    body = build_wifi_body(args.home_ssid, args.home_password)
    print(f"code={CODE_WIFI_SET} bodysize={len(body)}")
    print(f"  SSID: {args.home_ssid!r}  PASSWORD: {'*' * len(args.home_password)}")

    if not args.yes:
        print("\n(dry-run) 실제로 기기에 전송하려면 --yes 를 추가하세요.")
        print("전송될 헤더:", build_header(CODE_WIFI_SET, len(body)))
        return 0

    if not preflight(args):
        return 1

    print(f"\n대상: {args.host}:{args.port} — 실제 전송")
    try:
        header, resp_body = send_request(CODE_WIFI_SET, body, args.timeout, args.host, args.port)
    except Exception as e:
        print(f"연결/통신 실패: {e}")
        return 1
    if header is None:
        print(f"응답 헤더 파싱 실패: {resp_body!r}")
        return 1
    result = struct.unpack_from("<i", resp_body, 0)[0] if len(resp_body) >= 4 else None
    print(f"응답: code={header['code']} bodysize={header['bodysize']} result={result}")
    if header["code"] == RESP_WIFI_SET and result == 0:
        print(">>> 성공(code=201, result=0). 다음: 'python3 provision.py reset --yes'로 홈 와이파이 전환을 트리거하세요.")
    return 0


# ---------------------------------------------------------------------------
# code=102 Reset
# ---------------------------------------------------------------------------

def cmd_reset(args):
    print(f"code={CODE_RESET} — SoftAP를 내리고 기기가 저장된 홈 와이파이로 전환을 시도합니다.")
    if not args.yes:
        print("(dry-run) 실제로 전송하려면 --yes 를 추가하세요.")
        return 0

    if not preflight(args):
        return 1

    try:
        header, resp_body = send_request(CODE_RESET, b"", args.timeout, args.host, args.port)
    except Exception as e:
        print(f"연결/통신 실패: {e}")
        return 1
    if header is None:
        print(f"응답 헤더 파싱 실패: {resp_body!r}")
        return 1
    result = struct.unpack_from("<i", resp_body, 0)[0] if len(resp_body) >= 4 else None
    print(f"응답: code={header['code']} bodysize={header['bodysize']} result={result}")
    if header["code"] == RESP_RESET and result == 0:
        print(">>> 성공(code=202, result=0). SoftAP가 곧 내려갑니다 — 홈 와이파이 쪽에서 기기를 확인하세요.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_common_device_args(sub_p):
    sub_p.add_argument("--host", default=DEVICE_HOST)
    sub_p.add_argument("--port", type=int, default=DEVICE_PORT)
    sub_p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="응답 대기 타임아웃(초)")
    sub_p.add_argument("--wait", type=int, default=0,
                        help="기기가 아직 응답하지 않으면 이 초만큼 재시도 대기(그동안 사용자가 수동으로 SoftAP에 연결). 기본 0=대기 안 함")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    p_info = sub.add_parser("info", help="code=103 Device Info 조회")
    add_common_device_args(p_info)
    p_info.set_defaults(func=cmd_info)

    p_inj = sub.add_parser("inject-wifi", help="code=101 홈 WiFi 자격증명 주입 (위험 — --yes 필요)")
    p_inj.add_argument("--home-ssid", required=True)
    p_inj.add_argument("--home-password", required=True)
    add_common_device_args(p_inj)
    p_inj.add_argument("--yes", action="store_true", help="실제로 전송 (기본은 dry-run)")
    p_inj.set_defaults(func=cmd_inject_wifi)

    p_reset = sub.add_parser("reset", help="code=102 Reset — 홈 와이파이 전환 트리거 (위험 — --yes 필요)")
    add_common_device_args(p_reset)
    p_reset.add_argument("--yes", action="store_true", help="실제로 전송 (기본은 dry-run)")
    p_reset.set_defaults(func=cmd_reset)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
