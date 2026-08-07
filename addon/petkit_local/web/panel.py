"""The add-on's own web panel: HTTP routes plus the JSON API behind them.

This is the operator-facing side of petkit-local, next to (but separate from)
the device-facing API in `http/`. It covers real-device bring-up and day-to-day
control:

* **Devices** — one expandable panel per device from the live `DeviceRegistry`, per-device readable state
  resolved through the same dotted `value_path` HA uses, editable controls and a
  manual command sender (`api_devices`, `api_device_detail`, `api_send_command`).
* **Log** — the in-memory `EventHub` ring of HTTP+MQTT traffic, polled
  (`api_events`) or streamed over a WebSocket (`api_ws`).
* **Timeline** — visit sessions grouped at query time out of the `EventStore`,
  with their media (`api_timeline`).
* **Media** — files and thumbnails out of the friendly media tree
  (`api_media_file`, `api_media_thumb`) and the retention caps (`api_retention`).
* **Settings** — the runtime settings that can be flipped without a restart
  (`api_settings`), plus per-device capability/AI toggles.
* **Patchers** — apply/remove the on-device binary patches (`api_patcher_*`),
  which run as long-lived background tasks and report progress via the hub.
* **Pets** — CRUD and reference face photos for on-device recognition
  (`api_pets_*`).
* **Provisioning** — the Web Bluetooth page is pure frontend; it talks to the
  device directly from the browser, so there is no route for it here.

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
rendered by `handle_index` and links `static/app.js` + `static/styles.css`, both
shipped inside the package (see `TEMPLATE_DIR` / `STATIC_DIR`). The JS consumes
the exact key names the handlers below emit, so response shapes are a contract:
adding a key is safe, renaming or removing one is not.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import time
from collections import deque
from collections.abc import Coroutine
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp_jinja2
import jinja2
from aiohttp import web

from petkit_local.devices.base import Device, split_bucket_authority
from petkit_local.devices.registry import get_entities_for_device
from petkit_local.devices.state_parsers import apply_consumable_state
from petkit_local.events import codes, decode, ingest
from petkit_local.events.store import MAX_FACES_PER_PET
from petkit_local.ha.commands import (
    ALL_ACTIONS, PROPERTY_SET_SUFFIX, Refused, handle_ha_command, make_mqtt_property_set,
)
from petkit_local.media.stitch import QUIET_SECONDS as _STITCH_QUIET
from petkit_local.media.transcode import THUMB_TIMEOUT, have_ffmpeg, run_ffmpeg
from petkit_local.patchers.cacert import PATCHER_INFO as CACERT_PATCHER, load_our_cert, patch_ca_bundle
from petkit_local.media.go2rtc import stream_urls_with_rtsp
from petkit_local.patchers.camera import PATCHER_INFO as CAMERA_PATCHER
from petkit_local.patchers.cloud import PATCHER_INFO as CLOUD_PATCHER, patch_cloud
from petkit_local.patchers.common import (
    DEVICE_HTTPD_PORT, app_init_wrapper_path, build_wrapper_remove_cmd,
    cleanup_staged, detect_patch_storage_dir, download_from_device,
    ensure_space_for, generate_app_init_wrapper, md5hex, patched_file_path,
    patcher_device_files, send_run_cmd, stage_file, wait_for_heartbeat,
)
from petkit_local.patchers.mqtt import PATCHER_INFO as MQTT_PATCHER, patch_ctrl
from petkit_local.patchers.ssh import (
    PATCHER_INFO as SSH_PATCHER, build_install_commands as ssh_install_commands,
    ARCH_TO_BINARY as SSH_ARCH_TO_BINARY,
    DBKEY_RESERVE_BYTES, dropbear_path_for,
)
from petkit_local.patchers.verify import assert_download_plausible, elf_arch
from petkit_local.utils.coerce import to_int
from petkit_local.utils.const import (
    DEVICE_NAMES, DEVICE_TYPES_AI, VERSION, device_display_name,
)
from petkit_local.utils.dicts import dig_path
from petkit_local.utils.jsonio import atomic_write_json, read_json
from petkit_local.devices.ble import (
    BLE_TYPES, ble_command_for, get_ble_entities, normalize_mac,
)
from petkit_local.utils.paths import UnsafePathError, safe_join
from petkit_local.utils.timeutil import local_day_bounds, local_offset_hours

if TYPE_CHECKING:
    from petkit_local.ai.pets import PetRegistry
    from petkit_local.devices.ble import BLEDevice, BLERegistry
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.events.store import EventStore
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.media.retention import RetentionConfig
    from petkit_local.mqtt.bridge import MQTTBridge
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)

# Actions that COST something you cannot get back — the UI colours these red,
# confirms before sending, and sorts them last so a mis-click lands on a safe
# button. The line is "spends a consumable or takes the box out of service",
# not "moves the motor":
#
#   * `dump_litter` throws away the litter that is in the drum;
#   * `maintenance_start` stops the box serving the cat until someone ends it;
#   * the consumable resets overwrite a replacement date, and for the N50 that
#     date exists NOWHERE else (see devices/state_parsers.py) — press it by
#     accident and the real replacement date is gone.
#
# `reset` and `maintenance_stop` were in here and are not disruptive at all:
# both are `thing.service.end`, i.e. the verbs that STOP whatever is running
# and put the box back in service. Colouring the recovery button the same red
# as the one you are recovering from is exactly backwards.
DESTRUCTIVE_ACTIONS = {
    "dump_litter", "maintenance_start", "reset_n50", "reset_n60", "reset_desiccant",
}

# Runtime settings the panel may flip live. Each is read per-request from the
# shared app config (or applied via a setter), so no restart is needed. Value is
# the coercion type.
#
# These are the ONLY control surface for proxy mode and capture: neither has an
# add-on option or a CLI flag any more. Every key here must also appear in
# `config.PANEL_LIVE_KEYS`, or it is written now and dropped at the next start.
LIVE_SETTINGS = {
    "proxy_mode": bool,
    "proxy_upstream": str,
    "proxy_dns": str,
    "proxy_block_run_cmd": bool,
    "proxy_block_ota": bool,
    "proxy_block_log_upload": bool,
    "proxy_media_real_oss": bool,
    "proxy_mqtt_bridge": bool,
    "proxy_only": str,
    "capture": bool,
}

#: Defaults for `LIVE_SETTINGS`, used when the shared config has no value yet.
#: Both guards default ON: proxy mode is a debugging tool, and a debugging tool
#: that lets the cloud run a command or push firmware is a liability.
LIVE_SETTING_DEFAULTS = {
    "proxy_mode": False,
    "proxy_upstream": "",
    "proxy_dns": "",
    "proxy_block_run_cmd": True,
    "proxy_block_ota": True,
    "proxy_block_log_upload": True,
    "proxy_media_real_oss": False,
    "proxy_mqtt_bridge": True,
    "proxy_only": "",
    "capture": False,
}

# Upper bounds for caller-supplied `?limit=`. The panel port carries no
# authentication of its own — Ingress supplies it, and a direct mapping of 8099
# has none — so an unbounded limit is a free memory amplifier for anyone who
# can reach it. Both caps sit above anything the UI asks for, so no real
# request is affected:
# the event ring holds 800 entries (web/hub.py) and the capture reader pages in
# hundreds of lines.
MAX_EVENT_LIMIT = 1000
MAX_CAPTURE_LIMIT = 5000

# Device-log browser caps. The files come off an unauthenticated listener, so
# the line cap bounds the response and the character cap stops one pathological
# line (a firmware hexdump, a runaway loop) from being the whole payload.
MAX_LOG_LINES = 2000
MAX_LOG_LINE_CHARS = 2000
MAX_LOG_FILES = 200

# aiohttp app key holding the strong references to in-flight background tasks
# (see _spawn_background).
BACKGROUND_TASKS = "background_tasks"

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
    panel keeps running the previous `app.js` — new UI is deployed, served, and
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
    # opaque path, so "/static/" is not guaranteed to be at position 0.
    if "/static/" in request.path:
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
    app["registry"] = registry
    app["ble_registry"] = ble_registry
    app["hub"] = hub
    app["cfg"] = panel_config
    app["bridge"] = bridge
    # The live device-facing app config (same dict the HTTP handlers read), so
    # panel setting changes take effect immediately. None in tests.
    app["live_config"] = live_config if live_config is not None else {}
    app["event_store"] = event_store
    app["retention_config"] = retention_config
    app["pet_registry"] = pet_registry
    app["ha_publisher"] = ha_publisher
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
    app.on_response_prepare.append(_no_heuristic_caching)
    app.router.add_get("/api/info", api_info)

    # --- devices: list, detail, commands ---
    app.router.add_get("/api/devices", api_devices)
    app.router.add_get("/api/devices/{id}", api_device_detail)
    app.router.add_post("/api/devices/{id}/command", api_send_command)

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


def _live(request: web.Request) -> dict[str, Any]:
    """The shared device-facing config dict, or an empty stand-in for it."""
    return request.app.get("live_config") or {}


def _current_settings(request: web.Request) -> dict[str, Any]:
    """The `LIVE_SETTINGS` values as they are right now, all keys always present.

    `capture` alone falls back to the static panel config as well as the
    default, because it is the one setting that also existed before the shared
    live config did.
    """
    live = _live(request)
    cfg = request.app["cfg"]
    fallbacks = dict(LIVE_SETTING_DEFAULTS)
    fallbacks["capture"] = bool(cfg.get("capture", False))

    settings = {}
    for key, typ in LIVE_SETTINGS.items():
        value = live.get(key, fallbacks[key])
        settings[key] = bool(value) if typ is bool else (value or "")
    return settings


def _valid_upstream(value: str) -> bool:
    """Whether `proxy_upstream` names something `resolve_upstream` can use.

    Validated here rather than coerced, because the failure is silent otherwise:
    a typo'd URL would be saved happily and then produce a connection error per
    device request with nothing in the panel to say why.
    """
    from petkit_local.http.proxy import UPSTREAM_PRESETS

    value = (value or "").strip()
    if not value or value in UPSTREAM_PRESETS:
        return True
    parsed = urlparse(value)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _valid_dns(value: str) -> bool:
    """Whether `proxy_dns` is an IPv4 address, optionally with a port.

    A hostname is refused on purpose. Resolving the resolver would need the
    system DNS — the one this setting exists to stop trusting — so a name here
    would work until the moment it had to.
    """
    value = (value or "").strip()
    if not value:
        return True
    host, sep, port = value.partition(":")
    if sep and not (port.isdigit() and 0 < int(port) < 65536):
        return False
    octets = host.split(".")
    return (len(octets) == 4
            and all(o.isdigit() and len(o) <= 3 and int(o) < 256 for o in octets))


#: Extra validation for settings whose value is not just a type. `api_settings`
#: coerces to bool or str; anything with a narrower domain is checked here.
LIVE_SETTING_VALIDATORS = {"proxy_upstream": _valid_upstream, "proxy_dns": _valid_dns}


async def api_settings(request: web.Request) -> web.Response:
    """GET returns the live-editable runtime settings; POST updates them in the
    shared config (immediate effect) and persists to the overrides file.

    Both methods answer `{"settings": {...}, ...}`; POST adds `changed` (only
    the keys in `LIVE_SETTINGS` — anything else in the body is ignored), 400s
    if nothing in the request was applicable, and 400s on a value that fails
    `LIVE_SETTING_VALIDATORS` **without applying any of the batch**, so a
    rejected request cannot leave half its settings written.
    """
    if request.method == "GET":
        return web.json_response({"settings": _current_settings(request), "writable": bool(request.app.get("live_config"))})

    live = request.app.get("live_config")
    if not isinstance(live, dict) or not live:
        # Nothing wired to write into (empty fallback in tests / no device app).
        return web.json_response({"error": "settings not writable in this mode"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    changed: dict[str, Any] = {}
    for key, val in body.items():
        typ = LIVE_SETTINGS.get(key)
        if typ is None:
            continue
        val = bool(val) if typ is bool else str(val)
        validator = LIVE_SETTING_VALIDATORS.get(key)
        if validator is not None and not validator(val):
            return web.json_response({"error": f"bad value for {key}"}, status=400)
        changed[key] = val

    if not changed:
        return web.json_response({"error": "no valid settings in request"}, status=400)

    # Applied only once the whole batch validated.
    live.update(changed)

    # Persist overrides so panel changes survive a restart (merge with existing).
    path = request.app["cfg"].get("settings_path")
    if path:
        try:
            # A damaged overrides file is already dead weight — config.py's
            # apply_panel_overrides ignores it wholesale on a parse error — so
            # rewriting it from the current settings is a repair, not data loss.
            existing = read_json(path, {})
            if not isinstance(existing, dict):
                existing = {}
            existing.update(changed)
            atomic_write_json(path, existing)
        except OSError as e:
            log.warning("panel: could not persist settings overrides: %s", e)

    log.info("panel: runtime settings changed: %s", changed)
    return web.json_response({"ok": True, "changed": changed, "settings": _current_settings(request)})


#: How much of a blocked payload is shown before `?reveal=1`.
MASK_PREFIX = 6


def _mask(value: str | None) -> str | None:
    """Shorten a recorded payload to a recognisable stub.

    These rows can hold a real `deviceSecret` or a media AES key, and this panel
    is served unauthenticated on the HTTPS port (see the module docstring). The
    full value stays in `/data` — the database and the capture files — which is
    the same trust level as `devices.json`.
    """
    if not value:
        return value
    if len(value) <= MASK_PREFIX:
        return f"… ({len(value)} chars)"
    return f"{value[:MASK_PREFIX]}… ({len(value)} chars)"


async def api_blocked(request: web.Request) -> web.Response:
    """What the real cloud tried to do to a device, and did not get to do.

    `GET /api/blocked` — the persisted subset of proxy mode's redactions: shell
    commands, firmware pushes and credential swaps. Routine address
    substitutions are NOT here (they would be thousands of rows a day); the
    Setup tab shows those as counters from `/api/info`.

    On a healthy proxied session this is EMPTY. A row means the upstream
    actually tried something.

    Answers `{records: [...], counts: {...}}`. `payload_json` is masked unless
    `?reveal=1`, `?limit=` is clamped, and `?device=` / `?kind=` filter.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "no event store"}, status=400)

    device = request.query.get("device")
    rows = await store.recent_blocked_attempts(
        limit=_limit_param(request, 200, MAX_EVENT_LIMIT),
        device_id=to_int(device, None) if device else None,
        kind=request.query.get("kind") or None,
    )
    if request.query.get("reveal") != "1":
        rows = [{**r, "payload_json": _mask(r.get("payload_json"))} for r in rows]

    return web.json_response({"records": rows,
                              "counts": request.app["hub"].redaction_counts()})


