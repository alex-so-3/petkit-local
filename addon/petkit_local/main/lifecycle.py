"""The two hooks aiohttp runs around the request loop: startup and shutdown.

`start_background` opens the event store and starts every long-lived service;
`cleanup_background` stops them in dependency order — see its docstring for why
the event store closes last. Every task either goes through `_spawn` or is not
cancelled at all, which is the whole reason `_spawn` exists.
"""
from __future__ import annotations

import asyncio
import logging
import os
import ssl
from collections.abc import Coroutine
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from aiohttp import web

from sqlalchemy.exc import SQLAlchemyError

from petkit_local.devices.state_parsers import reports_used_times
from petkit_local.events.ingest import _MODULE_TYPE_TO_CATEGORY, backfill_event_rows
from petkit_local.events.normalize import visit_day_key
from petkit_local.http.bucket import create_bucket_app
from petkit_local.http.handlers.upload_file_info import wait_for_pending as wait_for_media_tasks
from petkit_local.media.retention import RetentionSweeper
from petkit_local.media.stitch import EpisodeStitcher
from petkit_local.mqtt.broker import ensure_self_signed, start_broker
from petkit_local.web.panel import create_panel_app

if TYPE_CHECKING:  # pragma: no cover - typing only
    from petkit_local.main.wiring import Services

log = logging.getLogger(__name__)

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


async def _seed_visit_counts(registry: Any, event_store: Any) -> None:
    """Restore `state.usedTimes` for the boxes whose firmware never reports it.

    Only those: `reports_used_times` is False exactly for the models where our
    count is the only count, and seeding a model that keeps its own would
    overwrite the device's number with ours before its first report lands.

    A store that cannot answer is not fatal — the counter simply starts the day
    from the next visit, which is where it was before this existed — so the
    failure is logged and startup continues.
    """
    for device in registry.all():
        if reports_used_times(device.device_type):
            continue
        try:
            count = await event_store.device_visit_count_today(
                device.petkit_id, device.device_type)
        except SQLAlchemyError:
            log.warning("Could not seed Times Used for %s %s; it will start from "
                        "the next visit", device.device_type, device.petkit_id,
                        exc_info=True)
            continue
        device.state["usedTimesDay"] = visit_day_key()
        device.state["usedTimes"] = count
        log.info("Seeded Times Used for %s %s from the event store: %d today",
                 device.device_type, device.petkit_id, count)


async def start_background(services: Services, app_instance: web.Application) -> None:
    """aiohttp on_startup hook: open the store, then spawn every service.

    Ordering is the whole point of this function. The event store is
    opened and repaired before any reader exists, and each long-lived
    service is started through `_spawn` so shutdown can cancel it from
    `app[BACKGROUND_TASKS]` instead of a hand-maintained list.
    """
    config = services.config
    registry = services.registry
    ble_registry = services.ble_registry
    hub = services.hub
    event_store = services.event_store
    retention_config = services.retention_config
    pet_registry = services.pet_registry
    ha_publisher = services.ha_publisher
    mqtt_bridge = services.mqtt_bridge
    upstream_mqtt = services.upstream_mqtt
    go2rtc = services.go2rtc
    app_config = services.app_config
    media_root = services.media_root
    cert_path = services.cert_path

    # FIRST: open the event store (schema create + in-place migration).
    # Everything that queries it — the device-facing handlers, the MQTT
    # bridge, the panel, the sweepers — starts only after this hook
    # returns, so this is the one point where the DB is guaranteed to be
    # migrated before a single reader exists.
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

    # Rebuild today's Times Used before anything can publish it. The count
    # lives in `Device.state`, which `to_dict` deliberately excludes, so a
    # restart at 18:00 would otherwise report a box that had been used five
    # times that day as unused until the next visit. The event store has the
    # visits; this is the one moment both it and the devices are ready and
    # nothing has read the state yet.
    await _seed_visit_counts(registry, event_store)

    # Web panel, over HTTP only, and deliberately not served a SECOND time on
    # an HTTPS port of its own. A self-signed listener with no authentication
    # — which is what Web Bluetooth's demand for a top-level secure context
    # tempts you into — puts every device setting, command, pet record and
    # on-device patcher on the LAN for anyone who can reach the port.
    # Provisioning asks the operator for a real certificate instead (see the
    # Provision tab).
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
             " (HA Ingress)" if services.ha_addon else "")

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
                                   registry=registry, data_dir=config.data_dir)
    bucket_runner = web.AppRunner(bucket_app)
    await bucket_runner.setup()
    # Bucket needs TLS: cloud parses the PAR URL as https:// and crashes
    # on http://. Reuse the same self-signed cert as the MQTT TLS listener.
    # Only the CERTIFICATE work is fallible here. The bind must stay outside
    # the try: wrap `.start()` in it as well and an EADDRINUSE on 9000 —
    # another add-on, a MinIO container — is "handled" by binding the same
    # port again, which raises again and escapes `start_background`. A
    # degradable condition becomes a crash loop, and the log line blaming TLS
    # is never written.
    bkt_ctx = None
    try:
        bkt_key = config.mqtt_key or f"{config.data_dir}/certs/broker.key"
        # The device uploads to whatever `bucket_endpoint` names, and unlike
        # MQTT -- where the patched mbedtls skips verification -- the uploader
        # checks the certificate against the address it dialled. So that host
        # has to be in the SAN, and it is not necessarily the host's own IP
        # (`_get_host_ip`): those agree on a plain HA OS box and part company
        # behind a reverse proxy, on a multi-homed host, or when the operator
        # configured a name.
        bucket_host = urlparse(config.bucket_endpoint or config.api_url).hostname
        if ensure_self_signed(cert_path, bkt_key,
                              extra_hosts=[bucket_host] if bucket_host else None):
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
    stitcher = EpisodeStitcher(event_store, registry, media_root,
                               work_dir=f"{media_root}/.raw", hub=hub)
    _spawn(app_instance, "episode-stitcher", stitcher.run())

    if not services.no_mqtt:
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


async def cleanup_background(services: Services, app_instance: web.Application) -> None:
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
    registry = services.registry
    ble_registry = services.ble_registry
    event_store = services.event_store
    upstream_mqtt = services.upstream_mqtt
    go2rtc = services.go2rtc

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
