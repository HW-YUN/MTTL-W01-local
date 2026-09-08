#!/usr/bin/env python3
"""
Minimal MQTT 3.1.1 CLIENT (stdlib only) for the MTTL-W01 HA bridge.

Used for two connections:
  - the HA Mosquitto broker  (username/password, optional TLS, LWT)
  - the relay observer 127.0.0.1:9883  (plaintext, no auth)

Framing primitives are reused read-only from the vendored smartrelay
`mqtt_wire` (Framer / Packet / parse_publish / build_publish / build_puback /
constants).  The vendored file is NOT modified.  Client-side packet builders
(CONNECT / SUBSCRIBE / PINGREQ / DISCONNECT) are ours.

Explicitly NOT copied from the device-side stack: packet-id reuse, DUP quirks,
"PUBACK for any qos>0".  This client behaves like a normal MQTT 3.1.1 client.

Threading: one background thread runs connect-with-backoff + the recv loop.
All socket writes are serialised by a lock.  stop() is clean and bounded.
Credentials are never logged.
"""

from __future__ import annotations

import os
import socket
import ssl
import sys
import threading
import time
from typing import Callable, Optional

_HERE = os.path.dirname(os.path.abspath(__file__))
_VENDOR = os.path.normpath(os.path.join(_HERE, "..", "vendor", "smartrelay"))
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

import mqtt_wire as mw  # noqa: E402  (vendored, read-only)

MAX_PACKET_BYTES = 1_000_000  # drop anything larger, do not buffer unbounded
_BACKOFF = (1, 2, 4, 8, 15, 30)


def _u16(n: int) -> bytes:
    return bytes(((n >> 8) & 0xFF, n & 0xFF))


def _str(b: bytes) -> bytes:
    return _u16(len(b)) + b


def build_connect(client_id: bytes, keepalive: int = 30, clean: bool = True,
                  username: Optional[bytes] = None, password: Optional[bytes] = None,
                  will_topic: Optional[bytes] = None, will_payload: bytes = b"",
                  will_qos: int = 1, will_retain: bool = True) -> mw.Packet:
    flags = 0
    if clean:
        flags |= 0x02
    if will_topic is not None:
        flags |= 0x04
        flags |= (will_qos & 0x03) << 3
        if will_retain:
            flags |= 0x20
    if username is not None:
        flags |= 0x80
    if password is not None:
        flags |= 0x40
    body = _str(b"MQTT") + bytes((4, flags)) + _u16(keepalive) + _str(client_id)
    if will_topic is not None:
        body += _str(will_topic) + _str(will_payload)
    if username is not None:
        body += _str(username)
    if password is not None:
        body += _str(password)
    return mw.Packet(kind=mw.CONNECT, flags=0, body=body)


def build_subscribe(packet_id: int, topics: list) -> mw.Packet:
    body = _u16(packet_id)
    for topic, qos in topics:
        body += _str(topic) + bytes((qos & 0x03,))
    return mw.Packet(kind=mw.SUBSCRIBE, flags=0x02, body=body)


def build_pingreq() -> mw.Packet:
    return mw.Packet(kind=mw.PINGREQ, flags=0, body=b"")


def build_disconnect() -> mw.Packet:
    return mw.Packet(kind=mw.DISCONNECT, flags=0, body=b"")


