#!/usr/bin/env python3
"""
CA-swap 투명 릴레이 + rules 기반 로컬(decloud) 응답 엔진 — 완전 동적. 도메인을 미리 알 필요 없다.

실측으로 확인된 다음 성질을 이용한다:
  - `:80`에서 내려주는 CA는 내용 검증이 없다 — 우리 CA를 그대로 심으면 기기가 새
    신뢰앵커로 설치한다. 단, 연결이 열리자마자 "즉시" 응답해야 한다(요청을 다 읽고
    나서 응답하면 거부됨 — 실측된 타이밍 특성). CA 자체는 도메인과 무관하므로
    이 응답은 항상 똑같다.
  - TLS 포트는 그 CA로 실제 서명된 리프인지만 검증한다 — 우리 CA로 서명한 리프면
    기기가 완전히 신뢰하고 TLS를 맺는다.

동작 방식:
  1) 루트 CA를 한 번만 만들어서 계속 재사용(--cert-dir에 저장).
  2) TLS 연결이 들어오면 ClientHello의 SNI(암호화되기 전 평문 필드)로 어떤
     도메인인지 알아낸다. 그 도메인용 리프 인증서가 없으면 그 자리에서 방금 만든
     CA로 서명해서 만들고(캐싱, 다음부턴 재사용), 그걸로 device 쪽 TLS를 종단한다.
  3) --dns로 지정한 DNS 서버에 그 도메인을 직접 질의해서 실제 IP를 얻는다.
     성공하면 그 IP로(포트는 --port 매핑 참고) 재암호화 연결해서 투명 릴레이(**Proxy**).
     --dns를 안 줬거나 조회에 실패하면 업스트림에 연결하지 않고, 대신 **rules/*.py**로
     HTTP/MQTT를 직접 서빙한다(**Decloud**) — rules가 아무것도 응답 안 하면 그냥 관찰만.

*** 엔진(이 파일)엔 기기별 하드코딩이 없다 ***
"무엇에 어떻게 응답할지"는 전부 `rules/*.py`(파일명 알파벳순, 핫리로드) 소관 —
계약은 rules_engine.py 참고. 이 엔진은 TLS 종단/HTTP·MQTT 프레이밍/DNS/rules 디스패치/
observer(로컬 평문 주입 포트)만 담당한다.

*** SNI를 안 보내는 기기가 실제로 있다(실측됨) ***
--default-domain 으로 SNI 없을 때 쓸 도메인을 지정할 것.

*** 전제조건: DNS 리다이렉션이 반드시 필요하다 (기기 쪽) ***
기기가 조회하는 도메인의 DNS 응답이 "이 스크립트를 돌리는 머신의 LAN IP"로 나가야 한다.

*** --dns는 "진짜" 조회처여야 한다 — 기기용 DNS 리다이렉션과 반대 역할, 헷갈리지 말 것 ***

*** 전제조건: 포트 독점 필요 + 관리자 권한 (1024 미만 포트) ***

*** 전제조건: provision.py보다 먼저 떠 있어야 한다 ***

*** 안전 ***
Proxy 모드(--dns 지정)에서는 실서버 응답이 그대로 기기에 전달된다. 화면 로그를 보면서
수상한 응답(펌웨어/OTA 지시 등)이 보이면 즉시 프로세스를 죽일 것 — 자동 차단은 없다.

*** observer / 로컬 주입 + 실시간 관찰 ***
--observer HOST:PORT 를 주면 평문 MQTT 리스너를 하나 더 연다(로컬신뢰망 전용, 반드시 방화벽
으로 보호할 것). 여기 붙은 클라이언트는 두 가지를 한다:
  1) 관찰 — 접속해있는 동안 기기 세션(:18831, Proxy든 Decloud든)에 오가는 모든 MQTT
     PUBLISH를 그대로 tap받는다(파일 캡처 없이 실시간으로 보는 용도).
  2) 주입 — PUBLISH한 JSON 커맨드(예: {"outlet":1,"on":true})가 rules/*.py의
     on_local_inject()로 번역되어 실제 기기 세션에 그대로 들어간다. POWER 제어(on/off)는
     실기기 캡처로 성공 확인됨(2026-08-28) — 다른 action(status/configuration)은 아직
     이 저장소 자체 와이어로 검증 안 됐으니 처음 시험할 땐 반드시 관찰할 것.

*** 파일 캡처는 기본 꺼짐 ***
--capture-dir 을 명시적으로 줄 때만 device_to_upstream.bin/upstream_to_device.bin 로 저장한다.
평소엔 위 observer로 필요할 때만 들여다볼 것 — 디스크에 아무것도 안 남기는 게 기본값이다.

사용:
  python3 relay.py serve -p 80... (아래 예시는 --help 참고)
  python3 relay.py gen-ca --force
"""

import argparse
import json
import logging
import os
import random
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Optional

try:
    import tomllib  # 표준 라이브러리(Python 3.11+) — 설정 파일 파싱용, 추가 의존성 없음
except ModuleNotFoundError:
    sys.exit("Python 3.11 이상이 필요합니다(tomllib 표준 라이브러리 사용).")

import mqtt_session
import mqtt_wire as mw
from rules_engine import LOG_LEVELS, HttpResponse, PublishSpec, RulesHandle

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

DEFAULT_CERT_DIR = os.path.join(SCRIPT_DIR, "certs")
DEFAULT_RULES_DIR = os.path.join(SCRIPT_DIR, "rules")
DEFAULT_CONFIG_NAME = "smartrelay.toml"


# ---------------------------------------------------------------------------
# 설정 파일 — 실행 시점의 working dir에서 읽는다(도커/앱 배포시 볼륨 마운트로
# smartrelay.toml 하나만 갈아끼우면 되게). 값 우선순위: CLI 인자 > 설정 파일 > 기본값.
# ---------------------------------------------------------------------------

