"""The add-on's own web panel: the application object and its route table.

This is the composition root. `create_panel_app` hangs every collaborator on
`app[...]` — the keys are listed in `web/appkeys.py` — and registers every route
in one function; the handlers themselves live in `web/api/`, whose package
docstring carries the per-tab map of what the panel does.

The table stays whole because three of its registrations are ordering-critical
and say so inline. Distributed over ten modules, "registered before" stops being
a property anybody can check.

Serving
-------
`create_panel_app` builds one `web.Application` that main.py mounts on plain
HTTP `web_port` (8099) — what Home Assistant Ingress proxies into the sidebar,
and which can optionally be mapped to the LAN as an unauthenticated debug port.
It is therefore reachable unauthenticated whenever that mapping exists, hence
the caller-supplied limits below being clamped and every path built from
request input going through `utils/paths.safe_join`.

There used to be a second, self-signed HTTPS site on 8098 so Web Bluetooth had
a top-level secure context. It was removed: it published this entire API — every
device setting, command, pet record and on-device patcher — to the LAN with no
authentication, in exchange for a convenience the operator can provide safely
with their own certificate in front of `web_port`.

Frontend
--------
The single-page app itself is no longer inlined here: `templates/index.html` is
rendered by `handle_index` and links `static/js/main.js` (an ES module, which
imports the rest of `static/js/`) + `static/styles.css`, both shipped inside
the package (see `TEMPLATE_DIR` / `STATIC_DIR`). The JS consumes
the exact key names the handlers in `web/api/` emit, so response shapes are a
contract: adding a key is safe, renaming or removing one is not.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp_jinja2
import jinja2
from aiohttp import web

from petkit_local.utils.paths import UnsafePathError, safe_join

from petkit_local.web.api.ble import (
    api_ble_accessories, api_ble_command, api_ble_delete, api_ble_import, api_ble_poll,
)
from petkit_local.web.api.devices import (
    api_ai_settings, api_capabilities, api_device_detail, api_device_log_settings,
    api_devices, api_send_command,
)
from petkit_local.web.api.logs import (
    api_capture_delete, api_capture_download, api_capture_list, api_capture_read,
    api_device_log_read, api_device_logs, api_events, api_ws,
)
from petkit_local.web.api.media import api_media_file, api_media_thumb
from petkit_local.web.api.patchers import api_patcher_apply, api_patcher_status
from petkit_local.web.api.pets import (
    api_pet_detail, api_pet_face_detail, api_pet_face_photo, api_pet_faces,
    api_pets_import, api_pets_list_create, api_pets_unbound,
)
from petkit_local.web.api.schedules import api_save_schedule
from petkit_local.web.api.settings import api_blocked, api_info, api_retention, api_settings
from petkit_local.web.api.stream import api_stream_proxy, api_turn
from petkit_local.web.api.timeline import api_event_detail, api_timeline
from petkit_local.web.appkeys import (
    BACKGROUND_TASKS, BLE_REGISTRY, BRIDGE, CFG, EVENT_STORE, HA_PUBLISHER, HUB,
    LIVE_CONFIG, PET_REGISTRY, REGISTRY, RETENTION_CONFIG,
)

if TYPE_CHECKING:
    from petkit_local.ai.pets import PetRegistry
    from petkit_local.devices.ble import BLERegistry
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.events.store import EventStore
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.media.retention import RetentionConfig
    from petkit_local.mqtt.bridge import MQTTBridge
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)


# Frontend assets ship inside the package, so they are resolved relative to this
# module — the add-on runs from /opt/petkit-local with an arbitrary CWD.
TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


async def _no_heuristic_caching(request: web.Request, response: web.StreamResponse) -> None:
    """Make browsers revalidate the panel's assets instead of guessing.

    `add_static` sends `ETag` and `Last-Modified` but no `Cache-Control`, and a
    response with neither `Cache-Control` nor `Expires` is one a browser may
    reuse WITHOUT asking, for a heuristic period derived from how old
    `Last-Modified` is. The practical effect after an add-on update is that the
    panel keeps running the previous script — new UI is deployed, served, and
    invisible, with nothing in the logs to say so.

    `no-cache` does not mean "do not store": the file stays cached and the
    conditional request still answers 304 from the ETag, so this costs one
    round trip per asset, not a re-download.

    This covers the static files. The INDEX matters more and sets the same
    header itself, in `handle_index`, rather than being matched on its path —
    it used to be covered only when the path happened to end in a slash, which
    depends on how Ingress routed the request. Cache-busting defends an asset
    and never the document: `?v=<hash>` only reaches the browser if the markup
    carrying it was fetched, so a stale index asks for the OLD asset URL and
    any cache can answer it correctly. The document is where the whole
    mechanism starts, so it does not get to depend on a path match.
    """
    # Substring, not prefix: behind HA Ingress the panel is mounted under an
    # opaque path, so neither segment is guaranteed to be at position 0.
    if "/asset/" in request.path:
        # The hash is IN this URL, so its content cannot change under it. Say so
        # and the browser stops revalidating twenty modules on every page load —
        # a new build asks for new URLs instead.
        response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
    elif "/static/" in request.path:
        response.headers.setdefault("Cache-Control", "no-cache")


def create_panel_app(registry: DeviceRegistry, ble_registry: BLERegistry | None,
                     hub: EventHub, panel_config: dict[str, Any],
                     bridge: MQTTBridge | None = None,
                     live_config: dict[str, Any] | None = None,
                     event_store: EventStore | None = None,
                     retention_config: RetentionConfig | None = None,
                     pet_registry: PetRegistry | None = None,
                     ha_publisher: HAPublisher | None = None) -> web.Application:
    """Build the panel application, wiring every collaborator into `app[...]`.

    Everything after `panel_config` is optional so tests (and a degraded
    runtime) can build a panel without the full stack; the handlers that need a
    missing collaborator answer HTTP 400 rather than raising.

    Args:
        panel_config: Static configuration read per request (`api_url`,
            `media_root`, `capture_dir`, `data_dir`, `settings_path`, ...).
        live_config: The SAME dict the device-facing HTTP handlers read, so a
            settings change from the panel takes effect with no restart.
    """
    # A pet's reference face photo is uploaded as a raw body, and a phone
    # camera produces 2-8 MB of JPEG. aiohttp's default cap is 1 MiB, which
    # rejected every real photo with a 413 whose text/plain body the panel then
    # failed to parse as JSON — so the button appeared to do nothing at all.
    # Same reasoning as http/bucket.py's cap, one size down: nothing the panel
    # accepts is media-sized.
    app = web.Application(client_max_size=16 * 1024 * 1024)  # 16MB
    app[REGISTRY] = registry
    app[BLE_REGISTRY] = ble_registry
    app[HUB] = hub
    app[CFG] = panel_config
    app[BRIDGE] = bridge
    # The live device-facing app config (same dict the HTTP handlers read), so
    # panel setting changes take effect immediately. None in tests.
    app[LIVE_CONFIG] = live_config if live_config is not None else {}
    app[EVENT_STORE] = event_store
    app[RETENTION_CONFIG] = retention_config
    app[PET_REGISTRY] = pet_registry
    app[HA_PUBLISHER] = ha_publisher
    app[BACKGROUND_TASKS] = set()
    # AppRunner.cleanup() fires on_cleanup, so main.py's existing
    # `panel_runner.cleanup()` already drains these — no change needed there.
    app.on_cleanup.append(cancel_background_tasks)

    aiohttp_jinja2.setup(app, loader=jinja2.FileSystemLoader(str(TEMPLATE_DIR)))

    # --- shell + assets ---
    app.router.add_get("/", handle_index)
    # Served under the app's own prefix, so the relative `static/...` URLs in
    # index.html resolve correctly whatever path Ingress mounts us at.
    app.router.add_static("/static", str(STATIC_DIR), name="static")
    # The same files again, under a path segment carrying the asset hash. An ES
    # module's `import './core.js'` resolves against the MODULE's URL, not the
    # document's, so a `?v=` on the entry point alone leaves the other nineteen
    # on URLs that never change — and `no-cache` is exactly what turned out not
    # to be enough here (see `_no_heuristic_caching`). A version in the path is
    # inherited by every relative import for free.
    app.router.add_get("/asset/{v}/js/{name}", handle_versioned_js)
    app.on_response_prepare.append(_no_heuristic_caching)
    app.router.add_get("/api/info", api_info)

    # --- devices: list, detail, commands ---
    app.router.add_get("/api/devices", api_devices)
    app.router.add_get("/api/devices/{id}", api_device_detail)
    app.router.add_post("/api/devices/{id}/command", api_send_command)
    app.router.add_post("/api/devices/{id}/schedule", api_save_schedule)

    # --- per-device toggles (GET reads, POST writes — same handler) ---
    app.router.add_get("/api/devices/{id}/capabilities", api_capabilities)
    app.router.add_post("/api/devices/{id}/capabilities", api_capabilities)
    app.router.add_get("/api/devices/{id}/ai", api_ai_settings)
    app.router.add_post("/api/devices/{id}/ai", api_ai_settings)
    app.router.add_get("/api/devices/{id}/logs", api_device_log_settings)
    app.router.add_post("/api/devices/{id}/logs", api_device_log_settings)

    # --- BLE accessories. Pairing lives here because it lives in the cloud:
    # the device pulls a list and scans for exactly those MACs, and no firmware
    # has any way to report a newly-found accessory upward. We are the cloud.
    app.router.add_get("/api/ble", api_ble_accessories)
    app.router.add_post("/api/ble", api_ble_accessories)
    app.router.add_post("/api/ble/import", api_ble_import)
    app.router.add_delete("/api/ble/{id}", api_ble_delete)
    app.router.add_post("/api/ble/{id}/command", api_ble_command)
    app.router.add_post("/api/ble/{id}/poll", api_ble_poll)

    # --- patchers (on-device binary patches) ---
    app.router.add_get("/api/devices/{id}/patcher", api_patcher_status)
    app.router.add_post("/api/devices/{id}/patcher", api_patcher_apply)

    # --- settings ---
    app.router.add_get("/api/settings", api_settings)
    app.router.add_post("/api/settings", api_settings)
    app.router.add_get("/api/blocked", api_blocked)
    app.router.add_get("/api/retention", api_retention)
    app.router.add_post("/api/retention", api_retention)

    # --- live log (ring buffer poll + WebSocket stream) ---
    app.router.add_get("/api/events", api_events)
    app.router.add_get("/api/ws", api_ws)

    # --- capture browser ---
    app.router.add_get("/api/capture", api_capture_list)
    app.router.add_get("/api/capture/{name}", api_capture_read)
    app.router.add_delete("/api/capture/{name}", api_capture_delete)
    app.router.add_get("/api/capture/{name}/download", api_capture_download)

    # --- device logs (uploaded by the device itself). Literal before dynamic,
    # so `/api/devicelogs` is not swallowed by the catch-all path pattern.
    app.router.add_get("/api/devicelogs", api_device_logs)
    app.router.add_get("/api/devicelogs/{path:.*}", api_device_log_read)

    # --- live view: the go2rtc streaming endpoints, proxied on our origin
    # (go2rtc's API is loopback-only), plus TURN credentials for off-LAN
    # WebRTC. Every method: the WS upgrade is a GET but `api/webrtc` is POSTed.
    app.router.add_route("*", "/api/stream/{path:.*}", api_stream_proxy)
    app.router.add_get("/api/turn", api_turn)

    # --- timeline + media. The thumb route is registered first because
    # `{path:.*}` would otherwise swallow `thumb/...` as a media path.
    app.router.add_get("/api/timeline", api_timeline)
    # Registered after the static "/api/events": aiohttp resolves a literal
    # path before a dynamic one, so this cannot shadow the hub's event list.
    app.router.add_get("/api/timeline/{id}", api_event_detail)
    app.router.add_get("/api/media/thumb/{path:.*}", api_media_thumb)
    app.router.add_get("/api/media/{path:.*}", api_media_file)

    # --- pets ---
    app.router.add_get("/api/pets", api_pets_list_create)
    app.router.add_post("/api/pets", api_pets_list_create)
    # Before "/api/pets/{id}", or the literal segment is swallowed by the
    # dynamic one and every request lands in api_pet_detail with id="unbound".
    app.router.add_get("/api/pets/unbound", api_pets_unbound)
    app.router.add_post("/api/pets/import", api_pets_import)
    app.router.add_get("/api/pets/{id}", api_pet_detail)
    app.router.add_post("/api/pets/{id}", api_pet_detail)
    app.router.add_delete("/api/pets/{id}", api_pet_detail)
    app.router.add_get("/api/pets/{id}/faces", api_pet_faces)
    app.router.add_post("/api/pets/{id}/faces", api_pet_faces)
    app.router.add_delete("/api/pets/{id}/faces/{face_id}", api_pet_face_detail)
    app.router.add_get("/api/pets/{id}/faces/{face_id}/photo", api_pet_face_photo)
    return app


def _log_background_result(task: asyncio.Task) -> None:
    """Retrieve a finished task's exception so it is logged, not swallowed."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("panel: background task %s failed: %s", task.get_name(), exc, exc_info=exc)


