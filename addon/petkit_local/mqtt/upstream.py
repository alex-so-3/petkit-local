"""Proxy mode's MQTT half: a second client, connected to the real Aliyun broker.

The HTTP side of proxy mode is a middleware that forwards a request and hands
back the cloud's answer. MQTT has no request to forward — it is two long-lived
sessions — so the equivalent is a bridge: the device stays connected to OUR
broker, and everything it says is relayed upward while everything the cloud says
comes back down through the same redaction rules.

Three things make this harder than it looks, and each shapes the code below.

**The credentials the device uses are ours.** `http/handlers/iot_device_info.py`
mints the productKey/deviceName/deviceSecret triple itself, so Aliyun has never
heard of them (`mqtt/auth.py` says as much in its header). The real ones can
only be learned by PROXYING `dev_iot_device_info` and reading them out of the
upstream reply before redaction replaces them — which is what
`UpstreamCredentials` stores and what makes this module depend on proxy mode
being on for HTTP too.

**Topics carry identity.** Ours name our pk/dn, Aliyun's name the real ones, so
every relayed frame is re-addressed (`mqtt/topics.py::rewrite_topic`).

**The bridge hears its own echo.** `MQTTBridge` holds one `#` subscription, so
it sees our own downward publishes come back. The upward path is therefore an
explicit ALLOW-LIST of what a device originates; a deny-list would relay our own
relayed frames straight back up and loop forever. That allow-list filters by
topic SHAPE, so it cannot tell a device's own event from one we just relayed
down onto the same topic — which is why the DOWNWARD path refuses everything
outside the server's own direction (`_on_upstream`, and #20 for the storm that
came of not doing it).

Nothing here may affect the local broker. Every failure path — no credentials, a
refused connection, a dropped one, a frame that will not parse — leaves the
device talking to us exactly as it would with proxy mode off.

**Partly verified against hardware.** The real endpoint has now been reached:
`dev_only_iot_device_info_v2` proxied upstream yields a real instance host
(`{iotInstanceId}.mqtt.iothub.aliyuncs.com`, matching Alibaba's documented shape
for an Enterprise instance), and its TLS listener answers on 8883 — see
`build_tls_context` for what that handshake shows and why it is not verified.
Everything past the handshake is still untested: the port is an inference the
reply does not state, `securemode=2` with it, and `compute_aliyun_sign` can only
be tested against itself — a synthetic-signature tautology, the same limitation
the repo already grades for the rest of the MQTT side. A rejected CONNECT would
look exactly like the retry loop this module already logs.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
from typing import TYPE_CHECKING, Any, Callable

from petkit_local.ha.learn import learn_settings
from petkit_local.http.redact import redact_mqtt
from petkit_local.mqtt.auth import compute_aliyun_sign
from petkit_local.mqtt.topics import (
    is_server_published, ota_upgrade_topic, parse_topic, rewrite_topic,
)
from petkit_local.utils.capture import capture_record
from petkit_local.utils.jsonio import atomic_write_json, read_json

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from petkit_local.devices.base import Device
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.events.store import EventStore
    from petkit_local.ha.publisher import HAPublisher
    from petkit_local.http.redact import RedactionPolicy
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)

CREDENTIALS_FILENAME = "proxy_upstream.json"

#: Aliyun IoT's TLS listener. `securemode=2` in the client id goes with it;
#: plaintext 1883 would be `securemode=3`.
ALIYUN_TLS_PORT = 8883


def build_tls_context() -> ssl.SSLContext:
    """The TLS context for the upstream link — encrypted, NOT authenticated.

    Aliyun IoT's MQTT endpoint does not chain to any public CA. Checked against
    the live `iot-600a5gmp.mqtt.iothub.aliyuncs.com`:

        0 s:O=Aliyun IoT, OU=IoT Platform, CN=*.iot-as.aliyuncs.com
        1 s:C=CN, ..., O=Aliyun IoT, OU=IoT Platform, CN=Aliyun
        Verify return code: 20 (unable to get local issuer certificate)

    That intermediate is issued by `CN=Aliyun IoT Root CA`, a private root: in
    no public trust store, never sent, and with no AIA extension to fetch it
    from. A default context — what `aiomqtt.TLSParameters()` asks for — therefore
    fails EVERY connection with CERTIFICATE_VERIFY_FAILED and the bridge retries
    forever without exchanging a frame. The device cannot verify it either
    (`/app/bin/ctrl` embeds only GlobalSign Root CA, for the HTTPS API, and the
    whole T5 rootfs holds no copy of the Aliyun root), so this matches the
    posture of the firmware being impersonated rather than relaxing something it
    enforces.

    The cost is real and accepted: anything on the path can pose as the cloud
    and have its `thing/service/*` commands relayed down to the box. Nothing
    else here rests on server identity — the OTA block (`_block_ota`), the
    content-keyed redaction (`http/redact/`) and the rule that only frames a
    device originates go up (`_is_from_device`) all hold regardless.

    `check_hostname` is cleared BEFORE `verify_mode`: the reverse order raises
    ValueError, since hostname checking cannot be left on without verification.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx

