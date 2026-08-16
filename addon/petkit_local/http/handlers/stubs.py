"""Endpoints answered from a constant or the Device model alone.

Plus `dev_event_report`, which is not a stub at all and lives here for
historical reasons: it persists events, refreshes device state and drives HA
entities.

The rest exist because a PetKit device blocks on them. Firmware treats a 4xx/5xx
as a server fault and retries forever, so every endpoint it calls needs *some*
well-formed answer even when the feature behind it (OTA, Agora live view, sound
packs) does not exist in a local deployment. The value each one returns is
therefore load-bearing: it is the answer that makes the device stop asking and
move on, not a placeholder.

Response *shape* matters as much as content — `ota_check`, `sound_get` and
`attire_over` must return JSON arrays, `multi_config`'s values must be
JSON-encoded strings, and so on. Those shapes were checked against the real
PetKit cloud (see the CHANGELOG's "Fidelity" entry); do not simplify them to
`{}` because a given field looks unused.
"""
from __future__ import annotations

import time

from aiohttp import web

from petkit_local.devices import payloads
from petkit_local.devices.base import Device
from petkit_local.events.ingest import (
    apply_derived_state, apply_state_snapshot, entity_for_event, from_event_report,
    parse_event_report_form,
)
from petkit_local.http.handlers._common import (
    device_id, no_device_response, request_device,
)
from petkit_local.mqtt.ble_relay import update_linked_k3
from petkit_local.media.crypto import resolve_key_string as _get_aes_key
from petkit_local.utils.capture import capture_record
from petkit_local.web.api.sounds import sound_list_for_device


async def handle_sync_time(request: web.Request) -> web.Response:
    """`dev_syncTime` — hand the device the current time so it can set its clock.

    Returns:
        ``{"result": <unix seconds>}``. This is the FIRST call of a device's
        boot sequence, before `dev_signup`, so it has to answer for a caller the
        registry has never heard of — a device with no clock cannot stamp events
        or run its own schedules.
    """
    return web.json_response({"result": int(time.time())})


async def handle_ota_check(request: web.Request) -> web.Response:
    """`dev_ota_check` / `dev_ota_heartbeat` — report that no firmware update exists.

    Returns:
        ``{"result": {}}`` — an OBJECT. This was an array, documented as "the
        real cloud's shape"; proxying the endpoint to PetKit on 2026-07-27
        showed the cloud answers `{"result":{}}` every time. Always empty by
        design either way: petkit-local hosts no firmware images, and a bad
        answer here is the one way this server could brick a device.
    """
    return web.json_response({"result": {}})


async def handle_attire_over(request: web.Request) -> web.Response:
    """`dev_attire_over` — acknowledge attire completion.

    Returns:
        ``{"result": 1}`` — an INTEGER. The real cloud returns ``1`` for a
        D4SH (capture 2026-08-11); the previous ``[0]`` was copied from an
        older capture and was wrong.
    """
    return web.json_response({"result": 1})


async def handle_oss_sts(request: web.Request) -> web.Response:
    """`dev_oss_sts_info_new_v2` — where to upload media, and with which key.

    The response's `capability[]` is the media control plane: a type the user
    switched off is absent from the list, and the device then stops uploading
    it at the source (see `Device.enabled_capabilities()`).

    Returns:
        `payloads.to_oss_sts()` — bucket URLs plus the AES key the device
        encrypts uploads with, which `media/pipeline.py` later decrypts against.
        An unidentified caller gets a full STS block built from a throwaway
        Device rather than the usual empty result, because that is the shape
        devices in the field already receive here and what the firmware does
        with a short one is untested.
    """
    config = request.app["config"]
    device = request_device(request)
    bucket_endpoint = config.get("bucket_endpoint", "")
    aes_key = _get_aes_key(config)
    if device:
        return web.json_response(payloads.to_oss_sts(device, bucket_endpoint, aes_key))

    # A throwaway Device, never registered: it only exists to render the payload
    # (see the docstring on why an unidentified caller gets a full block), and
    # minting a registry entry from an unidentified request is exactly what
    # `handlers/_common.py` refuses to do.
    x_dev = request.get("x_device", {})
    fallback = Device(device_type=x_dev.get("type", "unknown"), petkit_id=device_id(request) or 0)
    return web.json_response(payloads.to_oss_sts(fallback, bucket_endpoint, aes_key))


async def handle_video_device_info(request: web.Request) -> web.Response:
    """`dev_video_device_info` — tell a camera device it has no Agora account.

    Returns:
        `payloads.to_video_device_info()`, which is empty Agora credentials; the
        same empty block is returned for an unknown device. Live view runs over
        the local RTSP/go2rtc path instead, so the device has to be told the
        cloud relay does not exist rather than being left retrying it.
    """
    device = request_device(request)
    if device:
        return web.json_response(payloads.to_video_device_info(device))
    return web.json_response({"result": {"agora": {"license": "", "appId": ""}}})


async def handle_device_info(request: web.Request) -> web.Response:
    """`dev_device_info` — the device's own full configuration, as it sees it.

    Also the delivery path for a linked K3 spray, which has no WiFi and is
    embedded in its parent's payload rather than listed by `dev_ble_device` —
    hence the `ble_registry` lookup.

    Returns:
        `payloads.to_device_info()`, or the standard empty result for an unknown
        device.
    """
    device = request_device(request)
    if device:
        ble_registry = request.app.get("ble_registry")
        return web.json_response(payloads.to_device_info(device, ble_registry))
    return no_device_response()