def _spawn_background(app: web.Application, coro: Coroutine[Any, Any, Any], *,
                      name: str) -> asyncio.Task:
    """Run `coro` detached from the request that started it.

    The event loop only keeps a weak reference to a running task, so a bare
    `create_task` can be garbage-collected mid-flight — a patcher run would then
    stop somewhere between downloading a binary and uploading the patched one,
    silently. The task is registered on the app instead, which both pins it and
    lets shutdown drain it (`cancel_background_tasks`).

    Args:
        name: Shown in logs; make it identify the request that spawned it.
    """
    task = asyncio.create_task(coro, name=name)
    tasks: set[asyncio.Task] = app[BACKGROUND_TASKS]
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    task.add_done_callback(_log_background_result)
    return task


async def cancel_background_tasks(app: web.Application) -> None:
    """Cancel and await every in-flight background task.

    Registered as an `on_cleanup` hook by `create_panel_app`, so an
    `AppRunner.cleanup()` (what main.py already calls for the panel) drains
    them. Safe to call directly and safe to call twice.
    """
    tasks = list(app.get(BACKGROUND_TASKS) or ())
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # Already reported by _log_background_result; awaiting here would
            # otherwise re-raise into aiohttp's shutdown and abort it.
            pass


def asset_version() -> str:
    """Short content hash of the panel's assets, for cache-busting their URLs.

    `Cache-Control: no-cache` is already set on these (see
    `_no_heuristic_caching`) and is correct, but it only binds whoever chooses
    to honour it. Behind Home Assistant Ingress the panel is an iframe on HA's
    own origin, under a service worker and a proxy that are not ours, and in
    practice a deployed panel script kept being served from cache there even
    after a hard refresh -- which the same build does not do when the panel is
    reached directly on its own port.

    A changed URL cannot be answered from a cache keyed on the old one, so this
    works regardless of who ignores what. The hash is over content rather than
    mtime: a rebuild that changes nothing keeps the URL, and the browser keeps
    its copy.

    EVERY module, not just the entry point: only `js/main.js` carries the `?v=`
    in the document, and it imports the rest by bare relative path -- so a
    changed submodule that left this hash alone would ship under an unchanged
    URL and be answered from cache, which is the exact staleness this exists to
    prevent. Sorted by name, and the name is hashed with the bytes: a directory
    listing has no order to rely on, and a rename with identical content still
    has to move the hash.

    Computed once at import. The files ship inside the image and cannot change
    under a running process, so re-hashing per request would buy nothing.
    """
    h = hashlib.sha256()
    try:
        modules = sorted((STATIC_DIR / "js").glob("*.js"))
        if not modules:
            return "dev"
        for path in (STATIC_DIR / "styles.css", *modules):
            h.update(path.name.encode())
            h.update(path.read_bytes())
    except OSError:
        # Never fail to render the panel over a cache hint. A missing file is a
        # much louder problem two lines later.
        return "dev"
    return h.hexdigest()[:12]


