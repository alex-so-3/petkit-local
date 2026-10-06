"""The Devices tab: the list, one device's detail, and its writes.

`_device_summary` is the shared base of the list and the detail, so a key added
there appears in both. The detail then inlines everything a device panel needs —
resolved entity values, actions, and the three sidecar views — because the panel
renders one device from one request and refreshes on every WebSocket tick.

The per-device toggles (capabilities, AI, log upload) keep their own endpoints
for anything scripting the API, and share their bodies with the detail.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from aiohttp import web

from petkit_local.devices import defaults
from petkit_local.devices.base import Device
from petkit_local.devices.state_parsers import apply_consumable_state
from petkit_local.events import codes
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.ha.commands import (
    ALL_ACTIONS, PROPERTY_SET_SUFFIX, Refused, handle_ha_command,
    make_mqtt_property_set, multi_config_texts,
)
from petkit_local.media.go2rtc import stream_urls_with_rtsp
from petkit_local.mqtt.broker import delivery_view
from petkit_local.utils.coerce import to_float
from petkit_local.utils.timeutil import offset_hours_for_locale
from petkit_local.utils.const import device_display_name
from petkit_local.utils.dicts import dig_path
from petkit_local.web.api._common import (
    _deliver, _device_log_reason, _device_or_404, _json_body,
)

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from petkit_local.devices.ble import BLERegistry
    from petkit_local.web.hub import EventHub


# Actions that COST something you cannot get back — the UI colours these red,
# confirms before sending, and sorts them last so a mis-click lands on a safe
# button. The line is "spends a consumable or takes the box out of service",
# not "moves the motor":
#
#   * `dump_litter` throws away the litter that is in the drum;
#   * `maintenance_start` stops the box serving the cat until someone ends it;
#   * the consumable resets overwrite a replacement date, and for the N50 that
#     date exists NOWHERE else (see devices/state_parsers.py) — press it by
#     accident and the real replacement date is gone.
#
# `reset` and `maintenance_stop` were in here and are not disruptive at all:
# both are `thing.service.end`, i.e. the verbs that STOP whatever is running
# and put the box back in service. Colouring the recovery button the same red
# as the one you are recovering from is exactly backwards.
#   * `fountain_drain` empties a W7H and leaves its FlowLift module raised
#     until somebody runs Refill.
DESTRUCTIVE_ACTIONS = {
    "dump_litter", "maintenance_start", "reset_n50", "reset_n60", "reset_desiccant",
    "fountain_drain",
}


def _state_doc(d: Device) -> dict[str, Any]:
    """Same shape the HA publisher publishes, for value resolution.

    Recomputes the consumable countdowns first, for the same reason
    `HAPublisher._build_state` does: they move with the calendar, and the N50's
    has no device input to trigger it.
    """
    apply_consumable_state(d)
    settings = d.config.get("settings") or {}
    enabled = d.enabled_capabilities()
    return {
        "state": d.state or {},
        "settings": settings,
        "schedule": d.config.get("schedule", []),
        "feed_schedule": d.config.get("feed_schedule", {}),
        "capabilities": {ct: (ct in enabled) for ct in d.CAPABILITY_TYPES},
        "multi_config": multi_config_texts(d),
    }


def _delivery_view(broker: Any, d: Device) -> dict | None:
    """What the broker would deliver to this device, or None if it isn't running.

    `--no-mqtt`, and every panel test, runs with no broker at all.
    """
    if broker is None:
        return None
    return delivery_view(broker, d.mqtt_product_key, d.mqtt_device_name)


def _device_summary(d: Device, ble_registry: BLERegistry | None, hub: EventHub,
                    broker: Any = None) -> dict[str, Any]:
    """One device panel header: identity, liveness, traffic counters and BLE children.

    This is the shared base of both `/api/devices` and `/api/devices/{id}`, so a
    key added here appears in both.
    """
    diag = hub.diag(d.petkit_id)
    return {
        "id": d.petkit_id,
        "type": d.device_type,
        "name": device_display_name(d.device_type),
        "sn": d.serial_number,
        "mac": d.mac,
        "firmware": d.firmware,
        "online": d.online,
        "mqtt_connected": d.mqtt_connected,
        "is_camera": d.is_camera,
        "is_feeder": d.is_feeder,
        "supports_ai": d.supports_ai,
        "pk": d.mqtt_product_key,
        "dn": d.mqtt_device_name,
        "last_heartbeat": d.last_heartbeat,
        "last_state_report": d.last_state_report,
        "last_seen": d.last_seen,
        "last_mqtt": d.last_mqtt,
        "mqtt_subscriptions": list(d.mqtt_subscriptions),
        # What the broker will really deliver, as opposed to what we asked it
        # for above. Absent when the broker is not running (tests, --no-mqtt).
        "mqtt_delivery": _delivery_view(broker, d),
        "queue": len(d.command_queue),
        "http_count": diag.get("http_count", 0),
        "mqtt_count": diag.get("mqtt_count", 0),
        "entities": len(get_entities_for_device(d)),
        "ble": [{"id": b.petkit_id, "type": b.ble_type, "mac": b.mac}
                for b in (ble_registry.get_linked(d.petkit_id) if ble_registry else [])],
    }


async def api_devices(request: web.Request) -> web.Response:
    """A bare JSON array of `_device_summary` objects — no envelope object."""
    reg = request.app["registry"]
    ble = request.app["ble_registry"]
    hub = request.app["hub"]
    return web.json_response([_device_summary(d, ble, hub, request.app.get("mqtt_broker")) for d in reg.all()])


# --- device detail: the four sidecar views ---------------------------------
# Each of these is the GET body of its own endpoint AND part of the device
# detail. They live here so the two can never answer differently: the panel
# renders one device from one request, and the standalone endpoints stay for
# anything scripting the API.

def _capabilities_view(d: Device) -> dict[str, Any]:
    """Which media capabilities this device has, and which are switched on."""
    enabled = d.enabled_capabilities()
    return {
        "is_camera": d.is_camera,
        "capabilities": {ct: (ct in enabled) for ct in Device.CAPABILITY_TYPES},
    }


def _timezone_view(d: Device) -> dict[str, Any]:
    """The effective UTC offset and each source that could have supplied it.

    All are shown because an effective `0.0` otherwise gives no clue whether UTC
    was chosen or merely inherited — and a device provisioned before the BLE
    payload carried a timezone reports exactly that. `source` names which rung of
    `Device.timezone_offset` actually won, so `locale` here means the offset was
    recovered from the zone name the device sent, not from the number it did.
    """
    override = to_float(d.config.get("timezone"), None)
    reported = to_float(d.config.get("reported_timezone"), None)
    from_locale = offset_hours_for_locale(d.config.get("locale"))
    return {
        "effective": d.timezone_offset,
        "override": override,
        "reported": reported,
        "locale": d.config.get("locale", ""),
        "source": "override" if override is not None
        else "locale" if from_locale is not None
        else "device" if reported is not None else "server",
    }


def _ai_view(d: Device) -> dict[str, Any]:
    """Whether on-device recognition is supported and switched on."""
    return {
        "supports_ai": d.supports_ai,
        "ai_enabled": bool(d.config.get("ai_enabled", True)),
    }


def _log_settings_view(d: Device, request: web.Request) -> dict[str, Any]:
    """Debug-log collection state, plus why it cannot work if it cannot."""
    return {
        "ok": True,
        "log_upload_enabled": bool(d.config.get("log_upload_enabled", False)),
        "reason": _device_log_reason(request),
    }


async def api_device_detail(request: web.Request) -> web.Response:
    """Everything the device detail view needs, in one object.

    A `_device_summary` plus `state`, `settings`, `config`, `diag`, `entities`,
    `actions`, and the three sidecar views (`capInfo`, `logInfo`, `aiInfo`) that
    the panel would otherwise have to fetch separately — one panel refresh is
    one request, which matters now that there is a panel per device refreshing
    on every WebSocket tick.

    Each entity carries its resolved `value`, read out of `_state_doc` with the
    same dotted `value_path` HA's value_template uses, so the panel and HA can
    never disagree about what a sensor currently reads. `actions` is the subset
    of button entities that map to a real command, flagged `destructive` when
    the UI should confirm first.
    """
    ble = request.app["ble_registry"]
    hub = request.app["hub"]
    d = _device_or_404(request)

    entities = get_entities_for_device(d)
    doc = _state_doc(d)
    detail = _device_summary(d, ble, hub, request.app.get("mqtt_broker"))
    detail.update({
        "state": d.state,
        "settings": d.config.get("settings", {}),
        "config": {k: v for k, v in d.config.items() if k != "settings"},
        "diag": hub.diag(d.petkit_id),
        # Where to watch this device. Lives on the DEVICE, not on the patcher
        # that enables it: the patcher card is about applying and undoing a
        # change to the firmware, and once that is done the address belongs
        # with everything else about the device. Empty until a probe has
        # confirmed the device is really serving (`media/go2rtc.py`).
        "streams": stream_urls_with_rtsp(d, request.app.get("go2rtc")),
        # Every schedule this model has, with the value it is REALLY running:
        # `multi_config_ranges` resolves a stored one against the fallback the
        # device would otherwise be served, so the panel's editor and
        # `dev_multi_config` cannot show different things.
        "schedules": defaults.schedule_targets(d),
        "entities": [{
            "component": e.component, "key": e.key, "name": e.name,
            "value_path": e.value_path, "unit": e.unit, "device_class": e.device_class,
            "icon": e.icon, "options": e.options, "option_values": e.option_values,
            "settable": e.is_settable,
            # Config/diagnostic, exactly as HA groups them — the panel sorts by
            # it so the two present the same entity in the same place.
            "entity_category": e.entity_category,
            "min": e.min_value, "max": e.max_value, "step": e.step,
            # Same dotted `value_path` HA's value_template reads.
            "value": dig_path(doc, e.value_path),
        } for e in entities],
        # Destructive LAST, otherwise in declaration order (`sorted` is stable).
        # They were interleaved -- Enter/Exit Maintenance and Dump Litter sat
        # third to fifth in a row of eleven -- so the buttons you would not want
        # to hit by accident were in the middle of the ones you press daily.
        "actions": sorted(
            ({"key": e.key, "name": e.name,
              "destructive": e.key in DESTRUCTIVE_ACTIONS}
             for e in entities if e.component == "button" and e.key in ALL_ACTIONS),
            key=lambda a: a["destructive"]),
        # The sidecars, so rendering one device costs one request. With a panel
        # per device refreshing on every WebSocket tick, four requests each was
        # the difference between idle and a steady stream of them.
        "capInfo": _capabilities_view(d) if d.is_camera else None,
        "timezoneInfo": _timezone_view(d),
        "logInfo": _log_settings_view(d, request),
        "aiInfo": _ai_view(d) if d.supports_ai else None,
    })
    return web.json_response(detail)


async def api_send_command(request: web.Request) -> web.Response:
    """Send one command to a device.

    The body is one of three forms: `{"action": ...}` (a named button action),
    `{"entity": ..., "value": ...}`, or a raw `{"suffix": ..., "payload": ...}`
    escape hatch. The first two both route through `handle_ha_command`, so
    coercion, the optimistic settings update and a button's side effects stay
    identical to what Home Assistant does with the same press.

    Answers `{ok, delivered, suffix, envelope}` — or `{ok, delivered, entity}`
    for an entity write that only changed local state. `delivered` is what
    actually happened, not what was asked for: a failed MQTT publish falls back
    to the heartbeat queue rather than erroring, because the device picks the
    command up on its next poll either way.
    """
    reg = request.app["registry"]
    hub = request.app["hub"]
    bridge = request.app["bridge"]
    d = _device_or_404(request)

    body = await _json_body(request)

    # transport: "auto" (default) picks MQTT only when the device has a live
    # session, else the HTTP heartbeat queue. "mqtt"/"heartbeat" force it.
    transport = body.get("transport", "auto")
    action = body.get("action")
    entity_key = body.get("entity")

    if action:
        fn = ALL_ACTIONS.get(action)
        if not fn:
            return web.json_response({"error": f"unknown action {action}"}, status=400)
        # A named action and a `button` entity of the same key are the SAME
        # press and must have the same side effects. `handle_ha_command` is
        # where a consumable reset records its replacement date, and calling
        # `ALL_ACTIONS` directly skipped it: Reset N50 from the panel sent the
        # device command (a no-op for the N50 — see `ha/commands.py`) and never
        # wrote the date, which is that countdown's ONLY possible source. The
        # same button in Home Assistant worked, because HA routes through the
        # handler. `ALL_ACTIONS` stays the fallback for an action with no
        # entity behind it.
        ent = next((e for e in get_entities_for_device(d)
                    if e.key == action and e.component == "button"), None)
        if ent is None and d.device_type.lower() in codes.FOUNTAIN_NEXT_GEN:
            # NOT the fallback for a W7H. Every litter action is a
            # `start_action` too, and 4, 7, 9 and 10 are on this firmware's
            # accept list — the same position 5 was in until it crashed the
            # device (`codes.FOUNTAIN_W7H_START_ACTIONS_CRASH`). A W7H runs
            # only the jobs its own buttons name.
            return web.json_response(
                {"error": f"{action} is not an action this device has"}, status=400)
        if ent is None:
            suffix, envelope = fn(d)
        else:
            try:
                result = handle_ha_command(d, ent, "")
            except Refused as exc:
                return web.json_response({"error": str(exc)}, status=400)
            reg.save()
            if result is None:
                hub.record_command(d.petkit_id, "local", action)
                return web.json_response({"ok": True, "delivered": "local", "action": action})
            suffix, envelope = result
    elif entity_key is not None:
        # Route through the same handler as HA so coercion (switch/number/select
        # option_values) and optimistic settings update stay identical.
        ent = next((e for e in get_entities_for_device(d) if e.key == entity_key), None)
        if ent is None:
            return web.json_response({"error": f"unknown entity {entity_key}"}, status=400)
        try:
            result = handle_ha_command(d, ent, str(body.get("value", "")))
        except Refused as exc:
            # Understood and rejected. Answering ok here would tell the caller
            # a value took effect when the setting was left exactly as it was.
            return web.json_response({"error": str(exc)}, status=400)
        reg.save()
        if result is None:
            hub.record_command(d.petkit_id, "local", f"{ent.key}={body.get('value')}")
            return web.json_response({"ok": True, "delivered": "local", "entity": ent.key})
        suffix, envelope = result
    else:
        suffix = body.get("suffix")
        envelope = body.get("payload")
        if not suffix or envelope is None:
            return web.json_response({"error": "need action, entity+value, or suffix+payload"}, status=400)

    return await _deliver(hub, bridge, d, suffix, envelope, transport)


_TZ_MIN, _TZ_MAX = -12.0, 14.0


async def api_timezone(request: web.Request) -> web.Response:
    """GET/POST this device's fixed UTC offset override.

    POST takes `{"timezone": 5.75}` (a numeric string is accepted, for the HTML
    number input) or `{"timezone": null}` to go back to automatic. The value is
    persisted and used by `dev_signup` and `dev_device_info` from their next
    request, and pushed to a device that is on MQTT right away.

    **The MQTT value is a STRING and that is not a style choice.**
    `parse_recv_property_set_normal` reads `cJSON.valuestring` and calls
    `atof()`, so a JSON number leaves `valuestring` NULL and the write is a
    silent no-op -- verified on a live T5, where `5.75` as a number changed
    nothing for minutes and `"5.75"` moved the video watermark within ~30
    seconds. The HTTP payloads keep sending a NUMBER for the same field,
    because that is what their parser reads.

    Readback is lossy: the device reports its own `%.1f`, so `5.75` comes back
    as `5.8`. Delivery is immediate but the echo waits for an irregular
    `property/post`, so "sent" is not "visible".
    """
    reg = request.app["registry"]
    d = _device_or_404(request)

    if request.method == "GET":
        return web.json_response(_timezone_view(d))

    body = await _json_body(request)
    if "timezone" not in body:
        return web.json_response({"error": "need timezone"}, status=400)

    raw = body["timezone"]
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        d.config.pop("timezone", None)
    else:
        # `bool` is an int in Python and would coerce to 0.0/1.0 as an offset.
        value = None if isinstance(raw, bool) else to_float(raw, None)
        if value is None or not _TZ_MIN <= value <= _TZ_MAX:
            return web.json_response(
                {"error": f"timezone must be a number between {_TZ_MIN:g} "
                          f"and {_TZ_MAX:g}"}, status=400)
        d.config["timezone"] = value
    reg.save()

    # The runtime clock only moves for a `property.set{timezone}` whose value is
    # a JSON STRING (`f"{...:g}"` keeps it one); `dev_device_info` carries the
    # number as a fallback for a device that has not yet been pushed. Deliver by
    # whichever transport is live, and — crucially — fall back to the heartbeat
    # queue for a device on HTTP. It used to publish only over MQTT, so for a
    # feeder that never reaches the broker the push was a silent no-op: it stayed
    # on whatever it reported at signup (UTC for a box given only a locale) and
    # fired its scheduled meals two hours late.
    hub = request.app["hub"]
    bridge = request.app.get("bridge")
    envelope = make_mqtt_property_set({"timezone": f"{d.timezone_offset:g}"})
    mqtt_live = (d.mqtt_connected and bridge is not None
                 and getattr(bridge, "_client", None))
    delivered = "mqtt"
    if mqtt_live:
        try:
            await bridge.publish_to_device(d, PROPERTY_SET_SUFFIX, envelope)
        except Exception as exc:  # noqa: BLE001 - transport failure is not a rejection
            log.warning("timezone push failed for device %d: %s", d.petkit_id, exc)
            mqtt_live = False
    if not mqtt_live:
        envelope["_service_suffix"] = PROPERTY_SET_SUFFIX
        d.command_queue.append(envelope)
        delivered = "heartbeat-queue"
    hub.record_command(d.petkit_id, delivered,
                       f"{PROPERTY_SET_SUFFIX} timezone={d.timezone_offset:g}")

    return web.json_response({"ok": True, "delivered": delivered, **_timezone_view(d)})


async def api_capabilities(request: web.Request) -> web.Response:
    """GET/POST the STS media capability toggles (see devices/base.py::
    Device.CAPABILITY_TYPES) — the control point is the next
    dev_oss_sts_info_new_v2 poll, not a device push.

    Both methods answer `{"capabilities": {name: bool}}` with every capability
    present, so the UI never has to guess a default. POST applies only the keys
    it recognises and republishes HA state, since the capability switches are
    entities there too.
    """
    reg = request.app["registry"]
    d = _device_or_404(request)

    if request.method == "GET":
        return web.json_response(_capabilities_view(d))

    body = await _json_body(request)

    caps = d.config.setdefault("capabilities", {})
    for ct in Device.CAPABILITY_TYPES:
        if ct in body:
            caps[ct] = bool(body[ct])
    reg.save()

    ha_publisher = request.app.get("ha_publisher")
    if ha_publisher is not None:
        await ha_publisher.publish_state(d)

    enabled = d.enabled_capabilities()
    return web.json_response({"ok": True, "capabilities": {ct: (ct in enabled) for ct in Device.CAPABILITY_TYPES}})


async def api_ai_settings(request: web.Request) -> web.Response:
    """GET/POST the on-device facial recognition on/off toggle — separate
    from the STS media capabilities (see dev_discern_config).

    Answers `{"ai_enabled": bool}` (GET also reports `supports_ai`). The toggle
    is stored for every device, including those that cannot do recognition, so
    the value survives if a device is later replaced by one that can.
    """
    reg = request.app["registry"]
    d = _device_or_404(request)

    if request.method == "GET":
        return web.json_response(_ai_view(d))

    body = await _json_body(request)

    if "ai_enabled" in body:
        d.config["ai_enabled"] = bool(body["ai_enabled"])
        reg.save()
    return web.json_response({"ok": True, "ai_enabled": bool(d.config.get("ai_enabled", True))})


async def api_device_log_settings(request: web.Request) -> web.Response:
    """GET/POST whether this device may upload its own debug log to us.

    Answers `{"log_upload_enabled": bool}`. Per device rather than global,
    matching the media-capability and AI toggles: it decides what one device is
    told at `dev_upload_log_token`, and `http/bucket.py` checks it again on the
    upload itself so switching it off takes effect before the device's token
    would have expired.
    """
    reg = request.app["registry"]
    d = _device_or_404(request)

    if request.method == "POST":
        body = await _json_body(request)
        if "log_upload_enabled" in body:
            d.config["log_upload_enabled"] = bool(body["log_upload_enabled"])
            reg.save()

    return web.json_response(_log_settings_view(d, request))


async def api_device_delete(request: web.Request) -> web.Response:
    """Remove a device from the registry and clean up its HA entities."""
    reg = request.app["registry"]
    d = _device_or_404(request)
    did = d.petkit_id

    publisher = request.app.get("publisher")
    if publisher:
        await publisher.unpublish_discovery(d)

    ble = request.app.get("ble_registry")
    orphaned = []
    if ble:
        for acc in ble.get_linked(did):
            orphaned.append(acc.petkit_id)

    reg.remove(did)

    hub = request.app.get("event_hub")
    if hub:
        hub.forget_device(did)

    return web.json_response({
        "ok": True,
        "removed": did,
        "orphaned_accessories": orphaned,
        "note": "Device removed. Orphaned BLE accessories, if any, must be deleted separately.",
    })