async def handle_multi_config(request: web.Request) -> web.Response:
    """`dev_multi_config` — the schedule ranges (light, do-not-disturb, camera).

    Returns:
        `payloads.to_multi_config()`, whose values are JSON-encoded STRINGS rather
        than nested objects (verified against the real cloud), or the standard
        empty result for an unknown device.
    """
    device = request_device(request)
    if device:
        return web.json_response(payloads.to_multi_config(device))
    return no_device_response()


async def handle_event_report(request: web.Request) -> web.Response:
    """`dev_event_report` — record one reported event and fan it out.

    Sent by `ctrl`, which is always present, unlike the upload-only
    `dev_upload_file_info_v2` — so this is the one event source that works on a
    device with no media capability at all. Four things happen per report: the
    row is persisted to the EventStore, `device.state` is refreshed from the
    report's embedded `state` payload (which keeps a device that never calls
    `dev_state_report` again in sync), the panel's live log gets an entry, and
    the matching HA `event` entity fires.

    `event_type` here is a NUMERIC code string ("9", "10", "5", ...), a
    different namespace from MQTT's semantic strings; `events/normalize.py`'s
    module docstring has the confirmed mapping table.

    Returns:
        ``{"result": "success"}`` unconditionally. An unknown device, or a build
        with no event store, drops the report silently rather than reporting an
        error, per the never-fail-a-device rule in `http/server.py`.
    """
    registry = request.app["registry"]
    device = request_device(request, registry)

    store = request.app.get("event_store")
    if not device or store is None:
        return web.json_response({"result": "success"})
    petkit_id = device.petkit_id

    raw = await request.read()
    text = raw.decode("utf-8", "replace")

    form = parse_event_report_form(text)
    row = from_event_report(device, form)
    state = row.pop("_state", None)
    content = row.pop("_content", None)

    config = request.app["config"]
    if config.get("capture"):
        capture_record(config.get("capture_dir", "/data/capture"), "event_report",
                        {"id": petkit_id, "form": form})

    pet_registry = request.app.get("pet_registry")
    ha_publisher = request.app.get("ha_publisher")
    # `pet_ref` is what the device claimed; `pet_id` is what we could prove.
    # Resolving BEFORE the insert keeps the row consistent in one write, and
    # leaving `pet_id` None for an unknown id is what stops a stale cloud
    # identity from fabricating an HA pet device.
    if pet_registry is not None:
        row["pet_id"] = await pet_registry.resolve_pet_ref(row.get("pet_ref"))

    await store.upsert_event(row)

    if row.get("pet_id") is not None and pet_registry is not None and ha_publisher is not None:
        pet = await pet_registry.get(row["pet_id"])
        if pet:
            await ha_publisher.publish_pet_discovery(pet)
            await ha_publisher.publish_pet_state(pet, store)

    if apply_state_snapshot(device, state):
        registry.mark_dirty()
        if not device.state.get("ip") and request.remote:
            device.state["ip"] = request.remote

    # Applied AFTER the state block, so a derived timestamp is not overwritten
    # by the report the same request carried. Shared with `mqtt/bridge.py`.
    apply_derived_state(device, row["event_type"], content or {})
    registry.mark_dirty()

    hub = request.app.get("event_hub")
    if hub is not None:
        if isinstance(state, dict) and state:
            hub.set_state_report(petkit_id, state)
        hub.publish("event", petkit_id,
                    f"{row['event_type'] or 'event'} ({row['event_kind']})", detail=row)

    if ha_publisher is not None:
        # Resolved through the code table, not by name: over HTTP `event_type`
        # is a numeric code whose meaning depends on the device category.
        entity_suffix = entity_for_event(row["event_type"], device.device_type)
        if entity_suffix:
            await ha_publisher.publish_event(device, entity_suffix, row["event_type"], content)
        await ha_publisher.publish_state(device)
        await ha_publisher.publish_availability(device)
        # Every event report from a T4 carries `battery`/`liquid`/`k3Id` in
        # its embedded state — the ESP32's HTTP twin of the MQTT property/post
        # piggyback. See `mqtt.ble_relay.update_linked_k3`.
        if isinstance(state, dict):
            k3 = update_linked_k3(device, state, request.app.get("ble_registry"))
            if k3:
                await ha_publisher.publish_ble_discovery(k3)
                await ha_publisher.publish_ble_state(k3)

    return web.json_response({"result": "success"})


async def handle_sound_get(request: web.Request) -> web.Response:
    """`dev_sound_get` — the device's uploaded custom sounds.

    Returns:
        ``{"result": [...]}`` — one entry per uploaded sound, each carrying a
        download URL pointing at our bucket. Empty when no sounds have been
        uploaded. An ARRAY, because the firmware iterates the result.
    """
    config = request.app["config"]
    bucket_endpoint = config.get("bucket_endpoint", "")
    device = request_device(request)
    if device and bucket_endpoint:
        sounds = sound_list_for_device(request, device.petkit_id, bucket_endpoint)
        if sounds:
            return web.json_response({"result": sounds})
    return web.json_response({"result": []})
