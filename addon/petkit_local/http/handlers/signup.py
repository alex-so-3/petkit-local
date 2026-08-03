"""dev_signup — device registration, the first call of a device's life.

A device that has just been pointed at us announces itself here, and the reply
is what convinces it that it belongs to an account and may proceed to fetch MQTT
credentials, its config and its schedules. Everything downstream assumes the
registry entry this creates.

One of the two handlers that may CREATE a registry entry (the other is
iot_device_info); `handlers/_common.py` resolves but never creates, so the
`get_or_create` below stays here.
"""
from __future__ import annotations

import logging

from aiohttp import web

from petkit_local.http.handlers._common import device_id, device_serial, _coerce_device_id

log = logging.getLogger(__name__)


async def handle_signup(request: web.Request) -> web.Response:
    """Register the calling device, then hand it its identity back.

    `mac` and `firmware` are read from the query string only — they are not part
    of the `X-Device` header — and are stored as reported, since nothing else in
    the system can supply them.

    LOCAL PATCH (see CHANGELOG-LOCAL.md): some ESP32-family devices (D4
    feeder, confirmed on hardware 2026-08-01) send their identity as a
    `www-form-urlencoded` POST body instead of the query string / X-Device
    header the Ingenic family uses. When the header/query resolution comes up
    empty and the request is form-encoded, the body is consulted as a
    fallback — only for fields still missing, so a value already resolved
    from the trusted X-Device header is never overridden by the body.

    Returns:
        `Device.to_signup()` for the created or existing device. The one
        endpoint here that answers with an error status: without a usable id
        there is nothing to key a registry entry on, so a 400 is returned rather
        than minting a device under a fabricated id.

    Fires the app's ``on_signup`` callback (HA discovery) before replying, so a
    device is published to Home Assistant before it starts reporting state.
    """
    registry = request.app["registry"]
    device_type = request.get("device_type", "t5")

    petkit_id = device_id(request)
    sn = device_serial(request)
    mac = request.query.get("mac", "")
    firmware = request.query.get("firmware", "")

    if request.content_type == "application/x-www-form-urlencoded" and (
        petkit_id is None or not sn or not mac or not firmware
    ):
        try:
            form = await request.post()
        except Exception:
            form = {}
        if petkit_id is None:
            petkit_id = _coerce_device_id(form.get("id"))
        if not sn:
            sn = str(form.get("sn", "")).strip()
        if not mac:
            mac = str(form.get("mac", "")).strip()
        if not firmware:
            firmware = str(form.get("firmware", "")).strip()

    if petkit_id is None:
        return web.json_response({"error": "missing device id"}, status=400)

    device = registry.get_or_create(
        petkit_id=petkit_id,
        device_type=device_type,
        serial_number=sn,
        mac=mac,
        firmware=firmware,
    )
    log.info("Signup: %s id=%d sn=%s fw=%s", device_type, petkit_id, sn, firmware)

    on_signup = request.app.get("on_signup")
    if on_signup:
        await on_signup(device)

    return web.json_response(device.to_signup())