RECONNECT_DELAY_SECONDS = 10.0
#: How often the supervisor reconciles running connections with the live config.
#: Proxy mode is a panel toggle, so this is the latency of flipping it.
SUPERVISE_INTERVAL_SECONDS = 5.0

#: What a device ORIGINATES. Only these are relayed upward — see the module
#: docstring on the echo loop. `post_reply`, `thing/service/*` and `user/get`
#: are things WE publish, and `$SYS/#` is the broker talking about itself.
_UPSTREAM_ALLOWED = (("event", None), ("ota", "inform"))


class UpstreamCredentials:
    """The real Aliyun identity of each device, learned from a proxied reply.

    Persisted so a restart does not have to wait for the device to ask for its
    MQTT credentials again (it does so at boot, and only at boot).

    Holds real device secrets, so the file lives in `{data_dir}` beside
    `devices.json` and is never served by the panel or the device-facing app.
    """

    def __init__(self, path: str | Path) -> None:
        """Load whatever was learned in previous runs; a missing file is fine."""
        self._path = path
        data = read_json(path, {})
        self._data: dict[str, dict] = data if isinstance(data, dict) else {}

    def get(self, petkit_id: int) -> dict | None:
        """The real credentials for one device, or None if never seen."""
        return self._data.get(str(petkit_id))

    def all(self) -> dict[str, dict]:
        """Every device we have credentials for, keyed by our petkit id."""
        return dict(self._data)

    def put(self, petkit_id: int, creds: dict) -> None:
        """Record one device's real credentials, if they actually changed.

        Written through immediately rather than debounced: this fires at most
        once per device boot, and losing it costs a whole session of upstream
        MQTT.
        """
        key = str(petkit_id)
        merged = {**creds, "captured_at": time.time()}
        existing = self._data.get(key)
        if existing and all(existing.get(k) == v for k, v in creds.items()):
            return
        self._data[key] = merged
        try:
            atomic_write_json(self._path, self._data)
        except OSError as e:
            log.warning("Could not persist upstream credentials: %s", e)
        log.info("Learned real Aliyun credentials for device %d (host=%s)",
                 petkit_id, creds.get("mqtt_host"))


def build_client_id(product_key: str, device_name: str, timestamp: str) -> str:
    """The pipe-delimited client id Aliyun expects on CONNECT.

    Deliberately the exact shape `mqtt/auth.py::CLIENT_ID_PATTERN` parses, so
    the module that validates a device's id and the module that builds ours
    cannot drift apart.
    """
    params = (f"timestamp={timestamp},_ss=1,_v=sdk-python-1.0,securemode=2,"
              f"signmethod=hmacsha256,ext=3,")
    return f"{product_key}.{device_name}|{params}|"


def build_credentials(creds: dict, timestamp: str | None = None) -> dict[str, str]:
    """Turn stored credentials into the CONNECT triple Aliyun wants.

    Reuses `compute_aliyun_sign` to BUILD the password that `mqtt/auth.py`
    normally uses it to VERIFY — same function, opposite direction, so the two
    can never disagree about what the signature covers (the BARE `{pk}.{dn}`,
    not the pipe-delimited id actually sent).
    """
    pk = creds["product_key"]
    dn = creds["device_name"]
    ts = timestamp or str(int(time.time() * 1000))
    return {
        "client_id": build_client_id(pk, dn, ts),
        "username": f"{dn}&{pk}",
        "password": compute_aliyun_sign(creds["device_secret"], f"{pk}.{dn}", dn, pk, ts),
    }


