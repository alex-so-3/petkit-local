"""What more than one of the panel's API modules needs.

Two kinds of thing live here. The first is the reading end of the `app[...]`
contract (`web/appkeys.py`) plus the request parsers every handler starts with:
the `{id}` segment, the JSON body, `?limit=`, and the JSON refusal they raise.
The second is the handful of helpers two endpoints must not answer differently
— `_deliver` (the MQTT-or-heartbeat fallback every panel write takes) and the
pet lookups the Timeline shares with the Pets tab.

A helper only one module uses belongs in that module, not here.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from aiohttp import web

from petkit_local.devices.base import Device, split_bucket_authority
from petkit_local.ha.commands import RUN_CMD_SUFFIX
from petkit_local.http.proxy import resolve_upstream
from petkit_local.patchers.common import send_run_cmd
from petkit_local.utils.coerce import to_int

log = logging.getLogger(__name__)


# Upper bounds for caller-supplied `?limit=`. The panel port carries no
# authentication of its own — Ingress supplies it, and a direct mapping of 8099
# has none — so an unbounded limit is a free memory amplifier for anyone who
# can reach it. Both caps sit above anything the UI asks for, so no real
# request is affected:
# the event ring holds 800 entries (web/hub.py) and the capture reader pages in
# hundreds of lines.
MAX_EVENT_LIMIT = 1000
MAX_CAPTURE_LIMIT = 5000


def _live(request: web.Request) -> dict[str, Any]:
    """The shared device-facing config dict, or an empty stand-in for it."""
    return request.app.get("live_config") or {}


def _refuse(status: type[web.HTTPException], error: str) -> web.HTTPException:
    """A JSON `{"error": ...}` refusal, shaped exactly like a returned one.

    Raised rather than returned so the checks below read as one line at the top
    of a handler. There is no middleware on this app to swallow it.
    """
    return status(text=json.dumps({"error": error}),
                  content_type="application/json")


def _path_id(request: web.Request) -> int:
    """The `{id}` path segment as an integer.

    Raises:
        web.HTTPBadRequest: the segment is not a number.
    """
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise _refuse(web.HTTPBadRequest, "bad id") from None


def _device_or_404(request: web.Request) -> Device:
    """The device the `{id}` path segment names.

    Raises:
        web.HTTPBadRequest: the id is not a number.
        web.HTTPNotFound: no device has that id.
    """
    device = request.app["registry"].get(_path_id(request))
    if not device:
        raise _refuse(web.HTTPNotFound, "not found")
    return device


async def _json_body(request: web.Request) -> dict[str, Any]:
    """The request body as a JSON object.

    A body that is JSON but not an object is refused here too: every caller
    reads it with `.get`, so a list would reach that call as an AttributeError
    and answer 500 to what is a malformed request.

    Raises:
        web.HTTPBadRequest: the body is not a JSON object.
    """
    try:
        body = await request.json()
    except Exception:
        raise _refuse(web.HTTPBadRequest, "bad json") from None
    if not isinstance(body, dict):
        raise _refuse(web.HTTPBadRequest, "bad json")
    return body


async def _deliver(hub, bridge, d, suffix: str, envelope: Any,
                   transport: str = "auto") -> web.Response:
    """Send one already-built envelope, by whichever transport is live.

    Shared by every panel write rather than copied per endpoint: the fallback
    from MQTT to the heartbeat queue is the part that must not diverge. A failed
    publish is not an error to the caller — the device picks the command up on
    its next poll either way — so `delivered` reports what actually happened.

    A `@run_cmd` suffix is not a thing/service publish: it is a shell line for
    the device's run_cmd path (the T6 pan buttons), delivered by `send_run_cmd`,
    which picks MQTT-or-heartbeat itself and returns which it used.
    """
    if suffix == RUN_CMD_SUFFIX:
        command = envelope["command"]
        used = await send_run_cmd(d, command, bridge)
        hub.record_command(d.petkit_id, used, f"run_cmd {command[:80]}")
        return web.json_response({"ok": True, "delivered": used, "suffix": suffix,
                                  "envelope": envelope})

    if transport == "auto":
        mqtt_live = d.mqtt_connected and bridge is not None and getattr(bridge, "_client", None)
        transport = "mqtt" if mqtt_live else "heartbeat"

    if transport == "mqtt" and bridge is not None and getattr(bridge, "_client", None):
        try:
            await bridge.publish_to_device(d, suffix, envelope)
            delivered = "mqtt"
        except Exception as e:
            log.warning("panel: MQTT publish failed for device %d, queuing for heartbeat: %s",
                        d.petkit_id, e)
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
    hub.record_command(d.petkit_id, delivered, f"{suffix} {json.dumps(params)[:80]}")
    clean = {k: v for k, v in envelope.items() if k != "_service_suffix"} if isinstance(envelope, dict) else envelope
    return web.json_response({"ok": True, "delivered": delivered, "suffix": suffix, "envelope": clean})


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


def _cloud_upstream(request: web.Request) -> str:
    """Where to ask PetKit: the same `proxy_upstream` the device traffic uses.

    Read from `live_config` — the dict the device-facing handlers share — so
    changing the upstream in Setup moves this too, and there is never a second
    place that has to be kept in step.
    """
    return resolve_upstream(_live(request).get("proxy_upstream", ""))


def _limit_param(request: web.Request, default: int, maximum: int) -> int:
    """Read `?limit=`, ignoring junk and clamping to a serveable range."""
    return max(1, min(to_int(request.query.get("limit"), default), maximum))


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
