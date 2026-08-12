"""CommandRouter — the direction Home Assistant writes in.

Everything Home Assistant publishes to `petkit-local/{device_id}/cmd/{suffix}`
lands here: it is resolved to the entity that was discovered under that suffix,
turned into a device command (`ha/commands.py`), and delivered — over MQTT when
the device holds a session, else queued for its next HTTP heartbeat. A BLE
accessory has an HA identity of its own but no session at all, so its writes go
out as frames through the WiFi device that relays for it.

It publishes nothing itself. The one connection to HA's broker belongs to
`ha/publisher.py`, which owns this router, feeds it the messages off its own
subscription, and is called back for the optimistic state that keeps a control
from snapping back before the device confirms.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, AsyncIterable, Iterable

from petkit_local.devices.ble import ble_command_for
from petkit_local.ha.commands import RUN_CMD_SUFFIX, Refused, handle_ha_command
from petkit_local.patchers.common import send_run_cmd
from petkit_local.utils.coerce import to_int
from petkit_local.utils.logtext import excerpt

if TYPE_CHECKING:
    from petkit_local.devices.ble import BLERegistry
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.mqtt.bridge import MQTTBridge

log = logging.getLogger(__name__)

# A retained command payload can be arbitrarily large — bound what is logged.
PAYLOAD_LOG_CHARS = 120


def _ble_command_value(entity: Any, payload: str) -> int | None:
    """An HA command payload as the integer an accessory's frame carries.

    Switches arrive as ON/OFF, selects as one of their labels, numbers as a
    decimal string. Anything else returns None rather than a default: a write
    to a fountain is not worth guessing at.

    A button has no value at all — HA publishes the entity key as its
    `payload_press` — so it is answered with 0, which the command builder for a
    button never reads.
    """
    text = payload.strip()
    if entity.component == "button":
        return 0
    if entity.component == "switch":
        upper = text.upper()
        if upper in ("ON", "1", "TRUE"):
            return 1
        if upper in ("OFF", "0", "FALSE"):
            return 0
        return None
    if entity.component == "select":
        if text in (entity.options or []):
            index = list(entity.options).index(text)
            values = entity.option_values or list(range(len(entity.options)))
            # `option_values` are not necessarily numbers — the W7H's voice
            # language maps its labels to "en_US"/"zh_CN" — and an accessory
            # frame carries a byte. No Bluetooth model publishes such a select
            # today; coercing rather than `int()` keeps that a None instead of
            # an exception mid-publish if one ever does.
            return to_int(values[index], None)
        return None
    return to_int(text, None)


class CommandRouter:
    """Applies Home Assistant's writes to devices and BLE accessories.

    `_entity_index` is the routing table for the reverse direction: it is
    rebuilt by `publish_discovery`, so an entity that has never been published
    cannot be commanded — which is the intended behaviour, since HA cannot show
    a control it was never told about.
    """

    def __init__(self, publisher: HAPublisher, registry: DeviceRegistry,
                 ble_registry: BLERegistry | None = None) -> None:
        """Wire the router to the publisher that owns it.

        Args:
            publisher: Called back for the optimistic state publish that
                follows an applied command.
            registry: Resolves a command's device id; a topic naming an id it
                does not hold is an accessory's.
            ble_registry: Omit when the install has no BLE accessories — a
                command for one is then logged and dropped.
        """
        self._publisher = publisher
        self._registry = registry
        self._ble_registry = ble_registry
        # device_id -> {entity.unique_id_suffix: EntityDef}, for routing commands
        self._entity_index: dict[int, dict] = {}
        # Optional real-time downstream sink (the MQTT bridge). Used to push
        # settings changes to the device as Aliyun property.set messages.
        self._command_sink: MQTTBridge | None = None

    def set_command_sink(self, sink: MQTTBridge) -> None:
        """Wire the MQTT bridge so HA setting changes reach the device in real time."""
        self._command_sink = sink

    def set_entities(self, device_id: int, entities: Iterable[Any]) -> None:
        """Record which of a device's entities can be written to, by suffix.

        Called from the publisher's discovery passes, which is what makes the
        index track exactly what Home Assistant has been told about.
        """
        self._entity_index[device_id] = {
            e.unique_id_suffix: e for e in entities if e.is_settable}

    async def consume_commands(self, messages: AsyncIterable[Any],
                               fatal: tuple[type[BaseException], ...] = ()) -> None:
        """Dispatch HA commands, surviving a failure on any single one.

        Reconnecting costs a full rediscovery of every entity of every device,
        so one unparseable command payload must not be allowed to trigger it.

        Args:
            messages: Async iterable of aiomqtt messages.
            fatal: Exception types that mean the connection itself is gone and
                must therefore propagate to the reconnect handler.
        """
        async for message in messages:
            try:
                await self.handle_command(message)
            except asyncio.CancelledError:
                raise  # shutdown, not a bad command
            except fatal:
                raise
            except Exception:
                log.exception("Dropped HA command on %s: %s", message.topic,
                              excerpt(getattr(message, "payload", b""), PAYLOAD_LOG_CHARS))

    async def _handle_ble_command(self, ble_id: int, entity: Any, message: Any) -> None:
        """Apply one HA command to a BLE accessory, through its parent.

        Three things have to line up and any of them can be absent: the
        accessory must still be paired, the parent it is relayed by must be a
        registered device, and the accessory must have reported enough of its
        own state for the frame to be built (every write restates a whole
        block). Each is logged by name rather than collapsed into one failure,
        because from Home Assistant they look identical: the switch flips back
        and nothing happens.
        """
        ble_dev = self._ble_registry.get(ble_id) if self._ble_registry else None
        if ble_dev is None:
            log.warning("Command for unknown accessory %d", ble_id)
            return
        parent = self._registry.get(ble_dev.link_with)
        if parent is None:
            log.warning("Accessory %d has no registered parent (link_with=%s)",
                        ble_id, ble_dev.link_with)
            return
        if self._command_sink is None:
            log.warning("No MQTT bridge; cannot reach accessory %d", ble_id)
            return

        raw = message.payload
        payload = raw.decode(errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        value = _ble_command_value(entity, payload)
        if value is None:
            log.warning("Unusable value %r for %s on accessory %d",
                        payload, entity.key, ble_id)
            return

        try:
            cmd, frame_payload = ble_command_for(ble_dev, entity.key, value)
        except Refused as exc:
            log.warning("Refused %s on accessory %d: %s", entity.key, ble_id, exc)
            return

        sent = await self._command_sink.publish_ble_command(
            parent, ble_dev, cmd, frame_payload)
        if not sent:
            return
        # Optimistic, like a real device's: the accessory acknowledges the write
        # but its next status is what actually confirms it, and that is a poll
        # away. Reflecting immediately keeps the control from snapping back.
        # A button has no state to reflect — and no `value_path`, so writing one
        # anyway would file it under the empty string.
        #
        # The leading segment names the block the accessory's parser fills
        # (`states` or `consumables`, see devices/ble.py), so the reflection has
        # to land in that one; an unsectioned path is a `states` field.
        if entity.value_path:
            section, _, field = entity.value_path.rpartition(".")
            ble_dev.state.setdefault(section or "states", {})[field] = value
        if self._ble_registry:
            self._ble_registry.mark_dirty()
        await self._publisher.publish_ble_state(ble_dev)

    async def handle_command(self, message: Any) -> None:
        """Apply one HA command published to petkit-local/{device_id}/cmd/{suffix}.

        A topic that doesn't fit that shape is dropped without a word (the
        wildcard subscription can match anything), while an unknown device or
        entity is logged — that one means HA and the registry disagree.
        """
        topic = str(message.topic)
        parts = topic.split("/")
        if len(parts) != 4 or parts[0] != "petkit-local" or parts[2] != "cmd":
            return

        device_id = to_int(parts[1], None)
        if device_id is None:
            return
        suffix = parts[3]

        device = self._registry.get(device_id)
        entity = self._entity_index.get(device_id, {}).get(suffix)
        if entity is None:
            log.warning("Command for unknown entity '%s' on device %d", suffix, device_id)
            return

        if device is None:
            # Not a real device: an accessory has an HA identity of its own but
            # lives in the BLE registry, and its commands travel through the
            # parent that relays for it.
            await self._handle_ble_command(device_id, entity, message)
            return

        raw = message.payload
        try:
            payload = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
        except UnicodeDecodeError:
            # Not mojibake-repaired on purpose: a `text` entity would persist the
            # replacement characters into device config and serve them back.
            log.warning("Ignoring non-UTF-8 HA command on %s: %s", topic,
                        excerpt(raw, PAYLOAD_LOG_CHARS))
            return
        log.info("HA command %s=%s for device %d", suffix,
                 excerpt(payload, PAYLOAD_LOG_CHARS), device_id)

        try:
            result = handle_ha_command(device, entity, payload)
        except Refused as exc:
            # HA bounds its own number controls, so this is a command that came
            # from somewhere else on the topic. Log and drop: the entity keeps
            # its old value and the next state publish puts HA back in step.
            log.warning("Refused HA command on %s: %s", topic, exc)
            return

        # Deliver to the device: real-time over MQTT if the device has an active
        # MQTT session, else queue for the next HTTP heartbeat poll.
        if result:
            service_suffix, mqtt_payload = result
            # A `@run_cmd` is a shell line for the device's run_cmd path (the T6
            # pan buttons), not a thing/service publish; send_run_cmd picks the
            # transport itself, so it bypasses the publish/queue split below.
            if service_suffix == RUN_CMD_SUFFIX:
                await send_run_cmd(device, mqtt_payload["command"], self._command_sink)
                self._registry.mark_dirty()
                await self._publisher.publish_state(device)
                return
            device_on_mqtt = self._command_sink is not None and device.mqtt_connected
            if device_on_mqtt:
                try:
                    await self._command_sink.publish_to_device(device, service_suffix, mqtt_payload)
                except Exception as e:
                    log.warning("MQTT delivery failed for device %d, queuing for heartbeat: %s", device_id, e)
                    if isinstance(mqtt_payload, dict):
                        mqtt_payload["_service_suffix"] = service_suffix
                    device.command_queue.append(mqtt_payload)
            else:
                log.info("Device %d not on MQTT, queuing command for HTTP heartbeat", device_id)
                if isinstance(mqtt_payload, dict):
                    mqtt_payload["_service_suffix"] = service_suffix
                device.command_queue.append(mqtt_payload)

        # Reflect optimistic state and persist.
        self._registry.mark_dirty()
        await self._publisher.publish_state(device)