def _is_from_device(topic: str) -> bool:
    """Whether a local frame is something the DEVICE originated.

    The allow-list that prevents the echo loop. Anything we publish ourselves —
    an event acknowledgement, a service command, a `user/get` reply — matches
    nothing here and stops at our broker.
    """
    parsed = parse_topic(topic)
    if parsed is None:
        return False
    return any(parsed.category == cat and (detail is None or parsed.detail == detail)
               for cat, detail in _UPSTREAM_ALLOWED)


class UpstreamMQTT:
    """Bridges each device's MQTT session to the real Aliyun broker.

    One upstream connection per device, because Aliyun credentials are
    per-device. Started and stopped by `MQTTBridge`, which owns it: that is
    where every frame already arrives decoded, topic-parsed and attributed to a
    device, and where a live config toggle can start or stop it without touching
    the broker's fixed plugin set.
    """

    def __init__(self, registry: DeviceRegistry, credentials: UpstreamCredentials,
                 policy_factory: Callable[[Device], RedactionPolicy],
                 publish_local: Callable[[str, bytes], Any],
                 hub: EventHub | None = None,
                 event_store: EventStore | None = None,
                 live_config: dict[str, Any] | None = None,
                 ha_publisher: HAPublisher | None = None) -> None:
        """Wire the collaborators; nothing connects until `start()`.

        Args:
            ha_publisher: Told when a relayed `property.set` taught us a
                setting (`_learn_from_downlink`). None (`--no-ha`) still learns;
                it only skips the publish.
            policy_factory: Builds the redaction policy for one device. A
                callable rather than a value because the policy reads live
                settings that the panel can change mid-session.
            publish_local: Publishes a (topic, payload) onto OUR broker — the
                bridge's own client, passed in so this module never opens a
                second connection to it.
        """
        self._registry = registry
        self._credentials = credentials
        self._policy_factory = policy_factory
        self._publish_local = publish_local
        self._hub = hub
        self._event_store = event_store
        self._ha_publisher = ha_publisher
        self._live_config = live_config if live_config is not None else {}
        self._tasks: dict[int, asyncio.Task] = {}
        # The live client per device, present only while that connection is up.
        # `forward_up` publishes through it and treats a missing one as "not
        # connected", which is the normal state whenever proxy mode is off.
        self._clients: dict[int, Any] = {}

    @property
    def running(self) -> bool:
        """Whether any upstream connection is currently being maintained."""
        return bool(self._tasks)

    def wanted(self) -> bool:
        """Whether the live config asks for the bridge at all."""
        return bool(self._live_config.get("proxy_mode")
                    and self._live_config.get("proxy_mqtt_bridge", True))

    async def supervise(self) -> None:
        """Reconcile connections with the live config, forever.

        Polls rather than subscribing to a change because the panel's only
        contract is that it mutates the shared config dict — no callback, no
        event. `SUPERVISE_INTERVAL_SECONDS` is therefore the latency of the
        toggle, which is a fine price for not coupling the two.
        """
        while True:
            try:
                await self.reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Upstream MQTT supervisor pass failed")
            await asyncio.sleep(SUPERVISE_INTERVAL_SECONDS)

    async def reconcile(self) -> None:
        """Start connections the config wants and stop the ones it does not."""
        if not self.wanted():
            await self.stop()
            return

        # Reap finished tasks first. `_run` RETURNS (rather than looping) when
        # aiomqtt is missing or the device has momentarily gone from the
        # registry, and a done task left in the dict would make every later pass
        # skip that device — its bridge dead until proxy mode is toggled off and
        # on again.
        for petkit_id, task in list(self._tasks.items()):
            if task.done():
                del self._tasks[petkit_id]

        for key in self._credentials.all():
            petkit_id = int(key)
            if petkit_id in self._tasks:
                continue
            if self._registry.get(petkit_id) is None:
                continue
            self._tasks[petkit_id] = asyncio.create_task(
                self._run(petkit_id), name=f"upstream-mqtt-{petkit_id}")

    async def start(self) -> None:
        """Bring up whatever the config currently asks for. Idempotent."""
        await self.reconcile()

    async def stop(self) -> None:
        """Drop every upstream connection. Idempotent, and safe to call twice."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            # CancelledError is a BaseException, so it needs naming separately
            # from the catch-all; both are expected on the way out.
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                log.exception("Upstream MQTT task failed during shutdown")

    async def _run(self, petkit_id: int) -> None:
        """Hold one device's upstream connection open, reconnecting forever.

        Mirrors `MQTTBridge.start`'s loop, and for the same reason: a dropped
        cloud connection is normal, and must cost a retry rather than a task.
        """
        try:
            import aiomqtt  # noqa: PLC0415 - optional dependency, probed at use
        except ImportError:
            log.warning("aiomqtt not installed - upstream MQTT bridge disabled")
            return

        while True:
            creds = self._credentials.get(petkit_id)
            device = self._registry.get(petkit_id)
            if not creds or device is None:
                return
            try:
                conn = build_credentials(creds)
                async with aiomqtt.Client(
                    hostname=creds["mqtt_host"],
                    port=ALIYUN_TLS_PORT,
                    identifier=conn["client_id"],
                    username=conn["username"],
                    password=conn["password"],
                    tls_context=build_tls_context(),
                ) as client:
                    self._clients_set(petkit_id, client)
                    log.info("Upstream MQTT connected for device %d (%s)",
                             petkit_id, creds["mqtt_host"])
                    for topic in self._downstream_topics(creds):
                        await client.subscribe(topic)
                    async for message in client.messages:
                        try:
                            await self._on_upstream(device, creds, message)
                        except asyncio.CancelledError:
                            raise
                        except aiomqtt.MqttError:
                            raise
                        except Exception:
                            log.exception("Dropped upstream frame on %s", message.topic)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.warning("Upstream MQTT for device %d lost (%s), retrying in %ds",
                            petkit_id, e, RECONNECT_DELAY_SECONDS)
            finally:
                self._clients_set(petkit_id, None)
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    def _clients_set(self, petkit_id: int, client: Any) -> None:
        """Track the live client so `forward_up` can publish through it."""
        if client is None:
            self._clients.pop(petkit_id, None)
        else:
            self._clients[petkit_id] = client

    @staticmethod
    def _downstream_topics(creds: dict) -> list[str]:
        """What the cloud may send us for one device.

        `/ota/device/upgrade/…` is subscribed deliberately: it is the one topic
        we never relay, and subscribing is how an attempt gets seen and recorded
        instead of quietly never arriving.
        """
        pk, dn = creds["product_key"], creds["device_name"]
        return [
            f"/sys/{pk}/{dn}/thing/service/#",
            f"/sys/{pk}/{dn}/thing/event/+/post_reply",
            f"/{pk}/{dn}/user/get",
            ota_upgrade_topic(pk, dn),
            # RRPC — Aliyun's synchronous request/response, which arrives on a
            # topic of an entirely different shape:
            # `/ext/rrpc/{messageId}/sys/{pk}/{dn}/thing/service/{name}`.
            # Subscribed because the T5's own topic table names it
            # (`/app/bin/ctrl` holds that format string alongside the
            # `{name}_reply` it answers on), so it is a control path the cloud
            # may well use — and one this bridge was entirely blind to.
            f"/ext/rrpc/+/sys/{pk}/{dn}/#",
        ]

    async def forward_up(self, device: Device, topic: str, payload: Any) -> bool:
        """Relay one frame the device published to the real cloud.

        Called from `MQTTBridge._handle_message`, which has already decoded and
        attributed it. Everything is swallowed: an upstream problem must never
        interfere with the local delivery that has already happened.

        Returns:
            True if the frame was relayed.
        """
        client = self._clients.get(device.petkit_id)
        creds = self._credentials.get(device.petkit_id)
        if client is None or not creds or not _is_from_device(topic):
            return False
        try:
            upstream_topic = rewrite_topic(topic, creds["product_key"], creds["device_name"])
            if upstream_topic is None:
                return False
            await client.publish(upstream_topic, _as_bytes(payload))
            self._capture(device, "up", topic, upstream_topic, payload, [], blocked=False)
            return True
        except Exception as e:
            log.debug("Could not relay %s upstream: %s", topic, e)
            return False

    async def _on_upstream(self, device: Device, creds: dict, message: Any) -> None:
        """Bring one frame down from the cloud, blocking or redacting it first."""
        topic = str(message.topic)
        raw = bytes(message.payload or b"")

        parsed = parse_topic(topic)
        if parsed is not None and parsed.category == "ota" and parsed.detail == "upgrade":
            await self._block_ota(device, topic, raw)
            return

        # Only what the SERVER publishes may go down. The direction table at the
        # top of `topics.py` is the whole rule: a frame arriving from upstream
        # on a device-direction topic (`thing/event/*/post`) is not a command,
        # the local broker would not deliver it to the device anyway
        # (`topics.downstream_filters` does not carry it), and republishing it
        # locally starts a loop — the bridge's `#` subscription hands it back,
        # `is_server_published` does not claim it, so `_handle_message` ingests
        # it as device traffic and `forward_up` sends it straight back up.
        # Observed against the real cloud as ~90 round trips per property post,
        # every ~5s (#20). The module docstring's ALLOW-LIST is not enough on
        # its own: it filters by topic SHAPE, and what has to be told apart here
        # is PROVENANCE.
        #
        # Warned rather than dropped in silence, because a frame on a topic
        # `_downstream_topics` never subscribed to is itself the anomaly.
        if parsed is not None and not is_server_published(parsed):
            log.warning("Refusing to relay a device-direction frame from upstream "
                        "for device %d on %s: %s",
                        device.petkit_id, topic, _text_or_empty(raw)[:200])
            return

        local_topic = rewrite_topic(topic, device.mqtt_product_key, device.mqtt_device_name)
        if local_topic is None:
            # A `post_reply` is expected and deliberately dropped: our own
            # broker already acknowledges every event the device posts, so
            # relaying the cloud's ack too would double it.
            if not topic.endswith("_reply"):
                # Anything else is a control path we cannot re-address, and
                # dropping it without a word is how one stays invisible. RRPC
                # is why this logs: the firmware's topic table names
                # `/ext/rrpc/{id}/sys/{pk}/{dn}/thing/service/{name}`, nothing
                # here can rewrite that shape yet, and whether the cloud
                # actually uses it is the open question.
                log.info("Unmapped upstream frame for device %d on %s: %s",
                         device.petkit_id, topic, _text_or_empty(raw)[:300])
            return

        result = redact_mqtt(raw, topic=topic, policy=self._policy_factory(device))

        if self._hub is not None:
            for record in result.records:
                self._hub.record_redaction(
                    device.petkit_id, record.rule, f"{record.rule} on {topic}",
                    detail={"rule": record.rule, "topic": topic,
                            "original": record.original, "note": record.note},
                    blocked=record.blocking,
                )
        await self._persist(device, topic, result.blocked)
        self._capture(device, "down", local_topic, topic, result.body,
                      [r.rule for r in result.records], blocked=False)

        # Republished at QoS 0 and never retained, whatever the cloud used.
        # This deliberately does NOT carry the cloud's values over, which it
        # briefly did while we were hunting a command that would not actuate
        # (the cause turned out to be JSON whitespace, see `bridge._dumps`).
        #
        # QoS, because it cannot matter and must not: delivery is
        # min(publish, subscription) and the server-side subscription is QoS 0
        # (`auth.py::_server_subscribe`), so the cloud's value never reached
        # the device anyway — and raising that subscription to QoS 1 makes a
        # T5 PUBACK the frame and then drop the session seconds later, so this
        # must not become the half of a pair that reintroduces it.
        #
        # `retain`, because a retained `thing/service/start` would sit on our
        # broker and be redelivered on every reconnect, scooping the box each
        # time with nothing in the logs to explain why. The cloud has used
        # qos=0/retain=false on every frame observed; both are logged so a
        # change shows up as a question rather than being silently adopted.
        log.info("Cloud -> device %d: %s (cloud qos=%s retain=%s) %s",
                 device.petkit_id, local_topic,
                 getattr(message, "qos", 0), getattr(message, "retain", False),
                 _text_or_empty(result.body)[:160])
        await self._publish_local(local_topic, result.body)

        # Logged here rather than left to the bridge: this lands on a topic the
        # server publishes on, so the bridge skips its own echo of it, and
        # without this the panel would show a cloud command reaching the device
        # only as whatever redactions it happened to trigger.
        if self._hub is not None:
            self._hub.record_mqtt(device.petkit_id, local_topic, result.body,
                                  outbound=True, client=device.mqtt_device_name,
                                  origin="the real cloud")

        # Last: after the publish and the panel record, so recording what the
        # device was told can never delay, change or hide telling it.
        await self._learn_from_downlink(device, parsed, result.body)

    async def _learn_from_downlink(self, device: Device, parsed: Any, body: Any) -> None:
        """Record the settings a relayed `thing.service.property.set` carried.

        This is how the official app changes a setting, and for a device that
        reports none of its own (a W7H) it is the only way Home Assistant can
        find out what the owner chose (`ha/learn.py`). Read from the REDACTED
        body, the one the device was actually given.

        Observation only, and never fatal: the frame has already gone down
        untouched, and a learning failure costs a log line, not the relay.
        Gated on `wanted()` as well as on this bridge running at all, so a
        frame still in flight as proxy mode is switched off teaches nothing.
        """
        if parsed is None or parsed.category != "service" or parsed.detail != "property/set":
            return
        if not self.wanted():
            return
        try:
            frame = json.loads(_as_bytes(body))
            if not isinstance(frame, dict):
                return
            if frame.get("method", "thing.service.property.set") != "thing.service.property.set":
                return
            learned = learn_settings(device, frame.get("params"),
                                     source="the cloud's property.set (proxy)")
            if not learned:
                return
            self._registry.mark_dirty()
            if self._ha_publisher is not None:
                await self._ha_publisher.publish_state(device)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Could not learn settings from a relayed property.set "
                          "for device %d", device.petkit_id)

    async def _block_ota(self, device: Device, topic: str, raw: bytes) -> None:
        """Refuse a firmware push and keep a record of it.

        Nothing is republished locally. This add-on hosts no firmware and a bad
        answer here is the one way it could brick a device — the same reasoning
        `http/handlers/stubs.py::handle_ota_check` records for the HTTP side.
        """
        log.warning("BLOCKED upstream OTA push for device %d on %s", device.petkit_id, topic)
        if self._hub is not None:
            self._hub.record_redaction(device.petkit_id, "ota",
                                       f"blocked OTA push on {topic}",
                                       detail={"topic": topic, "payload": _text_or_empty(raw)},
                                       blocked=True)
        if self._event_store is not None:
            await self._event_store.add_blocked_attempts([{
                "device_id": device.petkit_id, "kind": "ota", "transport": "mqtt",
                "endpoint": topic, "field_path": "", "payload_json": _text_or_empty(raw),
                "detail_json": {"note": "firmware push over MQTT"},
            }])
        self._capture(device, "down", "", topic, raw, ["ota"], blocked=True)

    async def _persist(self, device: Device, topic: str, blocked: list) -> None:
        """Record the blocking redactions from one downstream frame."""
        if self._event_store is None or not blocked:
            return
        await self._event_store.add_blocked_attempts([{
            "device_id": device.petkit_id, "kind": record.rule, "transport": "mqtt",
            "endpoint": topic, "field_path": record.path,
            "payload_json": record.original, "detail_json": {"note": record.note},
        } for record in blocked])

    def _capture(self, device: Device, direction: str, local_topic: str,
                 upstream_topic: str, payload: Any, rules: list[str],
                 *, blocked: bool) -> None:
        """Append one relayed frame to the proxy capture stream, if enabled."""
        if not (self._live_config.get("capture") and self._live_config.get("proxy_mode")):
            return
        capture_record(self._live_config.get("capture_dir", "/data/capture"), "proxy_mqtt", {
            "device_id": device.petkit_id,
            "direction": direction,
            "local_topic": local_topic,
            "upstream_topic": upstream_topic,
            "payload": _text_or_empty(_as_bytes(payload)),
            "redactions": rules,
            "blocked": blocked,
        })


def _as_bytes(payload: Any) -> bytes:
    """Render a payload for the wire, whatever shape the caller had it in."""
    if isinstance(payload, (bytes, bytearray)):
        return bytes(payload)
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload).encode()


def _text_or_empty(raw: bytes | None) -> str:
    """Decode a frame for a log or a capture, never raising on a binary one.

    None collapses to "" rather than passing through: every caller here either
    slices the result for a log line or drops it into a JSON field, and neither
    has anything to say about the difference between an absent frame and an
    empty one. `http/middleware/logging.py::_text_or_none` keeps that difference
    because a proxied HTTP exchange does turn on it.
    """
    if raw is None:
        return ""
    return bytes(raw).decode("utf-8", "replace")
