"""Composition root: every long-lived collaborator, built once and wired together.

Construction only — nothing here connects, binds a port or opens the event
store; that is `start_background`'s job. What comes out is a `Services` bundle
the lifecycle hooks read, so the wiring is in one place and the ordering
constraints between the pieces (the bridge holds the upstream, the upstream
publishes through the bridge) are visible next to each other.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import web

from petkit_local.ai.pets import PetRegistry
from petkit_local.config import Config
from petkit_local.devices.ble import BLERegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.ha.publisher import HAPublisher
from petkit_local.http.redact import RedactionPolicy
from petkit_local.http.server import create_app
from petkit_local.main.lifecycle import BACKGROUND_TASKS
from petkit_local.media.crypto import resolve_key_string
from petkit_local.media.go2rtc import Go2rtc
from petkit_local.media.retention import RetentionConfig
from petkit_local.mqtt.bridge import MQTTBridge
from petkit_local.mqtt.ble_relay import update_linked_k3
from petkit_local.mqtt.upstream import (
    CREDENTIALS_FILENAME, UpstreamCredentials, UpstreamMQTT,
)
from petkit_local.web.hub import EventHub

if TYPE_CHECKING:  # pragma: no cover - typing only
    import argparse

    from petkit_local.devices.base import Device


@dataclass(frozen=True)
class Services:
    """Everything `main()` built, named rather than positional.

    `ha_addon` and `no_mqtt` ride along because both decide what
    `start_background` does — the panel's log line and whether the embedded
    broker starts at all — and neither is derivable from the `Config`.
    """

    config: Config
    app: web.Application
    registry: DeviceRegistry
    ble_registry: BLERegistry
    hub: EventHub
    event_store: EventStore
    retention_config: RetentionConfig
    pet_registry: PetRegistry
    ha_publisher: HAPublisher | None
    mqtt_bridge: MQTTBridge | None
    upstream_mqtt: UpstreamMQTT | None
    go2rtc: Go2rtc
    app_config: dict[str, Any]
    media_root: str
    cert_path: str
    ha_addon: bool
    no_mqtt: bool


async def _republish_camera_state(ha_publisher: HAPublisher | None,
                                  registry: DeviceRegistry) -> None:
    """Push `streamUrl` out as soon as the sidecar comes up or goes down.

    Without it the sensor waits for the device to report of its own accord,
    which on a quiet box is minutes.
    """
    if ha_publisher is None:
        return
    for device in registry.all():
        if device.is_camera:
            await ha_publisher.publish_state(device)


def _proxy_policy(app_config: dict[str, Any], device: Device):
    """Redaction policy for a frame coming down from the real cloud.

    Rebuilt per frame, from the same live config the HTTP side reads, so
    a guard toggled in the panel applies to MQTT too.
    """
    return RedactionPolicy(
        device=device,
        api_url=app_config.get("api_url", ""),
        mqtt_host=urlparse(app_config.get("api_url", "")).hostname or "",
        bucket_endpoint=app_config.get("bucket_endpoint", ""),
        aes_key=resolve_key_string(app_config),
        block_rce=app_config.get("proxy_block_run_cmd", True),
        block_ota=app_config.get("proxy_block_ota", True),
        media_to_real_oss=app_config.get("proxy_media_real_oss", False),
        local_cvr_window=app_config.get("proxy_local_cvr_window", False),
    )


class _LocalRepublisher:
    """The `publish_local` callable `UpstreamMQTT` is handed.

    The bridge is attached after construction because it does not exist yet:
    `MQTTBridge` takes the upstream, and the upstream takes this.
    """

    def __init__(self) -> None:
        self.bridge: MQTTBridge | None = None

    async def __call__(self, topic: str, payload: bytes) -> None:
        """Republish a redacted cloud frame onto our own broker.

        Goes through the bridge's client rather than opening a second
        connection to a broker we are already attached to. QoS 0 and no
        retain, deliberately and regardless of what the cloud used — see
        `mqtt/upstream.py::_on_upstream` for why carrying those over is
        both pointless and unsafe here.
        """
        client = getattr(self.bridge, "_client", None)
        if client is not None:
            await client.publish(topic, payload)


async def _on_state_report(ha_publisher: HAPublisher, ble_registry: BLERegistry | None,
                           device: Device, body: dict) -> None:
    """Mirror a freshly parsed state report into HA — parent AND any linked K3.

    On an unpatched ESP32 (T4, D4, D3) the device only ever HTTP-heartbeats,
    so its property/post never reaches the MQTT bridge and the K3 piggyback
    lift on that path is a no-op. `dev_state_report` carries the identical
    top-level `battery`/`liquid`/`k3Id` keys, so extracting them here is the
    HTTP-side twin of `mqtt/bridge.py`'s property/post branch — mirrors the
    "ONE helper called from BOTH transports" rule the feeder parser already
    follows.

    Only installed when HA publishing is enabled; the HTTP handler
    treats a missing hook as "nothing to notify", so no other code
    has to know whether HA is configured.
    """
    await ha_publisher.publish_state(device)
    await ha_publisher.publish_availability(device)
    k3 = update_linked_k3(device, body, ble_registry)
    if k3:
        await ha_publisher.publish_ble_discovery(k3)
        await ha_publisher.publish_ble_state(k3)


async def _on_signup(ha_publisher: HAPublisher | None, device: Device) -> None:
    """Publish MQTT discovery for a device that just registered.

    Installed unconditionally (unlike `on_state_report`) because signup is
    also what creates the device, and the None-check is cheaper than a
    second conditional wiring branch.
    """
    if ha_publisher:
        await ha_publisher.publish_discovery(device)
        await ha_publisher.publish_availability(device)


async def _on_device_seen(ha_publisher: HAPublisher | None, device: Device) -> None:
    """Reflect any HTTP contact from a device as availability in HA.

    Cheaper and far more frequent than a state report: a heartbeat alone
    is enough to prove the device is alive.
    """
    if ha_publisher:
        await ha_publisher.publish_availability(device)


def build_services(config: Config, args: argparse.Namespace) -> Services:
    """Build the device-facing app and everything that outlives a request."""
    registry = DeviceRegistry(persist_path=f"{config.data_dir}/devices.json")
    ble_registry = BLERegistry(persist_path=f"{config.data_dir}/ble_devices.json")
    hub = EventHub()

    # Friendly media root (also used by the bucket/pipeline wiring below) —
    # computed up front so HAPublisher can resolve relative media paths for
    # the Last Clip sensor.
    media_root = "/media/petkit" if config.data_dir == "/data" else f"{config.data_dir}/media/petkit"

    ha_publisher = None if args.no_ha else HAPublisher(registry, {
        "ha_mqtt_host": config.ha_mqtt_host,
        "ha_mqtt_port": config.ha_mqtt_port,
        "ha_mqtt_user": config.ha_mqtt_user,
        "ha_mqtt_pass": config.ha_mqtt_pass,
        "ha_discovery_prefix": config.ha_discovery_prefix,
        "media_root": media_root,
    }, ble_registry=ble_registry, hub=hub)

    # Constructed here so every consumer below can be wired to it, but not yet
    # usable: opening it is async and happens in `start_background`.
    event_store = EventStore(f"{config.data_dir}/petkit.db")
    retention_config = RetentionConfig.load(config.data_dir)
    pet_registry = PetRegistry(event_store, f"{config.data_dir}/faces")

    # The one config dict the device-facing handlers, the panel and the MQTT
    # bridge all read, so a live setting change reaches every one of them
    # without a restart. Built BEFORE the bridge because the bridge holds it.
    app_config = config.to_app_config()

    # The camera sidecar. Constructed here so the panel and the HA publisher can
    # ask it for an RTSP URL; it starts nothing until `start_background` and
    # nothing at all unless a device is actually serving a stream.
    go2rtc = Go2rtc(registry, data_dir=config.data_dir,
                    on_change=partial(_republish_camera_state, ha_publisher, registry))
    # `--no-ha` leaves the publisher as None, and that is a supported way to run
    # this — the local cloud and the panel work with no Home Assistant at all.
    if ha_publisher is not None:
        ha_publisher.go2rtc = go2rtc

    # Proxy mode's MQTT half. The credential store is loaded unconditionally
    # (it is a small JSON file and holds what previous proxied sessions
    # learned); the bridge itself only connects while the panel asks for it.
    upstream_creds = UpstreamCredentials(f"{config.data_dir}/{CREDENTIALS_FILENAME}")

    mqtt_bridge = None
    upstream_mqtt = None
    if not args.no_mqtt:
        publish_local = _LocalRepublisher()
        upstream_mqtt = UpstreamMQTT(
            registry, upstream_creds, partial(_proxy_policy, app_config), publish_local,
            hub=hub, event_store=event_store, live_config=app_config,
        )
        mqtt_bridge = MQTTBridge(
            registry, ha_publisher, ble_registry,
            api_url=config.api_url, hub=hub,
            event_store=event_store, pet_registry=pet_registry,
            live_config=app_config, upstream=upstream_mqtt,
        )
        publish_local.bridge = mqtt_bridge

    # Wire the downstream command path: HA setting changes -> device via MQTT.
    if ha_publisher and mqtt_bridge:
        ha_publisher.set_command_sink(mqtt_bridge)

    app = create_app(registry, app_config)
    app["ble_registry"] = ble_registry
    app["event_hub"] = hub
    app["event_store"] = event_store
    app["pet_registry"] = pet_registry
    app["ha_publisher"] = ha_publisher
    # Written by the proxy middleware when a proxied dev_iot_device_info reveals
    # the device's real Aliyun credentials — the only place they can be learned.
    app["proxy_upstream_creds"] = upstream_creds
    # Populated by `_spawn` once the loop is up. Created here, before
    # `AppRunner` freezes the app — a frozen Application rejects new keys.
    app[BACKGROUND_TASKS] = []

    if ha_publisher:
        app["on_state_report"] = partial(_on_state_report, ha_publisher, ble_registry)

    app["on_signup"] = partial(_on_signup, ha_publisher)
    app["on_device_seen"] = partial(_on_device_seen, ha_publisher)

    cert_path = config.mqtt_cert or f"{config.data_dir}/certs/broker.crt"

    return Services(
        config=config,
        app=app,
        registry=registry,
        ble_registry=ble_registry,
        hub=hub,
        event_store=event_store,
        retention_config=retention_config,
        pet_registry=pet_registry,
        ha_publisher=ha_publisher,
        mqtt_bridge=mqtt_bridge,
        upstream_mqtt=upstream_mqtt,
        go2rtc=go2rtc,
        app_config=app_config,
        media_root=media_root,
        cert_path=cert_path,
        ha_addon=args.ha_addon,
        no_mqtt=args.no_mqtt,
    )