def _state_doc(d: Device) -> dict[str, Any]:
    """Same shape the HA publisher publishes, for value resolution.

    Recomputes the consumable countdowns first, for the same reason
    `HAPublisher._build_state` does: they move with the calendar, and the N50's
    has no device input to trigger it.
    """
    apply_consumable_state(d)
    settings = d.config.get("settings") or {}
    enabled = d.enabled_capabilities()
    return {
        "state": d.state or {},
        "settings": settings,
        "schedule": d.config.get("schedule", []),
        "feed_schedule": d.config.get("feed_schedule", {}),
        "capabilities": {ct: (ct in enabled) for ct in d.CAPABILITY_TYPES},
    }


def _delivery_view(broker: Any, d: Device) -> dict | None:
    """What the broker would deliver to this device, or None if it isn't running.

    Imported locally so the panel does not pull amqtt in just to render a
    device list — `--no-mqtt` and every panel test run without a broker at all.
    """
    if broker is None:
        return None
    from petkit_local.mqtt.broker import delivery_view
    return delivery_view(broker, d.mqtt_product_key, d.mqtt_device_name)


def _device_summary(d: Device, ble_registry: BLERegistry | None, hub: EventHub,
                    broker: Any = None) -> dict[str, Any]:
    """One device panel header: identity, liveness, traffic counters and BLE children.

    This is the shared base of both `/api/devices` and `/api/devices/{id}`, so a
    key added here appears in both.
    """
    diag = hub.diag(d.petkit_id)
    return {
        "id": d.petkit_id,
        "type": d.device_type,
        "name": device_display_name(d.device_type),
        "sn": d.serial_number,
        "mac": d.mac,
        "firmware": d.firmware,
        "online": d.online,
        "mqtt_connected": d.mqtt_connected,
        "is_camera": d.is_camera,
        "supports_ai": d.supports_ai,
        "pk": d.mqtt_product_key,
        "dn": d.mqtt_device_name,
        "last_heartbeat": d.last_heartbeat,
        "last_state_report": d.last_state_report,
        "last_seen": d.last_seen,
        "last_mqtt": d.last_mqtt,
        "mqtt_subscriptions": list(d.mqtt_subscriptions),
        # What the broker will really deliver, as opposed to what we asked it
        # for above. Absent when the broker is not running (tests, --no-mqtt).
        "mqtt_delivery": _delivery_view(broker, d),
        "queue": len(d.command_queue),
        "http_count": diag.get("http_count", 0),
        "mqtt_count": diag.get("mqtt_count", 0),
        "entities": len(get_entities_for_device(d)),
        "ble": [{"id": b.petkit_id, "type": b.ble_type, "mac": b.mac}
                for b in (ble_registry.get_linked(d.petkit_id) if ble_registry else [])],
    }


async def api_info(request: web.Request) -> web.Response:
    """Server-wide facts for the Setup tab.

    The URLs/ports a device must dial, whether the TLS cert exists, bridge
    liveness, device count, and the current runtime settings — those last are
    `api_settings`' territory and are mirrored here only so the frontend can
    render the whole tab from one request.
    """
    cfg = request.app["cfg"]
    reg = request.app["registry"]
    cert = cfg.get("cert_path", "")
    ha_pub = request.app.get("ha_publisher")
    return web.json_response({
        # The hash of the assets THIS process would serve, so the panel can
        # compare it with the one baked into the page it is running from. They
        # are the same value from two different moments: `asset_version` in the
        # markup came with the document, possibly out of a cache, while this
        # one is answered live. A mismatch is the one failure the `version`
        # below cannot see — a fresh server running behind a stale page.
        "asset_version": ASSET_VERSION,
        # The running version. First thing to check when a device reports the
        # entities of a release you thought you had replaced.
        "version": VERSION,
        "api_url": cfg.get("api_url"),
        "mqtt_tls": cfg.get("mqtt_tls"),
        "mqtt_tls_port": cfg.get("mqtt_tls_port"),
        "mqtt_port": cfg.get("mqtt_port"),
        "strict_auth": cfg.get("strict_auth"),
        "cert_exists": bool(cert) and os.path.exists(cert),
        # The HA publisher, NOT the device-facing bridge. This used to read
        # `app["bridge"]._client`, which is the connection to our own embedded
        # broker -- up whenever the broker is, regardless of whether anything
        # reaches Home Assistant. Running with `--no-ha`, where there is no
        # publisher at all, it still reported a green "connected".
        "ha_publishing": bool(ha_pub and ha_pub.connected),
        # Whether publishing is configured at all, so the panel can say
        # "disabled" rather than "down" when it was switched off deliberately.
        "ha_enabled": ha_pub is not None,
        "device_count": len(reg.all()),
        # live-editable runtime settings (reflect the shared device-facing config)
        "settings": _current_settings(request),
        "settings_writable": bool(request.app.get("live_config")),
        "capture": _current_settings(request)["capture"],
        # The products that do on-device recognition, so the AI/Pets tab can
        # name them instead of carrying its own copy of the list. Sorted for a
        # stable UI; a codename with no marketing name is skipped rather than
        # shown as "Unknown".
        "ai_device_names": sorted(
            DEVICE_NAMES[c] for c in DEVICE_TYPES_AI if c in DEVICE_NAMES),
        "upstreams": _upstream_choices(),
        # Per-rule totals since start. The routine substitutions live only here
        # and in the capture files — see events/models.py::BlockedAttempt for
        # why they are not rows in the database.
        "redactions": request.app["hub"].redaction_counts(),
        # Proxied-call outcomes. The answer to "is proxy mode actually doing
        # anything?", which nothing else answers once a taken-over device
        # settles into polling only the heartbeat.
        "upstream": request.app["hub"].upstream_counts(),
    })


def _upstream_choices() -> list[dict[str, str | bool]]:
    """The upstream servers the Setup tab offers.

    Built from `http/proxy.py::UPSTREAM_PRESETS` so the panel cannot drift from
    what `resolve_upstream` actually accepts. `default` marks the one an empty
    setting resolves to, so the picker can show it selected without having to
    know which key that is.
    """
    from petkit_local.http.proxy import DEFAULT_UPSTREAM, UPSTREAM_PRESETS

    return [{"key": k, "url": v, "default": k == DEFAULT_UPSTREAM}
            for k, v in UPSTREAM_PRESETS.items()]


async def api_devices(request: web.Request) -> web.Response:
    """A bare JSON array of `_device_summary` objects — no envelope object."""
    reg = request.app["registry"]
    ble = request.app["ble_registry"]
    hub = request.app["hub"]
    return web.json_response([_device_summary(d, ble, hub, request.app.get("mqtt_broker")) for d in reg.all()])


# --- device detail: the three sidecar views --------------------------------
# Each of these is the GET body of its own endpoint AND part of the device
# detail. They live here so the two can never answer differently: the panel
# renders one device from one request, and the standalone endpoints stay for
# anything scripting the API.

def _capabilities_view(d: Device) -> dict[str, Any]:
    """Which media capabilities this device has, and which are switched on."""
    enabled = d.enabled_capabilities()
    return {
        "is_camera": d.is_camera,
        "capabilities": {ct: (ct in enabled) for ct in Device.CAPABILITY_TYPES},
    }


def _ai_view(d: Device) -> dict[str, Any]:
    """Whether on-device recognition is supported and switched on."""
    return {
        "supports_ai": d.supports_ai,
        "ai_enabled": bool(d.config.get("ai_enabled", True)),
    }


def _log_settings_view(d: Device, request: web.Request) -> dict[str, Any]:
    """Debug-log collection state, plus why it cannot work if it cannot."""
    return {
        "ok": True,
        "log_upload_enabled": bool(d.config.get("log_upload_enabled", False)),
        "reason": _device_log_reason(request),
    }


async def api_device_detail(request: web.Request) -> web.Response:
    """Everything the device detail view needs, in one object.

    A `_device_summary` plus `state`, `settings`, `config`, `diag`, `entities`,
    `actions`, and the three sidecar views (`capInfo`, `logInfo`, `aiInfo`) that
    the panel would otherwise have to fetch separately — one panel refresh is
    one request, which matters now that there is a panel per device refreshing
    on every WebSocket tick.

    Each entity carries its resolved `value`, read out of `_state_doc` with the
    same dotted `value_path` HA's value_template uses, so the panel and HA can
    never disagree about what a sensor currently reads. `actions` is the subset
    of button entities that map to a real command, flagged `destructive` when
    the UI should confirm first.
    """
    reg = request.app["registry"]
    ble = request.app["ble_registry"]
    hub = request.app["hub"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)

    entities = get_entities_for_device(d)
    doc = _state_doc(d)
    detail = _device_summary(d, ble, hub, request.app.get("mqtt_broker"))
    detail.update({
        "state": d.state,
        "settings": d.config.get("settings", {}),
        "config": {k: v for k, v in d.config.items() if k != "settings"},
        "diag": hub.diag(did),
        # Where to watch this device. Lives on the DEVICE, not on the patcher
        # that enables it: the patcher card is about applying and undoing a
        # change to the firmware, and once that is done the address belongs
        # with everything else about the device. Empty until a probe has
        # confirmed the device is really serving (`media/go2rtc.py`).
        "streams": stream_urls_with_rtsp(d, request.app.get("go2rtc")),
        "entities": [{
            "component": e.component, "key": e.key, "name": e.name,
            "value_path": e.value_path, "unit": e.unit, "device_class": e.device_class,
            "icon": e.icon, "options": e.options, "option_values": e.option_values,
            "settable": e.is_settable,
            # Config/diagnostic, exactly as HA groups them — the panel sorts by
            # it so the two present the same entity in the same place.
            "entity_category": e.entity_category,
            "min": e.min_value, "max": e.max_value, "step": e.step,
            # Same dotted `value_path` HA's value_template reads.
            "value": dig_path(doc, e.value_path),
        } for e in entities],
        # Destructive LAST, otherwise in declaration order (`sorted` is stable).
        # They were interleaved -- Enter/Exit Maintenance and Dump Litter sat
        # third to fifth in a row of eleven -- so the buttons you would not want
        # to hit by accident were in the middle of the ones you press daily.
        "actions": sorted(
            ({"key": e.key, "name": e.name,
              "destructive": e.key in DESTRUCTIVE_ACTIONS}
             for e in entities if e.component == "button" and e.key in ALL_ACTIONS),
            key=lambda a: a["destructive"]),
        # The sidecars, so rendering one device costs one request. With a panel
        # per device refreshing on every WebSocket tick, four requests each was
        # the difference between idle and a steady stream of them.
        "capInfo": _capabilities_view(d) if d.is_camera else None,
        "logInfo": _log_settings_view(d, request),
        "aiInfo": _ai_view(d) if d.supports_ai else None,
    })
    return web.json_response(detail)


