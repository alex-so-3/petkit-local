"""Proxy mode as a middleware: forward the request, answer with what came back.

Everything that decides whether a device is served the cloud's reply or ours
lives here — the endpoints that are never forwarded, the policy handed to
redaction, and the recording of each exchange onto the panel, the database and
the capture. `http/proxy.py` does the transport; this module does the policy.
"""
from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from petkit_local.ha.learn import learn_settings
from petkit_local.http.dns import loops_back
from petkit_local.http.handlers._common import request_device
from petkit_local.http.handlers.heartbeat import carries_commands
from petkit_local.http.handlers.iot_device_info import self_mqtt_host
from petkit_local.http.middleware import API_PREFIX, PROXY_OUTCOME, Handler
from petkit_local.http.middleware.logging import _short, _text_or_none
from petkit_local.http.proxy import forward, resolve_upstream
from petkit_local.http.redact import RedactionPolicy
from petkit_local.media.crypto import resolve_key_string
from petkit_local.utils.capture import capture_record

log = logging.getLogger(__name__)

#: Endpoints whose answer describes OUR state, not the cloud's. They are still
#: forwarded — the upstream reply is recorded and captured, which is the point of
#: proxy mode — but the device is always served ours.
#:
#: `dev_ble_device` lists the accessories WE have paired (`devices/ble/`); the
#: cloud's list is PetKit's, which for a taken-over device is empty. Serving the
#: cloud's answer is therefore always wrong — it would tell the device to forget
#: every accessory paired here — which is reason enough for this entry.
#:
#: It is NOT here because an empty `list` crashes the parent. The firmware's own
#: log shows:
#:
#:     res data :{"result": {"list": [], "nextTick": 3600}}
#:     [ble_relay_network.c]:[95][pk_schmg_parse_ble_dev_list]relay list prase, update:0
#:     E/ctrl [ble_relay_network.c]:[108][pk_schmg_parse_ble_dev_list]ERR:...parse item NULL
#:
#: which reads like an empty list walking into a null dereference that aborts the
#: boot chain — but PetKit's own cloud answers exactly that payload to a device
#: with no accessories, 234 times in one captured session, so the ERR line is a
#: logged parse error rather than an aborted boot. Whether WE send the empty
#: array is a separate and still unsettled question, argued out in
#: `handlers/ble_device.py`; this entry stands on the paragraph above either way.
#:
#: This set is for answers that would BREAK the device, not for answers that
#: are merely inconvenient to us. `dev_discern_pic` is deliberately absent: the
#: cloud's pets and face photos reaching the device is what proxy mode MEANS.
#: Turn proxy off to have our own pets take effect.
LOCAL_ONLY_ENDPOINTS = frozenset({"dev_ble_device"})

#: Endpoints whose REQUEST is the leak, withheld while the log-upload guard is
#: on and forwarded normally when it is off.
#:
#: `dev_upload_file_info_v2` is how the device says what it just uploaded:
#: `fileId`, `moduleType`, the AES IV, the `eventId` and the
#: pet/clean/toilet flags. Forwarded, that is a running account of what happened
#: in somebody's home — every visit, every recording, timestamped — sent to
#: PetKit by a device its owner has taken off PetKit. The media itself never
#: reaches them — it is PUT to our bucket — which makes this metadata the whole
#: of what they would learn, and it is enough.
#:
#: Redaction cannot help: it rewrites response bodies, and by the time there is
#: a body to rewrite the request has been delivered. So this is a request-side
#: gate, exactly like `_reports_a_local_log_upload` below and for the same
#: reason. It is NOT in `LOCAL_ONLY_ENDPOINTS`, because that would put it out of
#: proxy mode's reach permanently; switching the guard off proxies it again.
GUARDED_LOCAL_ENDPOINTS = frozenset({"dev_upload_file_info_v2"})

#: Request headers echoed into the capture record — the ones `http/proxy.py`
#: forwards, so a capture shows exactly what upstream was told.
_FORWARDED_HEADERS = ("X-Device", "X-Session", "F-Session", "User-Agent", "Content-Type")

