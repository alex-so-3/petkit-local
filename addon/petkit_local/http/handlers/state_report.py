"""dev_state_report — the device's periodic full state dump.

Every ~30 seconds (the `interval` this handler hands back) a device posts
everything it knows about itself: work state, error flags, WiFi, consumable
levels. That single payload is what almost every Home Assistant sensor in this
project is ultimately rendered from, so the handler's job is to get it parsed
and into `device.state` — nothing here talks back to the device beyond the
poll interval and a timestamp.

The body is deliberately parsed defensively. It arrives as
`state=<JSON>` form-urlencoded, sometimes gzip-compressed, sometimes
percent-encoded, and on some models as a bare JSON body; a shape we fail to
recognise must degrade to "no state this round" rather than to an error status,
because a device whose state report 500s just retries the same body forever.
"""
from __future__ import annotations

import gzip
import json
import logging
import time
import urllib.parse

from aiohttp import web

from petkit_local.devices.state_parsers import (
    apply_consumable_state, normalize_property_params, parse_state_report,
)
from petkit_local.http.handlers._common import device_id, request_device
from petkit_local.utils.capture import capture_record
from petkit_local.utils.timeutil import cloud_timestamp

log = logging.getLogger(__name__)


def _extract_state(text: str) -> dict:
    """Pull the state object out of a request body, whatever shape it came in.

    The T5 posts `state=<JSON>` (form-urlencoded); a bare JSON body and a
    percent-encoded value are accepted too, so a model that frames the same
    payload differently is still parsed rather than silently ignored.

    Returns:
        The decoded state dict, or ``{}`` for anything unrecognised — including
        a JSON body that decodes to a list or a scalar. Never raises: the input
        is device-supplied text.
    """
    text = text.strip()
    candidate = None
    if text.startswith("state="):
        candidate = text[len("state="):]
    elif text.startswith("{"):
        candidate = text
    if candidate is None:
        return {}
    for value in (candidate, urllib.parse.unquote_plus(candidate)):
        try:
            obj = json.loads(value)
            if isinstance(obj, dict):
                return obj
        except (json.JSONDecodeError, ValueError, TypeError):
            continue
    return {}


async def handle_state_report(request: web.Request) -> web.Response:
    """Parse the device's state dump into `device.state`, then ask for the next.

    The payload is run through BOTH `parse_state_report` (flat keys) and
    `normalize_property_params` (the nested `litter{}`/`wifi{}`/`err{}` shape the
    T5 sends, identical to the MQTT property post) — which one applies depends on
    the model, and applying both costs nothing while covering either.

    Fires the app's ``on_state_report`` callback, which is what pushes the new
    values to Home Assistant. An unidentified device still gets a valid answer;
    its report is parsed only far enough to mirror onto the panel.

    Returns:
        ``{"result": {"interval": 30, "time": "<ISO-8601 with milliseconds>"}}``.
        `interval` is how many seconds the device waits before reporting again,
        so it is the knob that sets state freshness for every HA sensor.
    """
    registry = request.app["registry"]
    device = request_device(request, registry)
    petkit_id = device.petkit_id if device else (device_id(request) or 0)

    try:
        raw = await request.read()
    except Exception:
        # A truncated or malformed body still gets the normal reply, so the
        # device carries on reporting instead of retrying a payload we cannot
        # read anyway.
        raw = b""

    config = request.app["config"]
    capture_dir = config.get("capture_dir", "/data/capture")
    if config.get("capture"):
        capture_record(capture_dir, "state_report_raw", {
            "id": petkit_id,
            "type": request.get("device_type", ""),
            "content_type": request.content_type,
            "content_encoding": request.headers.get("Content-Encoding", ""),
            "len": len(raw),
            "raw_hex": raw[:80].hex(),
            "raw_text": raw.decode("utf-8", "replace")[:1500],
        })

    # Decode: handle gzip, then the body. The device POSTs
    # `application/x-www-form-urlencoded` as `state=<raw JSON>` (not a bare JSON
    # body), so extract the `state` field and parse it.
    data = raw
    if raw[:2] == b"\x1f\x8b" or request.headers.get("Content-Encoding", "").lower() == "gzip":
        try:
            data = gzip.decompress(raw)
        except Exception:
            data = raw
    text = data.decode("utf-8", "replace") if isinstance(data, (bytes, bytearray)) else str(data)
    body = _extract_state(text)

    if config.get("capture") and body:
        capture_record(capture_dir, "state_report", {"id": petkit_id, "body": body})

    hub = request.app.get("event_hub")
    if hub is not None and body:
        hub.set_state_report(petkit_id, body)

    if device and body:
        # The T5 posts a NESTED structure (litter{}, wifi{}, err{}) — same as the
        # MQTT property post — so normalize it to the flat keys the entities read.
        # parse_state_report still handles any device that sends flat keys.
        device.state.update(parse_state_report(device.device_type, body))
        device.state.update(normalize_property_params(device.device_type, body))
        if not device.state.get("ip") and request.remote:
            device.state["ip"] = request.remote
        apply_consumable_state(device)
        device.last_state_report = time.time()
        device.online = True
        log.info("State report from %s (id=%d): %d state keys", device.device_type, device.petkit_id, len(device.state))
        registry.mark_dirty()

        on_state = request.app.get("on_state_report")
        if on_state:
            await on_state(device, body)

    return web.json_response({
        "result": {
            # DELIBERATELY not the cloud's value. PetKit answers 3600 here; we
            # keep 30 because this interval is how often an idle device pushes
            # the state every HA sensor reads, and hourly refreshes would be a
            # visible regression. Events carry a state snapshot of their own,
            # so the cloud can afford to be quiet and we cannot.
            "interval": 30,
            # LOCAL time with the device's own offset, not UTC: the firmware
            # takes its timezone from this field and sets the RTC its scheduled
            # feeds fire against, so `+0000` here fires every meal two hours late
            # on a UTC+2 box (cloud_timestamp). An unidentified device (no offset
            # to speak for) falls back to UTC.
            "time": cloud_timestamp(
                offset_hours=device.timezone_offset if device else None),
        }
    })
