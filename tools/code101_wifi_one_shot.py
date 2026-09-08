#!/usr/bin/env python3
"""
code101_wifi_one_shot.py  --  MTTL-W01 LGAPMODE code=101 (WiFi Setting) one-shot sender.

READ BEFORE USE.

  * Default run is a DRY RUN. It opens no socket and sends nothing.
  * Real transmission requires BOTH --execute AND typing an exact confirmation string.
  * It sends code=101 EXACTLY ONCE: one connect, one header sendall, one body sendall,
    no retry, no reconnect, no loop. A timeout or socket error is NOT retried.
  * code=101 makes the device write your home-WiFi credentials to flash 0xFA000
    (magic 0x5A5A) and "NOIP" to flash 0xFB000. This is the device's FIRST write.
    The flash write happens BEFORE the 201 response is sent, so:
        "no response received"  DOES NOT mean  "the write failed".
    Do not re-send on missing response. Power-cycle and inspect instead.

Firmware basis (V1.0.105 static analysis):
  - Header 20B: "LGAPMODE"(8) + "0010"(4) + int32-LE code + int32-LE bodysize.
  - Body 128B: SSID_EUCKR[32] + PW_EUCKR[32] + SSID_UTF8[32] + PW_UTF8[32], NUL-padded, plaintext.
  - Dispatch reads msg[0x0C] == 'e' (0x65 = 101). Response: code 201, bodysize 4, result int32-LE 0 (24B).
  - Two unsynchronised recv(): accept-loop recv (<=0x100, SO_RCVTIMEO 3000ms) reads the header;
    the 'e' handler's own recv (<=0xEC) reads the body. -> header and body MUST be separate writes,
    with a gap shorter than the 3s SO_RCVTIMEO.

Secrets:
  - SSID and password are NEVER hardcoded.
  - Password is read via getpass prompt (default) or an environment variable (--password-env NAME).
  - The password value is never printed to stdout/stderr and never written to any file/log.
  - Printable: SSID, SSID/PW byte lengths, header hex, payload length, structural summary. Not the body hex.

Python: standard library only.
"""

import argparse
import os
import socket
import struct
import sys
import time

try:
    import getpass
except Exception:  # pragma: no cover - getpass is stdlib on all supported platforms
    getpass = None

MAGIC = b"LGAPMODE"
VERSION = b"0010"
CODE_WIFI_SETTING = 101
BODY_SIZE = 128
FIELD = 32                      # bytes per body slot (hard firmware limit)
HEADER_LEN = 20
RESP_LEN = 24
EXPECTED_RESP_CODE = 201
EXPECTED_RESP_BODYSIZE = 4
EXPECTED_RESP_RESULT = 0

DEFAULT_HOST = "192.168.1.1"
DEFAULT_PORT = 30300
DEFAULT_TIMEOUT = 10.0
DEFAULT_SPLIT_DELAY = 0.5       # gap between header and body write
MIN_SPLIT_DELAY = 0.1
MAX_SPLIT_DELAY = 2.5           # must stay well under the firmware's 3s SO_RCVTIMEO

WPA2_MIN_BYTES = 8             # IEEE 802.11i passphrase minimum
CONFIRM_STRING = "TYPE-CODE101-ONCE"
PASSWORD_ENV_DEFAULT = None


class HardFail(Exception):
    pass


# ----------------------------------------------------------------------------- build / validate

def build_header():
    h = MAGIC + VERSION + struct.pack("<i", CODE_WIFI_SETTING) + struct.pack("<i", BODY_SIZE)
    if len(h) != HEADER_LEN:
        raise HardFail("internal: header length != 20")
    return h


def _encode(value, encoding, label):
    try:
        return value.encode(encoding)
    except (UnicodeEncodeError, LookupError) as exc:
        raise HardFail("%s: cannot encode as %s (%s)" % (label, encoding, exc.__class__.__name__))


def _field(raw, label):
    if b"\x00" in raw:
        raise HardFail("%s: input contains an embedded NUL byte" % label)
    if len(raw) > FIELD:
        raise HardFail("%s: %d bytes exceeds the %d-byte slot (cannot be transported by code101)"
                       % (label, len(raw), FIELD))
    return raw + b"\x00" * (FIELD - len(raw))