#: Seconds the HA publish after a learned `dev_device_info` may hold up the
#: device's reply. The value is stored either way; a late publish only waits
#: for the next state publish.
_LEARN_PUBLISH_TIMEOUT = 2.0


def _reports_a_local_log_upload(request: web.Request, config: dict) -> bool:
    """Whether this is a `dev_upload_log` naming an object in OUR bucket.

    Keyed on where the object actually is, not on the endpoint: a device still
    talking to PetKit's OSS reports a petkit.com URL here, and that exchange is
    ordinary proxied traffic worth forwarding and recording.
    """
    if request.path.rstrip("/").rsplit("/", 1)[-1] != "dev_upload_log":
        return False
    bucket = (config.get("bucket_endpoint") or "").rstrip("/")
    key = request.query.get("key", "")
    return bool(bucket) and key.startswith(bucket)


def _is_heartbeat(path: str) -> bool:
    """Whether this path is one of the three heartbeat routes.

    Named rather than inlined because the heartbeat is the ONE endpoint whose
    local answer may not be thrown away: `handle_heartbeat` drains the device's
    command queue to build it (`devices/base.py::pop_commands` is destructive
    and at-most-once), so the two replies get merged instead of replaced.
    """
    return path.endswith("/heartbeat")


def _endpoint_selected(request: web.Request, config: dict) -> bool:
    """Whether `proxy_only` says to forward this endpoint.

    Empty (the normal case) forwards everything. A non-empty list is the bisect
    tool: hardware has twice shown that a reply which is entirely valid — just
    not the one we usually send — can put the firmware into a boot loop, and
    narrowing that to a single endpoint is otherwise guesswork on a live device.

    Matched on the last path segment, so `dev_device_info` covers both
    `/6/t5/dev_device_info` and any version-less spelling.
    """
    only = (config.get("proxy_only") or "").strip()
    if not only:
        return True
    wanted = {name.strip() for name in only.split(",") if name.strip()}
    return request.path.rstrip("/").rsplit("/", 1)[-1] in wanted


def _build_policy(request: web.Request, device):
    """The redaction policy for one request, from the live config and device."""
    config = request.app["config"]
    return RedactionPolicy(
        device=device,
        api_url=config.get("api_url", ""),
        mqtt_host=self_mqtt_host(config),
        bucket_endpoint=config.get("bucket_endpoint", ""),
        aes_key=resolve_key_string(config),
        block_rce=config.get("proxy_block_run_cmd", True),
        block_ota=config.get("proxy_block_ota", True),
        block_log_upload=config.get("proxy_block_log_upload", True),
        media_to_real_oss=config.get("proxy_media_real_oss", False),
        local_cvr_window=config.get("proxy_local_cvr_window", False),
    )


