"""BLE provisioning from a command line, for hosts that cannot run the panel's page.

The Provision tab (`web/static/js/provision.js`) is the canonical implementation,
and this is a transcription of it, not a second design. It exists because Web
Bluetooth needs Chrome or Edge on a page served over HTTPS with a certificate the
browser trusts, and a lot of installs have neither: the Docker path publishes the
panel on plain HTTP by design, HA Container users often have no TLS at all, and the
machine that has a Bluetooth radio in range of the litter box is frequently a
headless server (in the case that prompted this, the Proxmox host in the same room,
with an Intel AX211). From there one command does what the tab does:

    pip install bleak
    WIFI_PASS='...' python3 ble_provision.py --ssid MyWifi --server http://192.168.1.10/6/

It needs nothing else from this package — stdlib plus `bleak` — so the file can be
copied to the Bluetooth host on its own. `bleak` is imported inside `run()` only:
the add-on image has no Bluetooth stack and CI imports every module, so a top-level
import would break both.

Verified on hardware: a Purobot Max Pro 2 (T5, firmware 943, hardware 1) took
key 151 on the first write, reported states 1 -> 2 -> 6 -> 10 and was heartbeating
to the new server ~15 s after the credentials ack. The ESP32 (BLUFI) path is the
same transcription and is pinned byte-for-byte by the tests, but nobody has run it
against a D4 or T4 from here yet.

Kept in step with the JS by `tests/test_ble_provision.py`, whose vectors were
generated FROM provision.js. If the page's framing changes, regenerate them — a
test failing there means this file has drifted, not that the vectors are stale.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

# --- PetKit Ingenic devices (T5/T6/T7/D4H/D4SH/W7H): service 0xAAA0 ---------
BLE_SERVICE = "0000aaa0-0000-1000-8000-00805f9b34fb"
BLE_TX = "0000aaa1-0000-1000-8000-00805f9b34fb"  # notify: device -> us
BLE_RX = "0000aaa2-0000-1000-8000-00805f9b34fb"  # write: us -> device
PK_MAGIC = bytes([0xFA, 0xFC, 0xFD, 0x46])
PK_TAIL = 0xFB
PK_TYPE_OUT = 0x18

# --- PetKit ESP32 devices (T4, D4): BLUFI service 0xFFFF ---------------------
BLUFI_SERVICE = "0000ffff-0000-1000-8000-00805f9b34fb"
BLUFI_P2E = "0000ff01-0000-1000-8000-00805f9b34fb"  # write
BLUFI_E2P = "0000ff02-0000-1000-8000-00805f9b34fb"  # notify
BLUFI_TYPE_DATA = 0x01
BLUFI_DATA_CUSTOM = 0x13
BLUFI_FC_FRAG = 0x10
BLUFI_DATA_WIFI_REP = 0x0F
BLUFI_DATA_ERROR_INFO = 0x12
#: PetKit's ESP32 firmware receives custom data in 12-byte JSON chunks.
BLUFI_FRAG_LEN = 12

#: Key 112 join report, PetKit app 13.8.1 `BleDeviceBindProgressPresenter` —
#: the same table as `PK_JOIN_STATES` in provision.js.
PK_JOIN_STATES = {
    0: "starting",
    1: "looking for the network",
    2: "connecting to the network",
    3: "the Wi-Fi password is wrong",
    4: "that Wi-Fi network was not found",
    5: "could not connect to the Wi-Fi",
    6: "on Wi-Fi, connecting to the server",
    7: "connected to the server",
    8: "could not connect to the server",
    9: "connecting to MQTT",
    10: "online",
}
PK_JOIN_FAILED = (4, 5, 8)
PK_JOIN_WARN = (3,)
PK_JOIN_DONE = (7, 10)

#: Bluetooth-only accessories: nothing to provision, they are paired through a parent.
BLE_ACCESSORY_PREFIXES = (
    "Petkit_W5", "Petkit_W4X", "Petkit_CTW2", "Petkit_CTW3", "Petkit_K2", "Petkit_K3",
)


def _json(obj) -> bytes:
    """Serialised exactly as `JSON.stringify` does it: no spaces, UTF-8.

    The bytes matter, not only the meaning — the CRC is over them, and the
    BLUFI fragment boundaries fall where they fall.
    """
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()


def pk_crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection, xorout 0."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def pk_frame(seq: int, obj) -> bytes:
    """One JSON object in the FA FC FD 46 envelope; the length counts the CRC."""
    body = _json(obj)
    crc = pk_crc16(body)
    length = len(body) + 2
    return (
        PK_MAGIC
        + bytes([PK_TYPE_OUT, seq & 0xFF, length & 0xFF, (length >> 8) & 0xFF])
        + body
        + bytes([crc & 0xFF, (crc >> 8) & 0xFF, PK_TAIL])
    )


def pk_parse(data: bytes):
    """An inbound PetKit document — the envelope or bare JSON — or None. Never raises."""
    u = bytes(data)
    if len(u) >= 11 and u[:4] == PK_MAGIC:
        length = u[6] | (u[7] << 8)
        for end in (8 + length - 2, len(u) - 3):
            try:
                return json.loads(u[8:end].decode())
            except (ValueError, UnicodeDecodeError):
                continue
        return None
    try:
        return json.loads(u.decode())
    except (ValueError, UnicodeDecodeError):
        return None


def blufi_frames(seq: int, data: bytes) -> tuple[list[bytes], int]:
    """BLUFI custom-data frames for `data`, and the next sequence number.

    `total_len` in a fragment is what REMAINS including that fragment — what the
    device reassembles against — not the length of the whole message.
    """
    head0 = BLUFI_TYPE_DATA | (BLUFI_DATA_CUSTOM << 2)
    if len(data) <= BLUFI_FRAG_LEN:
        return [bytes([head0, 0x00, seq & 0xFF, len(data)]) + data], seq + 1
    frames = []
    for off in range(0, len(data), BLUFI_FRAG_LEN):
        chunk = data[off:off + BLUFI_FRAG_LEN]
        remaining = len(data) - off
        if off + BLUFI_FRAG_LEN >= len(data):
            frames.append(bytes([head0, 0x00, seq & 0xFF, len(chunk)]) + chunk)
        else:
            frames.append(
                bytes([head0, BLUFI_FC_FRAG, seq & 0xFF, len(chunk) + 2,
                       remaining & 0xFF, (remaining >> 8) & 0xFF])
                + chunk
            )
        seq += 1
    return frames, seq


def join_state_text(payload) -> str:
    if not isinstance(payload, dict) or "state" not in payload:
        return "never reported"
    base = PK_JOIN_STATES.get(payload["state"], f"state {payload['state']}")
    return f"{base} (code {payload['code']})" if "code" in payload else base


def timezone_text(hours: float) -> str:
    """The offset as the app sends it: a string of hours, one decimal.

    Rounded half-up because that is what `Number.toFixed` does, and Python's
    `format` rounds half-to-even: the two disagree on exactly the quarter-hour
    zones a device can be in (+5:45 is 5.75, which the page sends as "5.8").
    """
    return str(Decimal(str(hours)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def build_payload(ssid: str, pwd: str, server: str, tz_hours: float, zone: str) -> dict:
    """The key-151 payload, field for field and in the page's order.

    `locale` is a time ZONE NAME (what the app takes from `TimeZone.getDefault()`),
    not a language; `hide` is the app's constant 1. See provision.js for both.
    """
    return {
        "ssid": ssid,
        "pwd": pwd,
        "hide": 1,
        "locale": zone,
        "timezone": timezone_text(tz_hours),
        "apiServers": [server],
        "ipServers": [server],
    }


def _redacted(obj):
    if isinstance(obj, dict) and isinstance(obj.get("payload"), dict) and "pwd" in obj["payload"]:
        return {**obj, "payload": {**obj["payload"], "pwd": "***"}}
    return obj


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


async def _provision_petkit(client, payload: dict) -> bool:
    """The Ingenic exchange. Delays are the page's, transcribed from captures of
    the official app across two sessions — do not round them."""
    replies: dict = {}

    def on_notify(_char, data: bytearray) -> None:
        msg = pk_parse(bytes(data))
        if not isinstance(msg, dict):
            return
        replies[msg.get("key")] = msg.get("payload") or {}
        _log(f"device: key {msg.get('key')} {json.dumps(msg.get('payload') or {})}")

    await client.start_notify(BLE_TX, on_notify)
    seq = 0

    async def send(obj) -> None:
        nonlocal seq
        _log(f"-> {json.dumps(_redacted(obj))}")
        await client.write_gatt_char(BLE_RX, pk_frame(seq, obj), response=True)
        seq += 1

    async def wait_for(pred, polls: int = 20) -> bool:
        for _ in range(polls):
            if pred():
                return True
            await asyncio.sleep(0.25)
        return pred()

    await send({"key": 110})
    if not await wait_for(lambda: 110 in replies):
        _log("no identity reply yet, retrying key 110 once")
        await send({"key": 110})
        if not await wait_for(lambda: 110 in replies):
            _log("the device never answered: make sure it is still in pairing mode")
            return False
    await asyncio.sleep(1.045)
    await send({"key": 151, "payload": payload})
    if not await wait_for(lambda: (replies.get(151) or {}).get("state") == 1):
        _log("the device did not accept the credentials (no state:1)")
        return False
    _log("credentials accepted, waiting for the device to join the network")
    shown = None
    for i in range(25):
        await asyncio.sleep(3.34 if i == 0 else 3.0)
        await send({"key": 112})
        report = replies.get(112)
        state = (report or {}).get("state")
        if state != shown:
            shown = state
            _log(f"device: {join_state_text(report)}")
            if state in PK_JOIN_WARN:
                _log("warning: wrong Wi-Fi password reported; seen to recover, still waiting")
        if state in PK_JOIN_DONE:
            await asyncio.sleep(5.78)
            await send({"key": 112})
            await asyncio.sleep(1.03)
            if (replies.get(112) or {}).get("state") in PK_JOIN_DONE:
                await send({"key": 101})
                return True
        if state in PK_JOIN_FAILED:
            _log(f"the device gave up: {join_state_text(report)}")
            return False
    # As on the page: it took the credentials and said so, so this is not a
    # failure — but it is not a confirmed join either, and the log says which.
    _log(f"no join reported within 25 polls (last: {join_state_text(replies.get(112))}); "
         "it may still get there, watch the panel's device list")
    return True


async def _provision_blufi(client, payload: dict, language: str) -> bool:
    """The ESP32 exchange: the same PetKit keys, carried as BLUFI custom data."""
    replies: dict = {}
    frag: list[bytes] = []

    def on_notify(_char, data: bytearray) -> None:
        b = bytes(data)
        if len(b) < 4:
            return
        ptype, subtype, fc = b[0] & 0x03, b[0] >> 2, b[1]
        body = b[4:4 + b[3]]
        if ptype == BLUFI_TYPE_DATA and subtype == BLUFI_DATA_CUSTOM:
            if fc & BLUFI_FC_FRAG:
                frag.append(body[2:])
                return
            frag.append(body)
            whole = b"".join(frag)
            frag.clear()
            msg = pk_parse(whole)
            if isinstance(msg, dict) and "key" in msg:
                replies[msg["key"]] = msg.get("payload") or {}
                _log(f"device: key {msg['key']} {json.dumps(msg.get('payload') or {})}")
        elif ptype == BLUFI_TYPE_DATA and subtype == BLUFI_DATA_WIFI_REP:
            connected = len(body) > 1 and body[1] == 0
            _log("device: wifi " + ("CONNECTED" if connected else "not connected"))
        elif ptype == BLUFI_TYPE_DATA and subtype == BLUFI_DATA_ERROR_INFO:
            _log(f"device: BLUFI error report, code {body[:1].hex()}")

    await client.start_notify(BLUFI_E2P, on_notify)
    seq = 0

    async def send(obj) -> None:
        nonlocal seq
        _log(f"-> {json.dumps(_redacted(obj))}")
        frames, seq = blufi_frames(seq, _json(obj))
        for f in frames:
            await client.write_gatt_char(BLUFI_P2E, f, response=True)

    async def wait_until(pred, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end and client.is_connected:
            if pred():
                return True
            await asyncio.sleep(0.25)
        return pred()

    await send({"key": 110})
    await asyncio.sleep(2)
    if 110 not in replies:
        await send({"key": 110})
    if not await wait_until(lambda: 110 in replies, 5):
        _log("timed out waiting for the device identity")
        return False
    await send({"key": 151, "payload": payload})
    acked = await wait_until(
        lambda: (replies.get(151) or {}).get("state") == 1
        or (replies.get(151) or {}).get("state") in PK_JOIN_FAILED, 10)
    if not acked or (replies.get(151) or {}).get("state") != 1:
        _log(f"the device refused or never acked the credentials: {join_state_text(replies.get(151))}")
        return False
    _log("credentials accepted, waiting for the device to join the network")
    await send({"key": 112})
    shown, asked_details, sent_language = None, False, False
    end = time.monotonic() + 120
    while time.monotonic() < end and client.is_connected:
        await asyncio.sleep(1)
        report = replies.get(112)
        state = (report or {}).get("state")
        if state != shown:
            shown = state
            _log(f"device: {join_state_text(report)}")
        if state in PK_JOIN_FAILED:
            _log(f"the device gave up: {join_state_text(report)}")
            return False
        if state in PK_JOIN_DONE:
            if not sent_language:
                sent_language = True
                await send({"key": 114, "payload": {"language": language}})
            await send({"key": 101})
            return True
        if state == 6 and not asked_details:
            asked_details = True
            await send({"key": 111})
        else:
            await send({"key": 112})
    _log(f"timed out (last status: {join_state_text(replies.get(112))})")
    return False


def _local_offset_hours() -> float:
    offset = datetime.now().astimezone().utcoffset()
    return offset.total_seconds() / 3600 if offset else 0.0


def _local_zone_name() -> str:
    """The IANA name if the host exposes one (`TZ`, or /etc/timezone, or the
    /etc/localtime symlink), else "" — never a guess from the offset, which
    names no single zone."""
    if os.environ.get("TZ", "").count("/"):
        return os.environ["TZ"].lstrip(":")
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            name = f.read().strip()
            if name:
                return name
    except OSError:
        pass
    try:
        target = os.path.realpath("/etc/localtime")
        if "/zoneinfo/" in target:
            return target.split("/zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


async def run(args: argparse.Namespace, pwd: str) -> int:
    try:
        from bleak import BleakClient, BleakScanner  # noqa: PLC0415 - optional, BLE host only
    except ImportError:
        _log("this needs bleak: pip install bleak (Linux: BlueZ must be running)")
        return 5

    _log(f"scanning up to {args.wait}s for a device named {args.name}* — put it in pairing mode")
    found = None
    end = time.monotonic() + args.wait
    while time.monotonic() < end and found is None:
        seen = await BleakScanner.discover(timeout=8, return_adv=True)
        for address, (dev, adv) in seen.items():
            name = dev.name or adv.local_name or ""
            if (args.address and address.upper() == args.address.upper()) or (
                    not args.address and name.startswith(args.name)):
                found = dev
                _log(f"found {name} {address} rssi={adv.rssi}")
                break
    if found is None:
        _log("no device found")
        return 2
    if (found.name or "").startswith(BLE_ACCESSORY_PREFIXES):
        _log(f"{found.name} is a Bluetooth-only accessory with no Wi-Fi to configure: pair it "
             "from the Devices tab of the litter box or feeder that will relay for it")
        return 3
    if args.scan_only:
        return 0

    tz_hours = args.tz if args.tz is not None else _local_offset_hours()
    payload = build_payload(args.ssid, pwd, args.server, tz_hours, args.zone or _local_zone_name())
    async with BleakClient(found, timeout=20) as client:
        # Ask by service rather than assume a model, as the page does.
        services = {s.uuid.lower() for s in client.services}
        if BLE_SERVICE in services:
            _log("protocol: PetKit (0xAAA0)")
            ok = await _provision_petkit(client, payload)
        elif BLUFI_SERVICE in services:
            _log("protocol: BLUFI (0xFFFF)")
            ok = await _provision_blufi(client, payload, args.language)
        else:
            _log("this device answered to neither 0xAAA0 nor 0xFFFF; services: "
                 + ", ".join(sorted(services)))
            return 4
    _log("provisioned — the device will join Wi-Fi and call the server" if ok else "FAILED")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Point a PetKit device at petkit-local over Bluetooth, without a browser. "
                    "The Wi-Fi password is read from $WIFI_PASS so it stays out of shell history.")
    ap.add_argument("--ssid", required=True, help="Wi-Fi network the device should join")
    ap.add_argument("--server", required=True,
                    help="apiServers URL, e.g. http://192.168.1.10/6/ — ESP32 models need port 80")
    ap.add_argument("--name", default="Petkit", help="BLE name prefix to look for (default: Petkit)")
    ap.add_argument("--address", help="exact BLE address, instead of the name prefix")
    ap.add_argument("--tz", type=float, help="UTC offset in hours (default: this host's, now)")
    ap.add_argument("--zone", help="IANA zone name sent as `locale` (default: this host's)")
    ap.add_argument("--language", default="en_US", help="ESP32 only, key 114 (default: en_US)")
    ap.add_argument("--wait", type=int, default=300, help="seconds to wait for pairing mode")
    ap.add_argument("--scan-only", action="store_true", help="find the device, change nothing")
    args = ap.parse_args(argv)
    pwd = os.environ.get("WIFI_PASS", "")
    if not args.scan_only and not pwd:
        ap.error("set WIFI_PASS in the environment")
    return asyncio.run(run(args, pwd))


if __name__ == "__main__":
    sys.exit(main())