ASSET_VERSION = asset_version()


async def handle_versioned_js(request: web.Request) -> web.StreamResponse:
    """Serve `static/js/{name}` from a URL whose path carries the asset hash.

    The hash segment is not checked against `ASSET_VERSION`: an older page is
    entitled to keep loading the modules it was built against for as long as it
    is open, and a mismatch is a stale document rather than an error. The
    segment exists to make the URL change, nothing more.
    """
    try:
        path = safe_join(str(STATIC_DIR / "js"), request.match_info["name"])
    except UnsafePathError:
        return web.json_response({"error": "not found"}, status=404)
    if not path.endswith(".js") or not os.path.isfile(path):
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path)


async def handle_index(request: web.Request) -> web.Response:
    """Render the single-page app shell.

    The template links its assets with RELATIVE URLs (`static/...`), which is
    what makes the panel work behind Home Assistant Ingress: the page is only
    ever routed at `/`, so the browser's document path always ends in a slash
    and a relative asset resolves under whatever opaque
    `/api/hassio_ingress/<token>/` prefix Ingress happens to be using. An
    absolute `/static/...` would escape that prefix and 404.

    `no-cache` is set here rather than left to `_no_heuristic_caching`'s path
    match, because this is the one response that must never be reused without
    asking: it is what names the asset URLs, so a stale copy of it re-requests
    the old ones and defeats the content hash entirely.
    """
    response = aiohttp_jinja2.render_template("index.html", request,
                                              {"asset_version": ASSET_VERSION})
    response.headers["Cache-Control"] = "no-cache"
    return response