def build_body(ssid, password):
    if not ssid:
        raise HardFail("SSID is empty")
    if not password:
        raise HardFail("password is empty")

    ssid_euckr = _encode(ssid, "euc_kr", "SSID_EUCKR")
    pw_euckr = _encode(password, "euc_kr", "PW_EUCKR")
    ssid_utf8 = _encode(ssid, "utf-8", "SSID_UTF8")
    pw_utf8 = _encode(password, "utf-8", "PW_UTF8")

    body = (
        _field(ssid_euckr, "SSID_EUCKR")
        + _field(pw_euckr, "PW_EUCKR")
        + _field(ssid_utf8, "SSID_UTF8")
        + _field(pw_utf8, "PW_UTF8")
    )
    if len(body) != BODY_SIZE:
        raise HardFail("internal: body length != 128")

    # Password length: two independent bounds, attributed to their source.
    #   upper bound  = FIELD (32)  -> firmware body slot; already enforced by _field()
    #   lower bound  = WPA2_MIN_BYTES (8) -> IEEE 802.11i passphrase minimum
    pw_min = min(len(pw_euckr), len(pw_utf8))
    if pw_min < WPA2_MIN_BYTES:
        raise HardFail("password is %d bytes; below the WPA2 passphrase minimum of %d bytes"
                       % (pw_min, WPA2_MIN_BYTES))

    lengths = {
        "ssid_euckr": len(ssid_euckr),
        "ssid_utf8": len(ssid_utf8),
        "pw_euckr": len(pw_euckr),
        "pw_utf8": len(pw_utf8),
        "ssid_slots_identical": ssid_euckr == ssid_utf8,
        "pw_slots_identical": pw_euckr == pw_utf8,
        "ssid_is_ascii": ssid.isascii(),
        "pw_is_ascii": password.isascii(),
    }
    return body, lengths


def length_notes(lengths):
    """Informational only. Hard limits are enforced in build_body(); this just restates the sources."""
    notes = [
        "firmware body slot limit  : %d bytes per field (SSID and PW) - HARD" % FIELD,
        "WPA2 passphrase minimum   : %d bytes - HARD" % WPA2_MIN_BYTES,
        "WPA2 passphrase (spec)    : 8..63 chars; code101 caps at %d bytes -> "
        "effective %d..%d bytes for this transport" % (FIELD, WPA2_MIN_BYTES, FIELD),
    ]
    if not lengths["pw_slots_identical"]:
        notes.append("password EUC-KR vs UTF-8  : encodings differ (non-ASCII); device stores both slots")
    return notes


def self_test():
    """Network-free. Runs on every invocation."""
    h = build_header()
    assert h[0:8] == MAGIC
    assert h[8:12] == VERSION
    assert h[12:16] == b"\x65\x00\x00\x00", "code field must be 101 LE"
    assert h[16:20] == b"\x80\x00\x00\x00", "bodysize field must be 128 LE"
    b, _ = build_body("selftest", "selftestpw")
    assert len(b) == BODY_SIZE
    assert b[0:8] == b"selftest"
    assert b[8:32] == b"\x00" * 24
    print("SELF-CHECK: PASS")


# ----------------------------------------------------------------------------- reporting (no secrets)

def print_structural_summary(ssid, lengths, header, body):
    print("SSID                      : %s" % ssid)
    print("SSID bytes (EUC-KR)       : %d" % lengths["ssid_euckr"])
    print("SSID bytes (UTF-8)        : %d" % lengths["ssid_utf8"])
    print("SSID slots identical      : %s" % ("yes" if lengths["ssid_slots_identical"] else "no"))
    print("PW bytes (EUC-KR)         : %d" % lengths["pw_euckr"])
    print("PW bytes (UTF-8)          : %d" % lengths["pw_utf8"])
    print("PW slots identical        : %s" % ("yes" if lengths["pw_slots_identical"] else "no"))
    for note in length_notes(lengths):
        print("  %s" % note)
    print("header (20 B, hex)        : %s" % " ".join(format(b, "02x") for b in header))
    print("body length               : %d bytes (4 x %d, NUL-padded)  [hex not shown - contains password]"
          % (len(body), FIELD))
    print("payload total             : %d bytes" % (len(header) + len(body)))


# ----------------------------------------------------------------------------- execute (guarded)

EXECUTE_WARNING = """
================================ EXECUTE MODE ================================
This will send LGAPMODE code=101 to %(host)s:%(port)d EXACTLY ONCE.

The device will WRITE your home-WiFi credentials to flash 0xFA000 (magic 0x5A5A)
and "NOIP" to flash 0xFB000. This is the device's first write and is only
reversible via a factory reset whose scope is UNVERIFIED.

The flash write happens BEFORE the 201 response is sent. Therefore:
   a missing / timed-out / malformed response DOES NOT mean the write failed.
This tool will NOT re-send under any circumstance. If the response is missing,
STOP, power-cycle the device, and inspect its state.

Preconditions you must have already met (this tool does not check them):
  - the target device and the WiFi credentials you intend to write are confirmed
  - any records you want beforehand (device identity / current state) are captured
  - the device segment has internet egress OFF ; no DNS redirect ; relay not running
  - DNS query logging is live so you can watch the device's first lookup after the write
  - your reboot method is a power-cycle (this tool does not use code102 / reset)
============================================================================
"""