def load_config(path: Optional[str]) -> dict:
    """--config로 명시하면 그 경로를 반드시 읽고, 안 주면 현재 working dir의
    smartrelay.toml을 있으면 읽고 없으면 그냥 빈 설정으로 취급한다."""
    explicit = path is not None
    if path is None:
        path = os.path.join(os.getcwd(), DEFAULT_CONFIG_NAME)
    if not os.path.exists(path):
        if explicit:
            sys.exit(f"설정 파일을 찾을 수 없음: {path}")
        return {}
    with open(path, "rb") as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            sys.exit(f"설정 파일 파싱 실패({path}): {e}")
    print(f"[config] {path} 로드함")
    return data

_leaf_lock = threading.Lock()


# ---------------------------------------------------------------------------
# 인증서: CA는 한 번만, 리프는 도메인별로 처음 볼 때 생성 후 캐싱
# ---------------------------------------------------------------------------

def _run_openssl(args):
    try:
        subprocess.run(["openssl"] + args, check=True, capture_output=True)
    except FileNotFoundError:
        sys.exit("openssl 실행 파일을 찾을 수 없습니다. 설치 후 다시 시도하세요.")
    except subprocess.CalledProcessError as e:
        sys.exit(f"openssl 실패: {' '.join(args)}\n{e.stderr.decode(errors='replace')}")


def _ca_paths(cert_dir):
    return os.path.join(cert_dir, "ca.crt"), os.path.join(cert_dir, "ca.key")


def ensure_ca(cert_dir: str, days: int = 7300, force: bool = False):
    ca_crt, ca_key = _ca_paths(cert_dir)
    os.makedirs(cert_dir, exist_ok=True)
    if not force and os.path.exists(ca_crt) and os.path.exists(ca_key):
        print(f"[ca] 기존 루트 CA 재사용: {ca_crt}")
        return ca_crt, ca_key

    print(f"[ca] 새 루트 CA 생성: {ca_crt}")
    with tempfile.TemporaryDirectory() as td:
        ca_cnf = os.path.join(td, "ca.cnf")
        with open(ca_cnf, "w") as f:
            f.write(
                "[req]\ndistinguished_name = dn\nx509_extensions = ext\nprompt = no\n"
                "[dn]\nC = KR\nO = Local MITM CA\nCN = Local MITM Root CA\n"
                "[ext]\nbasicConstraints = critical,CA:TRUE\n"
                "keyUsage = critical,digitalSignature,keyCertSign,cRLSign\n"
                "subjectKeyIdentifier = hash\n"
            )
        _run_openssl(["genrsa", "-out", ca_key, "2048"])
        _run_openssl(["req", "-new", "-x509", "-key", ca_key, "-out", ca_crt,
                      "-days", str(days), "-config", ca_cnf])
    return ca_crt, ca_key


def _sanitize(domain: str) -> str:
    return "".join(c if (c.isalnum() or c in ".-") else "_" for c in domain)


def get_or_make_leaf(cert_dir: str, domain: str, days: int = 7300):
    """domain 하나짜리 SAN 리프를 CA로 서명해서 만들고(없으면), (chain_pem, key) 경로를 돌려준다.

    *** RSA 고정 *** — 실기기가 TLS_RSA_WITH_AES_256_CBC_SHA256(static RSA 키교환, ECDHE 없음)
    만 협상하는 게 실측 확인됨. openssl
    기본 genrsa라 문제없지만, 다른 도구로 교체할 때 EC key로 바꾸면 핸드셰이크 자체가
    안 된다 — 절대 EC/Ed25519로 바꾸지 말 것.
    """
    leaf_dir = os.path.join(cert_dir, "leaves")
    os.makedirs(leaf_dir, exist_ok=True)
    safe = _sanitize(domain)
    leaf_crt = os.path.join(leaf_dir, f"{safe}.crt")
    leaf_key = os.path.join(leaf_dir, f"{safe}.key")
    chain_pem = os.path.join(leaf_dir, f"{safe}.chain.pem")

    with _leaf_lock:
        if os.path.exists(chain_pem) and os.path.exists(leaf_key):
            return chain_pem, leaf_key

        ca_crt, ca_key = ensure_ca(cert_dir)
        print(f"[cert] {domain} 용 리프 새로 생성")
        with tempfile.TemporaryDirectory() as td:
            leaf_cnf = os.path.join(td, "leaf.cnf")
            with open(leaf_cnf, "w") as f:
                f.write(
                    "[req]\ndistinguished_name = dn\nreq_extensions = ext\nprompt = no\n"
                    f"[dn]\nCN = {domain}\n"
                    "[ext]\nbasicConstraints = CA:FALSE\n"
                    "keyUsage = critical,digitalSignature,keyEncipherment\n"
                    "extendedKeyUsage = serverAuth\nsubjectAltName = @san\n"
                    f"[san]\nDNS.1 = {domain}\n"
                )
            leaf_csr = os.path.join(td, "leaf.csr")
            _run_openssl(["genrsa", "-out", leaf_key, "2048"])
            _run_openssl(["req", "-new", "-key", leaf_key, "-out", leaf_csr, "-config", leaf_cnf])
            _run_openssl(["x509", "-req", "-in", leaf_csr, "-CA", ca_crt, "-CAkey", ca_key,
                          "-CAcreateserial", "-out", leaf_crt, "-days", str(days),
                          "-extfile", leaf_cnf, "-extensions", "ext"])
        with open(chain_pem, "wb") as out:
            out.write(open(leaf_crt, "rb").read())
            out.write(open(ca_crt, "rb").read())
        return chain_pem, leaf_key


def cmd_gen_ca(args):
    ensure_ca(args.cert_dir, force=args.force)
    return 0


# ---------------------------------------------------------------------------
# 최소 DNS 클라이언트 (지정한 서버에 직접 A 레코드 질의, 표준 라이브러리만)
# ---------------------------------------------------------------------------