async def api_send_command(request: web.Request) -> web.Response:
    """Send one command to a device.

    The body is one of three forms: `{"action": ...}` (a named button action),
    `{"entity": ..., "value": ...}` (routed through the same handler HA commands
    take, so coercion and the optimistic settings update stay identical), or a
    raw `{"suffix": ..., "payload": ...}` escape hatch.

    Answers `{ok, delivered, suffix, envelope}` — or `{ok, delivered, entity}`
    for an entity write that only changed local state. `delivered` is what
    actually happened, not what was asked for: a failed MQTT publish falls back
    to the heartbeat queue rather than erroring, because the device picks the
    command up on its next poll either way.
    """
    reg = request.app["registry"]
    hub = request.app["hub"]
    bridge = request.app["bridge"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    # transport: "auto" (default) picks MQTT only when the device has a live
    # session, else the HTTP heartbeat queue. "mqtt"/"heartbeat" force it.
    transport = body.get("transport", "auto")
    action = body.get("action")
    entity_key = body.get("entity")

    if action:
        fn = ALL_ACTIONS.get(action)
        if not fn:
            return web.json_response({"error": f"unknown action {action}"}, status=400)
        suffix, envelope = fn()
    elif entity_key is not None:
        # Route through the same handler as HA so coercion (switch/number/select
        # option_values) and optimistic settings update stay identical.
        ent = next((e for e in get_entities_for_device(d) if e.key == entity_key), None)
        if ent is None:
            return web.json_response({"error": f"unknown entity {entity_key}"}, status=400)
        try:
            result = handle_ha_command(d, ent, str(body.get("value", "")))
        except Refused as exc:
            # Understood and rejected. Answering ok here would tell the caller
            # a value took effect when the setting was left exactly as it was.
            return web.json_response({"error": str(exc)}, status=400)
        reg.save()
        if result is None:
            hub.record_command(did, "local", f"{ent.key}={body.get('value')}")
            return web.json_response({"ok": True, "delivered": "local", "entity": ent.key})
        suffix, envelope = result
    else:
        suffix = body.get("suffix")
        envelope = body.get("payload")
        if not suffix or envelope is None:
            return web.json_response({"error": "need action, entity+value, or suffix+payload"}, status=400)

    if transport == "auto":
        mqtt_live = d.mqtt_connected and bridge is not None and getattr(bridge, "_client", None)
        transport = "mqtt" if mqtt_live else "heartbeat"

    if transport == "mqtt" and bridge is not None and getattr(bridge, "_client", None):
        try:
            await bridge.publish_to_device(d, suffix, envelope)
            delivered = "mqtt"
        except Exception as e:
            log.warning("panel: MQTT publish failed for device %d, queuing for heartbeat: %s", did, e)
            if isinstance(envelope, dict):
                envelope["_service_suffix"] = suffix
            d.command_queue.append(envelope)
            delivered = "heartbeat-queue"
    else:
        if isinstance(envelope, dict):
            envelope["_service_suffix"] = suffix
        d.command_queue.append(envelope)
        delivered = "heartbeat-queue"

    params = envelope.get("params", envelope) if isinstance(envelope, dict) else envelope
    hub.record_command(did, delivered, f"{suffix} {json.dumps(params)[:80]}")
    clean = {k: v for k, v in envelope.items() if k != "_service_suffix"} if isinstance(envelope, dict) else envelope
    return web.json_response({"ok": True, "delivered": delivered, "suffix": suffix, "envelope": clean})


# --- Capabilities / AI toggle / Retention --------------------------------------

async def api_capabilities(request: web.Request) -> web.Response:
    """GET/POST the STS media capability toggles (see devices/base.py::
    Device.CAPABILITY_TYPES) — the control point is the next
    dev_oss_sts_info_new_v2 poll, not a device push.

    Both methods answer `{"capabilities": {name: bool}}` with every capability
    present, so the UI never has to guess a default. POST applies only the keys
    it recognises and republishes HA state, since the capability switches are
    entities there too.
    """
    reg = request.app["registry"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)

    if request.method == "GET":
        return web.json_response(_capabilities_view(d))

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    caps = d.config.setdefault("capabilities", {})
    for ct in Device.CAPABILITY_TYPES:
        if ct in body:
            caps[ct] = bool(body[ct])
    reg.save()

    ha_publisher = request.app.get("ha_publisher")
    if ha_publisher is not None:
        await ha_publisher.publish_state(d)

    enabled = d.enabled_capabilities()
    return web.json_response({"ok": True, "capabilities": {ct: (ct in enabled) for ct in Device.CAPABILITY_TYPES}})


async def api_ai_settings(request: web.Request) -> web.Response:
    """GET/POST the on-device facial recognition on/off toggle — separate
    from the STS media capabilities (see dev_discern_config).

    Answers `{"ai_enabled": bool}` (GET also reports `supports_ai`). The toggle
    is stored for every device, including those that cannot do recognition, so
    the value survives if a device is later replaced by one that can.
    """
    reg = request.app["registry"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)

    if request.method == "GET":
        return web.json_response(_ai_view(d))

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    if "ai_enabled" in body:
        d.config["ai_enabled"] = bool(body["ai_enabled"])
        reg.save()
    return web.json_response({"ok": True, "ai_enabled": bool(d.config.get("ai_enabled", True))})


async def api_device_log_settings(request: web.Request) -> web.Response:
    """GET/POST whether this device may upload its own debug log to us.

    Answers `{"log_upload_enabled": bool}`. Per device rather than global,
    matching the media-capability and AI toggles: it decides what one device is
    told at `dev_upload_log_token`, and `http/bucket.py` checks it again on the
    upload itself so switching it off takes effect before the device's token
    would have expired.
    """
    reg = request.app["registry"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        if "log_upload_enabled" in body:
            d.config["log_upload_enabled"] = bool(body["log_upload_enabled"])
            reg.save()

    return web.json_response(_log_settings_view(d, request))


async def api_retention(request: web.Request) -> web.Response:
    """GET/POST the per-capability media retention caps (media/retention.py).

    Answers `{"retention": {...}}`. POST hands the body straight to
    `RetentionConfig.update`, which is what validates it, and persists the
    result so the next sweep uses it.
    """
    retention = request.app.get("retention_config")
    if retention is None:
        return web.json_response({"error": "retention not available"}, status=400)

    if request.method == "GET":
        return web.json_response({"retention": retention.data})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    retention.update(body)
    data_dir = request.app["cfg"].get("data_dir", "/data")
    retention.save(data_dir)
    return web.json_response({"ok": True, "retention": retention.data})


# --- Timeline + media --------------------------------------------------------

def _safe_media_path(request: web.Request, rel_path: str) -> str | None:
    """Resolve `rel_path` under the friendly media root, or None.

    Containment (`..`, absolute paths, symlink escapes) is `safe_join`'s job;
    what stays here is this endpoint's own contract: an unconfigured media root
    serves nothing (`safe_join` would happily resolve against the process CWD),
    the result must be an existing regular file, and a rejection is a 404 rather
    than an exception.
    """
    media_root = request.app["cfg"].get("media_root", "")
    if not media_root or not rel_path:
        return None
    try:
        candidate = safe_join(media_root, rel_path)
    except UnsafePathError:
        return None
    return candidate if os.path.isfile(candidate) else None


async def api_media_file(request: web.Request) -> web.StreamResponse:
    """Serve one file from the friendly media tree.

    The path is relative to the media root — the same form the timeline hands
    out. 404 covers both "missing" and "rejected", so a probe cannot tell the
    two apart.
    """
    p = _safe_media_path(request, request.match_info["path"])
    if not p:
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(p)


async def _generate_video_thumb(video_path: str, thumb_path: str) -> bool:
    """Grab one frame from `video_path` into `thumb_path`; True if it worked.

    ffmpeg writes to a temp file in the destination directory which is then
    renamed into place, so `thumb_path` only ever exists complete. The Timeline
    renders a whole day at once, so several requests for the same not-yet-made
    thumbnail arrive together; letting them all write the final path directly
    means one request can serve a half-written JPEG. Renaming is preferred over
    a per-path asyncio.Lock because it needs no shared state to keep correct
    (the runs are equivalent, so last-writer-wins is fine) and it also survives
    the process being killed mid-encode.
    """
    # The temp file must share a filesystem with the target for os.replace to
    # be atomic, so it goes in the same directory.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(thumb_path) or ".",
                                    prefix=".thumb.", suffix=".jpg")
    os.close(fd)  # ffmpeg opens the path itself; the extension picks the muxer
    try:
        rc, _, _ = await run_ffmpeg(
            ["ffmpeg", "-y", "-i", video_path, "-frames:v", "1", "-vf", "thumbnail", tmp_path],
            timeout=THUMB_TIMEOUT, what=video_path)
        if rc != 0 or not os.path.getsize(tmp_path):
            return False
        os.replace(tmp_path, thumb_path)
        return True
    except OSError as e:
        log.warning("panel: thumbnail generation failed for %s: %s", video_path, e)
        return False
    finally:
        # os.replace consumed the temp file on success; on every other path
        # (including cancellation) it is still there.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def api_media_thumb(request: web.Request) -> web.StreamResponse:
    """Serve a thumbnail for a media path: the image itself for stills, or a
    cached frame grab for video.

    Grabs are cached under `{data_dir}/thumbs` keyed by a hash of the source
    path, so the Timeline re-rendering a day costs one ffmpeg run per clip
    total, not per request. Without ffmpeg there is no video thumbnail at all
    and this 404s — the UI falls back to a poster still.
    """
    p = _safe_media_path(request, request.match_info["path"])
    if not p:
        return web.json_response({"error": "not found"}, status=404)

    if p.lower().endswith((".jpg", ".jpeg", ".png")):
        return web.FileResponse(p)  # the browser downsizes via CSS — no separate pipeline needed

    if not have_ffmpeg():
        return web.json_response({"error": "ffmpeg not available for video thumbnails"}, status=404)

    data_dir = request.app["cfg"].get("data_dir", "/data")
    thumbs_dir = os.path.join(data_dir, "thumbs")
    os.makedirs(thumbs_dir, exist_ok=True)
    thumb_path = os.path.join(thumbs_dir, hashlib.sha1(p.encode()).hexdigest() + ".jpg")

    if not os.path.isfile(thumb_path) and not await _generate_video_thumb(p, thumb_path):
        return web.json_response({"error": "thumbnail generation failed"}, status=500)

    return web.FileResponse(thumb_path)


def _rel_media(path: str | None, media_root: str) -> str | None:
    """An absolute stored media path as the media-root-relative URL part."""
    if not path:
        return None
    try:
        return os.path.relpath(path, media_root)
    except ValueError:
        # Different drives on Windows — no relative form exists.
        return None


def _pick_chunked_video(rows: list[dict[str, Any]], media_root: str,
                        now: float) -> tuple[str | None, bool]:
    """`(url, pending)` for a CHUNKED video role (fullVideo / cloudDouble).

    A real T5 uploads these as many ~4s chunks that media/stitch.py joins into
    one clip once the episode goes quiet. Until that's done we must NOT hand
    the UI a raw chunk — playing a 4-second fragment of a visit is exactly the
    rough edge to avoid. So a chunked video is "ready" only when the stitched
    result exists, or a lone chunk has clearly settled (the stitcher looked
    and left it because there was nothing to join). Otherwise it's `pending`
    and the UI shows a still / "processing" state instead."""
    ready = [m for m in rows if m.get("status") == "ready" and m.get("media_path")]
    stitched = next((m for m in ready if m.get("stitch_state") == "stitched"), None)
    if stitched:
        return _rel_media(stitched["media_path"], media_root), False
    expecting = bool(rows)  # any row (even pending) means a recording is coming
    if len(ready) == 1 and not any(m.get("status") == "pending" for m in rows):
        newest = ready[0].get("created_at") or 0
        if now - newest > _STITCH_QUIET:
            return _rel_media(ready[0]["media_path"], media_root), False
    return None, expecting


def _pick_single_video(rows: list[dict[str, Any]], media_root: str) -> str | None:
    """A SINGLE-file clip role (dynamicVideo / highLight = the app's short
    'Highlight'): the device uploads it as one complete ~4s file, not chunks,
    so it's ready the moment it's processed — no stitching, never a fragment."""
    for m in rows:
        if m.get("status") == "ready" and m.get("media_path"):
            r = _rel_media(m["media_path"], media_root)
            if r:
                return r
    return None


def _session_media_urls(sessions_media: list[dict[str, Any]], media_root: str,
                        now: float | None = None) -> dict[str, Any]:
    """Media slots for one episode, keyed by role (see events/ingest.py).

    - `highlight_url` — the short ~4s event clip (`dynamicVideo`); ready at once.
    - `playback_url`  — the long continuous recording (`fullVideo`); ready only
      once media/stitch.py has joined its chunks (else `video_pending`).
    - `preview_url`   — the `cloudDouble` timelapse; same chunked/stitched gate.
    - `poster_url` / `waste` / `health` — stills.
    - `snapshot_url`  — best still to use as a poster/thumbnail fallback.
    - `video_pending` — a playable recording is expected but still assembling,
      so the UI shows a still or a "processing" placeholder, not a fragment.
    """
    now = now if now is not None else time.time()
    by: dict[str | None, list[dict[str, Any]]] = {}
    for m in sessions_media:
        by.setdefault(m.get("category"), []).append(m)

    playback_url, pb_pending = _pick_chunked_video(by.get("fullVideo", []), media_root, now)
    preview_url, _pv_pending = _pick_chunked_video(by.get("cloudDouble", []), media_root, now)
    highlight_url = _pick_single_video(by.get("dynamicVideo", []) + by.get("highLight", []), media_root)
    poster_url = _pick_single_video(by.get("eventImage", []), media_root)

    def _ready_rels(cat: str) -> list[str]:
        """Every ready file of one still-image role, as relative URLs."""
        return [r for r in (_rel_media(m["media_path"], media_root)
                            for m in by.get(cat, [])
                            if m.get("status") == "ready" and m.get("media_path")) if r]

    out: dict[str, Any] = {
        "highlight_url": highlight_url, "playback_url": playback_url,
        "preview_url": preview_url, "poster_url": poster_url,
        "waste": _ready_rels("wasteCheck"), "health": _ready_rels("healthPic"),
        "snapshot_url": None, "video_pending": bool(pb_pending),
    }

    # Prefer the purpose-made poster, then a waste shot, as the thumbnail.
    out["snapshot_url"] = out["poster_url"] or (out["waste"][0] if out["waste"] else None)
    return out


def _has_media(slots: dict[str, Any]) -> bool:
    """True if a `_session_media_urls` result is worth rendering at all.

    `video_pending` counts: the placeholder is the point of that flag.
    """
    return bool(slots.get("playback_url") or slots.get("highlight_url")
                or slots.get("waste") or slots.get("health")
                or slots.get("snapshot_url") or slots.get("preview_url")
                or slots.get("video_pending"))


async def api_timeline(request: web.Request) -> web.Response:
    """Grouped visit sessions for one LOCAL day, with their media.

    `GET /api/timeline?device=&filter=&date=`; grouping is
    events/ingest.py::group_sessions. Answers
    `{"date", "tz_offset", "counts", "sessions"}`: `counts` is per filter chip
    and is computed BEFORE filtering, so the chips keep showing their totals
    while one of them is active. Sessions are grouped in Python at query time
    rather than read pre-grouped from SQL. An unparseable `date` falls back to
    today rather than erroring — the response echoes the day it actually used.

    The day is cut at LOCAL midnight, not UTC. Cutting it at UTC filed every
    event between local midnight and the UTC offset under the previous day —
    50 of the 268 events in the reference corpus, including six complete
    after-midnight toilet visits. `tz_offset` is reported so the panel's date
    picker agrees with the server about which day "today" is.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)

    device = request.query.get("device")
    did = to_int(device, None) if device else None
    pet = request.query.get("pet")
    pet_id = to_int(pet, None) if pet else None
    filt = request.query.get("filter", "all")

    start_ts, end_ts, day_label = local_day_bounds(request.query.get("date", ""))

    rows = await store.query_timeline(device_id=did, start_ts=start_ts, end_ts=end_ts, limit=2000)
    sessions = ingest.group_sessions(rows)
    # Narrowing by pet happens BEFORE the counts, like the device filter and
    # unlike the chips: picking a cat means "this is her timeline", so the chip
    # badges should count her events, not the household's. Attribution is a
    # property of the grouped session (it can fill down from a detail event), so
    # this cannot move into the SQL query.
    if pet_id is not None:
        sessions = [s for s in sessions if s.get("pet_id") == pet_id]
    counts = ingest.filter_counts(sessions)
    filtered = [s for s in sessions if ingest.matches_filter(s, filt)]

    media_root = request.app["cfg"].get("media_root", "")
    reg = request.app["registry"]
    # Resolved once for the whole day rather than per card: a household has a
    # handful of pets, and a card carrying a bare `pet_id` is what made the
    # Timeline silent about which cat it was showing.
    pets = await _pets_by_id(request)
    payload: list[dict[str, Any]] = []
    for s in filtered:
        dev = reg.get(s.get("device_id"))
        # The codename decides what a numeric code MEANS -- 2 is a cleared
        # fault on a litter box and a completed meal on a feeder -- so it has
        # to reach every label call. The stored row is preferred over the live
        # registry so a re-typed or unregistered device still reads correctly.
        device_type = s.get("device_type") or (dev.device_type if dev else None)
        sub_events: list[dict[str, Any]] = []
        for e in s.get("sub_events", []):
            slots = _session_media_urls(e.get("media") or [], media_root)
            content = e.get("content") or {}
            sub_events.append({
                "id": e.get("id"), "event_type": e.get("event_type"), "ts": e.get("ts"),
                "label": decode.event_label(e.get("event_type"), content, device_type,
                                            e.get("state")),
                # Low-level mechanism/start steps the official app never shows;
                # the UI collapses these behind an expander.
                "detail": e.get("detail", False),
                "media": slots if _has_media(slots) else None,
            })
        content = s.get("content") or {}
        payload.append({
            "kind": s["kind"], "id": s["id"], "related_event": s.get("related_event"),
            "device_id": s.get("device_id"),
            "device_name": device_display_name(dev.device_type) if dev else "",
            "ts": s.get("ts"),
            # What to SHOW. For a visit this is when the pet entered; `ts` is
            # when the closing report reached us, several seconds later.
            "display_ts": s.get("display_ts") or s.get("ts"),
            "pet_id": s.get("pet_id"), "event_type": s.get("event_type"),
            **_pet_fields(pets, s.get("pet_id")),
            # A visit builds its own summary line (duration/weight); every
            # other card is titled by its event's label, now decoded from the
            # content too, so a lone cleaning reads "Manual cleaning completed"
            # rather than a generic "Cleaning done".
            "label": None if s["kind"] == "visit"
                     else decode.event_label(s.get("event_type"), content,
                                             device_type, s.get("state")),
            "event_kind": s.get("event_kind"), "duration_sec": s.get("duration_sec"),
            "weight": s.get("weight"),
            # Confirmed facts worth putting on the card itself: waste weight,
            # litter level, a named fault. Everything else stays in Debug info.
            "bits": decode.summary_bits(s.get("event_type"), content),
            "sub_events": sub_events,
            "media": _session_media_urls(s.get("media") or [], media_root),
        })

    return web.json_response({
        "date": day_label,
        # Seconds east of UTC for the day just returned, so the client's date
        # picker cuts "today" where the server did.
        "tz_offset": int(round(local_offset_hours(start_ts) * 3600)),
        "counts": counts,
        "sessions": payload,
    })


# --- Pets ----------------------------------------------------------------------

async def api_pets_list_create(request: web.Request) -> web.Response:
    """List the pets, or create one.

    GET answers `{"pets": [...]}`, POST `{"pet": {...}}`. A `name` is required;
    unparseable `device_ids` degrade to "no devices" rather than failing the
    create, since the link can be fixed afterwards. A new pet is immediately
    published to HA as its own virtual device.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)

    if request.method == "GET":
        return web.json_response({"pets": list((await _pets_by_id(request)).values())})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    name = str(body.get("name", "")).strip()
    if not name:
        return web.json_response({"error": "name required"}, status=400)
    try:
        device_ids = [int(x) for x in (body.get("device_ids") or [])]
    except (TypeError, ValueError):
        device_ids = []

    pet = await pet_registry.create(name, device_ids=device_ids, weight=body.get("weight"))
    ha_publisher = request.app.get("ha_publisher")
    if ha_publisher is not None:
        await ha_publisher.publish_pet_discovery(pet)
    return web.json_response({"pet": pet})


