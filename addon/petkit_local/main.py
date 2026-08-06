"""Entry point: builds the device-facing app and owns the process lifecycle.

Everything that outlives a single request is created in `start_background` and
torn down in `cleanup_background`, in a deliberate order — see the comment on
`cleanup_background` for why the event store closes last.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import ssl
import sys
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any

from aiohttp import web

from petkit_local.config import Config
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.devices.ble import BLERegistry
from petkit_local.http.server import create_app
from petkit_local.http.handlers.upload_file_info import wait_for_pending as wait_for_media_tasks
from petkit_local.http.proxy import close_proxy_session
from petkit_local.ha.publisher import HAPublisher
from petkit_local.mqtt.bridge import MQTTBridge
from petkit_local.web.hub import EventHub
from petkit_local.http.bucket import create_bucket_app
from petkit_local.web.panel import create_panel_app

if TYPE_CHECKING:  # pragma: no cover - typing only
    from petkit_local.devices.base import Device

# Every other module logs under `__name__`; this one is also an entry point, and
# `python3 -m petkit_local.main` (what the add-on's Dockerfile runs) would make
# that "__main__" — a useless prefix in the add-on log. Pin the module path so
# the name is the same whether this module is imported or executed.
log = logging.getLogger(__name__ if __name__ != "__main__" else "petkit_local.main")

#: aiohttp app key holding every long-lived task started by `start_background`.
#: The list is created before `AppRunner` freezes the app, and `_spawn` is the
#: ONLY place a task gets created — shutdown iterates this list rather than a
#: hand-maintained tuple of names, which had already drifted out of date.
BACKGROUND_TASKS = "background_tasks"


def _spawn(app_instance: web.Application, name: str,
           coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
    """Start a long-lived background task AND register it for shutdown.

    Creating and registering in one step is the point: a task that shutdown
    does not know about keeps running against a closed event store / broker.

    Args:
        name: Task name, used in shutdown log lines.
    """
    task = asyncio.create_task(coro, name=name)
    app_instance[BACKGROUND_TASKS].append(task)
    return task


async def _stop_tasks(tasks: list[asyncio.Task[Any]]) -> None:
    """Cancel every task, then await them all.

    Cancel-then-await in two passes so a slow task does not delay the others'
    cancellation. Tolerates a task that already finished (awaiting it just
    re-delivers its result) and one that raises something other than
    `CancelledError` on the way out: only `CancelledError` is swallowed, any
    other exception is logged with its traceback rather than being hidden or
    aborting the rest of shutdown.
    """
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Background task %s failed during shutdown", task.get_name())


def main() -> None:
    """Parse the command line, build the `Config`, and run until stopped.

    Two entry paths converge here: `--ha-addon` reads the add-on options file
    written by the Supervisor, while the individual flags are the way the
    add-on is run outside Home Assistant. Flags always win over the add-on
    options, so a bad option file can be overridden without editing it.
    """
    parser = argparse.ArgumentParser(description="petkit-local: local PetKit cloud replacement")
    parser.add_argument("--config", default="config.json", help="Config file path")
    parser.add_argument("--port", type=int, default=None, help="HTTP port override")
    parser.add_argument("--mqtt-port", type=int, default=None, help="MQTT broker port")
    parser.add_argument("--api-url", default=None, help="API URL the device sees")
    parser.add_argument("--data-dir", default=None, help="Data directory for persistence")
    parser.add_argument("--ha-mqtt-host", default=None, help="HA MQTT broker host")
    parser.add_argument("--ha-mqtt-port", type=int, default=None, help="HA MQTT broker port")
    parser.add_argument("--ha-mqtt-user", default=None)
    parser.add_argument("--ha-mqtt-pass", default=None)
    parser.add_argument("--no-ha", action="store_true", help="Disable HA MQTT publishing")
    parser.add_argument("--no-mqtt", action="store_true", help="Disable embedded MQTT broker")
    parser.add_argument("--offline-timeout", type=int, default=None, help="Seconds without contact before a device is marked offline")
    parser.add_argument("--mqtt-tls", action="store_true", help="Enable device-facing MQTT TLS listener")
    parser.add_argument("--mqtt-tls-port", type=int, default=None, help="MQTT TLS listener port (default 443)")
    parser.add_argument("--mqtt-cert", default=None, help="TLS cert path (self-signed generated if missing)")
    parser.add_argument("--mqtt-key", default=None, help="TLS key path")
    parser.add_argument("--mqtt-strict-auth", action="store_true", help="Enforce Aliyun HMAC sign (default accept-all)")
    # Every other port has a flag; these two did not, so a standalone run could
    # not move the panel or the media bucket off their defaults without hand-
    # writing a config file. That matters outside the add-on: Home Assistant
    # Container and Core have no add-on system at all, so those users run this
    # as a plain container beside HA and configure it entirely from the command
    # line.
    parser.add_argument("--web-port", type=int, default=None, help="Web panel port (default 8099)")
    parser.add_argument("--bucket-port", type=int, default=None, help="Media upload bucket port (default 9000)")
    parser.add_argument("--bucket-endpoint", default=None,
                        help="Media bucket base URL the device is told to use "
                             "(default: https://<api-url host>:<bucket-port>)")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--ha-addon", action="store_true", help="Self-configure from /data/options.json + Supervisor (HA add-on mode)")
    args = parser.parse_args()

    if args.ha_addon:
        config = Config.from_ha_addon()
    else:
        config = Config.from_file(args.config)
        if args.port:
            config.http_port = args.port
        if args.mqtt_port:
            config.mqtt_port = args.mqtt_port
        if args.api_url:
            config.api_url = args.api_url
        if args.data_dir:
            config.data_dir = args.data_dir
        if args.ha_mqtt_host:
            config.ha_mqtt_host = args.ha_mqtt_host
        if args.ha_mqtt_port:
            config.ha_mqtt_port = args.ha_mqtt_port
        if args.ha_mqtt_user:
            config.ha_mqtt_user = args.ha_mqtt_user
        if args.ha_mqtt_pass:
            config.ha_mqtt_pass = args.ha_mqtt_pass
        if args.offline_timeout is not None:
            config.offline_timeout = args.offline_timeout
        if args.mqtt_tls:
            config.mqtt_tls = True
        if args.mqtt_tls_port is not None:
            config.mqtt_tls_port = args.mqtt_tls_port
        if args.mqtt_cert:
            config.mqtt_cert = args.mqtt_cert
        if args.mqtt_key:
            config.mqtt_key = args.mqtt_key
        if args.mqtt_strict_auth:
            config.mqtt_strict_auth = True
        if args.web_port is not None:
            config.web_port = args.web_port
        if args.bucket_port is not None:
            config.bucket_port = args.bucket_port
        if args.bucket_endpoint is not None:
            config.bucket_endpoint = args.bucket_endpoint
        if args.debug:
            config.log_level = "DEBUG"

        # Re-read the panel's overrides now that --data-dir has been applied.
        # `from_file` already read them, but from wherever `data_dir` pointed
        # BEFORE the flag moved it — and proxy mode and capture now live only in
        # that file, so a standalone run with --data-dir would otherwise start
        # with them silently off however the panel was left.
        config.apply_panel_overrides()

    # After the flags, because --api-url and --bucket-port both feed it. The
    # add-on path already has an endpoint from the Supervisor, so this is a
    # no-op there.
    config.resolve_bucket_endpoint()

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        stream=sys.stdout,
    )

    if args.ha_addon:
        # After logging is configured, so the one line it writes is visible, and
        # before anything long-running, so the sidebar entry appears while the
        # user is still looking at the add-on page they just started.
        from petkit_local.config import show_in_sidebar_once
        show_in_sidebar_once(config.data_dir)

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

    from petkit_local.events.store import EventStore
    from petkit_local.media.retention import RetentionConfig, RetentionSweeper
    from petkit_local.ai.pets import PetRegistry
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
    from petkit_local.media.go2rtc import Go2rtc
    async def _republish_camera_state() -> None:
        """Push `streamUrl` out as soon as the sidecar comes up or goes down.

        Without it the sensor waits for the device to report of its own accord,
        which on a quiet box is minutes.
        """
        if ha_publisher is None:
            return
        for device in registry.all():
            if device.is_camera:
                await ha_publisher.publish_state(device)

    go2rtc = Go2rtc(registry, data_dir=config.data_dir,
                    on_change=_republish_camera_state)
    # `--no-ha` leaves the publisher as None, and that is a supported way to run
    # this — the local cloud and the panel work with no Home Assistant at all.
    if ha_publisher is not None:
        ha_publisher.go2rtc = go2rtc

    # Proxy mode's MQTT half. The credential store is loaded unconditionally
    # (it is a small JSON file and holds what previous proxied sessions
    # learned); the bridge itself only connects while the panel asks for it.
    from petkit_local.mqtt.upstream import (
        CREDENTIALS_FILENAME, UpstreamCredentials, UpstreamMQTT,
    )
    upstream_creds = UpstreamCredentials(f"{config.data_dir}/{CREDENTIALS_FILENAME}")

    mqtt_bridge = None
    upstream_mqtt = None
    if not args.no_mqtt:
        def _proxy_policy(device: Device):
            """Redaction policy for a frame coming down from the real cloud.

            Rebuilt per frame, from the same live config the HTTP side reads, so
            a guard toggled in the panel applies to MQTT too.
            """
            from petkit_local.http.redact import RedactionPolicy
            from petkit_local.media.crypto import resolve_key_string
            from urllib.parse import urlparse
            return RedactionPolicy(
                device=device,
                api_url=app_config.get("api_url", ""),
                mqtt_host=urlparse(app_config.get("api_url", "")).hostname or "",
                bucket_endpoint=app_config.get("bucket_endpoint", ""),
                aes_key=resolve_key_string(app_config),
                block_rce=app_config.get("proxy_block_run_cmd", True),
                block_ota=app_config.get("proxy_block_ota", True),
                media_to_real_oss=app_config.get("proxy_media_real_oss", False),
            )

        async def _publish_local(topic: str, payload: bytes) -> None:
            """Republish a redacted cloud frame onto our own broker.

            Goes through the bridge's client rather than opening a second
            connection to a broker we are already attached to. QoS 0 and no
            retain, deliberately and regardless of what the cloud used — see
            `mqtt/upstream.py::_on_upstream` for why carrying those over is
            both pointless and unsafe here.
            """
            client = getattr(mqtt_bridge, "_client", None)
            if client is not None:
                await client.publish(topic, payload)

        upstream_mqtt = UpstreamMQTT(
            registry, upstream_creds, _proxy_policy, _publish_local,
            hub=hub, event_store=event_store, live_config=app_config,
        )
        mqtt_bridge = MQTTBridge(
            registry, ha_publisher, ble_registry,
            api_url=config.api_url, hub=hub,
            event_store=event_store, pet_registry=pet_registry,
            live_config=app_config, upstream=upstream_mqtt,
        )

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
        async def on_state_report(device: Device, body: dict) -> None:
            """Mirror a freshly parsed state report into HA.

            Only installed when HA publishing is enabled; the HTTP handler
            treats a missing hook as "nothing to notify", so no other code
            has to know whether HA is configured.
            """
            await ha_publisher.publish_state(device)
            await ha_publisher.publish_availability(device)

        app["on_state_report"] = on_state_report

    async def on_signup(device: Device) -> None:
        """Publish MQTT discovery for a device that just registered.

        Installed unconditionally (unlike `on_state_report`) because signup is
        also what creates the device, and the None-check is cheaper than a
        second conditional wiring branch.
        """
        if ha_publisher:
            await ha_publisher.publish_discovery(device)
            await ha_publisher.publish_availability(device)

    app["on_signup"] = on_signup

    async def on_device_seen(device: Device) -> None:
        """Reflect any HTTP contact from a device as availability in HA.

        Cheaper and far more frequent than a state report: a heartbeat alone
        is enough to prove the device is alive.
        """
        if ha_publisher:
            await ha_publisher.publish_availability(device)

    app["on_device_seen"] = on_device_seen

    cert_path = config.mqtt_cert or f"{config.data_dir}/certs/broker.crt"

    async def start_background(app_instance: web.Application) -> None:
        """aiohttp on_startup hook: open the store, then spawn every service.

        Ordering is the whole point of this function. The event store is
        opened and repaired before any reader exists, and each long-lived
        service is started through `_spawn` so shutdown can cancel it from
        `app[BACKGROUND_TASKS]` instead of a hand-maintained list.
        """
        # FIRST: open the event store (schema create + in-place migration).
        # Everything that queries it — the device-facing handlers, the MQTT
        # bridge, the panel, the sweepers — starts only after this hook
        # returns, so this is the one point where the DB is guaranteed to be
        # migrated before a single reader exists.
        from petkit_local.events.ingest import _MODULE_TYPE_TO_CATEGORY, backfill_event_rows
        await event_store.connect()
        # Correct any rows stored under an older, wrong moduleType->category
        # mapping before anything reads them (notably the stitcher, which must
        # never mix two different video streams — see events/ingest.py).
        await event_store.reclassify_media_categories(_MODULE_TYPE_TO_CATEGORY)
        # Same idea for events: re-derive event_kind/parent_event on rows stored
        # before those were understood, so old sessions group correctly.
        await backfill_event_rows(event_store)

        # Both registries are constructed before the loop exists (nothing is
        # started in their constructors); this hands them the running loop so
        # `mark_dirty()` coalesces writes instead of fsyncing per message.
        await registry.start()
        await ble_registry.start()

        # Web panel, over HTTP only. It used to be served a SECOND time on
        # 8098 over HTTPS with a self-signed certificate and no authentication,
        # purely so Web Bluetooth had a top-level secure context — which put
        # every device setting, command, pet record and on-device patcher on
        # the LAN for anyone who could reach the port. Provisioning now asks
        # the operator for a real certificate instead (see the Provision tab).
        panel_config = {
            "api_url": config.api_url,
            "mqtt_port": config.mqtt_port, "mqtt_tls": config.mqtt_tls,
            "mqtt_tls_port": config.mqtt_tls_port,
            "strict_auth": config.mqtt_strict_auth,
            "capture": config.capture, "capture_dir": config.capture_dir,
            "cert_path": cert_path,
            "settings_path": str(config.overrides_path),
            "data_dir": config.data_dir,
            "media_root": media_root,
            "device_log_root": config.device_log_dir,
            # The panel reports WHY device logs cannot be collected, and an
            # unsplittable (or absent) bucket address is one of the two reasons.
            "bucket_endpoint": config.bucket_endpoint,
        }
        # Pass the SAME app_config dict the device-facing handlers read, so the
        # panel can flip runtime settings (proxy, capture) live with no restart.
        panel = create_panel_app(registry, ble_registry, hub, panel_config,
                                 mqtt_bridge, live_config=app_config,
                                 event_store=event_store, retention_config=retention_config,
                                 pet_registry=pet_registry, ha_publisher=ha_publisher)
        # The panel asks the sidecar for each device's RTSP URL. Set here rather
        # than passed to `create_panel_app`, so every test that builds a panel
        # does not have to know about a camera sidecar it never uses — but
        # BEFORE `runner.setup()`, which freezes the Application and makes a new
        # key a DeprecationWarning today and an error under aiohttp 4.
        panel["go2rtc"] = go2rtc
        runner = web.AppRunner(panel)
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", config.web_port).start()
        app_instance["panel_runner"] = runner
        # "(HA Ingress)" only when there IS a Supervisor to proxy it. Home
        # Assistant Container and Core have no add-on system, so this also runs
        # as a plain container beside HA, and telling those users their panel is
        # behind Ingress sends them looking for a sidebar entry that cannot
        # exist.
        log.info("Web panel on port %d%s", config.web_port,
                 " (HA Ingress)" if args.ha_addon else "")

        # Local S3/OSS bucket — accepts uploads from the device's cloud
        # process. Raw uploads (AES-encrypted, cryptically named) land in a
        # HIDDEN dir HA's media source ignores; media/pipeline.py
        # decrypts/remuxes/renames them into the friendly tree below once
        # dev_upload_file_info_v2 describes what they are. Same filesystem,
        # so the move is an atomic rename.
        raw_root = f"{media_root}/.raw"
        os.makedirs(raw_root, exist_ok=True)
        app_config["media_root"] = media_root
        app_config["media_raw_root"] = raw_root
        # Device logs live under data_dir, not the media share: they are not
        # media-browser content, and keeping them out of `.raw` stops the media
        # pipeline's substring-matched file lookup from ever claiming one.
        device_log_root = config.device_log_dir
        os.makedirs(device_log_root, exist_ok=True)
        app_config["device_log_root"] = device_log_root
        bucket_app = create_bucket_app(raw_root, hub=hub, log_root=device_log_root,
                                       registry=registry)
        bucket_runner = web.AppRunner(bucket_app)
        await bucket_runner.setup()
        # Bucket needs TLS: cloud parses the PAR URL as https:// and crashes
        # on http://. Reuse the same self-signed cert as the MQTT TLS listener.
        # Only the CERTIFICATE work is fallible here. The bind must stay outside
        # the try: this used to wrap `.start()` too, so an EADDRINUSE on 9000 —
        # another add-on, a MinIO container — was "handled" by binding the same
        # port again, which raised again and escaped `start_background`. A
        # degradable condition became a crash loop, and the log line blaming TLS
        # was never written.
        bkt_ctx = None
        try:
            from petkit_local.mqtt.broker import ensure_self_signed
            bkt_key = config.mqtt_key or f"{config.data_dir}/certs/broker.key"
            if ensure_self_signed(cert_path, bkt_key):
                bkt_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                bkt_ctx.load_cert_chain(cert_path, bkt_key)
        except Exception as e:
            log.warning("Local bucket TLS failed (%s), falling back to plain HTTP", e)

        await web.TCPSite(bucket_runner, "0.0.0.0", config.bucket_port,
                          ssl_context=bkt_ctx).start()
        if bkt_ctx is not None:
            log.info("Local bucket (HTTPS) on port %d -> %s", config.bucket_port, media_root)
        else:
            # Worth a warning rather than an info: the `cloud` process parses
            # the upload URL as https:// and crashes on http://, so plain HTTP
            # means media uploads will not work at all.
            log.warning("Local bucket on port %d (plain HTTP - cloud may reject)",
                        config.bucket_port)
        app_instance["bucket_runner"] = bucket_runner

        if ha_publisher:
            _spawn(app_instance, "ha-publisher", ha_publisher.start())
            _spawn(app_instance, "availability-watchdog",
                   ha_publisher.availability_watchdog(config.offline_timeout))

        # A BLE accessory reports only when we tell its parent to open a
        # session. Reacting to the parent's own traffic is not enough — a
        # feeder has none to react to (see `poll_ble_loop`).
        if mqtt_bridge is not None:
            _spawn(app_instance, "ble-poll", mqtt_bridge.poll_ble_loop())

        sweeper = RetentionSweeper(event_store, retention_config,
                                   log_root=config.device_log_dir,
                                   raw_root=raw_root,
                                   thumb_root=f"{config.data_dir}/thumbs")
        _spawn(app_instance, "retention-sweeper", sweeper.run())

        # Joins each visit's rolling ~4s chunks into one continuous clip once
        # the episode goes quiet (see media/stitch.py).
        from petkit_local.media.stitch import EpisodeStitcher
        stitcher = EpisodeStitcher(event_store, registry, media_root,
                                   work_dir=f"{media_root}/.raw", hub=hub)
        _spawn(app_instance, "episode-stitcher", stitcher.run())

        if not args.no_mqtt:
            from petkit_local.mqtt.broker import start_broker
            broker = await start_broker(
                config.mqtt_port, registry,
                tls=config.mqtt_tls, tls_port=config.mqtt_tls_port,
                certfile=cert_path,
                keyfile=config.mqtt_key or f"{config.data_dir}/certs/broker.key",
                strict_auth=config.mqtt_strict_auth, hub=hub,
            )
            app_instance["mqtt_broker"] = broker
            # The panel is a separate application, and its device view reports
            # what the broker will actually deliver (`broker.delivery_view`) —
            # the only thing that distinguishes a command with nowhere to land
            # from one the firmware ignored. Set here rather than passed to
            # `create_panel_app`, because the broker starts after it is built.
            panel["mqtt_broker"] = broker

            if mqtt_bridge:
                _spawn(app_instance, "mqtt-bridge",
                       mqtt_bridge.start("127.0.0.1", config.mqtt_port))

            if upstream_mqtt:
                # Proxy mode's upstream half runs alongside the local session
                # and follows a live panel toggle rather than the bridge's
                # connection state — so it is its own tracked task, not
                # something the bridge starts. `_spawn` is the only place a
                # task is created, precisely so shutdown can find it.
                _spawn(app_instance, "upstream-mqtt-supervisor",
                       upstream_mqtt.supervise())

        # Camera sidecar. Own task for the same reason as the one above: what it
        # should be running is derived from the devices, not from anything here.
        _spawn(app_instance, "go2rtc-supervisor", go2rtc.supervise())

    async def cleanup_background(app_instance: web.Application) -> None:
        """Tear down in dependency order: writers first, storage last.

        1. Media-pipeline tasks (`dev_upload_file_info_v2`) outlive their
           request and write to the event store, so they are drained FIRST.
           `http/server.py::create_app` registers the same drain earlier in
           this signal; repeating it here is a no-op when nothing is pending
           and keeps the ordering guarantee next to the `close()` it protects,
           instead of depending on aiohttp's signal order staying as it is.
        2. Long-lived tasks (HA publisher, watchdog, retention sweeper,
           stitcher, MQTT bridge) — every one of them writes to the event
           store and/or a registry, so they stop before either is closed.
           Proxy mode's upstream MQTT connections are closed right after, by
           hand: the supervisor task in that list SPAWNED them, so cancelling it
           leaves the connections themselves open.
        3. Broker, then the panel/bucket runners. `AppRunner.cleanup()` fires
           each sub-app's own `on_cleanup`, which is what drains the panel's
           patcher tasks (`web/panel.py::cancel_background_tasks`).
        4. Registries flush last among the writers: their final `stop()` must
           capture whatever steps 1-3 changed on their way out.
        5. `event_store.close()` is last, once nothing can still write to it.

        The proxy's shared upstream session is closed by a separate
        `close_proxy_session` hook registered after this one.
        """
        await wait_for_media_tasks()

        await _stop_tasks(app_instance[BACKGROUND_TASKS])
        app_instance[BACKGROUND_TASKS].clear()

        # The supervisor spawned these, so cancelling it does not close them —
        # they are per-device tasks of their own, holding live TLS connections
        # to Aliyun.
        if upstream_mqtt:
            await upstream_mqtt.stop()

        # Belt and braces: `supervise()` kills the child on its own cancellation,
        # but a supervisor that died earlier would have left it running, and an
        # orphaned go2rtc would hold both the RTSP port and the device's one
        # connection past our exit.
        await go2rtc.stop()

        if "mqtt_broker" in app_instance:
            await app_instance["mqtt_broker"].shutdown()

        if "bucket_runner" in app_instance:
            await app_instance["bucket_runner"].cleanup()

        if "panel_runner" in app_instance:
            await app_instance["panel_runner"].cleanup()

        # Stops the debounced writer and synchronously writes anything still
        # pending — without it the debounce would trade crash-safety for lost
        # writes at every restart.
        await registry.stop()
        await ble_registry.stop()

        await event_store.close()

    app.on_startup.append(start_background)
    app.on_cleanup.append(cleanup_background)
    # Closed last: the shared upstream connection pool must outlive anything
    # above that could still be forwarding a request.
    app.on_cleanup.append(close_proxy_session)

    log.info("Starting petkit-local on port %d", config.http_port)
    log.info("API URL: %s", config.api_url)
    log.info("MQTT broker: port %d %s", config.mqtt_port, "(disabled)" if args.no_mqtt else "")
    log.info("HA MQTT: %s:%d %s", config.ha_mqtt_host, config.ha_mqtt_port,
             "(disabled)" if args.no_ha else "")
    log.info("Registered devices: %d", len(registry.all()))

    web.run_app(app, host="0.0.0.0", port=config.http_port, print=None)


if __name__ == "__main__":
    main()