def _encode_qname(domain: str) -> bytes:
    out = b""
    for part in domain.strip(".").split("."):
        out += bytes([len(part)]) + part.encode()
    return out + b"\x00"


def _skip_name(data: bytes, offset: int) -> int:
    while True:
        length = data[offset]
        if length == 0:
            return offset + 1
        if length & 0xC0 == 0xC0:
            return offset + 2
        offset += 1 + length


def _parse_name(data: bytes, offset: int) -> str:
    labels = []
    jumped = False
    while True:
        length = data[offset]
        if length == 0:
            offset += 1
            break
        if length & 0xC0 == 0xC0:
            pointer = ((length & 0x3F) << 8) | data[offset + 1]
            if not jumped:
                jumped = True
            offset = pointer
            continue
        offset += 1
        labels.append(data[offset:offset + length].decode(errors="replace"))
        offset += length
    return ".".join(labels)


def _dns_query_once(domain: str, dns_server: str, timeout: float):
    qid = random.randint(0, 0xFFFF)
    header = struct.pack(">HHHHHH", qid, 0x0100, 1, 0, 0, 0)
    question = _encode_qname(domain) + struct.pack(">HH", 1, 1)  # A, IN
    query = header + question
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.settimeout(timeout)
        s.sendto(query, (dns_server, 53))
        data, _ = s.recvfrom(4096)
    if len(data) < 12:
        return None, None
    rid, flags, qdcount, ancount, _, _ = struct.unpack(">HHHHHH", data[:12])
    if rid != qid:
        return None, None
    offset = 12
    for _ in range(qdcount):
        offset = _skip_name(data, offset)
        offset += 4
    ips, cname = [], None
    for _ in range(ancount):
        offset = _skip_name(data, offset)
        rtype, rclass, ttl, rdlen = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        if rtype == 1 and rclass == 1 and rdlen == 4:
            ips.append(".".join(str(b) for b in data[offset:offset + 4]))
        elif rtype == 5:
            cname = _parse_name(data, offset)
        offset += rdlen
    return ips, cname


def resolve_via_dns(domain: str, dns_server: str, timeout: float = 3.0, max_hops: int = 5):
    """--dns 서버에 직접 A 레코드 질의(표준 라이브러리 raw UDP). CNAME은 따라감. 실패하면 None."""
    current = domain
    for _ in range(max_hops):
        try:
            ips, cname = _dns_query_once(current, dns_server, timeout)
        except Exception:
            return None
        if ips:
            return ips[0]
        if cname:
            current = cname
            continue
        return None
    return None


# ---------------------------------------------------------------------------
# 로깅
# ---------------------------------------------------------------------------

def ts():
    return time.strftime("%H:%M:%S")


def log(cid, msg):
    print(f"[{ts()}] [{cid}] {msg}")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# 살아있는 기기 MQTT 세션 레지스트리 — observer 주입 대상 조회용.
# 로컬(가정용) 배포 전제라 client_id로 키잉하되 "가장 최근 세션"도 별도로 기억해서
# observer가 대상을 안 밝혀도 기본으로 거기에 주입한다.
# ---------------------------------------------------------------------------

class DeviceRegistry:
    def __init__(self):
        self._lock = threading.Lock()
        self._sessions = {}  # client_id(bytes) -> (sock, write_lock)
        self._latest = None  # client_id(bytes)

    def register(self, client_id: bytes, sock, write_lock: threading.Lock):
        with self._lock:
            old = self._sessions.get(client_id)  # LOCAL PATCH (see LOCAL_PATCHES.md)
            self._sessions[client_id] = (sock, write_lock)
            self._latest = client_id
        if old is not None and old[0] is not sock:  # LOCAL PATCH: same client_id reconnected
            try:
                old[0].close()  # tear down the stale duplicate connection
            except Exception:
                pass
        log("registry", f"기기 세션 등록: {client_id!r}")

    def unregister(self, client_id: bytes, sock=None):  # LOCAL PATCH: + sock identity
        with self._lock:
            cur = self._sessions.get(client_id)
            # LOCAL PATCH: only the session that is *currently* registered may
            # remove itself — a stale old socket closing must not evict a newer one.
            if cur is not None and (sock is None or cur[0] is sock):
                del self._sessions[client_id]
                if self._latest == client_id:
                    self._latest = next(reversed(self._sessions), None)
        log("registry", f"기기 세션 해제: {client_id!r}")

    def get(self, client_id: Optional[bytes] = None):
        with self._lock:
            cid = client_id or self._latest
            if cid is None:
                return None
            return self._sessions.get(cid)

    def latest_client_id(self) -> Optional[bytes]:
        with self._lock:
            return self._latest

    def list_client_ids(self) -> list:
        with self._lock:
            return list(self._sessions.keys())


DEVICE_REGISTRY = DeviceRegistry()


# ---------------------------------------------------------------------------
# tap 버스 — observer에 접속해있는 동안 기기 세션의 실제 MQTT PUBLISH를 그대로
# 실시간으로 흘려보낸다(파일 캡처 없이 관찰하는 용도, --capture-dir과 무관하게 항상 동작).
# ---------------------------------------------------------------------------

class TapBus:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs = {}  # id(sock) -> (sock, write_lock)

    def subscribe(self, sock, write_lock: threading.Lock):
        with self._lock:
            self._subs[id(sock)] = (sock, write_lock)

    def unsubscribe(self, sock):
        with self._lock:
            self._subs.pop(id(sock), None)

    def publish(self, topic: bytes, payload: bytes):
        with self._lock:
            subs = list(self._subs.values())
        pkt = mw.build_publish(topic, payload, qos=0)
        for sock, write_lock in subs:
            try:
                with write_lock:
                    sock.sendall(pkt.bytes())
            except Exception:
                pass  # 죽은 observer 연결 — on_close가 알아서 정리함


TAP = TapBus()


@dataclass
class Ctx:
    cid: str
    domain: Optional[str] = None
    client_id: Optional[bytes] = None
    device_client_id: Optional[bytes] = None