async def api_pet_detail(request: web.Request) -> web.Response:
    """Read, update or delete one pet.

    GET/POST answer `{"pet": {...}}`, DELETE `{"ok": bool}`.
    POST is a partial update: only the fields present in the body are touched,
    and a `device_ids` that will not parse is skipped rather than clearing the
    links.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)
    try:
        pid = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)

    if request.method == "DELETE":
        return web.json_response({"ok": await pet_registry.delete(pid)})

    if request.method == "GET":
        pet = await pet_registry.get(pid)
        if pet is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response({"pet": pet})

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    # Read before the update: the alias bookkeeping below needs to know which
    # ids are being REMOVED, and after the write that information is gone.
    before = await pet_registry.get(pid)
    if before is None:
        return web.json_response({"error": "not found"}, status=404)

    fields: dict[str, Any] = {}
    if "name" in body:
        fields["name"] = str(body["name"]).strip()
    if "device_ids" in body:
        try:
            fields["device_ids_json"] = [int(x) for x in body["device_ids"]]
        except (TypeError, ValueError):
            pass
    if "device_pet_ids" in body:
        # Identities the device reports that are not ours — see
        # ai/pets.py::resolve_pet_ref. Same skip-on-garbage rule as above:
        # dropping an existing binding by accident would silently un-attribute
        # every event it covers.
        try:
            fields["device_pet_ids_json"] = json.dumps(
                [int(x) for x in body["device_pet_ids"]])
        except (TypeError, ValueError):
            pass
    if "weight" in body:
        fields["weight"] = body["weight"]

    pet = await pet_registry.update(pid, **fields)
    if pet is None:
        return web.json_response({"error": "not found"}, status=404)

    # Binding a foreign identity is retroactive: the events already carry it in
    # `pet_ref`, so attributing them is a single UPDATE and the user sees their
    # history named the moment they confirm the mapping.
    #
    # Three cases, and the last two are why this is not just a loop over the
    # new list. An id REMOVED from the list has to have its events cleared, or
    # history stays on this pet while new events resolve to nobody. An id taken
    # from ANOTHER pet has to be removed from that pet's list, or
    # `resolve_pet_ref` keeps handing future events to whichever id is lower
    # while the history has already moved — the two would diverge permanently,
    # with no way back through the UI.
    bound = 0
    store = request.app.get("event_store")
    if "device_pet_ids_json" in fields and store is not None:
        wanted = set(json.loads(fields["device_pet_ids_json"]))
        try:
            had = set(json.loads(before.get("device_pet_ids_json") or "[]"))
        except (json.JSONDecodeError, TypeError):
            had = set()

        for ref in had - wanted:
            bound += await store.bind_pet_ref(ref, None)

        for ref in wanted:
            for other in await store.pets_claiming_ref(ref):
                if other == pid:
                    continue
                other_pet = await pet_registry.get(other)
                try:
                    aliases = json.loads((other_pet or {}).get("device_pet_ids_json") or "[]")
                except (json.JSONDecodeError, TypeError):
                    aliases = []
                await pet_registry.update(
                    other, device_pet_ids_json=json.dumps([a for a in aliases if a != ref]))
            bound += await store.bind_pet_ref(ref, pid)

    ha_publisher = request.app.get("ha_publisher")
    if ha_publisher is not None:
        await ha_publisher.publish_pet_discovery(pet)
    return web.json_response({"pet": pet, "bound_events": bound})


async def _pets_by_id(request: web.Request) -> dict[int, dict[str, Any]]:
    """Every pet keyed by id, faces attached; `{}` with no pet registry."""
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return {}
    pets = await pet_registry.all()
    for pet in pets:
        pet["faces"] = await _face_summaries(pet_registry, pet["id"])
    return {p["id"]: p for p in pets}


def _pet_fields(pets: dict[int, dict[str, Any]], pet_id: Any) -> dict[str, Any]:
    """`{pet_name, pet_photo_url}` for a card, both None when unattributed.

    A `pet_id` with no row is possible and deliberate — a pet can be deleted
    while its events remain (`EventStore.delete_pet`), so the name is looked up
    rather than assumed.
    """
    pet = pets.get(pet_id) if pet_id is not None else None
    if pet is None:
        return {"pet_name": None, "pet_photo_url": None}
    faces = pet.get("faces") or []
    return {
        "pet_name": pet.get("name"),
        "pet_photo_url": faces[0]["url"] if faces else None,
    }


async def _face_summaries(pet_registry: Any, pet_id: int) -> list[dict[str, Any]]:
    """One pet's faces as the panel wants them: `[{id, url}]`.

    `photo_path` is an absolute filesystem path and is deliberately not sent —
    the panel renders the image from its own route instead.
    """
    return [{"id": f["id"], "url": f"api/pets/{pet_id}/faces/{f['id']}/photo"}
            for f in await pet_registry.faces(pet_id)]


async def api_pet_faces(request: web.Request) -> web.Response:
    """List a pet's reference photos, or add one.

    GET answers `{"faces": [{id, url}]}`. POST takes the raw image bytes (not a
    multipart form) and answers `{"face": {id, url}}`; only a real JPEG is
    accepted, since `dev_discern_pic` only ever serves JPEGs to the device.

    A pet holds at most `MAX_FACES_PER_PET` photos — the firmware has its own
    "Too many face picture" guard whose limit we could not recover, so we stop
    at the largest count the real cloud was seen to serve.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)
    try:
        pid = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    if await pet_registry.get(pid) is None:
        return web.json_response({"error": "not found"}, status=404)

    if request.method == "GET":
        return web.json_response({"faces": await _face_summaries(pet_registry, pid)})

    data = await request.read()
    if not data:
        return web.json_response({"error": "empty body"}, status=400)
    if data[:3] != b"\xff\xd8\xff":
        return web.json_response({"error": "not a valid JPEG"}, status=400)
    if len(await pet_registry.faces(pid)) >= MAX_FACES_PER_PET:
        return web.json_response(
            {"error": f"at most {MAX_FACES_PER_PET} photos per pet"}, status=400)

    face = await pet_registry.add_face(pid, data)
    if face is None:
        return web.json_response({"error": "could not store photo"}, status=400)
    return web.json_response(
        {"face": {"id": face["id"], "url": f"api/pets/{pid}/faces/{face['id']}/photo"}})