@web.middleware
async def proxy_middleware(request: web.Request, handler: Handler) -> web.StreamResponse:
    """Answer with the real cloud's reply, redacted, when proxy mode is on.

    Off, this is a pure pass-through with no measurable cost — the config flag is
    read from the same dict the panel mutates, so the mode flips live.

    On, the local handler still runs FIRST and in full. Its response is usually
    discarded, but its side effects are not: that is what keeps the event store,
    the HA entities and the media pipeline populated while a device is being
    observed against PetKit. Five things then decide what the device receives:

    1. **A heartbeat carrying a queued command is answered immediately, without
       forwarding at all.** Building that answer already drained the queue
       (`devices/base.py::pop_commands` is destructive and at-most-once), so any
       await between the pop and the send is a window in which the command can
       be lost — to a cancelled request, a slow upstream, or an exception. There
       is no way to put it back. The next heartbeat is ~15s away and almost
       always idle, so the observation this costs is nil.
    2. An unidentified request is never forwarded. Redaction substitutes OUR
       credentials and addresses into the reply, and without a registered device
       there is nothing to substitute — forwarding raw would be the one way this
       middleware could hand a device someone else's cloud.
    3. `forward` returning None (unreachable, too slow, breaker open) falls back
       to the local response. A device must never pay for PetKit being down.
    4. An upstream reply the device could not act on falls back to the local
       response too — a non-2xx, OR a 200 carrying PetKit's refusal envelope
       (`{"error": {"code": 704}}`, which is what a taken-over device gets on
       every session-bearing endpoint, since the session it presents is one WE
       issued). Relaying either breaks the never-404 rule from the far side:
       observed on real hardware, a `dev_serverinfo` with no server list puts the
       device into a boot loop every ~2.4s. The status, the error and the body
       are all still recorded — observing the refusal is the point, showing it to
       the device is not.
    5. Anything else is answered with the redacted upstream reply. A heartbeat
       that got this far was idle, so merging is a no-op on our side and simply
       lets the cloud's commands through.

    Everything after the local handler is wrapped: a failure in forwarding,
    redaction or recording answers with the local response rather than letting a
    500 reach a device that is waiting for one.
    """
    config = request.app["config"]
    if not config.get("proxy_mode") or not request.path.startswith(API_PREFIX):
        return await handler(request)

    # Read before the handler so we own the bytes whatever it does with them.
    # aiohttp caches the payload, so the handler's own `request.read()` still
    # works. A future handler using `request.multipart()` — a one-shot stream —
    # would need this revisited.
    try:
        body = await request.read()
    except Exception:
        body = b""

    local = await handler(request)

    if _is_heartbeat(request.path):
        if carries_commands(local):
            log.debug("Not forwarding %s: it is delivering a queued command",
                      request.path)
            return local

    if _reports_a_local_log_upload(request, config):
        # `dev_upload_log` reports the object URL as a QUERY parameter, and once
        # the device uploads to us that URL is this add-on's own LAN address and
        # bucket layout. Redaction only sanitises response bodies, so forwarding
        # this would hand PetKit exactly what the log-upload guard exists to
        # withhold — where the device's logs are going now — while the guard was
        # busy scrubbing the reply. Not forwarded at all rather than rewritten:
        # there is nothing upstream can usefully say about an object it cannot
        # see, and a doctored `key` would be a lie rather than a redaction.
        log.debug("Not forwarding %s: it reports an upload to our own bucket",
                  request.path)
        return local

    if (config.get("proxy_block_log_upload", True)
            and request.path.rstrip("/").rsplit("/", 1)[-1] in GUARDED_LOCAL_ENDPOINTS):
        log.debug("Not forwarding %s: the log-upload guard withholds it",
                  request.path)
        return local

    device = request_device(request)
    if device is None:
        return local

    if not _endpoint_selected(request, config):
        return local

    try:
        upstream = resolve_upstream(config.get("proxy_upstream", ""))
        dns_server = config.get("proxy_dns", "")

        # A LAN that points PetKit's names here points them here for US too, and
        # forwarding into ourselves does not fail — our own handler answers, and
        # the reply is then recorded as the cloud's. Checked before the request
        # rather than detected after, because there is nothing in the answer to
        # detect it by. See `http/dns.py`.
        looped = await loops_back(upstream, _local_socket(request), dns_server)
        if looped:
            log.warning(
                "PROXY: not forwarding %s — %s resolves to %s, which is this add-on. "
                "Your DNS redirects it here. Set an upstream DNS server in Setup to "
                "reach the real server.", request.path, upstream, looped)
            hub = request.app.get("event_hub")
            if hub is not None:
                hub.record_upstream("dns_loop")
            return local

        exchange = await forward(request, body=body, upstream=upstream,
                                 policy=_build_policy(request, device),
                                 dns_server=dns_server)
        if exchange is None:
            return local

        await _record_exchange(request, device, exchange, body=body, local=local)
        _note_outcome(request, exchange)

        if request.path.rstrip("/").rsplit("/", 1)[-1] in LOCAL_ONLY_ENDPOINTS:
            return local

        if not exchange.usable:
            log.info("PROXY: upstream gave nothing usable for %s (status %d%s), "
                     "serving locally", request.path, exchange.status,
                     f", error {exchange.error.get('code')}" if exchange.error else "")
            return local

        if request.path.rstrip("/").rsplit("/", 1)[-1] == "dev_device_info":
            await _learn_from_device_info(request, device, exchange)

        return exchange.to_response()
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("PROXY: failed on %s, serving the local answer", request.path)
        return local