def _send_specs_to(sock, write_lock: threading.Lock, specs, pid_counter: "list[int]", log_prefix: str):
    for spec in specs or []:
        with write_lock:
            pid = None
            if spec.qos > 0:
                pid_counter[0] = (pid_counter[0] % 0xFFFF) + 1
                pid = pid_counter[0]
            pkt = mw.build_publish(spec.topic, spec.payload, qos=spec.qos, packet_id=pid)
            sock.sendall(pkt.bytes())
        log(log_prefix, f">>> PUBLISH {spec.topic.decode(errors='replace')} ({len(spec.payload)}B)")


# ---------------------------------------------------------------------------
# :80 — 즉시 CA 응답 (도메인 무관, 요청을 기다리지 않음, 타이밍 민감)
# ---------------------------------------------------------------------------

def drain(client, cid):
    try:
        client.settimeout(2.0)
        buf = b""
        while True:
            try:
                d = client.recv(65536)
            except socket.timeout:
                break
            if not d:
                break
            buf += d
    except Exception:
        pass


def handle80(client, addr, ca_pem: bytes):
    cid = f":80-{addr[0]}:{addr[1]}-{int(time.time())}"
    log(cid, f"{addr[0]}에서 :80 연결")
    date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime())
    headers = (
        b"HTTP/1.1 200 OK\r\n"
        b"Date: " + date.encode() + b"\r\n"
        b"Server: Apache\r\n"
        b"Content-Disposition: attachment;filename=oneM2M_HTTP_CA.pem\r\n"
        b"Content-Length: " + str(len(ca_pem)).encode() + b"\r\n"
        b"Content-Type: application/xml;charset=UTF-8\r\n"
        b"\r\n"
    )
    threading.Thread(target=drain, args=(client, cid), daemon=True).start()
    try:
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.sendall(headers + ca_pem)
        log(cid, f"인증서(CA) 제공함 ({len(headers) + len(ca_pem)}B)")
    except Exception as e:
        log(cid, f"전송 실패: {e}")
    time.sleep(0.3)
    try:
        client.close()
    except Exception:
        pass
    log(cid, "연결 종료")


def bind_listener(listen_host: str, port: int) -> socket.socket:
    """지금(메인 스레드에서) bind — 실패하면 스레드 안이 아니라 여기서 바로 죽는다."""
    ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        ls.bind((listen_host, port))
    except OSError as e:
        sys.exit(f"{listen_host}:{port} bind 실패: {e} — 다른 프로세스가 이 포트를 쓰고 있거나 "
                 f"(포트 공유 불가, 리스너는 포트당 하나뿐) 권한이 부족할 수 있음"
                 f"(1024 미만 포트는 관리자 권한 필요)")
    ls.listen(16)
    return ls


def listen80(ls: socket.socket, ca_pem):
    print(f"listening {ls.getsockname()[0]}:{ls.getsockname()[1]} — CA 즉시 응답")
    sys.stdout.flush()
    while True:
        c, a = ls.accept()
        threading.Thread(target=handle80, args=(c, a, ca_pem), daemon=True).start()


# ---------------------------------------------------------------------------
# TLS 포트 — SNI로 도메인 학습 -> 리프 즉석 발급
#   업스트림 있음(Proxy): 기존 순수 byte relay + capture, device->upstream 방향만
#     살짝 tee해서 MQTT CONNECT의 client_id를 registry에 등록(observer 주입 대상 확보용).
#   업스트림 없음(Decloud): HTTP/MQTT 프로토콜을 직접 스니핑해서 rules로 서빙.
# ---------------------------------------------------------------------------

class _ProxyTap:
    """Proxy 모드에서 한쪽 방향의 raw 바이트를 tee해서, CONNECT 패킷이 완성되면 client_id를
    registry에 등록하고, PUBLISH는 TAP으로 흘려서 observer가 실시간으로 볼 수 있게 한다
    (Proxy 모드는 rules를 안 타므로 여기가 유일한 관찰 지점). MQTT가 아닌 트래픽(:443
    HTTP 등)이면 그냥 아무 일도 안 함."""

    def __init__(self, dev_sock, write_lock):
        self.framer = mw.Framer()
        self.dev_sock = dev_sock
        self.write_lock = write_lock
        self.client_id = None

    def feed(self, data: bytes):
        try:
            for pkt in self.framer.push(data):
                if pkt.kind == mw.CONNECT and self.client_id is None:
                    info = mw.parse_connect(pkt.body)
                    self.client_id = info.client_id
                    DEVICE_REGISTRY.register(self.client_id, self.dev_sock, self.write_lock)
                elif pkt.kind == mw.PUBLISH:
                    info = mw.parse_publish(pkt)
                    TAP.publish(info.topic, info.payload)
        except Exception:
            pass  # MQTT가 아닌 트래픽으로 판단, 조용히 무시


def pump(src, dst, label, cid, fp, tee=None, write_lock: Optional[threading.Lock] = None):
    total = 0
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            total += len(data)
            if fp:
                fp.write(data)
                fp.flush()
            log(cid, f"{label} +{len(data)}B (누적 {total}B)")
            if tee is not None:
                tee(data)
            if dst is not None:
                if write_lock is not None:
                    with write_lock:
                        dst.sendall(data)
                else:
                    dst.sendall(data)
    except Exception as e:
        log(cid, f"{label} 종료: {e}")
    finally:
        if dst is not None:
            try:
                dst.shutdown(socket.SHUT_WR)
            except Exception:
                pass
    return total


# --- Decloud(업스트림 없음) 로컬 서빙: 프로토콜 스니핑 후 HTTP 또는 MQTT로 rules 디스패치 ---

_HTTP_METHODS = (b"GET", b"POST", b"PUT", b"HEAD", b"DELETE")


def _looks_like_http(first: bytes) -> bool:
    return any(first.startswith(m + b" ") for m in _HTTP_METHODS)


