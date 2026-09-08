#!/usr/bin/env python3
# device_info_probe.py
# -----------------------------------------------------------------------------
# MTTL-W01 / Voltra SoftAP  ---  LGAPMODE "Device Info" (code 103) read-only probe.
#
# Scope: this tool can ONLY send the single 20-byte `code=103` (Device Info) request
#        and parse the reply. It is a non-destructive query of the device.
#
# It CANNOT and DOES NOT:
#   - send code 101 (WiFi Setting) or code 102 (Reset)          <- not implemented, no option
#   - accept an arbitrary code or arbitrary payload             <- no such option exists
#   - take a Wi-Fi SSID / password                              <- never handled
#   - reset / provision / reboot the device
#   - run shell commands / subprocess / networksetup / touch OS Wi-Fi
#   - open more than one socket, or send more than one request
#
# Default run = DRY RUN (prints the request, opens no socket).
# A real TCP connection happens ONLY with the explicit  --execute  flag.
#
# Protocol basis: the SoftAP LGAPMODE byte layout (cross-checked against smartrelay
#                 and the V1.0.105 firmware's SoftAP handler, 'g' branch).
# -----------------------------------------------------------------------------

import argparse
import socket
import struct
import sys

# ---- fixed protocol constants (NOT configurable) ----------------------------
MAGIC   = b"LGAPMODE"          # header 0x00..0x07
VERSION = b"0010"             # header 0x08..0x0B
CODE_DEVICE_INFO   = 103      # header 0x0C..0x0F  (int32 LE)  -- hard-coded, the only command
REQUEST_BODYSIZE   = 0        # header 0x10..0x13  (int32 LE)  -- Device Info has no body

EXPECTED_RESP_CODE     = 20301
EXPECTED_RESP_BODYSIZE = 70
RESP_HEADER_LEN        = 20
MAX_SANE_BODYSIZE      = 512  # never recv more than this; anything larger = abort

# Device Info body layout (70 bytes) -- offsets within the body
BODY_FIELDS = [
    ("MAC",      0,  12),
    ("Serial",  12,  20),
    ("Firmware",32,  16),
    ("Model",   48,  16),
    ("Extra",   64,   2),
    ("Result",  66,   4),   # int32 LE
]

DEFAULT_HOST    = "192.168.1.1"
DEFAULT_PORT    = 30300
DEFAULT_TIMEOUT = 5.0


def build_request() -> bytes:
    """The one and only request this tool can produce: a 20-byte code=103 header."""
    hdr = MAGIC + VERSION
    hdr += struct.pack("<i", CODE_DEVICE_INFO)
    hdr += struct.pack("<i", REQUEST_BODYSIZE)
    assert len(hdr) == 20, "request header must be exactly 20 bytes"
    return hdr


def hexs(b: bytes) -> str:
    return " ".join(f"{x:02x}" for x in b)


EXPECTED_REQUEST_HEX = "4c 47 41 50 4d 4f 44 45 30 30 31 30 67 00 00 00 00 00 00 00"


def self_test() -> None:
    """Network-free sanity checks (spec §6). Aborts the program on any mismatch."""
    req = build_request()
    assert len(req) == 20, f"request length {len(req)} != 20"
    assert req[0:8]   == b"LGAPMODE",            "magic mismatch"
    assert req[8:12]  == b"0010",                "version mismatch"
    assert req[12:16] == bytes([0x67, 0, 0, 0]), "code field != 67 00 00 00 (103 LE)"
    assert req[16:20] == bytes([0, 0, 0, 0]),    "bodysize field != 00 00 00 00"
    assert struct.unpack("<i", req[12:16])[0] == 103, "code != 103"
    assert hexs(req) == EXPECTED_REQUEST_HEX, f"request hex {hexs(req)!r} != expected"
    print("SELF-CHECK: PASS  (request = 20 bytes, code 103 = 67 00 00 00, bodysize = 00 00 00 00)")


def show_request(host: str, port: int, timeout: float) -> None:
    req = build_request()
    print(f"destination           : {host}:{port}  (tcp, timeout {timeout}s)")
    print(f"request length        : {len(req)} bytes")
    print(f"request header hex     : {hexs(req)}")
    print( "parsed request header :")
    print(f"    0x00  magic        : {req[0:8]!r}")
    print(f"    0x08  version      : {req[8:12]!r}")
    print(f"    0x0c  code         : {struct.unpack('<i', req[12:16])[0]}  (Device Info)")
    print(f"    0x10  bodysize     : {struct.unpack('<i', req[16:20])[0]}")