def _local_socket(request: web.Request) -> tuple[str, int] | None:
    """The `(address, port)` of ours this device's connection arrived on.

    Better than enumerating our own interfaces: in a container most of those are
    not what a device can reach, and host networking, bridged networking and
    Ingress each give a different answer. This one is not a guess.
    """
    transport = request.transport
    sockname = transport.get_extra_info("sockname") if transport is not None else None
    if not sockname or len(sockname) < 2:
        return None
    return (sockname[0], sockname[1])


def _note_outcome(request: web.Request, exchange) -> None:
    """Make one proxied call visible, without adding a log line of its own.

    The panel's Log tab already shows every device request with an expandable
    detail; proxy mode belongs IN that detail rather than beside it. Without
    this, a steady-state proxied session looks exactly like an unproxied one —
    the device polls only the heartbeat, PetKit refuses it, nothing is redacted,
    and so nothing anywhere says the request went to the cloud at all.
    """
    error = exchange.error or {}
    outcome = ("ok" if exchange.usable
               else f"error_{error.get('code')}" if error
               else f"http_{exchange.status}")

    request[PROXY_OUTCOME] = {
        "upstream": exchange.url,
        "status": exchange.status,
        "error": error or None,
        "outcome": outcome,
        "served": "upstream" if exchange.usable else "local",
        "redactions": [r.rule for r in exchange.records],
        "upstream_body": _short(_text_or_none(exchange.upstream_body)),
    }

    hub = request.app.get("event_hub")
    if hub is not None:
        hub.record_upstream(outcome)


async def _record_exchange(request: web.Request, device, exchange, *,
                           body: bytes, local: web.StreamResponse) -> None:
    """Report one proxied exchange to the panel, the database and the capture.

    Every failure here is swallowed: this is observability running on the
    device-facing request path, and a full disk or a closed store must cost a
    log line, not the answer the device is waiting for.
    """
    try:
        hub = request.app.get("event_hub")
        store = request.app.get("event_store")

        if hub is not None:
            for record in exchange.records:
                hub.record_redaction(
                    device.petkit_id, record.rule,
                    f"{record.rule} on {request.path}",
                    detail={"rule": record.rule, "path": record.path,
                            "endpoint": request.path, "upstream": exchange.url,
                            "original": record.original, "note": record.note},
                    blocked=record.blocking,
                )

        if store is not None and exchange.blocked:
            await store.add_blocked_attempts([{
                "device_id": device.petkit_id,
                "kind": record.rule,
                "transport": "http",
                "endpoint": request.path,
                "upstream": exchange.url,
                "field_path": record.path,
                "payload_json": record.original,
                "detail_json": {"note": record.note, "status": exchange.status},
            } for record in exchange.blocked])

        _capture_exchange(request, exchange, body=body, local=local)
        _remember_upstream_credentials(request, device, exchange)
    except Exception:
        log.exception("PROXY: could not record the exchange for %s", request.path)


