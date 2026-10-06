"""Device-facing HTTP server: routing table, index and catch-all.

This is the half of petkit-local a PetKit device believes is the official cloud.
Every endpoint the firmware calls is registered here and dispatched to a handler
in `handlers/`; the module owns only the wiring, so that the URL shapes the
firmware actually uses (including the several it calls under more than one path)
live in one readable table instead of being scattered across the handlers.

Three rules that reading the handlers alone will not reveal:

* **Never 404 a device.** An endpoint we do not implement falls through to
  `handle_catchall`, which answers `{"result": {}}`. Firmware treats a 4xx/5xx
  as a server fault and retries forever, so an empty success is the only safe
  answer to something unrecognised.
* **Route order matters.** The `/{path:.*}` catch-all is registered last on
  purpose; aiohttp matches in registration order, so anything added after it
  would be unreachable.
* **Proxy mode is not visible here.** Forwarding to the real PetKit cloud is a
  middleware (`http/middleware/proxy.py::proxy_middleware`), not a branch in any
  handler: it wraps every route including the catch-all, so a handler is written
  as though only the local answer existed.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from aiohttp import web

from petkit_local.http.middleware import (
    device_middleware, logging_middleware, never_fail_middleware, proxy_middleware,
)
from petkit_local.http.handlers.signup import handle_signup
from petkit_local.http.handlers.iot_device_info import handle_iot_device_info
from petkit_local.http.handlers.serverinfo import handle_serverinfo
from petkit_local.http.handlers.state_report import handle_state_report
from petkit_local.http.handlers.heartbeat import handle_heartbeat
from petkit_local.http.handlers.ble_device import handle_ble_device
from petkit_local.http.handlers.k3_device_info import handle_k3_device_info
from petkit_local.http.handlers.schedule import handle_schedule_get
from petkit_local.http.handlers.feed import handle_feed_get
from petkit_local.http.handlers.upload_file_info import (
    handle_upload_file_info, wait_for_pending as wait_for_media_tasks,
)
from petkit_local.http.handlers.discern import handle_discern_pic, handle_discern_config, handle_faces
from petkit_local.http.handlers.upload_log import (
    handle_upload_log_token, handle_upload_log_done,
)
from petkit_local.http.handlers.stubs import (
    handle_sync_time,
    handle_ota_check,
    handle_oss_sts,
    handle_video_device_info,
    handle_device_info,
    handle_multi_config,
    handle_event_report,
    handle_sound_get,
    handle_attire_over,
)
from petkit_local.patchers.common import STAGE_DIR
from petkit_local.utils.paths import UnsafePathError, safe_join

if TYPE_CHECKING:
    from petkit_local.devices.registry import DeviceRegistry

log = logging.getLogger(__name__)


def create_app(registry: DeviceRegistry, config: dict) -> web.Application:
    """Build the device-facing aiohttp application with every firmware route.

    Args:
        registry: Device registry the handlers resolve requests against, stored
            as ``app["registry"]``.
        config: Runtime config, stored as ``app["config"]``. Handlers read
            ``api_url``, ``bucket_endpoint``, ``data_dir``, ``capture`` /
            ``capture_dir`` and the ``proxy_*`` keys from it.

    Returns:
        An app wired for the device only. `main.py` additionally injects the
        optional collaborators the handlers look up with ``app.get(...)`` and
        degrade gracefully without: ``ble_registry``, ``event_hub``,
        ``event_store``, ``pet_registry``, ``ha_publisher`` and the
        ``on_signup`` / ``on_state_report`` / ``on_device_seen`` callbacks.
    """
    # Outermost first. `never_fail_middleware` wraps everything, because the
    # one rule this server has is that a device never sees a 4xx/5xx; it is the
    # backstop for a handler that raises rather than returning. Then
    # `device_middleware` must precede the proxy so the device type is resolved
    # before the upstream is chosen, and `logging_middleware` must wrap both so
    # the panel log shows what the device actually received.
    app = web.Application(middlewares=[
        never_fail_middleware, logging_middleware, device_middleware, proxy_middleware])
    app["registry"] = registry
    app["config"] = config

    for ver in ("6",):
        p = f"/{ver}/{{device_type}}"
        app.router.add_route("*", f"{p}/dev_signup", handle_signup)
        # All three iot_device_info endpoints share one handler. It answers
        # ali-wrapped, as the cloud does for a D4SH on dev_iot_device_info,
        # except for the ESP32 litter boxes, which the cloud answers flat
        # (`iot_device_info.FLAT_CREDENTIAL_TYPES`).
        app.router.add_route("*", f"{p}/dev_iot_device_info", handle_iot_device_info)
        app.router.add_route("*", f"{p}/dev_only_iot_device_info", handle_iot_device_info)
        app.router.add_route("*", f"{p}/dev_only_iot_device_info_v2", handle_iot_device_info)
        app.router.add_route("*", f"{p}/dev_serverinfo", handle_serverinfo)
        app.router.add_route("*", f"{p}/dev_state_report", handle_state_report)
        app.router.add_route("*", f"{p}/dev_syncTime", handle_sync_time)
        app.router.add_route("*", f"{p}/dev_ota_check", handle_ota_check)
        app.router.add_route("*", f"{p}/dev_ota_heartbeat", handle_ota_check)
        app.router.add_route("*", f"{p}/dev_ota_start", handle_event_report)
        app.router.add_route("*", f"{p}/dev_ota_complete", handle_event_report)
        app.router.add_route("*", f"{p}/dev_oss_sts_info_new", handle_oss_sts)
        app.router.add_route("*", f"{p}/dev_oss_sts_info_new_v2", handle_oss_sts)
        app.router.add_route("*", f"{p}/dev_video_device_info", handle_video_device_info)
        app.router.add_route("*", f"{p}/dev_device_info", handle_device_info)
        app.router.add_route("*", f"{p}/dev_multi_config", handle_multi_config)
        app.router.add_route("*", f"{p}/dev_ble_device", handle_ble_device)
        app.router.add_route("*", f"{p}/dev_k3_device_info", handle_k3_device_info)
        app.router.add_route("*", f"{p}/dev_schedule_get", handle_schedule_get)
        app.router.add_route("*", f"{p}/dev_sound_get", handle_sound_get)
        app.router.add_route("*", f"{p}/dev_attire_over", handle_attire_over)
        app.router.add_route("*", f"{p}/dev_feed_get", handle_feed_get)
        app.router.add_route("*", f"{p}/dev_event_report", handle_event_report)
        app.router.add_route("*", f"{p}/dev_upload_file_info", handle_upload_file_info)
        app.router.add_route("*", f"{p}/dev_upload_file_info_v2", handle_upload_file_info)
        app.router.add_route("*", f"{p}/dev_discern_pic", handle_discern_pic)
        app.router.add_route("*", f"{p}/dev_discern_config", handle_discern_config)
        app.router.add_route("*", f"{p}/dev_upload_log_token", handle_upload_log_token)
        app.router.add_route("*", f"{p}/dev_upload_log", handle_upload_log_done)

        # Heartbeat comes as /6/poll/{type}/heartbeat OR /6/{type}/heartbeat.
        app.router.add_route("*", f"/{ver}/poll/{{device_type}}/heartbeat", handle_heartbeat)
        app.router.add_route("*", f"/{ver}/{{device_type}}/heartbeat", handle_heartbeat)

        # A few endpoints are also called WITHOUT the device type in the path.
        app.router.add_route("*", f"/{ver}/dev_serverinfo", handle_serverinfo)
        app.router.add_route("*", f"/{ver}/dev_signup", handle_signup)
        app.router.add_route("*", f"/{ver}/poll/heartbeat", handle_heartbeat)

    # dev_upload_file_info_v2 spawns media-pipeline tasks that outlive their
    # request and write to the event store. aiohttp runs on_cleanup callbacks
    # in registration order, so registering here — before main.py appends its
    # own — drains them while the store is still open.
    app.on_cleanup.append(_drain_media_tasks)

    app.router.add_route("*", "/patcher/download/{device_id}/{filename}", handle_patcher_download)
    app.router.add_route("*", "/faces/{filename}", handle_faces)
    app.router.add_route("*", "/", handle_index)
    app.router.add_route("*", "/{path:.*}", handle_catchall)

    return app


async def _drain_media_tasks(app: web.Application) -> None:
    """Let the in-flight dev_upload_file_info_v2 pipeline tasks finish."""
    await wait_for_media_tasks()


async def handle_patcher_download(request: web.Request) -> web.Response:
    """Serve staged patched files for the device to wget."""
    device_id = request.match_info["device_id"]
    try:
        path = safe_join(
            safe_join(STAGE_DIR, device_id),
            request.match_info["filename"],
        )
    except UnsafePathError:
        return web.Response(status=400, text="bad filename")

    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


async def handle_index(request: web.Request) -> web.Response:
    """Report which devices are registered — a liveness probe for humans.

    Returns:
        ``{"service": "petkit-local", "devices": [{type, id, sn, online}, ...]}``.
        Not part of the PetKit protocol; the web panel is served by
        `web/panel.py` on its own port.
    """
    registry = request.app["registry"]
    devices = [
        {"type": d.device_type, "id": d.petkit_id, "sn": d.serial_number, "online": d.online}
        for d in registry.all()
    ]
    return web.json_response({"service": "petkit-local", "devices": devices})


async def handle_catchall(request: web.Request) -> web.Response:
    """Answer any endpoint we do not implement, without ever failing the device.

    The WARNING is unconditional, and that is the point of this handler: the log
    line is how a not-yet-implemented endpoint gets discovered, so it must not be
    suppressed in proxy mode — exactly the mode you turn on to find new
    endpoints. Proxy mode still forwards the request, from the middleware, and
    still answers with the cloud's reply; this line fires either way.

    Returns:
        ``{"result": {}}`` — an empty success, because a 404 would put the
        firmware into an endless retry loop.
    """
    log.warning("Unhandled: %s %s", request.method, request.path)
    return web.json_response({"result": {}})