def _decode_text(b: bytes) -> str:
    b = b.split(b"\x00", 1)[0]
    try:
        return b.decode("ascii")
    except UnicodeDecodeError:
        return "(non-ascii) " + b.hex()


def recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive up to n bytes; return what arrived (may be < n on EOF/timeout)."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
    return bytes(buf)


def parse_body(body: bytes) -> None:
    print("Device Info:")
    for name, off, size in BODY_FIELDS:
        field = body[off:off + size]
        if name == "Result":
            val = struct.unpack("<i", field)[0] if len(field) == 4 else None
            print(f"    Result   : {val}")
        elif name == "Extra":
            print(f"    Extra    : {field.hex()}  ({_decode_text(field)})")
        else:
            print(f"    {name:<9}: {_decode_text(field)}")


def execute(host: str, port: int, timeout: float) -> int:
    req = build_request()
    print("=" * 70)
    print(f"--execute : opening ONE tcp connection to {host}:{port}")
    print(f"            sending EXACTLY these 20 bytes, once: {hexs(req)}")
    print("            (code 103 / Device Info only -- no second request will be sent)")
    print("=" * 70)

    sock = socket.create_connection((host, port), timeout=timeout)
    sock.settimeout(timeout)
    try:
        # 2. send exactly the 20-byte request, one time
        sock.sendall(req)

        # 3. receive the 20-byte response header
        hdr = recv_exact(sock, RESP_HEADER_LEN)
        print(f"response header ({len(hdr)} B): {hexs(hdr)}")
        if len(hdr) < RESP_HEADER_LEN:
            print("WARNING: short response header — aborting.")
            return 2

        r_magic = hdr[0:8]
        r_ver   = hdr[8:12]
        r_code  = struct.unpack("<i", hdr[12:16])[0]
        r_bsize = struct.unpack("<i", hdr[16:20])[0]
        print(f"    magic    : {r_magic!r}")
        print(f"    version  : {r_ver!r}")
        print(f"    code     : {r_code}")
        print(f"    bodysize : {r_bsize}")
        if r_magic != MAGIC or r_ver != VERSION:
            print("NOTE: response magic/version differs from request (informational).")

        # 4. validate
        if r_code != EXPECTED_RESP_CODE:
            print(f"WARNING: response code {r_code} != expected {EXPECTED_RESP_CODE}. Not a Device Info reply — aborting.")
            return 2
        if r_bsize < 0 or r_bsize > MAX_SANE_BODYSIZE:
            print(f"WARNING: bodysize {r_bsize} is out of sane range (0..{MAX_SANE_BODYSIZE}). Not reading further — aborting.")
            return 2
        if r_bsize != EXPECTED_RESP_BODYSIZE:
            print(f"WARNING: bodysize {r_bsize} != expected {EXPECTED_RESP_BODYSIZE}.")
            take = min(r_bsize, EXPECTED_RESP_BODYSIZE)
            body = recv_exact(sock, take)
            print(f"raw body ({len(body)} B): {body.hex()}")
            return 2

        # 5. receive exactly the 70-byte body
        body = recv_exact(sock, EXPECTED_RESP_BODYSIZE)
        print(f"response body ({len(body)} B): {body.hex()}")
        if len(body) < EXPECTED_RESP_BODYSIZE:
            print("WARNING: short body — parsing what arrived.")

        # 6. parse
        parse_body(body)
        return 0
    finally:
        # 7. close
        sock.close()
        print("socket closed.")


def main() -> int:
    self_test()  # spec §6 — always, network-free

    p = argparse.ArgumentParser(
        description="LGAPMODE Device Info (code 103) read-only probe. "
                    "Default = dry run (no socket). Use --execute to actually connect.")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"device IP (default {DEFAULT_HOST})")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"tcp port (default {DEFAULT_PORT})")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help=f"seconds (default {DEFAULT_TIMEOUT})")
    p.add_argument("--execute", action="store_true",
                   help="actually open ONE tcp connection and send the code=103 request once")
    args = p.parse_args()

    show_request(args.host, args.port, args.timeout)

    if not args.execute:
        print()
        print("DRY RUN — no socket opened.")
        print("(re-run with --execute to actually query the device)")
        return 0

    return execute(args.host, args.port, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
