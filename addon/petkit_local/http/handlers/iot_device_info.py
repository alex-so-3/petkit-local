"""dev_iot_device_info / dev_only_iot_device_info[_v2] — MQTT credentials.

The second of the two handlers that may CREATE a registry entry (the other is
signup): a device that was provisioned before this server existed asks for its
credentials without ever signing up again, and answering "unknown device" would
leave it with no way to reach the broker.
"""
from __future__ import annotations

import logging
from urllib.parse import urlparse

from aiohttp import web

from petkit_local.devices import payloads
from petkit_local.devices.base import Device
from petkit_local.http.handlers._common import (
    device_id, device_serial, no_device_response, request_device,
)

log = logging.getLogger(__name__)

#: The models answered with the FLAT credential block (no `ali` wrapper). Only
#: the ESP32 litter boxes: PetKit's own cloud answers a T3 (fw 1.491) flat on
#: `dev_iot_device_info` (proxied capture, issue #35), and a stock T4 (fw 1.652)
#: handed the wrapped block re-runs signup -> iot_device_info in a loop (the
#: loop of issue #33) where the flat block lets it settle (live, one T4; a T3
#: owner reports the same in #35). The one contrary report, a T4 on
#: LocalKit-patched firmware reading the wrapped block (#34), is not stock
#: firmware. Deliberately NOT
#: `not device.is_next_gen`: that complement also holds the ESP32 feeders, the
#: BLE-only codenames and any model not yet listed, none of which has evidence
#: either way, so they keep the wrapped block the D4SH capture showed.
FLAT_CREDENTIAL_TYPES = frozenset({"t3", "t4"})


def self_mqtt_host(config: dict) -> str:
    """Return the broker hostname to hand devices — the box they already reach.

    Derived from the configured `api_url`, not from the request's Host header:
    the device opens a separate MQTT connection, so the value has to be an
    address it can reach on its own.

    Takes the config rather than the request because proxy mode's redaction
    policy needs the same answer with no request in hand, and the two must not
    be able to disagree: redaction rewrites the upstream cloud's broker address
    to whatever this returns, so a second derivation that drifted would hand a
    device an address it is not listening on.

    Returns:
        The hostname, or "" when `api_url` has none to give. Empty is a working
        outcome, not a failure — the device then falls back to the HTTP
        heartbeat, which is slower but needs no broker.
    """
    return urlparse(config.get("api_url", "")).hostname or ""


def mqtt_host_for(device: Device, config: dict) -> str:
    """The `mqttHost` to hand `device`: our broker, unless opted out for ESP32.

    With the `esp32_aliyun_mqtt_host` option on, an ESP32 model (anything not
    `is_next_gen`) is handed `Device.aliyun_mqtt_host`,
    `<productKey>.iot-as-mqtt.eu-central-1.aliyuncs.com`, instead of the host
    `self_mqtt_host` derives from `api_url`. Off by default, and the Linux
    models never see it: they connect with the derived host, and the `cacert`
    patcher pins their trust to it.

    Why it exists: a T4 handed the API host completes the TLS handshake to the
    broker, then never gets a session up and gives up after ~10 s, while the
    same device handed a name under `iot-as-mqtt.eu-central-1.aliyuncs.com`
    connects at once (issue #34; independently, a D4S in another fork). The
    option is useful only with two things this add-on does not do for you:
    DNS that resolves that name to this host, and a broker certificate whose
    DNS SAN covers it (`*.iot-as-mqtt.eu-central-1.aliyuncs.com`) from a CA the
    device trusts. A stock ESP32 that pins PetKit's CA will refuse the
    certificate either way.

    Proxy mode does not consult this: its redaction rewrites the cloud's
    `mqttHost` to `self_mqtt_host` (`http/redact/rules.py`), so a proxied
    device is handed this server's address as before.
    """
    if config.get("esp32_aliyun_mqtt_host") and not device.is_next_gen:
        return device.aliyun_mqtt_host
    return device.resolve_mqtt_host(self_mqtt_host(config))


def _resolve_or_create_device(request: web.Request) -> Device | None:
    """Return the requesting device, registering it if it is unknown.

    Resolution (id, then serial) is the shared one, so an unusable id falls
    back to the serial number and we return the SAME device with its real MQTT
    credentials instead of minting a phantom device with fresh keys. Only if
    that finds nothing AND the request carries a usable id do we create.
    """
    device = request_device(request)
    if device is not None:
        return device

    petkit_id = device_id(request)
    sn = device_serial(request)
    if petkit_id is None:
        log.warning("iot_device_info: no device for id=%s sn=%s (X-Device=%s)",
                    petkit_id, sn, request.get("x_device", {}))
        return None

    registry = request.app["registry"]
    return registry.get_or_create(
        petkit_id=petkit_id,
        device_type=request.get("device_type", "t5"),
        serial_number=sn,
    )


async def handle_iot_device_info(request: web.Request) -> web.Response:
    """`dev_iot_device_info` / `dev_only_iot_device_info[_v2]` — MQTT credentials.

    All three endpoint names are routed here (`http/server.py`), so the shape
    is chosen by model, not by endpoint.

    Returns:
        The broker host plus the product key, device name and secret the
        device signs its MQTT CONNECT with (see `mqtt/auth.py`):
        `payloads.to_iot_device_info_flat()` for `FLAT_CREDENTIAL_TYPES`,
        `payloads.to_iot_device_info()` (nested in the Aliyun `ali` envelope)
        for everything else.
    """
    device = _resolve_or_create_device(request)
    if not device:
        return no_device_response()
    mqtt_host = mqtt_host_for(device, request.app["config"])
    flat = device.device_type in FLAT_CREDENTIAL_TYPES
    log.info("IoT device info%s: id=%d -> pk=%s dn=%s mqttHost=%s",
             " (flat)" if flat else "", device.petkit_id, device.mqtt_product_key,
             device.mqtt_device_name, mqtt_host)
    body = (payloads.to_iot_device_info_flat(device, mqtt_host) if flat
            else payloads.to_iot_device_info(device, mqtt_host))
    return web.json_response(body)


async def handle_iot_device_info_flat(request: web.Request) -> web.Response:
    """The same credentials, always un-nested. Not routed.

    `http/server.py` sends every endpoint name to `handle_iot_device_info`,
    which picks the flat shape itself for `FLAT_CREDENTIAL_TYPES`; this is
    kept for callers that want the flat block unconditionally.
    """
    device = _resolve_or_create_device(request)
    if not device:
        return no_device_response()
    mqtt_host = mqtt_host_for(device, request.app["config"])
    log.info("IoT device info (flat): id=%d -> pk=%s dn=%s mqttHost=%s",
             device.petkit_id, device.mqtt_product_key, device.mqtt_device_name, mqtt_host)
    return web.json_response(payloads.to_iot_device_info_flat(device, mqtt_host))