def _capture_exchange(request: web.Request, exchange, *,
                      body: bytes, local: web.StreamResponse) -> None:
    """Append the full exchange to the proxy capture stream, if enabled.

    Deliberately gated on capture AND proxy both being on, and written to files
    of its own: a proxied session is a different kind of artifact from the
    ordinary `requests.jsonl`, and unlike that one it carries full bodies —
    which is the entire reason to turn it on.
    """
    config = request.app["config"]
    if not config.get("capture"):
        return

    capture_dir = config.get("capture_dir", "/data/capture")

    capture_record(capture_dir, "proxy_http", {
        "method": request.method,
        "path": request.path,
        "query": dict(request.query),
        "headers": {h: request.headers[h] for h in _FORWARDED_HEADERS
                    if h in request.headers},
        "req_body": _text_or_none(body),
        "upstream_url": exchange.url,
        "upstream_status": exchange.status,
        "upstream_body": _text_or_none(exchange.upstream_body),
        "sent_body": _text_or_none(exchange.body),
        "local_status": local.status,
        "local_body": _text_or_none(getattr(local, "body", None)),
        "redactions": [r.rule for r in exchange.records],
    })

    for record in exchange.records:
        capture_record(capture_dir, "proxy_redactions", {
            "path": request.path,
            "upstream": exchange.url,
            "rule": record.rule,
            "field_path": record.path,
            "original": record.original,
            "replacement": record.replacement,
            "note": record.note,
        })


def _remember_upstream_credentials(request: web.Request, device, exchange) -> None:
    """Persist the real credentials a proxied reply just revealed.

    Two different ones, learned from two different endpoints:

    * The **API secret** from `dev_signup`, which the device signs its requests
      with. Adopting it is what stops PetKit answering 704 to everything — see
      `Device.signing_secret`. Stored on the device itself, because every local
      `dev_signup` from now on has to hand out the same value or the device
      reverts to a secret the cloud rejects.
    * The **Aliyun MQTT credentials** from `dev_iot_device_info`, for
      `mqtt/upstream.py`. Kept in their own file, not on the device.
    """
    api_secret = exchange.captured.get("api_secret")
    if api_secret and api_secret != device.api_secret:
        device.api_secret = api_secret
        registry = request.app.get("registry")
        if registry is not None:
            registry.mark_dirty()
        log.info("Adopted the real PetKit API secret for device %d — its requests "
                 "will now verify upstream", device.petkit_id)

    creds = exchange.captured.get("mqtt")
    store = request.app.get("proxy_upstream_creds")
    if not creds or store is None:
        return
    # All four or none. A partial capture would have `UpstreamMQTT` dialling an
    # empty host with an empty client id every 10s forever, with nothing but a
    # warning to say why — worse than simply not having the credentials.
    if not all(creds.get(k) for k in ("mqtt_host", "product_key",
                                      "device_name", "device_secret")):
        log.debug("Incomplete upstream MQTT credentials from %s, ignoring", request.path)
        return
    store.put(device.petkit_id, creds)


async def _learn_from_device_info(request: web.Request, device, exchange) -> None:
    """Record the settings a proxied `dev_device_info` reply is serving.

    That reply's `settings` block is the device's whole configuration as
    PetKit's account holds it, and for a device that reports no settings of
    its own (a W7H) it is the one place they can be read (`ha/learn.py`). Read
    from the REDACTED body, the one the device is about to be given, and only
    for a reply it is actually given (`exchange.usable`).

    A SNAPSHOT, so it only fills fields nothing has stored yet
    (`learn_settings(only_missing=True)`): in proxy mode a write from Home
    Assistant reaches the device but not PetKit's account, and the cloud's
    older value must not revert it.

    Observation only: the response is built from `exchange` either way, and a
    failure here costs a log line, never the answer. It runs before the reply
    is returned, so the HA publish is bounded (`_LEARN_PUBLISH_TIMEOUT`).
    """
    try:
        reply = json.loads(exchange.body)
        result = reply.get("result") if isinstance(reply, dict) else None
        settings = result.get("settings") if isinstance(result, dict) else None
        learned = learn_settings(device, settings,
                                 source="the cloud's dev_device_info (proxy)",
                                 only_missing=True)
        if not learned:
            return
        registry = request.app.get("registry")
        if registry is not None:
            registry.mark_dirty()
        ha_publisher = request.app.get("ha_publisher")
        if ha_publisher is not None:
            await asyncio.wait_for(ha_publisher.publish_state(device),
                                   _LEARN_PUBLISH_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("PROXY: could not learn settings from %s", request.path)