def send_once(host, port, header, body, split_delay, timeout):
    print(EXECUTE_WARNING % {"host": host, "port": port})
    try:
        confirm = input("Type exactly %r to proceed (anything else aborts): " % CONFIRM_STRING)
    except EOFError:
        print("no confirmation on stdin - aborting.")
        return 2
    if confirm != CONFIRM_STRING:
        print("confirmation string did not match exactly - aborting. Nothing was sent.")
        return 2

    sock = socket.create_connection((host, port), timeout=timeout)   # exactly one connect
    sent_header = sent_body = False
    try:
        sock.sendall(header)                                          # sendall #1 of 2
        sent_header = True
        time.sleep(split_delay)
        sock.sendall(body)                                            # sendall #2 of 2
        sent_body = True
        sock.settimeout(timeout)
        resp = sock.recv(256)                                         # single read, no loop
        return interpret_response(resp)
    except socket.timeout:
        print("RESULT: no response within %.1fs. NOT resending. "
              "This is NOT proof the write failed - power-cycle and inspect." % timeout)
        return 3
    except OSError as exc:
        stage = "after body" if sent_body else ("after header" if sent_header else "before send")
        print("RESULT: socket error (%s) %s. NOT resending." % (exc.__class__.__name__, stage))
        return 4
    finally:
        try:
            sock.close()
        except OSError:
            pass


def interpret_response(resp):
    if len(resp) < RESP_LEN:
        print("RESULT: short response (%d bytes, expected %d). NOT resending." % (len(resp), RESP_LEN))
        return 5
    magic_ok = resp[0:8] == MAGIC and resp[8:12] == VERSION
    code = struct.unpack("<i", resp[12:16])[0]
    bodysize = struct.unpack("<i", resp[16:20])[0]
    result = struct.unpack("<i", resp[20:24])[0]
    print("response magic/version ok : %s" % magic_ok)
    print("response code             : %d (expected %d)" % (code, EXPECTED_RESP_CODE))
    print("response bodysize         : %d (expected %d)" % (bodysize, EXPECTED_RESP_BODYSIZE))
    print("response result           : %d (expected %d)" % (result, EXPECTED_RESP_RESULT))
    if magic_ok and code == EXPECTED_RESP_CODE and bodysize == EXPECTED_RESP_BODYSIZE \
            and result == EXPECTED_RESP_RESULT:
        print("RESULT: code101 acknowledged (201 / result 0). Do NOT send again. "
              "Next: power-cycle the device, then watch DNS on the relay host.")
        return 0
    print("RESULT: unexpected response. NOT resending. Inspect device state manually.")
    return 6


# ----------------------------------------------------------------------------- main

def resolve_password(args):
    if args.password_env:
        if args.password_env not in os.environ:
            raise HardFail("environment variable %r is not set" % args.password_env)
        return os.environ[args.password_env]
    if getpass is None:
        raise HardFail("getpass unavailable and --password-env not given")
    return getpass.getpass("WPA2 passphrase for the SSID (input hidden, not stored): ")


def resolve_ssid(args):
    if args.ssid is not None:
        return args.ssid
    try:
        return input("SSID: ").strip()
    except EOFError:
        raise HardFail("no SSID provided (use --ssid or interactive input)")


def main(argv=None):
    p = argparse.ArgumentParser(
        description="LGAPMODE code=101 one-shot sender. Default = DRY RUN (no socket).")
    p.add_argument("--ssid", default=None, help="target home SSID (else prompted)")
    p.add_argument("--password-env", default=PASSWORD_ENV_DEFAULT, metavar="NAME",
                   help="read the passphrase from this environment variable instead of prompting")
    p.add_argument("--host", default=DEFAULT_HOST)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    p.add_argument("--split-delay", type=float, default=DEFAULT_SPLIT_DELAY,
                   help="seconds between header and body writes (%.1f..%.1f)"
                        % (MIN_SPLIT_DELAY, MAX_SPLIT_DELAY))
    p.add_argument("--self-check", action="store_true",
                   help="run the network-free self-check and exit")
    p.add_argument("--execute", action="store_true",
                   help="actually send (also requires typing the confirmation string). "
                        "Without this, DRY RUN.")
    args = p.parse_args(argv)

    self_test()
    if args.self_check:
        return 0

    if not (MIN_SPLIT_DELAY <= args.split_delay <= MAX_SPLIT_DELAY):
        print("HARD FAIL: --split-delay must be between %.1f and %.1f seconds "
              "(< firmware 3s SO_RCVTIMEO)" % (MIN_SPLIT_DELAY, MAX_SPLIT_DELAY))
        return 1

    try:
        ssid = resolve_ssid(args)
        password = resolve_password(args)
        header = build_header()
        body, lengths = build_body(ssid, password)
    except HardFail as exc:
        print("HARD FAIL: %s" % exc)
        return 1

    print_structural_summary(ssid, lengths, header, body)
    print("VALIDATION: PASS")

    if not args.execute:
        print("DRY RUN - no socket opened, nothing sent. Re-run with --execute to send once.")
        return 0

    return send_once(args.host, args.port, header, body, args.split_delay, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