async def _owned_face(request: web.Request) -> dict[str, Any] | None:
    """The face named by `{id}/{face_id}`, or None unless that pet owns it.

    Both ids are in the path and BOTH have to be checked. Matching only
    `face_id` made `DELETE /api/pets/999/faces/3` delete face 3 whoever owned
    it, unlink its file and answer `{"ok": true}` — a destructive route acting
    on an ownership assumption it never verified. A stale panel tab is enough
    to trigger that.
    """
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return None
    try:
        pid = int(request.match_info["id"])
        face_id = int(request.match_info["face_id"])
    except ValueError:
        return None
    face = await pet_registry.face(face_id)
    return face if face and face.get("pet_id") == pid else None


async def api_pet_face_detail(request: web.Request) -> web.Response:
    """DELETE one reference photo; answers `{"ok": bool}`."""
    pet_registry = request.app.get("pet_registry")
    if pet_registry is None:
        return web.json_response({"error": "pet registry not available"}, status=400)
    face = await _owned_face(request)
    if face is None:
        return web.json_response({"error": "not found"}, status=404)
    return web.json_response({"ok": await pet_registry.delete_face(face["id"])})


async def api_pet_face_photo(request: web.Request) -> web.Response:
    """Serve one reference photo to the PANEL.

    The device fetches these from `/faces/{filename}` on the device-facing app;
    that is a different aiohttp application on a different port, so the panel
    cannot link to it and needs its own route to show a thumbnail.
    """
    face = await _owned_face(request)
    if face is None or not face.get("photo_path"):
        return web.Response(status=404, text="not found")

    # The stored path is re-resolved against the faces directory rather than
    # trusted. Every writer today goes through `safe_join`, so this is
    # unreachable — but `photo_path` is a writable column and this route is on
    # an unauthenticated panel, so it must not depend on that staying true.
    # Same reasoning as handlers/discern.py::handle_faces.
    faces_dir = os.path.join(request.app["cfg"].get("data_dir", "/data"), "faces")
    try:
        path = safe_join(faces_dir, os.path.basename(face["photo_path"]))
    except UnsafePathError:
        return web.Response(status=404, text="not found")
    if not os.path.isfile(path):
        return web.Response(status=404, text="not found")
    return web.FileResponse(path)


async def api_pets_unbound(request: web.Request) -> web.Response:
    """Identities the device reported that match no pet row.

    Answers `{"unbound": [{pet_ref, count, last_ts}]}`. These are normally ids
    a box cached from PetKit's cloud before the takeover; the user binds one to
    a pet via `POST /api/pets/{id}` `device_pet_ids`, and that backfills every
    past event carrying it. We never guess the mapping — with one pet in the
    table the guess would even look right, and be wrong the day a second
    animal appears.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)
    return web.json_response({"unbound": await store.unbound_pet_refs()})


# --- Patchers -----------------------------------------------------------------

#: One lock per device id, so patcher runs on the same device serialise. They
#: share the device's `killall httpd`, its /system writes and its reboot, none
#: of which tolerate a second run interleaving.
_PATCHER_LOCKS: dict[int, asyncio.Lock] = {}

ALL_PATCHERS: dict[str, dict[str, Any]] = {
    p["id"]: p for p in [MQTT_PATCHER, CLOUD_PATCHER, CACERT_PATCHER, CAMERA_PATCHER, SSH_PATCHER]
}


# --- BLE accessories ---------------------------------------------------------
# A K3 Pura Air spray or W5 fountain has no network identity: it is reached over BLE
# by a mains-powered neighbour that relays for it. The device does NOT discover
# accessories — it pulls a list from the cloud (`dev_ble_device`) and scans for
# exactly those MACs, and no firmware in any of the three we have examined has
# an endpoint for reporting a newly-found one upward. Pairing happens in
# PetKit's app, which is to say in the cloud; since we ARE the cloud, pairing
# has to be entered here. That is why this is a form and not a discovery scan.


#: Accessory ids are allocated from here up. Deliberately far above the 8-digit
#: ids PetKit issues real devices, so a generated one cannot be mistaken for a
#: real device id and cannot collide with one that registers later.
ACCESSORY_ID_BASE = 900001


def _next_accessory_id(reg: DeviceRegistry, ble: BLERegistry) -> int:
    """An unused id for a new accessory.

    The id is ours to choose. The firmware stores whatever we send in its relay
    list, but every report it sends back identifies the accessory by
    `{"mac", "type"}` — the id appears in no report format string in any of the
    three firmwares. So it is a handle for our side (it becomes the Home
    Assistant device identity and the MQTT topic), not something the user has
    to go and look up.
    """
    taken = {d.petkit_id for d in ble.all()} | {d.petkit_id for d in reg.all()}
    candidate = ACCESSORY_ID_BASE
    while candidate in taken:
        candidate += 1
    return candidate


def _ble_view(dev: BLEDevice, reg: DeviceRegistry | None = None) -> dict[str, Any]:
    """One accessory as the panel shows it: identity, wire entry, and its state.

    Carries the same `entities` block a real device's detail does — resolved
    values included — because the panel renders an accessory as its own device
    panel and reuses the very same table and control renderers. Without it the
    accessory was three cells in its parent's card while its decoded state, its
    entities and its controls existed only in Home Assistant.

    The state document needs no adapter: an accessory's `value_path` is already
    `states.x`/`consumables.x` and `dev.state` has exactly those sections, so
    `dig_path` reads it directly. A button has no path and no value — it is an
    action, and `None` is the honest answer rather than the whole document.
    """
    entities = get_ble_entities(dev.ble_type)
    parent = reg.get(dev.link_with) if (reg and dev.link_with) else None
    return {
        "petkit_id": dev.petkit_id,
        "ble_type": dev.ble_type,
        "name": device_display_name(dev.ble_type),
        # Who relays for it. An accessory with no reachable parent is not
        # merely offline, it is unaddressable, and the panel says which.
        "parent_name": device_display_name(parent.device_type) if parent else "",
        "parent_type": parent.device_type if parent else "",
        "parent_online": bool(parent and parent.online),
        "last_seen": dev.last_seen,
        "entities": [{
            "component": e.component, "key": e.key, "name": e.name,
            "value_path": e.value_path, "unit": e.unit, "device_class": e.device_class,
            "icon": e.icon, "options": e.options, "option_values": e.option_values,
            "settable": e.is_settable,
            "entity_category": e.entity_category,
            "min": e.min_value, "max": e.max_value, "step": e.step,
            "value": dig_path(dev.state, e.value_path) if e.value_path else None,
        } for e in entities],
        "mac": dev.mac,
        "secret": dev.secret,
        "interval": dev.interval,
        "link_with": dev.link_with,
        "serial_number": dev.serial_number,
        "scan_type": dev.scan_type,
        # True when the `type` in the wire entry below is a working assumption
        # rather than a value anybody has captured. Surfaced because the person
        # who can settle it is the one whose fountain either pairs or does not.
        "scan_type_is_guessed": dev.scan_type_is_guessed,
        # Exactly what `dev_ble_device` will hand the parent, so a user can see
        # whether what they typed is what the device will be told to scan for.
        "wire_entry": dev.to_ble_list_entry(),
        "state": dev.state,
    }


async def _send_k3_link(request: web.Request, parent_id: int, k3_id: int) -> str:
    """Tell a parent litter box which K3 it owns. Returns the transport used.

    A K3 is the one accessory NOT served through `dev_ble_device` — the relay
    list deliberately excludes it — so pairing one means writing `k3Id` on the
    parent instead. `autoRefresh` rides along on a link, and `k3Id: 0` unlinks.
    Both come from localkit's `PetkitPuraMax::link/unlink`, which is the only
    source for this; no capture of ours has ever contained a linked K3.

    Best-effort by design: the accessory is registered either way, because a
    parent that is asleep must not fail the pairing the user just entered. It
    picks the property up from `dev_device_info` on its next poll regardless.
    """
    reg = request.app["registry"]
    bridge = request.app.get("bridge")
    parent = reg.get(parent_id)
    if parent is None:
        return "no parent"

    params = {"k3Id": k3_id, "autoRefresh": 1} if k3_id else {"k3Id": 0}
    envelope = make_mqtt_property_set(params)
    if parent.mqtt_connected and bridge is not None and getattr(bridge, "_client", None):
        try:
            await bridge.publish_to_device(parent, PROPERTY_SET_SUFFIX, envelope)
            return "mqtt"
        except Exception as e:
            log.warning("panel: K3 link publish failed for device %d, queueing: %s", parent_id, e)
    envelope["_service_suffix"] = PROPERTY_SET_SUFFIX
    parent.command_queue.append(envelope)
    return "heartbeat-queue"


async def api_ble_accessories(request: web.Request) -> web.Response:
    """List (GET) or pair/update (POST) a BLE accessory.

    POST body: `{ble_type, petkit_id, link_with, mac, secret, interval,
    serial_number}`. The five wire fields are the ones the firmware's own parse
    logs name (`id`, `mac`, `secret`, `interval`, `type` in
    `ble_relay_network.c`), so this form is the protocol, not a UI convenience.

    Answers `{"accessories": [...]}` either way, so the caller never has to
    re-fetch after a write.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    if ble is None:
        return web.json_response({"error": "no BLE registry"}, status=503)

    if request.method == "POST":
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "bad json"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "expected an object"}, status=400)

        ble_type = str(body.get("ble_type", "")).lower().strip()
        if ble_type not in BLE_TYPES:
            return web.json_response(
                {"error": f"ble_type must be one of {', '.join(BLE_TYPES)}"}, status=400)

        petkit_id = to_int(body.get("petkit_id"), 0) or 0
        if petkit_id < 0:
            return web.json_response({"error": "petkit_id cannot be negative"}, status=400)
        if not petkit_id:
            petkit_id = _next_accessory_id(reg, ble)
        # An accessory shares the `petkit_{id}` HA identity and the
        # `petkit-local/{id}/state` topic with real devices, so a collision does
        # not merely confuse the panel — it makes two devices fight over one
        # Home Assistant entity set.
        if reg.get(petkit_id) is not None:
            return web.json_response(
                {"error": f"id {petkit_id} is already a real device"}, status=409)

        mac = normalize_mac(str(body.get("mac", "")))
        if not mac:
            return web.json_response(
                {"error": "mac must be 12 hex digits, e.g. AA:BB:CC:DD:EE:FF"}, status=400)
        clash = ble.get_by_mac(mac)
        if clash is not None and clash.petkit_id != petkit_id:
            return web.json_response(
                {"error": f"mac already paired to id {clash.petkit_id}"}, status=409)

        link_with = to_int(body.get("link_with"), 0) or 0
        if link_with and reg.get(link_with) is None:
            return web.json_response({"error": f"no device with id {link_with}"}, status=400)

        ble.register(
            ble_type=ble_type,
            petkit_id=petkit_id,
            mac=mac,
            secret=str(body.get("secret", "")),
            interval=to_int(body.get("interval"), 240) or 240,
            link_with=link_with,
            serial_number=str(body.get("serial_number", "")),
            # Optional: overrides the `type` the parent is told to scan for.
            # Only the W5's is a captured value, so for the other fountains this
            # is the field that turns a guess into something a user can correct.
            scan_type=max(to_int(body.get("scan_type"), 0) or 0, 0),
        )
        # Publish immediately rather than waiting for the next HA reconnect:
        # an accessory that appears in the panel but not in HA reads as broken.
        publisher = request.app.get("ha_publisher")
        dev = ble.get(petkit_id)
        if publisher is not None and dev is not None:
            await publisher.publish_ble_discovery(dev)
            await publisher.publish_ble_state(dev)

        # A W5 is picked up from the relay list the parent already polls; a K3
        # is not in that list at all and has to be named on the parent.
        if ble_type == "k3" and link_with:
            await _send_k3_link(request, link_with, petkit_id)

    return web.json_response({"accessories": [_ble_view(d, reg) for d in ble.all()]})


