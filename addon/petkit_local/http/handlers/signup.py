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

import hashlib
import logging
from typing import Any

from aiohttp import web

from petkit_local.devices import payloads
from petkit_local.devices.base import TIMEZONE_PUSHED_KEY
from petkit_local.http.handlers._common import device_field, device_id, device_serial
from petkit_local.utils.coerce import to_float

log = logging.getLogger(__name__)

#: The range a UTC offset can occupy, in hours. Wider than the real world's
#: -12..+14 by nothing at all — the point is to reject a device reporting a
#: parse failure as 0 from being told apart from a device genuinely at UTC,
#: which is why an absent value and an unparseable one both leave the field
#: alone rather than writing a number.
_TZ_MIN, _TZ_MAX = -12.0, 14.0


def _remember_where_it_thinks_it_is(request: web.Request, device: Any,
                                    registry: Any) -> None:
    """Store the timezone and locale the device just reported about itself.

    Its signup body carries both — `timezone=%s&locale=%s` is in the T4's own
    format string, and a captured D4 body has `timezone=2.0&locale=Europe/
    Amsterdam`. Both were read off the wire and thrown away, so `to_signup`
    answered with the SERVER's offset and an empty locale: a device that had
    just said it was at -4.0 was told 2.0, and one that said `en-US` was told
    nothing.

    The device is the authority on where it is. It got that value over BLE at
    provisioning and burns it into its video watermarks, so a reply that
    contradicts it is at best noise and at worst a second, disagreeing source.

    Kept under its own key rather than written into `config["timezone"]`, which
    is the manual override for an install whose box lives in another zone —
    that has to keep winning over anything a device says about itself.
    """
    tz = to_float(device_field(request, "timezone"), None)
    if tz is not None and _TZ_MIN <= tz <= _TZ_MAX:
        if device.config.get("reported_timezone") != tz:
            device.config["reported_timezone"] = tz
            registry.mark_dirty()
        # A signup is the device saying what it holds NOW, which is newer than
        # whatever we last pushed to it — and it may well differ, since nobody
        # has shown that a runtime `property.set{timezone}` survives a reboot.
        # Forgetting the push makes `ha/timezone_sync.py` compare the zone
        # against this statement instead, and push again if it is wrong.
        if device.config.pop(TIMEZONE_PUSHED_KEY, None) is not None:
            registry.mark_dirty()

    locale = (device_field(request, "locale") or "").strip()
    if locale and device.config.get("locale") != locale:
        device.config["locale"] = locale
        registry.mark_dirty()


#: Where a locally-minted id starts. PetKit's own ids are nine digits opening
#: with `3` -- 300004258, 30020324, 30000369 across four families -- so ours
#: open with `4`: same shape, no chance of landing on a real one.
_LOCAL_ID_BASE = 400_000_000


def _local_device_id(*candidates: str) -> int | None:
    """Mint a stable id for a device that has never been given one.

    A device gets its id from PetKit at first registration and repeats it
    forever after. One that has never reached PetKit has none to repeat -- and
    the "broken" units people buy second-hand are exactly that, which is how a
    unit that works perfectly ends up unable to register with the thing built
    to replace the cloud it cannot reach.

    Derived from the MAC rather than allocated from a counter, because signup
    is retried: an allocator would mint a new device on every attempt and fill
    the registry with duplicates of one feeder. The same MAC therefore always
    produces the same id, across restarts and across a lost `devices.json`.

    Returns None when the device offered no stable identifier at all, which is
    the only case left where there is genuinely nothing to key an entry on.
    """
    seed = next((c.strip().lower() for c in candidates if c and c.strip()), "")
    if not seed:
        return None
    digest = hashlib.sha256(seed.encode()).digest()
    return _LOCAL_ID_BASE + int.from_bytes(digest[:4], "big") % 100_000_000


async def handle_signup(request: web.Request) -> web.Response:
    """Register the calling device, then hand it its identity back.

    `mac` and `firmware` are read from the query string or the POST body — they
    are not part of the `X-Device` header — and are stored as reported, since
    nothing else in the system can supply them. Reading them from the body is
    not cosmetic: a device that sends everything there would otherwise register
    with a blank serial, and `mqtt_device_name` is built from the serial once
    and never repaired (`registry.get_or_create` only fills blanks).

    Returns:
        `payloads.to_signup()` for the created or existing device. The one
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
    mac = device_field(request, "mac") or ""
    firmware = device_field(request, "firmware") or ""

    minted = False
    if petkit_id is None:
        # No id of its own: never registered with PetKit, or no longer able to
        # reach it. Being the local cloud means being the thing that hands one
        # out, not the second party to refuse.
        petkit_id = _local_device_id(mac, device_field(request, "bt_mac") or "")
        minted = petkit_id is not None
    if petkit_id is None:
        # Nothing stable to key an entry on -- not even a MAC. A 400 here is
        # still a device that will retry forever, but there is no id to invent
        # that would survive the next request.
        log.warning("Signup from a %s with no id and no MAC; cannot register it",
                    device_type)
        return web.json_response({"error": "missing device id"}, status=400)

    known = registry.get(petkit_id) is not None
    device = registry.get_or_create(
        petkit_id=petkit_id,
        device_type=device_type,
        serial_number=sn,
        mac=mac,
        firmware=firmware,
    )
    # An ESP32 feeder is the only source of its own BLE address, and it offers
    # it exactly once, here. `devices/payloads.py` already reads `bt_mac` out of
    # `config` when answering `dev_device_info`; nothing had ever written it.
    if minted and not known:
        # Said once, loudly: this id is OURS, not PetKit's, and anyone
        # comparing it against the app or a capture needs to know that.
        device.config["locally_assigned_id"] = True
        registry.mark_dirty()
        log.info("Registered a %s that had no id of its own: minted %d from its "
                 "MAC (%s). The same device will always get this id back.",
                 device_type, petkit_id, mac or "no mac")

    bt_mac = device_field(request, "bt_mac")
    if bt_mac and not device.config.get("bt_mac"):
        device.config["bt_mac"] = bt_mac
        registry.mark_dirty()
    _remember_where_it_thinks_it_is(request, device, registry)
    log.info("Signup: %s id=%d sn=%s fw=%s", device_type, petkit_id, sn, firmware)

    on_signup = request.app.get("on_signup")
    if on_signup:
        await on_signup(device)

    return web.json_response(payloads.to_signup(device))