class MqttClient:
    def __init__(self, host: str, port: int, client_id: str, *,
                 username: Optional[str] = None, password: Optional[str] = None,
                 tls: bool = False, ca: Optional[str] = None, keepalive: int = 30,
                 will_topic: Optional[str] = None, will_payload: str = "offline",
                 will_retain: bool = True, will_qos: int = 1,
                 on_connect: Optional[Callable[["MqttClient"], None]] = None,
                 on_message: Optional[Callable[[str, bytes, int, bool, bool], None]] = None,
                 log: Optional[Callable[[str, str], None]] = None,
                 name: str = "mqtt"):
        self.host = host
        self.port = int(port)
        self.client_id = client_id
        self._user = username.encode() if username else None
        self._pw = password.encode() if password else None
        self._tls = tls
        self._ca = ca or None
        self._keepalive = max(10, int(keepalive))
        self._will_topic = will_topic.encode() if will_topic else None
        self._will_payload = (will_payload or "").encode()
        self._will_retain = will_retain
        self._will_qos = will_qos
        self._on_connect = on_connect
        self._on_message = on_message
        self._log = log or (lambda lvl, m: None)
        self.name = name

        self._sock: Optional[socket.socket] = None
        self._wlock = threading.Lock()
        self._stop = threading.Event()
        self._connected = threading.Event()
        self._pid = 0
        self._last_tx = 0.0
        self._thr = threading.Thread(target=self._run, name=f"mqtt-{name}", daemon=True)

    # ---- public -------------------------------------------------------------

    def start(self) -> None:
        self._thr.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        try:
            with self._wlock:
                if self._sock is not None:
                    try:
                        self._sock.sendall(build_disconnect().bytes())
                    except Exception:
                        pass
                    try:
                        self._sock.close()
                    except Exception:
                        pass
        except Exception:
            pass
        self._thr.join(timeout=timeout)

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        return self._connected.wait(timeout)

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> bool:
        if isinstance(payload, str):
            payload = payload.encode()
        pid = None
        if qos > 0:
            self._pid = (self._pid % 0xFFFF) + 1
            pid = self._pid
        try:
            pkt = mw.build_publish(topic.encode(), payload, qos=qos, packet_id=pid, retain=retain)
        except Exception as exc:
            self._log("WARNING", f"[{self.name}] build_publish failed: {exc!r}")
            return False
        return self._send(pkt)

    def subscribe(self, topics: list) -> bool:
        self._pid = (self._pid % 0xFFFF) + 1
        enc = [(t.encode() if isinstance(t, str) else t, q) for t, q in topics]
        return self._send(build_subscribe(self._pid, enc))

    # ---- internals ---------------------------------------------------------

    def _send(self, pkt: mw.Packet) -> bool:
        with self._wlock:
            if self._sock is None:
                return False
            try:
                self._sock.sendall(pkt.bytes())
                self._last_tx = time.monotonic()
                return True
            except Exception as exc:
                self._log("DEBUG", f"[{self.name}] send failed: {exc!r}")
                return False

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                self._connect_once()
                attempt = 0
                self._recv_loop()
            except Exception as exc:
                self._log("DEBUG", f"[{self.name}] session ended: {exc!r}")
            finally:
                self._connected.clear()
                with self._wlock:
                    if self._sock is not None:
                        try:
                            self._sock.close()
                        except Exception:
                            pass
                    self._sock = None
            if self._stop.is_set():
                break
            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            self._log("INFO", f"[{self.name}] reconnect in {delay}s")
            self._stop.wait(delay)

    def _connect_once(self) -> None:
        self._log("INFO", f"[{self.name}] connecting {self.host}:{self.port} as {self.client_id!r}")
        raw = socket.create_connection((self.host, self.port), timeout=10)
        raw.settimeout(10)
        sock: socket.socket = raw
        if self._tls:
            ctx = ssl.create_default_context(cafile=self._ca) if self._ca else ssl.create_default_context()
            sock = ctx.wrap_socket(raw, server_hostname=self.host)
        conn = build_connect(
            self.client_id.encode(), keepalive=self._keepalive, clean=True,
            username=self._user, password=self._pw,
            will_topic=self._will_topic, will_payload=self._will_payload,
            will_qos=self._will_qos, will_retain=self._will_retain,
        )
        sock.sendall(conn.bytes())
        fr = mw.Framer()
        sock.settimeout(10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            data = sock.recv(4096)
            if not data:
                raise RuntimeError("closed during CONNECT")
            for pkt in fr.push(data):
                if pkt.kind == mw.CONNACK:
                    rc = pkt.body[1] if len(pkt.body) >= 2 else 0xFF
                    if rc != 0:
                        raise RuntimeError(f"CONNACK rc={rc}")
                    with self._wlock:
                        self._sock = sock
                    self._framer = fr
                    self._last_tx = time.monotonic()
                    self._connected.set()
                    self._log("INFO", f"[{self.name}] connected")
                    if self._on_connect:
                        try:
                            self._on_connect(self)
                        except Exception as exc:
                            self._log("WARNING", f"[{self.name}] on_connect: {exc!r}")
                    return
        raise RuntimeError("no CONNACK")

    def _recv_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        sock.settimeout(1.0)
        fr = self._framer
        while not self._stop.is_set():
            now = time.monotonic()
            if now - self._last_tx >= self._keepalive / 2:
                self._send(build_pingreq())
            try:
                data = sock.recv(65536)
            except socket.timeout:
                continue
            except Exception:
                break
            if not data:
                break
            for pkt in fr.push(data):
                self._dispatch(pkt)

    def _dispatch(self, pkt: mw.Packet) -> None:
        if len(pkt.body) > MAX_PACKET_BYTES:
            self._log("WARNING", f"[{self.name}] oversize packet kind={pkt.kind} dropped")
            return
        if pkt.kind == mw.PUBLISH:
            try:
                info = mw.parse_publish(pkt)
            except Exception:
                self._log("DEBUG", f"[{self.name}] malformed PUBLISH dropped")
                return
            if info.qos == 1 and info.packet_id is not None:
                self._send(mw.build_puback(info.packet_id))
            if self._on_message:
                try:
                    self._on_message(info.topic.decode("utf-8", "replace"),
                                     info.payload, info.qos, bool(info.retain), bool(info.dup))
                except Exception as exc:
                    self._log("WARNING", f"[{self.name}] on_message: {exc!r}")
        elif pkt.kind in (mw.PUBACK, mw.SUBACK, mw.PINGRESP, mw.CONNACK):
            return
        # anything else (incl. QoS2 machinery) is intentionally ignored