def _looks_like_mqtt_connect(first: bytes) -> bool:
    return len(first) >= 1 and (first[0] >> 4) == mw.CONNECT


def _serve_stream(dev_tls, cid, resp) -> None:
    """LOCAL PATCH (see LOCAL_PATCHES.md): serve an HttpResponse whose `.stream`
    (a StreamPlan) is set. Send the status line + headers, then stream the file
    from disk in fixed-size chunks straight to the TLS socket — the whole file is
    never held in memory. Content-Length is the real on-disk size. `pace` seconds
    are slept between chunks (0 = no pacing). Whatever happens, on_complete(sent,
    filesize) is invoked once; `sent == filesize` means the transfer finished.

    Non-stream responses never reach here (serve_http_locally keeps its original
    header/body + single sendall path for `resp.stream is None`)."""
    sp = resp.stream
    sent = 0
    filesize = -1
    try:
        filesize = os.path.getsize(sp.path)
        hdrs = {k: v for k, v in resp.headers.items() if k.lower() != "content-length"}
        header_lines = "".join(f"{k}: {v}\r\n" for k, v in hdrs.items())
        header_lines += f"Content-Length: {filesize}\r\n"
        head = resp.status_line + b"\r\n" + header_lines.encode() + b"\r\n"
        chunk_size = sp.chunk if sp.chunk and sp.chunk > 0 else 4096
        pace = sp.pace if sp.pace and sp.pace > 0 else 0.0
        # the header/body read loop above shrank the socket timeout; restore a
        # generous one for the (paced, possibly multi-second) transfer.
        try:
            dev_tls.settimeout(60.0)
        except Exception:
            pass
        dev_tls.sendall(head)
        with open(sp.path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                dev_tls.sendall(chunk)
                sent += len(chunk)
                if pace:
                    time.sleep(pace)
    except (BrokenPipeError, ConnectionResetError, ssl.SSLError, socket.timeout, OSError) as e:
        log(cid, f"stream 전송 중단 {sent}/{filesize}B: {e}")
    else:
        log(cid, f"stream 전송 완료 {sent}/{filesize}B")
    cb = getattr(sp, "on_complete", None)
    if callable(cb):
        try:
            cb(sent, filesize)
        except Exception:
            log(cid, "stream on_complete 콜백 예외")


def serve_http_locally(dev_tls, cid, ctx, rules: RulesHandle, first: bytes, fp_dev):
    buf = bytearray(first)
    deadline = time.time() + 5.0
    while b"\r\n\r\n" not in buf and time.time() < deadline:
        dev_tls.settimeout(max(0.1, deadline - time.time()))
        try:
            chunk = dev_tls.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf.extend(chunk)
    if fp_dev:
        fp_dev.write(bytes(buf))
        fp_dev.flush()
    if b"\r\n\r\n" not in buf:
        log(cid, "HTTP 헤더 미완성 — 종료")
        return

    head, _, rest = bytes(buf).partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    try:
        method, path, _ = lines[0].split(b" ", 2)
    except ValueError:
        log(cid, f"HTTP 요청줄 파싱 실패: {lines[0]!r}")
        return
    headers = {}
    for line in lines[1:]:
        if b":" in line:
            k, v = line.split(b":", 1)
            headers[k.strip().decode(errors="replace")] = v.strip().decode(errors="replace")
    content_length = int(headers.get("Content-Length", "0") or "0")
    body = bytearray(rest)
    while len(body) < content_length and time.time() < deadline:
        dev_tls.settimeout(max(0.1, deadline - time.time()))
        try:
            chunk = dev_tls.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        body.extend(chunk)
        if fp_dev:
            fp_dev.write(chunk)
            fp_dev.flush()

    log(cid, f"HTTP {method.decode(errors='replace')} {path.decode(errors='replace')} "
             f"(body {len(body)}B)")
    resp: Optional[HttpResponse] = rules.on_http_request(ctx, method, path, headers, bytes(body))
    if resp is None:
        log(cid, "rules 응답 없음 — 종료")
        return
    if resp.stream is not None:  # LOCAL PATCH: paced file stream (see _serve_stream / LOCAL_PATCHES.md)
        _serve_stream(dev_tls, cid, resp)
        return
    header_lines = "".join(f"{k}: {v}\r\n" for k, v in resp.headers.items())
    header_lines += f"Content-Length: {len(resp.body)}\r\n"
    out = resp.status_line + b"\r\n" + header_lines.encode() + b"\r\n" + resp.body
    dev_tls.sendall(out)
    log(cid, f"rules 응답 전송 ({len(out)}B)")


def serve_mqtt_locally(dev_tls, cid, ctx, rules: RulesHandle, first: bytes, fp_dev):
    write_lock = threading.Lock()

    def on_connect(session):
        ctx.client_id = session.client_id
        DEVICE_REGISTRY.register(session.client_id, dev_tls, write_lock)

    def on_subscribed(session):
        return rules.on_session_start(ctx) or None

    def on_publish(session, topic, payload, qos):
        TAP.publish(topic, payload)
        try:
            msg = json.loads(payload)
        except Exception:
            log(cid, f"JSON 파싱 실패, topic={topic!r}")
            return None
        specs = rules.on_message(ctx, msg, topic)
        for spec in specs or []:
            TAP.publish(spec.topic, spec.payload)
        return specs

    def on_close(session):
        if session.client_id:
            DEVICE_REGISTRY.unregister(session.client_id, dev_tls)  # LOCAL PATCH: pass session identity

    mqtt_session.run_server_session(
        dev_tls, cid, lambda m: log(cid, m),
        on_connect=on_connect, on_subscribed=on_subscribed,
        on_publish=on_publish, on_close=on_close,
        initial_data=first, write_lock=write_lock, capture_fp=fp_dev,
    )


def handle_tls(raw_client, addr, local_port, remote_port, cert_dir, dns_server, port_domain,
               default_domain, capture_dir, rules: RulesHandle):
    cid = f":{local_port}-{addr[0]}:{addr[1]}-{int(time.time())}"
    log(cid, f"{addr[0]}에서 :{local_port} 연결(TLS)")

    sni_holder = {}

    def sni_cb(sslsock, server_hostname, sslctx):
        domain = server_hostname
        if not domain:
            fallback = port_domain or default_domain
            if not fallback:
                log(cid, f"SNI 없음 — :{local_port}용 기본 도메인이 없어 인증서 발급 불가"
                         f"(포트별 도메인도 --default-domain도 없음), 연결 거부")
                return
            domain = fallback
            log(cid, f"SNI 없음 — :{local_port} 기본 도메인({fallback}) 사용")
        sni_holder["value"] = domain
        chain_pem, leaf_key = get_or_make_leaf(cert_dir, domain)
        domain_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        domain_ctx.load_cert_chain(certfile=chain_pem, keyfile=leaf_key)
        sslsock.context = domain_ctx

    base_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    base_ctx.sni_callback = sni_cb
    try:
        dev_tls = base_ctx.wrap_socket(raw_client, server_side=True)
    except Exception as e:
        log(cid, f"TLS 핸드셰이크 실패: {e}")
        raw_client.close()
        return

    domain = sni_holder.get("value")
    log(cid, f"TLS 성공(도메인={domain!r}, cipher={dev_tls.cipher()}) — 기기가 인증서를 신뢰함")
    ctx = Ctx(cid=cid, domain=domain)

    up_tls = None
    if dns_server and domain:
        real_ip = resolve_via_dns(domain, dns_server)
        if real_ip:
            log(cid, f"DNS({dns_server})로 {domain} 조회 -> {real_ip}")
            try:
                raw_up = socket.create_connection((real_ip, remote_port), timeout=15)
                raw_up.settimeout(None)
                up_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                up_ctx.check_hostname = False
                up_ctx.verify_mode = ssl.CERT_NONE
                up_tls = up_ctx.wrap_socket(raw_up, server_hostname=domain)
                log(cid, f"{real_ip}:{remote_port}(SNI={domain})로 릴레이 시작(Proxy)")
            except Exception as e:
                log(cid, f"업스트림({real_ip}:{remote_port}) 연결 실패: {e} — Decloud로 폴백")
                up_tls = None
        else:
            log(cid, f"DNS({dns_server})로 {domain} 조회 실패 — Decloud로 폴백")
    elif not dns_server:
        log(cid, "--dns 없음 — Decloud(rules 직접 서빙)")

    fp_dev = fp_up = None
    if capture_dir:
        os.makedirs(capture_dir, exist_ok=True)
        fp_dev = open(os.path.join(capture_dir, f"{cid}_device_to_upstream.bin"), "wb")

    if up_tls is not None:
        # --- Proxy: 기존 순수 relay, 양방향을 tee해서 registry 등록 + TAP으로 흘림 ---
        if capture_dir:
            fp_up = open(os.path.join(capture_dir, f"{cid}_upstream_to_device.bin"), "wb")
        write_lock = threading.Lock()
        tee_down = _ProxyTap(dev_tls, write_lock)  # device->upstream (CONNECT 등록도 여기서)
        tee_up = _ProxyTap(dev_tls, write_lock)    # upstream->device (등록 없이 tap만)
        threads = [
            threading.Thread(target=pump, args=(dev_tls, up_tls, "device->upstream", cid, fp_dev),
                              kwargs={"tee": tee_down.feed}),
            threading.Thread(target=pump, args=(up_tls, dev_tls, "upstream->device", cid, fp_up),
                              kwargs={"write_lock": write_lock, "tee": tee_up.feed}),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if fp_up:
            fp_up.close()
        if tee_down.client_id:
            DEVICE_REGISTRY.unregister(tee_down.client_id, dev_tls)  # LOCAL PATCH: pass session identity
    else:
        # --- Decloud: 프로토콜 스니핑 후 rules로 직접 서빙 ---
        try:
            dev_tls.settimeout(5.0)
            first = dev_tls.recv(65536)
        except Exception as e:
            log(cid, f"첫 데이터 수신 실패: {e}")
            first = b""
        if not first:
            log(cid, "데이터 없음 — 종료")
        elif _looks_like_http(first):
            serve_http_locally(dev_tls, cid, ctx, rules, first, fp_dev)
        elif _looks_like_mqtt_connect(first):
            serve_mqtt_locally(dev_tls, cid, ctx, rules, first, fp_dev)
        else:
            log(cid, f"알 수 없는 프로토콜(첫 바이트 {first[:4].hex()}) — 관찰만 하고 종료")
            if fp_dev:
                fp_dev.write(first)
                fp_dev.flush()

    if fp_dev:
        fp_dev.close()
    for s in (dev_tls, up_tls):
        if s is not None:
            try:
                s.close()
            except Exception:
                pass
    log(cid, "연결 종료")


def listen_tls(ls: socket.socket, local_port, remote_port, cert_dir, dns_server, port_domain,
               default_domain, capture_dir, rules: RulesHandle):
    domain_note = f", SNI 없을 때 기본 도메인={port_domain}" if port_domain else ""
    print(f"listening {ls.getsockname()[0]}:{local_port} — device TLS 종단"
          f"{'' if local_port == remote_port else f' (업스트림 포트는 :{remote_port})'}{domain_note}")
    sys.stdout.flush()
    while True:
        c, a = ls.accept()
        threading.Thread(
            target=handle_tls,
            args=(c, a, local_port, remote_port, cert_dir, dns_server, port_domain,
                  default_domain, capture_dir, rules),
            daemon=True,
        ).start()


# ---------------------------------------------------------------------------
# observer — 평문 로컬 MQTT 리스너, 외부 도구의 명령을 실제 기기 세션에 주입
# ---------------------------------------------------------------------------

def handle_observer_conn(client, addr, rules: RulesHandle):
    cid = f"observer-{addr[0]}:{addr[1]}-{int(time.time())}"
    log(cid, f"observer 연결")
    write_lock = threading.Lock()

    def on_connect(session):
        TAP.subscribe(client, write_lock)

    def on_close(session):
        TAP.unsubscribe(client)

    def on_publish(session, topic, payload, qos):
        try:
            cmd = json.loads(payload)
        except Exception:
            log(cid, f"JSON 파싱 실패: {payload!r}")
            return None
        if cmd.get("list"):
            ids = [c.decode(errors="replace") for c in DEVICE_REGISTRY.list_client_ids()]
            latest = DEVICE_REGISTRY.latest_client_id()
            resp = json.dumps({
                "devices": ids,
                "latest": latest.decode(errors="replace") if latest else None,
            }).encode()
            # mosquitto_pub처럼 발행 즉시 끊는 1회성 클라이언트는 같은 연결로 응답을 못
            # 받으므로, TAP으로 브로드캐스트해서 그 순간 붙어있는 다른 observer(예:
            # mosquitto_sub -t '#')가 받게 한다.
            TAP.publish(b"mtap/devices", resp)
            return None
        target_client_id = cmd.get("device_client_id", "").encode() if cmd.get("device_client_id") else None
        entry = DEVICE_REGISTRY.get(target_client_id)
        if entry is None:
            log(cid, "주입 대상 기기 세션 없음(연결된 기기 없음) — 무시")
            return None
        dev_sock, write_lock = entry
        inj_ctx = Ctx(cid=cid, device_client_id=target_client_id or DEVICE_REGISTRY.latest_client_id())
        specs = rules.on_local_inject(inj_ctx, cmd)
        if not specs:
            log(cid, "rules.on_local_inject 응답 없음")
            return None
        _send_specs_to(dev_sock, write_lock, specs, [0], cid)
        return None  # observer 자신에게는 별도 응답 없음(로그로 충분)

    mqtt_session.run_server_session(
        client, cid, lambda m: log(cid, m),
        on_connect=on_connect, on_publish=on_publish, on_close=on_close,
        write_lock=write_lock,
    )
    try:
        client.close()
    except Exception:
        pass


def listen_observer(ls: socket.socket, rules: RulesHandle):
    print(f"listening {ls.getsockname()[0]}:{ls.getsockname()[1]} — observer(평문 MQTT, 로컬주입)")
    sys.stdout.flush()
    while True:
        c, a = ls.accept()
        threading.Thread(target=handle_observer_conn, args=(c, a, rules), daemon=True).start()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_ports(specs) -> dict:
    """-p를 반복(또는 콤마 구분)으로 받는다. 각 항목은 'PORT', 'LOCAL:REMOTE',
    또는 'LOCAL:REMOTE:DOMAIN'(그 포트에서 SNI 없을 때 쓸 기본 도메인) 형식.
    돌려주는 값: {local: {"remote": int, "domain": str|None}}."""
    ports = {}
    for spec in (specs or []):
        for token in spec.split(","):
            token = token.strip()
            if not token:
                continue
            parts = token.split(":")
            domain = None
            try:
                if len(parts) == 1:
                    local = remote = int(parts[0])
                elif len(parts) == 2:
                    local, remote = int(parts[0]), int(parts[1])
                elif len(parts) == 3:
                    local, remote = int(parts[0]), int(parts[1])
                    domain = parts[2]
                else:
                    raise ValueError
            except ValueError:
                sys.exit(f"--port 형식 오류: {token!r} ('PORT', 'LOCAL:REMOTE' 또는 'LOCAL:REMOTE:DOMAIN')")
            ports[local] = {"remote": remote, "domain": domain}
    if not ports:
        sys.exit("-p/--port로 포트를 하나 이상 지정하세요. 예: -p 443")
    return ports


def _config_port_specs(cfg_ports) -> list:
    """설정 파일의 [[relay.port]] 배열을 parse_ports()가 받는 'LOCAL:REMOTE:DOMAIN' 문자열로 변환."""
    specs = []
    for entry in cfg_ports:
        local = entry["local"]
        remote = entry.get("remote", local)
        domain = entry.get("domain")
        specs.append(f"{local}:{remote}:{domain}" if domain else f"{local}:{remote}")
    return specs


def cmd_serve(args):
    cfg = load_config(args.config).get("relay", {})

    log_level_name = str(args.log_level or cfg.get("log_level") or "INFO").upper()
    if log_level_name not in LOG_LEVELS:
        sys.exit(f"--log-level 값 오류: {log_level_name!r} (가능한 값: {', '.join(l.lower() for l in LOG_LEVELS)})")
    logging.basicConfig(
        level=LOG_LEVELS[log_level_name],
        format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    listen_host = args.listen_host if args.listen_host is not None else cfg.get("listen_host", "0.0.0.0")
    http_port = args.http_port if args.http_port is not None else cfg.get("http_port", 80)
    cert_dir = args.cert_dir if args.cert_dir is not None else cfg.get("cert_dir", DEFAULT_CERT_DIR)
    rules_dir = args.rules_dir if args.rules_dir is not None else cfg.get("rules_dir", DEFAULT_RULES_DIR)
    dns = args.dns if args.dns is not None else cfg.get("dns")
    default_domain = args.default_domain if args.default_domain is not None else cfg.get("default_domain")
    observer = args.observer if args.observer is not None else cfg.get("observer")
    capture_dir = args.capture_dir if args.capture_dir is not None else cfg.get("capture_dir")

    port_specs = args.port if args.port else (_config_port_specs(cfg["port"]) if cfg.get("port") else None)
    ports = parse_ports(port_specs)

    # bind는 여기(메인 스레드)에서 미리 해서, 실패하면 스레드 없이 바로 죽는다.
    http_ls = bind_listener(listen_host, http_port)
    tls_listeners = {local: bind_listener(listen_host, local) for local in ports}
    observer_ls = None
    if observer:
        ohost, oport = observer.rsplit(":", 1)
        observer_ls = bind_listener(ohost or "0.0.0.0", int(oport))

    ca_crt, _ = ensure_ca(cert_dir)
    with open(ca_crt, "rb") as f:
        ca_pem = f.read()

    rules = RulesHandle(rules_dir) if rules_dir else RulesHandle.empty()

    print("=" * 70)
    print("DNS 리다이렉션 확인: 대상 도메인 조회가 이 머신으로 오고 있어야 동작합니다.")
    print("  (예: dnsmasq) address=/<대상 도메인>/<이 머신의 LAN IP>")
    port_summary = {local: (cfg2["remote"], cfg2["domain"]) for local, cfg2 in ports.items()}
    print(f"리슨: :{http_port}(CA 응답) / TLS {port_summary} (local: (remote, 포트별기본도메인))")
    if default_domain:
        print(f"SNI 없는 연결의 기본 도메인: {default_domain}")
    if dns:
        print(f"업스트림: --dns {dns} 로 실제 IP를 조회해서 릴레이(Proxy) — 실패시 Decloud 폴백")
        print(f"  (주의: {dns}가 기기용 DNS 리다이렉션과 같은 서버면 안 됨)")
    else:
        print("업스트림: 없음(--dns 미지정) — 전부 Decloud(rules가 직접 응답)")
    print(f"rules 디렉터리: {rules_dir or '(비활성화)'}")
    if observer_ls:
        print(f"observer: {observer_ls.getsockname()}")
    print(f"파일 캡처: {capture_dir if capture_dir else '꺼짐(--capture-dir로 지정하면 켜짐)'}")
    print(f"로그 레벨: {log_level_name.lower()}")
    print("=" * 70)
    sys.stdout.flush()

    threads = [threading.Thread(target=listen80, args=(http_ls, ca_pem), daemon=True)]
    for local, cfg2 in ports.items():
        threads.append(threading.Thread(
            target=listen_tls,
            args=(tls_listeners[local], local, cfg2["remote"], cert_dir, dns,
                  cfg2["domain"], default_domain, capture_dir, rules),
            daemon=True,
        ))
    if observer_ls:
        threads.append(threading.Thread(target=listen_observer, args=(observer_ls, rules), daemon=True))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("gen-ca", help="루트 CA 생성(이미 있으면 재사용, --force로 강제 재생성)")
    p_gen.add_argument("--cert-dir", default=DEFAULT_CERT_DIR, help="CA/리프 저장 위치")
    p_gen.add_argument("--force", action="store_true")
    p_gen.set_defaults(func=cmd_gen_ca)

    p_serve = sub.add_parser("serve", help="릴레이 서버 실행")
    p_serve.add_argument(
        "--config", default=None,
        help=f"설정 파일 경로(TOML). 안 주면 현재 working dir의 {DEFAULT_CONFIG_NAME}을 있으면 "
             "자동으로 읽는다(없어도 에러 아님 — CLI 인자/기본값으로 진행). "
             "값 우선순위: CLI 인자 > 설정 파일 > 기본값.",
    )
    p_serve.add_argument(
        "--log-level", default=None,
        help=f"콘솔 로그 레벨: {', '.join(l.lower() for l in LOG_LEVELS)} (기본 info). "
             "trace는 매 텔레메트리 이벤트를 무조건 찍는 가장 시끄러운 레벨.",
    )
    p_serve.add_argument("--cert-dir", default=None,
                          help=f"CA/리프 저장 위치(없으면 자동 생성, 있으면 재사용). 기본: {DEFAULT_CERT_DIR}")
    p_serve.add_argument("--listen-host", default=None, help="기본: 0.0.0.0")
    p_serve.add_argument("--http-port", type=int, default=None,
                          help="CA를 즉시 응답할 평문 포트(기본 80)")
    p_serve.add_argument(
        "-p", "--port", action="append", default=None,
        help="TLS로 종단할 로컬 포트. 반복 또는 콤마로 여러 개. "
             "'PORT'(로컬=업스트림 같은 포트), 'LOCAL:REMOTE'(예: 18883:8883), 또는 "
             "'LOCAL:REMOTE:DOMAIN'(그 포트에서 SNI 없을 때 쓸 기본 도메인 지정, "
             "--default-domain보다 우선). 예: -p 443 -p 18831:18831:example.com. "
             "안 주면 설정 파일의 [[relay.port]]를 대신 사용.",
    )
    p_serve.add_argument(
        "--dns", default=None,
        help="실제 업스트림 IP를 조회할 DNS 서버(예: 8.8.8.8). 안 주면(또는 조회 실패시) "
             "Decloud로 폴백(rules가 직접 응답). 기기용 DNS 리다이렉션 서버와 절대 같으면 안 됨.",
    )
    p_serve.add_argument(
        "--default-domain", default=None,
        help="ClientHello에 SNI가 없는 기기를 위한 전역 기본 도메인(인증서 발급 + --dns 조회에 "
             "그대로 씀). -p로 그 포트의 도메인을 따로 지정하지 않은 경우의 fallback.",
    )
    p_serve.add_argument("--rules-dir", default=None,
                          help="rules/*.py 디렉터리(빈 문자열로 주면 비활성화 — 순수 관찰/릴레이만). "
                               f"기본: {DEFAULT_RULES_DIR}")
    p_serve.add_argument("--observer", default=None,
                          help="평문 MQTT 로컬주입 리스너 HOST:PORT (예: 127.0.0.1:9883)")
    p_serve.add_argument("--capture-dir", default=None,
                          help="지정하면 연결별로 device_to_upstream.bin/upstream_to_device.bin "
                               "파일 캡처를 남긴다(기본은 꺼짐 — 필요할 땐 --observer로 실시간 관찰).")
    p_serve.set_defaults(func=cmd_serve)

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()
