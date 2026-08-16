"""The BLE relay: accessories talked to through the WiFi device that relays
for them.

A K3 spray or an EverSweet fountain has no network of its own (`devices/ble/`).
It pairs to a mains-powered litter box or feeder, and everything it says
arrives inside that parent's MQTT traffic — as `ble_response` frames, or
piggybacked on the parent's own `property/post`. Nothing arrives unprompted:
the parent opens its radio only while the server holds a session open with
`thing/service/connect`, which is why this both polls and decodes.

It owns no broker connection. Every frame goes out through the bridge
(`mqtt/bridge.py`), which holds the one client to the embedded broker, and
every path here checks that connection first — a session opened for a publish
that cannot leave costs a sequence number and a throttle slot and delivers
nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING

from petkit_local.devices.ble import ble_command_frame, parser_for
from petkit_local.devices.ble.framing import _iter_ble_frames
from petkit_local.utils.dicts import dig

if TYPE_CHECKING:
    from petkit_local.devices.base import Device
    from petkit_local.devices.ble import BLEDevice, BLERegistry
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.mqtt.bridge import MQTTBridge

log = logging.getLogger(__name__)

#: Seconds to ask a parent to hold a BLE relay session open (`connect.time`).
#: Confirmed working at 30 on a CTW3 behind a D4SH; the unit is inferred.
BLE_SESSION_HOLD_SECONDS = 30


def update_linked_k3(device: "Device", params: dict,
                     ble_registry: "BLERegistry | None") -> "BLEDevice | None":
    """Merge K3 consumable fields piggybacked on the parent's state report.

    Transport-agnostic: the K3 spray is BLE-only and never posts anything of
    its own, so its `battery` and `liquid` ride along on the parent litter
    box's continuous state — MQTT `property/post`, HTTP `dev_state_report`,
    and the embedded `state` block of every `dev_event_report`. All three
    carry the same top-level keys, so ONE extractor covers every transport,
    for the reason `_extract_feeder_next_gen` records: a mapping added to
    only one of them works on whichever frames happen to carry it and
    silently does nothing on the other.

    Returns:
        The K3 BLEDevice if anything changed (the caller re-publishes it to
        HA), else None.
    """
    if not ble_registry:
        return None
    k3 = ble_registry.get_linked_k3(device.petkit_id)
    if not k3:
        return None

    updated = False
    if "battery" in params:
        k3.state.setdefault("consumables", {})["battery"] = params["battery"]
        updated = True
    if "liquid" in params:
        k3.state.setdefault("consumables", {})["liquid"] = params["liquid"]
        updated = True

    if not updated:
        return None
    ble_registry.mark_dirty()
    log.info("Updated K3 (id=%d) from parent device %d: battery=%s liquid=%s",
             k3.petkit_id, device.petkit_id,
             params.get("battery", "?"), params.get("liquid", "?"))
    return k3

class BLERelay:
    """Reads and writes BLE accessories through their parent's MQTT session.

    Constructed with the bridge rather than with a broker client, because both
    of the things it needs from that connection change over its lifetime: the
    client is rebound on every reconnect, and whether there is one at all
    decides whether a method does anything before it does it.

    The BLE registry and the HA publisher are both optional and both checked
    before use — an install can have no accessories and no Home Assistant.
    """

    def __init__(self, bridge: MQTTBridge, registry: DeviceRegistry,
                 ble_registry: BLERegistry | None = None,
                 ha_publisher: HAPublisher | None = None) -> None:
        self._bridge = bridge
        self._registry = registry
        self._ble_registry = ble_registry
        self._ha_publisher = ha_publisher
        self._ble_poll_ts: dict[int, float] = {}
        #: Per-accessory BLE frame sequence, wrapping at a byte.
        self._ble_seq: dict[int, int] = {}

    async def publish_ble_command(self, device: Device, ble: BLEDevice,
                                 cmd: int, payload: bytes) -> bool:
        """Write one framed command to an accessory, through its parent.

        The mirror of `_handle_ble_response`: `thing/service/ble` carries the
        MQTT `cmd` alongside the same `FA FC FD ... FB` frame the accessory
        answers with. The parent forwards the bytes; it does not interpret them.

        A write needs a session that is already open. Published on its own it is
        accepted by the parent, forwarded, and does nothing — confirmed on a
        CTW3 by @strxno, who saw the same frame acknowledged with a session held
        and silently ignored without one. So the session is opened first, on
        every write, and left for `_handle_ble_response` to close once a reading
        comes back.

        Returns False when nothing could be sent, so a caller does not report
        success for a command that never left.
        """
        if not self._bridge.connected:
            return False
        await self._ble_connect(device, ble, action=1)
        seq = self._ble_seq.get(ble.petkit_id, 0)
        self._ble_seq[ble.petkit_id] = (seq + 1) & 0xFF
        data = ble_command_frame(cmd, seq, payload)
        if data is None:
            log.warning("No BLE opcode known for cmd %s (%s id=%d)",
                        cmd, ble.ble_type, ble.petkit_id)
            return False
        now = int(time.time())
        envelope = {
            "method": "thing.service.ble",
            "id": str(now),
            "params": {
                "device": {"type": ble.ble_type_int, "mac": ble.wire_mac},
                "payload": {"cmd": cmd, "data": data},
                "timestamp": now,
            },
            "version": "1.0.0",
        }
        await self._bridge.publish_service(device, "ble", envelope)
        log.info("BLE cmd %d -> %s (mac=%s) via parent %d",
                 cmd, ble.ble_type, ble.mac, device.petkit_id)
        return True

    async def request_ble_reading(self, device: Device, ble: BLEDevice) -> bool:
        """Ask the parent for a reading from this accessory, right now.

        Bypasses the `interval` throttle on purpose: this exists for the moment
        somebody is standing in front of the panel asking "is it alive", and
        making them wait up to four minutes for the timer is the opposite of an
        answer. The throttle is then re-armed so the periodic loop does not
        immediately ask again.
        """
        if not self._bridge.connected:
            return False
        await self._ble_connect(device, ble, action=1)
        self._ble_poll_ts[ble.petkit_id] = time.time()
        return True

    async def poll_ble_loop(self, period: float = 30.0) -> None:
        """Ask every paired accessory's parent for a reading, forever.

        An accessory reports only when the server tells its parent to open a
        BLE session -- the parent never does it unprompted. Until this existed
        the only thing that asked was the handler for the parent's own
        `property/post`, which meant an accessory paired to a FEEDER never
        reported at all: a feeder does not send that topic. Its owner saw an
        accessory that paired, appeared in Home Assistant, and stayed unknown.

        A timer rather than a reaction, because that is what the cloud does and
        because it is the only trigger that does not depend on the parent
        having something of its own to say. `_poll_ble_accessories` still holds
        each accessory's `interval`, so this loop can tick often without
        talking often; `period` is only how finely those intervals are honoured.
        """
        while True:
            try:
                await asyncio.sleep(period)
                if not self._bridge.connected or not self._ble_registry:
                    continue
                for ble in self._ble_registry.all():
                    if not ble.link_with:
                        continue
                    parent = self._registry.get(ble.link_with)
                    if parent is not None:
                        await self._poll_ble_accessories(parent)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad poll must not end the loop
                log.exception("BLE poll loop")

    async def _ble_connect(self, device: Device, ble: BLEDevice, action: int) -> None:
        """Open (`action=1`) or close (`action=0`) the parent's BLE session.

        `thing.service.connect` is the only way an accessory reports at all:
        the parent does not poll on its own initiative, so nothing arrives
        until this is pushed. Closing matters too — an accessory whose session
        is never ended keeps the parent's radio busy between readings.
        """
        if not self._bridge.connected:
            return
        now = int(time.time())
        envelope = {
            "method": "thing.service.connect",
            "id": str(now),
            "params": {
                "connect_action": action,
                # How long to HOLD the session open, in seconds. Without it the
                # parent opens the radio and lets it go again before the
                # accessory has said anything useful: a CTW3 answers a bare
                # open with a short 251/252 and only then does its own run-info
                # pass, so the status arrives after the window we were giving
                # it. 30 is what @strxno confirmed on hardware; the unit is
                # inferred from the value working, not from a capture of the
                # field being varied.
                **({"time": BLE_SESSION_HOLD_SECONDS} if action else {}),
                "device": {"type": ble.ble_type_int, "mac": ble.wire_mac},
                "timestamp": now,
            },
            "version": "1.0.0",
        }
        await self._bridge.publish_service(device, "connect", envelope)
        log.info("BLE %s -> %s (mac=%s) via parent %d",
                 "connect" if action else "disconnect",
                 ble.ble_type, ble.mac, device.petkit_id)

    async def publish_relay_update(self, device: Device) -> bool:
        """Tell a parent its accessory list has changed, so it refetches now.

        `dev_ble_device` answers with `nextTick: 3600`, and the parent honours
        it: pair an accessory and it is an HOUR before that parent knows to scan
        for the MAC. Everything downstream looks broken for that hour — the poll
        pushes `thing/service/connect` for an accessory the parent was never
        told about, and nothing comes back, which is indistinguishable from a
        wrong scan type or a bad MAC.

        `update_action: 1` is the trigger, and any other value (or no field at
        all) is logged by the firmware and ignored. Confirmed on a T5: the
        publish is followed immediately by a `GET /6/t5/dev_ble_device`.
        Reported by @strxno.

        Returns False when nothing could be sent, so a caller does not report a
        refresh that never left.
        """
        if not self._bridge.connected:
            return False
        now = int(time.time())
        envelope = {
            "method": "thing.service.ble_relay_update",
            "id": str(now),
            "params": {"update_action": 1},
            "version": "1.0.0",
        }
        await self._bridge.publish_service(device, "ble_relay_update", envelope)
        log.info("BLE relay list refresh -> parent %d", device.petkit_id)
        return True

    async def _poll_ble_accessories(self, device: Device) -> None:
        """Ask the parent to open a BLE relay session for each linked accessory
        so it starts posting ble_response frames. Without this poll the parent
        never relays W5 data. Throttled per accessory `interval`.

        Mirrors localkit ServiceConnect (thing/service/connect, connect_action=1).
        """
        if not self._bridge.connected or not self._ble_registry:
            return
        # `non_k3_for_parent`, not `get_linked`: a K3 is never in the relay list
        # at all (it travels inside the parent's own device_info), so asking a
        # parent to open a BLE session for one sent a `connect` with `type: 0`.
        linked = self._ble_registry.non_k3_for_parent(device.petkit_id)
        if not linked:
            return
        now = time.time()
        for ble in linked:
            interval = getattr(ble, "interval", 240) or 240
            if now - self._ble_poll_ts.get(ble.petkit_id, 0) < interval:
                continue
            self._ble_poll_ts[ble.petkit_id] = now
            await self._ble_connect(device, ble, action=1)

    def _update_linked_k3(self, device: Device, params: dict) -> BLEDevice | None:
        """Merge K3 consumable fields piggybacked on the parent's property post.

        Thin wrapper around :func:`update_linked_k3` — every transport now
        reaches the same helper, this is only kept for the MQTT call site's
        readability. See the free function for the full contract.
        """
        return update_linked_k3(device, params, self._ble_registry)

    async def _handle_ble_response(self, device: Device, params: dict) -> None:
        """Apply one relayed BLE frame to the accessory it came from.

        The accessory is identified by MAC, not by the relaying parent: the
        same W5 can in principle be reachable through more than one WiFi
        device, and the MAC is the only stable key in the frame. That is not
        hypothetical: one D4SH in the 2026-08-11 capture relays two separate
        type-14 fountains.

        TWO THINGS THIS DOES NOT DO, both visible in that capture and neither
        fixed here because neither can be tested without the hardware:

        * `ble_relay_over` is the completion of the CONNECT, not of the data
          exchange. The order is `connect(1)` -> `ble_relay_start(action=1)`
          -> `ble_relay_over(result=0)` -> and only THEN the response frames.
          Anything that treats `relay_over` as the end of a session would cut
          off everything it was opened for.
        * A failed write is not retried. `result=6` on `action=1` means the
          link never came up (73 of 1081 attempts), and the one `cmd 221`
          write in that capture hit it: the frame was correct, the session
          died 38 seconds later, and all 237 subsequent reads still showed the
          setting unchanged. Nothing noticed. A retry belongs here, but it has
          to be bounded -- this loop already opens a session per accessory per
          poll, and a retry that races the next poll would leave the parent's
          radio permanently busy.
        """
        if not self._ble_registry:
            return

        content = params.get("content", {})
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (ValueError, TypeError):
                content = {}

        ble_mac = dig(content, "device", "mac", default="")

        ble_dev = self._ble_registry.get_by_mac(ble_mac) if ble_mac else None
        if not ble_dev:
            log.debug("BLE response with no matching accessory (mac=%s)", ble_mac)
            return

        parse = parser_for(ble_dev.ble_type)
        fragment = parse(content) if parse is not None else {}
        if fragment:
            for section, vals in fragment.items():
                ble_dev.state.setdefault(section, {}).update(vals)
            ble_dev.last_seen = time.time()
            self._ble_registry.mark_dirty()
            log.info("Decoded %s (id=%d) from parent %d: %s",
                     ble_dev.ble_type.upper(), ble_dev.petkit_id,
                     device.petkit_id, json.dumps(fragment)[:120])
        else:
            # Not a status frame, or one we cannot read. Either way, name what
            # came back: a write is answered with a bare `01` on its own cmd,
            # or a short 251/252, and dropping those in silence left "the
            # switch flipped back" as the only symptom of a frame the accessory
            # did not accept.
            replies = [(cmd, bytes(data).hex() or "(empty)")
                       for cmd, data in _iter_ble_frames(content)]
            if replies:
                log.info("%s (id=%d) replied: %s", ble_dev.ble_type.upper(),
                         ble_dev.petkit_id,
                         ", ".join(f"cmd {c} {d}" for c, d in replies))
            else:
                log.info("%s ble_response not decodable yet (id=%d) - turn capture on in the "
                         "panel (Setup -> Settings) to collect frames",
                         ble_dev.ble_type.upper(), ble_dev.petkit_id)

        # Hang up only once a reading is actually in. A session is also
        # answered with frames that are not status — a bare `01` write ack, a
        # short 251/252 — and closing on those cut the radio before the
        # accessory got to its run-info pass, which is most of why a CTW3 was so
        # hard to get anything out of. The hold above bounds the session, so
        # leaving it open costs a fixed number of seconds rather than for ever.
        if fragment:
            await self._ble_connect(device, ble_dev, action=0)

        if self._ha_publisher:
            await self._ha_publisher.publish_ble_discovery(ble_dev)
            await self._ha_publisher.publish_ble_state(ble_dev)
