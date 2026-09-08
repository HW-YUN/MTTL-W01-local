#!/usr/bin/env python3
"""
서버측 MQTT 세션 루프 — CONNECT/SUBSCRIBE/PING처럼 프로토콜상 항상 정해진 응답만 여기서
처리하고(제네릭, 기기 무관), PUBLISH 내용에 대한 실제 응답은 콜백(on_publish)에 위임한다.

decloud(:18831에 업스트림 없을 때 relay.py가 직접 브로커 역할)와 observer(로컬 평문 주입
포트) 양쪽이 이 세션 루프를 공유한다 — 둘의 차이는 on_publish 콜백 내용뿐.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import mqtt_wire as mw


@dataclass
class MqttSession:
    """살아있는 서버측 MQTT 세션 상태 — packet id 카운터, 구독 목록, 마지막 활동시각."""

    client_id: bytes = b""
    username: Optional[bytes] = None
    password: Optional[bytes] = None
    subscriptions: list = field(default_factory=list)
    _next_pid: int = 1
    last_activity: float = field(default_factory=time.time)

    def next_packet_id(self) -> int:
        pid = self._next_pid
        self._next_pid = (self._next_pid % 0xFFFF) + 1
        return pid


def _sock_sendall(sock, data: bytes):
    if hasattr(sock, "sendall"):
        sock.sendall(data)
    else:
        sock.send(data)


def run_server_session(
    sock,
    cid: str,
    log: Callable[[str], None],
    on_connect: Optional[Callable[[MqttSession], None]] = None,
    on_subscribed: Optional[Callable[[MqttSession], list]] = None,
    on_publish: Optional[Callable[[MqttSession, bytes, bytes, int], Optional[list]]] = None,
    on_close: Optional[Callable[[MqttSession], None]] = None,
    recv_timeout: float = 300.0,
    initial_data: bytes = b"",
    write_lock: Optional[threading.Lock] = None,
    capture_fp=None,
):
    """sock: TLS 종단됐거나 평문인 소켓형 객체(recv/sendall 지원). 블로킹.

    on_connect(session): CONNECT 파싱 직후(CONNACK 보내기 전) 호출 — session.client_id 등 확정됨.
    on_subscribed(session) -> list[PublishSpec]|None: 첫 SUBSCRIBE 배치 완료 직후(부트스트랩 트리거용).
    on_publish(session, topic, payload, qos) -> list[PublishSpec]|None: 상대가 보낸 PUBLISH 처리.
    on_close(session): 세션 종료(정리용, 예: 레지스트리에서 제거).
    initial_data: 프로토콜 스니핑 과정에서 이미 읽어버린 첫 바이트(있으면 framer에 먼저 먹임).
    write_lock: 주어지면 이 소켓에 대한 모든 송신(엔진 응답 + 외부 주입)이 같은 락을 공유해서
        interleave를 막는다(예: observer가 같은 소켓에 직접 쓰는 경우 — relay.py 참고).
    """
    framer = mw.Framer()
    session = MqttSession()
    subscribed_once = False
    lock = write_lock or threading.Lock()

    def send_pkt(pkt: mw.Packet):
        with lock:
            _sock_sendall(sock, pkt.bytes())

    def send_specs(specs):
        for spec in specs or []:
            pid = session.next_packet_id() if spec.qos > 0 else None
            pkt = mw.build_publish(spec.topic, spec.payload, qos=spec.qos, packet_id=pid)
            send_pkt(pkt)
            log(f">>> PUBLISH {spec.topic.decode(errors='replace')} ({len(spec.payload)}B, qos={spec.qos})")

    nonlocal_subscribed_once = [subscribed_once]  # 클로저에서 bool 변경하려고 리스트로 감쌈

    def handle_packets(pkts) -> bool:
        """False를 돌려주면 세션 종료(DISCONNECT 수신)."""
        for pkt in pkts:
            if pkt.kind == mw.CONNECT:
                info = mw.parse_connect(pkt.body)
                session.client_id = info.client_id
                session.username = info.username
                session.password = info.password
                log(f"CONNECT client_id={info.client_id!r} clean={info.clean_session}")
                if on_connect:
                    on_connect(session)
                send_pkt(mw.build_connack(session_present=False, return_code=0))

            elif pkt.kind == mw.SUBSCRIBE:
                info = mw.parse_subscribe(pkt)
                session.subscriptions.extend(t for t, _ in info.topics)
                granted = [min(q, 1) for _, q in info.topics]
                send_pkt(mw.build_suback(info.packet_id, granted))
                for t, q in info.topics:
                    log(f"SUBSCRIBE {t.decode(errors='replace')} (qos={q})")
                if not nonlocal_subscribed_once[0]:
                    nonlocal_subscribed_once[0] = True
                    if on_subscribed:
                        send_specs(on_subscribed(session))

            elif pkt.kind == mw.PUBLISH:
                info = mw.parse_publish(pkt)
                log(f"PUBLISH<- {info.topic.decode(errors='replace')} ({len(info.payload)}B, qos={info.qos})")
                if info.qos > 0 and info.packet_id is not None:
                    send_pkt(mw.build_puback(info.packet_id))
                if on_publish:
                    send_specs(on_publish(session, info.topic, info.payload, info.qos))

            elif pkt.kind == mw.PUBACK:
                pass  # 우리가 보낸 PUBLISH에 대한 ack — 별도 처리 없음(fire-and-forget)

            elif pkt.kind == mw.PINGREQ:
                send_pkt(mw.build_pingresp())

            elif pkt.kind == mw.DISCONNECT:
                log("DISCONNECT")
                return False
        return True

    try:
        if initial_data:
            if capture_fp:
                capture_fp.write(initial_data)
                capture_fp.flush()
            if not handle_packets(framer.push(initial_data)):
                return
        sock.settimeout(recv_timeout)
        while True:
            try:
                data = sock.recv(65536)
            except socket.timeout:
                log("recv timeout — 세션 종료")
                break
            if not data:
                break
            if capture_fp:
                capture_fp.write(data)
                capture_fp.flush()
            session.last_activity = time.time()
            if not handle_packets(framer.push(data)):
                break
    except Exception as e:
        log(f"세션 예외 종료: {e}")
    finally:
        if on_close:
            try:
                on_close(session)
            except Exception:
                pass