def _ble_entity_value(entity: Any, payload: str) -> int | None:
    """A panel control's payload as the integer an accessory frame carries.

    Same three shapes Home Assistant sends — ON/OFF, a select label, a decimal
    — because `controlRow` in the panel emits exactly what the HA entity would.
    None for anything else: a write to a fountain is not worth guessing at.

    A button carries no value; 0 stands in for one, and the command it builds
    never reads it.
    """
    text = payload.strip()
    if entity.component == "button":
        return 0
    if entity.component == "switch":
        upper = text.upper()
        if upper in ("ON", "1", "TRUE"):
            return 1
        if upper in ("OFF", "0", "FALSE"):
            return 0
        return None
    if entity.component == "select":
        options = list(entity.options or [])
        if text in options:
            values = entity.option_values or list(range(len(options)))
            return int(values[options.index(text)])
        return None
    return to_int(text, None)


async def api_ble_command(request: web.Request) -> web.Response:
    """Set one entity on a BLE accessory, from the panel.

    The accessory twin of `api_send_command`, and it has to be a twin rather
    than a branch: the delivery rules are different in a way that matters.

    There is no `transport` here. A real device that is off MQTT still has a
    heartbeat queue to hold a command until it polls; an accessory has neither
    — it is reachable only while its parent is on MQTT, because the command is
    a `thing/service/ble` publish to that parent. So the honest answers are
    "sent" or "cannot reach it", and queueing into nothing is not one of them.

    Returns 400 with the reason when the write is refused — both CTW3 frames
    restate every field they carry, so a setting cannot be changed before the
    accessory has reported the rest of them.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    bridge = request.app.get("bridge")
    hub = request.app["hub"]

    ble_id = to_int(request.match_info.get("id"), 0) or 0
    dev = ble.get(ble_id)
    if dev is None:
        return web.json_response({"error": "not found"}, status=404)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    entity_key = body.get("entity")
    entity = next((e for e in get_ble_entities(dev.ble_type) if e.key == entity_key), None)
    if entity is None:
        return web.json_response({"error": f"unknown entity {entity_key}"}, status=400)

    value = _ble_entity_value(entity, str(body.get("value", "")))
    if value is None:
        return web.json_response(
            {"error": f"unusable value for {entity.key}"}, status=400)

    parent = reg.get(dev.link_with) if dev.link_with else None
    if parent is None:
        return web.json_response(
            {"error": "no registered parent to relay through"}, status=409)
    if bridge is None or not getattr(bridge, "_client", None):
        return web.json_response({"error": "MQTT bridge is not running"}, status=409)
    if not parent.mqtt_connected:
        return web.json_response(
            {"error": f"{device_display_name(parent.device_type)} is not on MQTT — "
                      f"an accessory can only be reached while its parent is"},
            status=409)

    try:
        cmd, payload = ble_command_for(dev, entity.key, value)
    except Refused as exc:
        return web.json_response({"error": str(exc)}, status=400)

    if not await bridge.publish_ble_command(parent, dev, cmd, payload):
        return web.json_response({"error": "nothing was sent"}, status=502)

    # Optimistic, exactly as the HA path is: the accessory acknowledges the
    # write, but only its next status proves it, and that is a poll away. A
    # button has no state and no `value_path` to file one under.
    if entity.value_path:
        dev.state.setdefault("states", {})[entity.value_path.split(".")[-1]] = value
    ble.mark_dirty()
    hub.record_command(ble_id, "ble", f"{entity.key}={value} (cmd {cmd})")
    return web.json_response({"ok": True, "delivered": "ble", "entity": entity.key,
                              "cmd": cmd, "via": parent.petkit_id})


async def api_ble_poll(request: web.Request) -> web.Response:
    """Ask an accessory's parent to fetch a reading now.

    The one action a BLE accessory has, and it is not the accessory's: nothing
    in the CTW3 protocol is shaped like "do X now" — its four writes are all
    settings. What IS worth a button is the relay itself, because an accessory
    speaks only when its parent is told to open a session, and otherwise that
    happens on a timer up to `interval` seconds away. When the scan type is a
    guess, "has it ever answered" is the only question, and waiting four
    minutes to ask it is not a workflow.
    """
    ble = request.app["ble_registry"]
    reg = request.app["registry"]
    bridge = request.app.get("bridge")
    hub = request.app["hub"]

    dev = ble.get(to_int(request.match_info.get("id"), 0) or 0)
    if dev is None:
        return web.json_response({"error": "not found"}, status=404)
    parent = reg.get(dev.link_with) if dev.link_with else None
    if parent is None:
        return web.json_response(
            {"error": "no registered parent to relay through"}, status=409)
    if bridge is None or not getattr(bridge, "_client", None):
        return web.json_response({"error": "MQTT bridge is not running"}, status=409)
    if not parent.mqtt_connected:
        return web.json_response(
            {"error": f"{device_display_name(parent.device_type)} is not on MQTT — "
                      f"an accessory can only be reached while its parent is"},
            status=409)

    if not await bridge.request_ble_reading(parent, dev):
        return web.json_response({"error": "nothing was sent"}, status=502)
    hub.record_command(dev.petkit_id, "ble", "read now")
    return web.json_response({"ok": True, "via": parent.petkit_id})


async def api_ble_delete(request: web.Request) -> web.Response:
    """Unpair an accessory.

    The parent simply stops being told to scan for it on its next
    `dev_ble_device`; there is no revoke command. Its Home Assistant entities
    are left behind — nothing here publishes an empty discovery payload — so
    the answer says so rather than letting the user think HA has been tidied.
    """
    ble = request.app["ble_registry"]
    if ble is None:
        return web.json_response({"error": "no BLE registry"}, status=503)
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    dev = ble.get(did)
    was_k3 = dev is not None and dev.ble_type == "k3"
    parent_id = dev.link_with if dev is not None else 0
    if not ble.remove(did):
        return web.json_response({"error": "not found"}, status=404)
    if was_k3 and parent_id:
        await _send_k3_link(request, parent_id, 0)
    return web.json_response({
        "ok": True,
        "accessories": [_ble_view(d, request.app["registry"]) for d in ble.all()],
        "note": "Home Assistant keeps the entities until you delete the device there.",
    })


async def api_patcher_status(request: web.Request) -> web.Response:
    """Per-patcher status for a device: `{"patchers", "device_ip", "supported"}`.

    Only Ingenic Linux devices can be patched at all; anything else answers
    `supported: False` with no patchers rather than a 400. The MQTT patcher is
    reported as active-and-`greyed` while the device holds an MQTT session,
    because a live session is proof the patch took regardless of what was
    recorded.
    """
    reg = request.app["registry"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)
    if not d.is_next_gen:
        return web.json_response({"patchers": {}, "device_ip": "", "supported": False})

    active = _get_active_patchers(d)
    result: dict[str, dict[str, Any]] = {}
    for pid, pinfo in ALL_PATCHERS.items():
        applied = pid in active
        status = "applied" if applied else "not applied"
        if pid == "mqtt" and d.mqtt_connected:
            status = "active (MQTT connected)"
        entry: dict[str, Any] = {
            "id": pid,
            "name": pinfo["name"],
            "description": pinfo["description"],
            "status": status,
            "applied": applied,
            "unavailable": "",
            "greyed": pid == "mqtt" and d.mqtt_connected,
            # What to warn about before we know the model. The actual gate at
            # apply time uses the measured size of the patched file, which is
            # smaller on every model we have measured.
            "needs_bytes": pinfo["needs_bytes"],
        }
        if pinfo.get("needs_pubkey"):
            entry["needs_pubkey"] = True
            entry["ssh_pubkey"] = d.config.get("ssh_pubkey", "")
        result[pid] = entry
    return web.json_response({"patchers": result, "device_ip": d.state.get("ip", ""), "supported": True})


async def api_patcher_apply(request: web.Request) -> web.Response:
    """Start applying or removing a patcher; answers `{ok, patcher, action}`.

    The 200 means "accepted", not "done": a run takes minutes (it waits out
    device reboots), so it is spawned as a tracked background task and reports
    progress as `patcher` events on the hub, which the UI follows over the
    WebSocket. Failures land there too, never in this response.
    """
    reg = request.app["registry"]
    hub = request.app["hub"]
    try:
        did = int(request.match_info["id"])
    except ValueError:
        return web.json_response({"error": "bad id"}, status=400)
    d = reg.get(did)
    if not d:
        return web.json_response({"error": "not found"}, status=404)

    if not d.is_next_gen:
        return web.json_response(
            {"error": "patchers only supported on Linux devices"}, status=400)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "bad json"}, status=400)

    patcher_id = body.get("patcher")
    action = body.get("action", "apply")
    if patcher_id not in ALL_PATCHERS:
        return web.json_response({"error": f"unknown patcher: {patcher_id}"}, status=400)
    # SSH needs a public key. Accept it in the request, persist it on the device
    # config so a re-apply after an OTA does not ask again, and validate it
    # before spawning a background task that would fail minutes later.
    pubkey = body.get("pubkey", "").strip() or d.config.get("ssh_pubkey", "")
    if patcher_id == "ssh" and action != "remove":
        if not pubkey:
            return web.json_response({"error": "paste your SSH public key"}, status=400)
        if not any(pubkey.startswith(t) for t in ("ssh-rsa ", "ecdsa-sha2-", "ssh-ed25519 ")):
            return web.json_response({"error": "not a recognised SSH public key format"}, status=400)
        d.config["ssh_pubkey"] = pubkey
        reg.save()

    device_ip = d.state.get("ip", "")
    if not device_ip:
        return web.json_response({"error": "device IP not known - wait for a state_report"}, status=400)

    api_url = request.app["cfg"].get("api_url", "")
    if not api_url:
        return web.json_response({"error": "api_url not configured"}, status=500)

    # Extract host:port from api_url for the device to wget from
    parsed = urlparse(api_url)
    download_base = f"http://{parsed.hostname}:{parsed.port or 80}/patcher/download"

    # Launch as a background task — the UI tracks progress via WS events. A
    # patcher run outlives its request by minutes (it sleeps between device
    # reboots), so it must be pinned and drained at shutdown, not orphaned.
    async def _run() -> None:
        """Drive one patcher run, reporting progress and failure over the hub.

        Cancellation is re-raised after logging it: this task is drained at
        shutdown, and swallowing CancelledError would hang the drain.
        """
        try:
            # One run at a time per device. Two concurrent runs would race over
            # `killall httpd` — which is global and cannot target a port — and
            # each would tear down the other's file server mid-transfer.
            lock = _PATCHER_LOCKS.setdefault(did, asyncio.Lock())
            if lock.locked():
                hub.publish("patcher", did,
                            f"[{patcher_id}] waiting for the running patcher to finish...")
            async with lock:
                if action == "remove":
                    await _patcher_remove(d, patcher_id, device_ip, download_base,
                                          hub, request.app)
                else:
                    await _patcher_apply(d, patcher_id, device_ip, download_base, hub, request.app)
        except asyncio.CancelledError:
            hub.publish("patcher", did, f"[{patcher_id}] cancelled (shutting down)")
            raise
        except Exception as e:
            log.exception("Patcher %s %s failed for device %d", patcher_id, action, did)
            hub.publish("patcher", did, f"[{patcher_id}] FAILED: {e}")

    _spawn_background(request.app, _run(), name=f"patcher-{patcher_id}-{action}-{did}")
    hub.publish("patcher", did, f"[{patcher_id}] {action} started")
    return web.json_response({"ok": True, "patcher": patcher_id, "action": action})


def _get_active_patchers(d: Device) -> set[str]:
    """Determine which patchers are currently active for a device, based on
    which patched files exist on /system (tracked in device config)."""
    return set(d.config.get("active_patchers", []))


def _save_active_patchers(d: Device, active: set[str], registry: DeviceRegistry) -> None:
    """Record the active patcher set on the device and persist it."""
    d.config["active_patchers"] = sorted(active)
    registry.save()


async def _patcher_apply(d: Device, patcher_id: str, device_ip: str, download_base: str,
                         hub: EventHub, app: web.Application) -> None:
    """Download the target file from the device, patch it, and put it back.

    Runs detached from the request (see `api_patcher_apply`) and narrates every
    step onto the hub, because from the UI's side this is a multi-minute silent
    operation otherwise. The `asyncio.sleep`s are the device's side of the
    handshake: each step waits out roughly one delivery-plus-execution interval
    before assuming the previous one ran. Over the heartbeat that includes the
    device's ~10s poll; over MQTT the command is pushed immediately and the wait
    is only execution time, so the sleeps are conservative rather than wrong.
    The binary is fetched over a temporary busybox httpd the device is told to
    start, and which is killed again even if patching raises.

    The final step rewrites /system/app_init.sh from the FULL active set and
    reboots, so patchers compose instead of overwriting each other.
    """
    did = d.petkit_id
    reg = app["registry"]
    # Which transport each run_cmd takes is decided per command by
    # `send_run_cmd`: a device that has joined MQTT stops polling the heartbeat
    # entirely, and the queue it used to be given would never be drained.
    bridge = app.get("bridge")
    cfg = app.get("cfg", {})
    data_dir = cfg.get("data_dir", "/data") if "data_dir" in cfg else "/data"
    P = f"[{patcher_id}]"
    storage_dir = await detect_patch_storage_dir(d, device_ip, bridge=bridge)
    reg.save()
    wrapper_path = app_init_wrapper_path(d)

    if patcher_id in ("mqtt", "cloud", "cacert"):
        hub.publish("patcher", did, f"{P} starting temp httpd on device...")
        await send_run_cmd(d, f"busybox httpd -p {DEVICE_HTTPD_PORT} -h /app/bin &", bridge)
        await asyncio.sleep(12)

        try:
            if patcher_id == "mqtt":
                hub.publish("patcher", did, f"{P} downloading ctrl from device...")
                binary = await download_from_device(device_ip, "ctrl")
                assert_download_plausible(binary, "ctrl")
                hub.publish("patcher", did, f"{P} downloaded ctrl ({len(binary)} bytes)")
                patched, offset = patch_ctrl(binary)
                hub.publish("patcher", did, f"{P} patched at offset 0x{offset:x} (md5={md5hex(patched)[:12]})")
                staged_name = "ctrl_patched"
                device_path = patched_file_path(staged_name, d)
            elif patcher_id == "cloud":
                hub.publish("patcher", did, f"{P} downloading cloud from device...")
                binary = await download_from_device(device_ip, "cloud")
                assert_download_plausible(binary, "cloud")
                hub.publish("patcher", did, f"{P} downloaded cloud ({len(binary)} bytes)")
                patched, applied = patch_cloud(binary)
                names = ", ".join(a["name"] for a in applied if a["status"] == "applied")
                hub.publish("patcher", did, f"{P} applied {len(applied)} patches: {names}")
                staged_name = "cloud_patched"
                device_path = patched_file_path(staged_name, d)
            else:
                hub.publish("patcher", did, f"{P} downloading ca.crt from device...")
                ca_data = await download_from_device(device_ip, "ca.crt")
                assert_download_plausible(ca_data, "ca.crt")
                hub.publish("patcher", did, f"{P} downloaded ca.crt ({len(ca_data)} bytes)")
                our_cert = load_our_cert(data_dir)
                patched = patch_ca_bundle(ca_data, our_cert)
                hub.publish("patcher", did, f"{P} appended our cert ({len(patched)} bytes)")
                staged_name = "ca_patched.crt"
                device_path = patched_file_path(staged_name, d)
        finally:
            # The space probe starts its OWN httpd on a different port, and
            # `killall httpd` cannot target one — so the download server has to
            # be gone before the probe runs, not merely before we finish.
            await send_run_cmd(d, "killall httpd 2>/dev/null", bridge)

        # Only now, with the patched bytes in hand, is the exact requirement
        # known. Checking before staging means a device that cannot fit the
        # write is left completely untouched.
        hub.publish("patcher", did, f"{P} checking free space on device...")
        hub.publish("patcher", did, f"{P} " + await ensure_space_for(
            d, device_ip, write_bytes=len(patched),
            targets=[device_path, wrapper_path], bridge=bridge,
            mount=storage_dir))

        stage_file(staged_name, patched)
        hub.publish("patcher", did, f"{P} uploading {staged_name} to device...")
        await asyncio.sleep(12)

        await send_run_cmd(d, f"wget -q -O {device_path} {download_base}/{staged_name} && chmod +x {device_path}", bridge)
        await asyncio.sleep(12)
        cleanup_staged(staged_name)
        hub.publish("patcher", did, f"{P} file uploaded to {device_path}")

    elif patcher_id == "ssh":
        pubkey = d.config.get("ssh_pubkey", "")
        if not pubkey:
            hub.publish("patcher", did, f"{P} FAILED: no public key configured")
            return

        hub.publish("patcher", did, f"{P} starting temp httpd on device...")
        await send_run_cmd(d, f"busybox httpd -p {DEVICE_HTTPD_PORT} -h /app/bin &", bridge)
        await asyncio.sleep(12)

        try:
            hub.publish("patcher", did, f"{P} downloading ctrl header to detect CPU...")
            ctrl_head = await download_from_device(device_ip, "ctrl")
            arch = elf_arch(ctrl_head)
            if not arch or arch not in SSH_ARCH_TO_BINARY:
                hub.publish("patcher", did,
                            f"{P} FAILED: ctrl is not a recognised architecture "
                            f"({arch or 'not an ELF'})")
                return
            bin_name = SSH_ARCH_TO_BINARY[arch]
            hub.publish("patcher", did, f"{P} device is {arch}, using {bin_name}")
        finally:
            await send_run_cmd(d, "killall httpd 2>/dev/null", bridge)
            await asyncio.sleep(2)

        bin_path = dropbear_path_for(arch)
        with open(bin_path, "rb") as f:
            dropbear = f.read()

        from petkit_local.patchers.ssh import AUTHKEYS_STAGED_NAME
        authkeys = (pubkey.strip() + "\n").encode()
        ssh_paths = patcher_device_files(SSH_PATCHER, d)
        hub.publish("patcher", did, f"{P} checking free space on device...")
        hub.publish("patcher", did, f"{P} " + await ensure_space_for(
            d, device_ip, write_bytes=len(dropbear) + len(authkeys) + DBKEY_RESERVE_BYTES,
            targets=[*ssh_paths, wrapper_path],
            bridge=bridge, mount=storage_dir))

        stage_file(bin_name, dropbear)
        stage_file(AUTHKEYS_STAGED_NAME, authkeys)
        hub.publish("patcher", did, f"{P} staged {bin_name} + authorized_keys for download")

        cmds = ssh_install_commands(download_base, bin_name, d)
        for i, cmd in enumerate(cmds, 1):
            hub.publish("patcher", did, f"{P} step {i}/{len(cmds)}: {cmd[:80]}...")
            await send_run_cmd(d, cmd, bridge)
            if not await wait_for_heartbeat(d, timeout=30):
                hub.publish("patcher", did, f"{P} step {i} timed out (device may not have polled)")
            await asyncio.sleep(12)

        cleanup_staged(bin_name)
        cleanup_staged(AUTHKEYS_STAGED_NAME)
        hub.publish("patcher", did, f"{P} dropbear installed, SSH should be reachable now")

    else:
        # camera writes no file of its own — but the wrapper below is still a
        # write to persistent storage, and a full filesystem is exactly why
        # that would fail silently, leaving the patch marked active but inert.
        hub.publish("patcher", did, f"{P} checking free space on device...")
        hub.publish("patcher", did, f"{P} " + await ensure_space_for(
            d, device_ip, write_bytes=0, targets=[wrapper_path], bridge=bridge,
            mount=storage_dir))

    active = _get_active_patchers(d)
    active.add(patcher_id)
    _save_active_patchers(d, active, reg)

    hub.publish("patcher", did, f"{P} uploading {wrapper_path} wrapper...")
    wrapper_content = generate_app_init_wrapper(active, d)
    stage_file("app_init.sh", wrapper_content.encode())
    await send_run_cmd(
        d,
        f"wget -q -O {wrapper_path} {download_base}/app_init.sh && "
        f"chmod +x {wrapper_path}",
        bridge,
    )
    # Wait for the device to actually fetch the file BEFORE cleaning it up.
    # The old code cleaned after a fixed 15 s, which raced with the heartbeat:
    # the device polls every ~10 s, the command reaches it on the next poll, and
    # the wget runs after that — easily 20+ s total. A 404 meant the wrapper
    # was never delivered, and the device rebooted into a stale one.
    if not await wait_for_heartbeat(d, timeout=30):
        hub.publish("patcher", did, f"{P} wrapper upload timed out - staged file kept for retry")
    else:
        await asyncio.sleep(15)
        cleanup_staged("app_init.sh")

    hub.publish("patcher", did, f"{P} rebooting device...")
    await send_run_cmd(d, "reboot", bridge)
    hub.publish("patcher", did, f"{P} done - device will reboot, patch active on next boot")


async def _patcher_remove(d: Device, patcher_id: str, device_ip: str, download_base: str,
                          hub: EventHub, app: web.Application) -> None:
    """Drop one patcher from the active set and reboot the device into it.

    Nothing is downloaded or patched here — removal is just deleting the
    patched files and rewriting the app_init.sh wrapper from the REMAINING set,
    so removing one patcher leaves the others working. With none left the
    wrapper itself is removed and the device boots stock again.
    """
    did = d.petkit_id
    reg = app["registry"]
    bridge = app.get("bridge")  # see _patcher_apply
    P = f"[{patcher_id}]"
    await detect_patch_storage_dir(d, device_ip, bridge=bridge)
    reg.save()
    wrapper_path = app_init_wrapper_path(d)

    active = _get_active_patchers(d)
    active.discard(patcher_id)
    _save_active_patchers(d, active, reg)

    pinfo = ALL_PATCHERS[patcher_id]
    # A pure bind-mount patcher (camera) puts no file on the device, and
    # busybox `rm -f` with no operands prints its usage and exits non-zero —
    # which would be a confusing failure for a step that has nothing to do.
    file_paths = patcher_device_files(pinfo, d)
    files = " ".join(file_paths)
    cleanup_cmds = f"rm -f {files}" if files else "true"

    if active:
        hub.publish("patcher", did, f"{P} uploading updated wrapper...")
        wrapper_content = generate_app_init_wrapper(active, d)
        stage_file("app_init.sh", wrapper_content.encode())
        # Delete the patched files and download the new wrapper in one command,
        # so the device never boots with a wrapper that references files that
        # no longer exist.
        await send_run_cmd(
            d,
            f"{cleanup_cmds}; "
            f"wget -q -O {wrapper_path} {download_base}/app_init.sh && "
            f"chmod +x {wrapper_path}",
            bridge,
        )
        # Same wait-then-cleanup as apply: the staged file must survive until
        # the device actually fetches it.
        if not await wait_for_heartbeat(d, timeout=30):
            hub.publish("patcher", did, f"{P} wrapper upload timed out - staged file kept for retry")
        else:
            await asyncio.sleep(15)
            cleanup_staged("app_init.sh")
        hub.publish("patcher", did, f"{P} rebooting device...")
        await send_run_cmd(d, "reboot", bridge)
    else:
        await send_run_cmd(
            d, f"{cleanup_cmds}; {build_wrapper_remove_cmd(d)} && reboot",
            bridge)

    hub.publish("patcher", did, f"{P} removal queued - device will reboot")


def _limit_param(request: web.Request, default: int, maximum: int) -> int:
    """Read `?limit=`, ignoring junk and clamping to a serveable range."""
    return max(1, min(to_int(request.query.get("limit"), default), maximum))


async def api_event_detail(request: web.Request) -> web.Response:
    """Everything the database holds about one stored event.

    `GET /api/timeline/{id}` — backs the Timeline's "Debug info" expander.
    Answers `{event, code, decoded, content, state}`:

    * `event` — the row's own columns, including the ones the timeline drops
      (`event_uid`, `source`, `parent_event`, `score`).
    * `code` — what `events/codes.py` knows about this event type: its
      evidence grade, the firmware function behind it, and any note recording
      where the firmware RE and our captures disagree.
    * `decoded` — every `content` field rendered by `events/decode.py`,
      including ones we do not claim to understand.
    * `content` / `state` — the raw stored payloads, so our interpretation can
      always be checked against the original.

    Served per-event rather than inlined into `/api/timeline` because every
    event carries a state snapshot averaging 1.2 kB; on a busy day that is
    ~100 kB of payload re-fetched on every websocket refresh, to show one row
    the user may never open.
    """
    store = request.app.get("event_store")
    if store is None:
        return web.json_response({"error": "event store not available"}, status=400)

    event_id = to_int(request.match_info.get("id"), None)
    if event_id is None:
        return web.json_response({"error": "invalid event id"}, status=400)

    row = await store.get_event(event_id)
    if row is None:
        return web.json_response({"error": "no such event"}, status=404)

    event_type = row.get("event_type")
    device_type = row.get("device_type")
    content = ingest.content_of_row(row)
    state = ingest.state_of_row(row)
    code = codes.lookup(event_type, device_type)

    return web.json_response({
        "event": {
            "id": row.get("id"), "event_uid": row.get("event_uid"),
            "event_type": event_type, "event_kind": row.get("event_kind"),
            "label": decode.event_label(event_type, content, device_type),
            "ts": row.get("ts"), "created_at": row.get("created_at"),
            "source": row.get("source"), "device_id": row.get("device_id"),
            "device_type": device_type, "pet_id": row.get("pet_id"),
            # Both, and they differ on purpose: `pet_ref` is what the device
            # claimed, `pet_id` is what resolved to one of our rows. A ref with
            # no id is the signal that the box is still matching against faces
            # cached from PetKit's cloud.
            "pet_ref": row.get("pet_ref"),
            **_pet_fields(await _pets_by_id(request), row.get("pet_id")),
            "score": row.get("score"),
            "related_event": row.get("related_event"),
            "parent_event": row.get("parent_event"),
        },
        "code": {
            "code": event_type, "label": code.label, "kind": code.kind,
            "grade": code.grade, "role": code.role, "detail": code.detail,
            "firmware": code.firmware, "note": code.note,
        } if code else None,
        "decoded": [
            {"key": f.key, "label": f.label, "raw": f.raw,
             "text": f.text, "grade": f.grade, "note": f.note}
            for f in decode.decode_content(event_type, content, device_type)
        ],
        "content": content,
        "state": state,
    })


async def api_events(request: web.Request) -> web.Response:
    """A bare JSON array of the most recent hub events, oldest first.

    Optionally narrowed to one `?device=`. This backfills the Log tab on load;
    `api_ws` is what keeps it live afterwards.
    """
    hub = request.app["hub"]
    limit = _limit_param(request, 200, MAX_EVENT_LIMIT)
    device = request.query.get("device")
    did = to_int(device, None) if device else None
    return web.json_response(hub.recent(limit, device_id=did))


async def api_ws(request: web.Request) -> web.WebSocketResponse:
    """Stream hub events to the panel: a replay of the last 80, then live ones.

    A `{"kind": "ping"}` frame goes out whenever the queue is idle for 25s. It
    is not a keepalive (aiohttp's own `heartbeat` covers that) — it is what
    tells the frontend the connection is still good, and its absence is what
    flips the header pill to disconnected.
    """
    hub = request.app["hub"]
    ws = web.WebSocketResponse(heartbeat=25)
    await ws.prepare(request)
    q = hub.subscribe()
    try:
        for ev in hub.recent(80):
            await ws.send_json(ev)
        while not ws.closed:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=25)
                await ws.send_json(ev)
            except asyncio.TimeoutError:
                await ws.send_json({"kind": "ping", "ts": time.time()})
    except (asyncio.CancelledError, ConnectionResetError, RuntimeError):
        pass
    finally:
        hub.unsubscribe(q)
    return ws


def _capture_dir(request: web.Request) -> str:
    """The configured capture directory, or an empty string when there is none."""
    return request.app["cfg"].get("capture_dir", "")


async def api_capture_list(request: web.Request) -> web.Response:
    """List the capture files as `{"dir", "files": [{name, size, lines}], "enabled"}`.

    A file that cannot be read is skipped rather than failing the listing —
    a capture in progress is being appended to underneath us.
    """
    d = _capture_dir(request)
    files: list[dict[str, Any]] = []
    if d and os.path.isdir(d):
        for name in sorted(os.listdir(d)):
            if name.endswith(".jsonl"):
                p = os.path.join(d, name)
                try:
                    with open(p) as f:
                        lines = sum(1 for _ in f)
                    files.append({"name": name, "size": os.path.getsize(p), "lines": lines})
                except OSError:
                    pass
    return web.json_response({"dir": d, "files": files, "enabled": _current_settings(request)["capture"]})


def _safe_capture_path(request: web.Request) -> str | None:
    """Resolve the `{name}` route part inside the capture directory, or None.

    Containment is `safe_join`'s job. The extension check stays because it is a
    contract of this endpoint, not a safety measure: the capture directory is
    listed as `*.jsonl` only, and both readers below assume JSON lines.
    """
    d = _capture_dir(request)
    name = request.match_info["name"]
    if not d or not name.endswith(".jsonl"):
        return None
    try:
        p = safe_join(d, name)
    except UnsafePathError:
        return None
    return p if os.path.isfile(p) else None


async def api_capture_read(request: web.Request) -> web.Response:
    """Answer `{"records": [...], "total": n}` for one capture file.

    `records` is the TAIL of the file — the newest `?limit=` lines — while
    `total` counts every line, so the UI can say "showing 100 of 40000". A line
    that is not valid JSON is returned as `{"raw": line}` rather than dropped:
    a malformed record is usually the one being looked for.
    """
    p = _safe_capture_path(request)
    if not p:
        return web.json_response({"error": "not found"}, status=404)
    limit = _limit_param(request, 100, MAX_CAPTURE_LIMIT)
    # Only the tail is kept in memory — a capture file grows unbounded while
    # capture mode is on, and reading all of it just to slice the end of it made
    # the response cost scale with the file, not with the request.
    tail: deque[str] = deque(maxlen=limit)
    total = 0
    try:
        with open(p) as f:
            for line in f:
                total += 1
                tail.append(line)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    out: list[dict[str, Any]] = []
    for line in tail:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"raw": line})
    return web.json_response({"records": out, "total": total})


async def api_capture_download(request: web.Request) -> web.StreamResponse:
    """Serve a capture file as an attachment, for offline analysis."""
    p = _safe_capture_path(request)
    if not p:
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(p, headers={"Content-Disposition": f'attachment; filename="{os.path.basename(p)}"'})


async def api_capture_delete(request: web.Request) -> web.Response:
    """Delete one capture file; answers `{"ok": True, "name": ...}`.

    Capture files grow without bound while capture mode is on and nothing prunes
    them — unlike media and device logs, they have no retention sweep, because
    a capture is something you turn on deliberately and want in full. So the way
    to reclaim the space is to delete a file once you have what you needed from
    it.

    The name goes through `_safe_capture_path`, the same containment check the
    read and download endpoints use, so this cannot reach outside the capture
    directory or touch anything that is not a `.jsonl`. A file the device is
    still appending to can be deleted: the writer holds an open descriptor and
    keeps writing to the now-unlinked inode until it rotates, which loses only
    what has not been written yet.
    """
    p = _safe_capture_path(request)
    if not p:
        return web.json_response({"error": "not found"}, status=404)
    name = os.path.basename(p)
    try:
        await asyncio.to_thread(os.unlink, p)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    log.info("panel: deleted capture file %s", name)
    return web.json_response({"ok": True, "name": name})


def _device_log_root(request: web.Request) -> str:
    """Where uploaded device logs live, or an empty string when unconfigured."""
    return request.app["cfg"].get("device_log_root", "")


def _device_log_reason(request: web.Request) -> str:
    """Why no logs can arrive right now, as a short machine-readable token.

    "Collection is on and nothing appears" is otherwise unanswerable from the
    UI, and there are two silent ways to be in that state. `no_bucket_endpoint`
    is not hypothetical: it has no add-on option and no CLI flag, so an install
    where the Supervisor host-IP lookup failed has none at all.
    """
    if not _device_log_root(request):
        return "no_log_root"
    endpoint = request.app["cfg"].get("bucket_endpoint", "")
    if not endpoint:
        return "no_bucket_endpoint"
    if split_bucket_authority(endpoint) is None:
        return "authority_not_splittable"
    return ""


async def api_device_logs(request: web.Request) -> web.Response:
    """List the uploaded device logs, newest first.

    Answers `{"dir", "reason", "enabled_devices", "files": [...], "total_bytes"}`.
    There is no database table behind this: the file IS the record, and the
    directory our own `pathPrefix` created carries the device id — the same
    arrangement `api_capture_list` uses for captures.
    """
    root = _device_log_root(request)
    reg = request.app["registry"]
    files: list[dict[str, Any]] = []
    if root and os.path.isdir(root):
        for dirpath, _dirs, names in os.walk(root):
            for name in names:
                p = os.path.join(dirpath, name)
                try:
                    st = os.stat(p)
                except OSError:
                    continue  # being written to, or vanished mid-walk
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                files.append({
                    "rel": rel,
                    "name": name,
                    "device": to_int(rel.split("/")[0], None),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
    files.sort(key=lambda f: f["mtime"], reverse=True)
    want = to_int(request.query.get("device"), None)
    if want is not None:
        files = [f for f in files if f["device"] == want]
    return web.json_response({
        "dir": root,
        "reason": _device_log_reason(request),
        "enabled_devices": [d.petkit_id for d in reg.all()
                            if d.config.get("log_upload_enabled", False)],
        "files": files[:MAX_LOG_FILES],
        "total_bytes": sum(f["size"] for f in files),
    })


def _grep(text: str, query: str) -> list[list[Any]]:
    """Filter `text` to `[line_number, line]` pairs matching every term.

    Case-insensitive substring over whitespace-separated terms, ANDed. Never a
    regular expression: the panel is served unauthenticated on the HTTPS port,
    and a caller-supplied pattern over a caller-supplied file is a denial of
    service with no upside for what is a grep over logcat output.

    Line numbers are the FILE's, so a filtered view still says where you are.
    """
    terms = [t.lower() for t in query.split()] if query else []
    out: list[list[Any]] = []
    for n, line in enumerate(text.splitlines(), 1):
        if terms:
            low = line.lower()
            if not all(t in low for t in terms):
                continue
        out.append([n, line[:MAX_LOG_LINE_CHARS]])
    return out


def _read_device_log(path: str, query: str, limit: int, offset: int) -> dict[str, Any]:
    """Read, filter and window one log file. Blocking; call in a thread.

    Decoded with `errors="replace"`: the bytes came from an unauthenticated
    listener, and a log that is 99% readable is worth showing.
    """
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    matched = _grep(text, query)
    window = matched[offset:offset + limit]
    return {
        "lines": window,
        "total": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
        "matched": len(matched),
        "offset": offset,
        "limit": limit,
        "size": os.path.getsize(path),
    }


async def api_device_log_read(request: web.Request) -> web.StreamResponse:
    """One log's contents, filtered and windowed — or the raw file to download.

    Served unmasked, unlike `/api/blocked`: masking a log makes it useless, and
    this is the same exposure `/api/capture/{name}` already accepts on the same
    unauthenticated port. It is not a new trust level, it is an existing one
    extended — which is also why collection is off until switched on.
    """
    root = _device_log_root(request)
    if not root:
        return web.json_response({"error": "not found"}, status=404)
    try:
        path = safe_join(root, request.match_info["path"])
    except UnsafePathError:
        return web.json_response({"error": "not found"}, status=404)
    if not os.path.isfile(path):
        return web.json_response({"error": "not found"}, status=404)

    if request.query.get("download"):
        return web.FileResponse(path, headers={
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f'attachment; filename="{os.path.basename(path)}"',
        })

    limit = _limit_param(request, 500, MAX_LOG_LINES)
    offset = max(0, to_int(request.query.get("offset"), 0) or 0)
    query = request.query.get("q", "")
    # The one panel endpoint whose work scales with a file an unauthenticated
    # listener wrote, so it does not run on the event loop.
    try:
        payload = await asyncio.to_thread(_read_device_log, path, query, limit, offset)
    except OSError as e:
        return web.json_response({"error": str(e)}, status=500)
    return web.json_response(payload)


def asset_version() -> str:
    """Short content hash of the panel's assets, for cache-busting their URLs.

    `Cache-Control: no-cache` is already set on these (see
    `_no_heuristic_caching`) and is correct, but it only binds whoever chooses
    to honour it. Behind Home Assistant Ingress the panel is an iframe on HA's
    own origin, under a service worker and a proxy that are not ours, and in
    practice a deployed `app.js` kept being served from cache there even after
    a hard refresh -- which the same build does not do when the panel is
    reached directly on its own port.

    A changed URL cannot be answered from a cache keyed on the old one, so this
    works regardless of who ignores what. The hash is over content rather than
    mtime: a rebuild that changes nothing keeps the URL, and the browser keeps
    its copy.

    Computed once at import. The files ship inside the image and cannot change
    under a running process, so re-hashing per request would buy nothing.
    """
    h = hashlib.sha256()
    for name in ("app.js", "styles.css"):
        try:
            h.update((STATIC_DIR / name).read_bytes())
        except OSError:
            # Never fail to render the panel over a cache hint. A missing file
            # is a much louder problem two lines later.
            return "dev"
    return h.hexdigest()[:12]


ASSET_VERSION = asset_version()


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
