#!/usr/bin/env python3
"""
MQTT 3.1.1(MQIsdp 3.1 호환) 패킷 프레이머 — 순수 프로토콜, 기기/서비스 특화 내용 없음.

relay.py(엔진)와 rules/*.py(기기별 응답 로직) 양쪽에서 공용으로 쓴다. 여기엔 MTTL-W01이나
oneM2M 관련 문자열이 단 한 줄도 없어야 한다 — 그런 건 전부 rules/ 쪽 소관.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

# 패킷 타입
CONNECT = 1
CONNACK = 2
PUBLISH = 3
PUBACK = 4
PUBREC = 5
PUBREL = 6
PUBCOMP = 7
SUBSCRIBE = 8
SUBACK = 9
UNSUBSCRIBE = 10
UNSUBACK = 11
PINGREQ = 12
PINGRESP = 13
DISCONNECT = 14


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            break
    return bytes(out)


def _parse_varint(buf: bytes, offset: int):
    """(value, 다음오프셋) 또는 데이터가 모자라면 None."""
    value = 0
    mult = 1
    i = offset
    for _ in range(4):
        if i >= len(buf):
            return None
        b = buf[i]
        value += (b & 0x7F) * mult
        i += 1
        if not (b & 0x80):
            return value, i
        mult *= 128
    return None  # 4바이트 넘게 이어지면 프레이밍 오류로 취급(취소)


@dataclass
class Packet:
    kind: int
    flags: int
    body: bytes

    def bytes(self) -> bytes:
        head = bytes([(self.kind << 4) | (self.flags & 0x0F)])
        return head + encode_varint(len(self.body)) + self.body


class Framer:
    """스트림에 push()로 바이트를 흘려주면 완성된 Packet들을 리스트로 돌려준다."""

    def __init__(self):
        self._buf = bytearray()

    def push(self, data: bytes) -> list[Packet]:
        self._buf.extend(data)
        out = []
        while True:
            if len(self._buf) < 2:
                break
            parsed = _parse_varint(self._buf, 1)
            if parsed is None:
                break
            rem_len, body_off = parsed
            total = body_off + rem_len
            if len(self._buf) < total:
                break
            byte0 = self._buf[0]
            body = bytes(self._buf[body_off:total])
            out.append(Packet(kind=byte0 >> 4, flags=byte0 & 0x0F, body=body))
            del self._buf[:total]
        return out


# --- 문자열/필드 helpers (MQTT 2바이트 길이프리픽스) ---

def _read_str(buf: bytes, off: int):
    n = struct.unpack_from(">H", buf, off)[0]
    off += 2
    s = buf[off:off + n]
    return s, off + n


def _write_str(b: bytes) -> bytes:
    return struct.pack(">H", len(b)) + b


@dataclass
class ConnectInfo:
    protocol_name: bytes
    protocol_level: int
    flags: int
    keep_alive: int
    client_id: bytes
    username: Optional[bytes] = None
    password: Optional[bytes] = None

    @property
    def clean_session(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def has_username(self) -> bool:
        return bool(self.flags & 0x80)

    @property
    def has_password(self) -> bool:
        return bool(self.flags & 0x40)


def parse_connect(body: bytes) -> ConnectInfo:
    off = 0
    proto_name, off = _read_str(body, off)
    proto_level = body[off]; off += 1
    flags = body[off]; off += 1
    keep_alive = struct.unpack_from(">H", body, off)[0]; off += 2
    client_id, off = _read_str(body, off)

    if flags & 0x04:  # will flag
        _, off = _read_str(body, off)  # will topic
        _, off = _read_str(body, off)  # will message

    username = None
    password = None
    if flags & 0x80:
        username, off = _read_str(body, off)
    if flags & 0x40:
        password, off = _read_str(body, off)

    return ConnectInfo(
        protocol_name=proto_name, protocol_level=proto_level, flags=flags,
        keep_alive=keep_alive, client_id=client_id, username=username, password=password,
    )


def build_connack(session_present: bool = False, return_code: int = 0) -> Packet:
    body = bytes([1 if session_present else 0, return_code])
    return Packet(kind=CONNACK, flags=0, body=body)


@dataclass
class PublishInfo:
    topic: bytes
    qos: int
    dup: bool
    retain: bool
    packet_id: Optional[int]
    payload: bytes


def parse_publish(pkt: Packet) -> PublishInfo:
    qos = (pkt.flags >> 1) & 0x03
    dup = bool(pkt.flags & 0x08)
    retain = bool(pkt.flags & 0x01)
    off = 0
    topic, off = _read_str(pkt.body, off)
    packet_id = None
    if qos > 0:
        packet_id = struct.unpack_from(">H", pkt.body, off)[0]
        off += 2
    payload = pkt.body[off:]
    return PublishInfo(topic=topic, qos=qos, dup=dup, retain=retain, packet_id=packet_id, payload=payload)


def build_publish(topic: bytes, payload: bytes, qos: int = 1, packet_id: Optional[int] = None,
                   dup: bool = False, retain: bool = False) -> Packet:
    flags = (1 if dup else 0) << 3 | (qos & 0x03) << 1 | (1 if retain else 0)
    body = _write_str(topic)
    if qos > 0:
        if packet_id is None:
            raise ValueError("QoS>0 PUBLISH는 packet_id가 필요함")
        body += struct.pack(">H", packet_id)
    body += payload
    return Packet(kind=PUBLISH, flags=flags, body=body)


def build_puback(packet_id: int) -> Packet:
    return Packet(kind=PUBACK, flags=0, body=struct.pack(">H", packet_id))


@dataclass
class SubscribeInfo:
    packet_id: int
    topics: list  # [(topic_bytes, requested_qos), ...]


def parse_subscribe(pkt: Packet) -> SubscribeInfo:
    off = 0
    packet_id = struct.unpack_from(">H", pkt.body, off)[0]; off += 2
    topics = []
    while off < len(pkt.body):
        topic, off = _read_str(pkt.body, off)
        qos = pkt.body[off]; off += 1
        topics.append((topic, qos))
    return SubscribeInfo(packet_id=packet_id, topics=topics)


def build_suback(packet_id: int, granted_qos: list) -> Packet:
    body = struct.pack(">H", packet_id) + bytes(granted_qos)
    return Packet(kind=SUBACK, flags=0, body=body)


def build_pingresp() -> Packet:
    return Packet(kind=PINGRESP, flags=0, body=b"")
