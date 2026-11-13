"""Integration test of MQTTBridge event handling with a fake HA publisher —
verifies the new wiring (events, timestamps, K3 update, setting sync) without a
broker."""
import asyncio
import contextlib
import json
import sys
import types

import pytest

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.devices.ble import BLERegistry
from petkit_local.mqtt import bridge as bridge_module
from petkit_local.mqtt.bridge import MQTTBridge
from tests._fakes import FakeMessage, FakeMqttClient


class FakePublisher:
    def __init__(self):
        self.events = []
        self.states = []
        self.ble_states = []

    async def publish_event(self, device, suffix, event_type, attrs=None):
        self.events.append((device.petkit_id, suffix, event_type, attrs))

    async def publish_state(self, device):
        self.states.append(device.petkit_id)

    async def publish_availability(self, device):
        pass

    async def publish_ble_discovery(self, ble_dev):
        pass

    async def publish_ble_state(self, ble_dev):
        self.ble_states.append(ble_dev.petkit_id)


def _setup():
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN")
    ble = BLERegistry()
    pub = FakePublisher()
    bridge = MQTTBridge(reg, pub, ble)
    return reg, dev, ble, pub, bridge


async def test_pet_out_sets_timestamp_weight_and_fires_event():
    import json
    reg, dev, ble, pub, bridge = _setup()
    # pet_weight arrives inside params.content (a JSON string), per the reference.
    await bridge._handle_event(dev, "pet_out", {"params": {"content": json.dumps({"pet_weight": 4200})}})
    assert dev.state["lastVisit"]
    assert dev.state["petWeight"] == 4200
    assert ("toilet_event", "pet_out") in [(s, e) for _, s, e, _ in pub.events]


async def test_clean_over_sets_last_clean_and_fires_cleaning_event():
    reg, dev, ble, pub, bridge = _setup()
    await bridge._handle_event(dev, "clean_over", {"params": {}})
    assert dev.state["lastClean"]
    assert any(s == "cleaning_event" and e == "clean_over" for _, s, e, _ in pub.events)


async def test_disinfect_over_records_the_result_and_fires_the_work_event():
    reg, _, ble, pub, bridge = _setup()
    dev = reg.get_or_create(petkit_id=11, device_type="w7h", serial_number="SN")
    await bridge._handle_event(dev, "disinfect_over", {"params": {"content": json.dumps(
        {"result": 0, "err": "", "start_reason": 2})}})
    assert dev.state["lastDeepCleanResult"] == 0
    assert dev.state["lastDeepClean"]
    assert ("cleaning_event", "disinfect_over") in [(s, e) for _, s, e, _ in pub.events]


async def test_property_syncs_known_settings_and_ignores_others():
    reg, dev, ble, pub, bridge = _setup()
    await bridge._handle_event(dev, "property", {"params": {"autoWork": 0, "sandPercent": 40}})
    # a physical setting change reflects in HA controls...
    assert dev.config["settings"]["autoWork"] == 0
    # ...while sensor telemetry stays in state, not settings
    assert dev.state["sandPercent"] == 40
    assert "sandPercent" not in dev.config["settings"]


async def test_property_updates_linked_k3_and_publishes():
    reg, dev, ble, pub, bridge = _setup()
    ble.register(ble_type="k3", petkit_id=555, mac="AA:BB", link_with=10)
    await bridge._handle_event(dev, "property", {"params": {"battery": 88, "liquid": 60}})
    k3 = ble.get(555)
    assert k3.state["consumables"]["battery"] == 88
    assert k3.state["consumables"]["liquid"] == 60
    assert 555 in pub.ble_states


async def test_non_event_property_still_publishes_state():
    reg, dev, ble, pub, bridge = _setup()
    await bridge._handle_event(dev, "property", {"params": {"sandPercent": 30}})
    assert dev.petkit_id in pub.states


async def test_ble_poll_sent_to_parent_connect_topic():
    reg, dev, ble, pub, bridge = _setup()
    ble.register(ble_type="w5", petkit_id=700, mac="CC:DD", link_with=10, interval=240)
    bridge._client = FakeMqttClient()
    await bridge._handle_event(dev, "property", {"params": {"sandPercent": 30}})
    topics = [t for t, _, _ in bridge._client.published]
    assert any(t.endswith("/thing/service/connect") for t in topics)
    # throttled: a second property post within the interval sends no new poll
    n = len(bridge._client.published)
    await bridge._handle_event(dev, "property", {"params": {"sandPercent": 31}})
    assert len(bridge._client.published) == n


async def test_a_command_is_published_as_compact_json():
    """The T5's data-model parser silently drops a spaced `thing/service/*`
    frame before any handler runs, so a command with `json.dumps` default
    whitespace never actuates — proven on hardware. Every broker->device
    publish must be compact."""
    reg, dev, ble, pub, bridge = _setup()
    bridge._client = FakeMqttClient()
    await bridge.publish_to_device(
        dev, "start", {"method": "thing.service.start",
                       "id": "1", "params": {"start_action": 0}, "version": "1.0.0"})
    _topic, payload, _kw = bridge._client.published[-1]
    assert ", " not in payload and ": " not in payload, payload
    assert payload == ('{"method":"thing.service.start","id":"1",'
                       '"params":{"start_action":0},"version":"1.0.0"}')


async def test_user_get_and_post_reply_are_also_compact():
    """Every broker->device topic goes through the same strict parser."""
    reg, dev, ble, pub, bridge = _setup()
    bridge._client = FakeMqttClient()
    await bridge._handle_event(dev, "data_get", {"params": {"dataType": "dev_multi_config"}})
    _t, payload, _kw = bridge._client.published[-1]
    assert ", " not in payload and ": " not in payload, payload


async def test_data_get_replies_on_user_get():
    reg, dev, ble, pub, bridge = _setup()
    bridge._client = FakeMqttClient()
    await bridge._handle_event(dev, "data_get", {"params": {"dataType": "dev_multi_config"}})
    pubs = bridge._client.published
    assert pubs, "expected a user/get publish"
    topic, payload, _kw = pubs[-1]
    assert topic.endswith("/user/get")
    assert "result" in json.loads(payload)


# --- message-loop resilience -------------------------------------------------
# The bridge holds one wildcard subscription for every device, so a failure on
# one message used to disconnect (and rediscover) all of them.


async def _stream(*messages):
    for message in messages:
        yield message


def _event_topic(device, event: str = "property") -> str:
    return f"/sys/{device.mqtt_product_key}/{device.mqtt_device_name}/thing/event/{event}/post"


async def test_unparseable_payload_does_not_stop_the_message_loop():
    reg, dev, ble, pub, bridge = _setup()
    bridge._client = FakeMqttClient()
    topic = _event_topic(dev)

    await bridge._consume(_stream(
        FakeMessage(topic, b"\x00\x01 not json at all"),   # binary garbage
        FakeMessage(topic, b"[1, 2]"),                     # valid JSON, wrong shape
        FakeMessage(topic, b'\xff\xfe\x00'),               # not even decodable
        FakeMessage(topic, json.dumps({"params": {"sandPercent": 40}}).encode()),
    ))

    # The good message that followed the bad ones was still processed.
    assert dev.state["sandPercent"] == 40


async def test_handler_failure_does_not_stop_the_message_loop():
    reg, dev, ble, pub, bridge = _setup()
    seen = []

    async def flaky(message):
        seen.append(message.topic)
        if len(seen) == 1:
            raise RuntimeError("boom inside a device handler")

    bridge._handle_message = flaky
    await bridge._consume(_stream(FakeMessage("a", b"{}"), FakeMessage("b", b"{}")))

    assert seen == ["a", "b"]


async def test_connection_error_propagates_out_of_the_message_loop():
    """A lost connection must still reach start()'s reconnect handler."""
    class ConnectionLost(Exception):
        pass

    reg, dev, ble, pub, bridge = _setup()

    async def dead(message):
        raise ConnectionLost("broker went away")

    bridge._handle_message = dead
    try:
        await bridge._consume(_stream(FakeMessage("a", b"{}")), (ConnectionLost,))
    except ConnectionLost:
        pass
    else:
        assert False, "connection errors must not be swallowed"


async def test_cancellation_propagates_out_of_the_message_loop():
    reg, dev, ble, pub, bridge = _setup()

    async def cancelled(message):
        raise asyncio.CancelledError()

    bridge._handle_message = cancelled
    try:
        await bridge._consume(_stream(FakeMessage("a", b"{}")))
    except asyncio.CancelledError:
        pass
    else:
        assert False, "shutdown must not be swallowed"


async def test_start_keeps_one_subscription_across_a_malformed_frame():
    """End-to-end over start(): a bad frame must not cost a reconnect."""
    reg, dev, ble, pub, bridge = _setup()
    topic = _event_topic(dev)
    queue = [
        FakeMessage(topic, b"<<not json>>"),
        FakeMessage(topic, json.dumps({"params": {"sandPercent": 41}}).encode()),
    ]
    drained = asyncio.Event()
    connects = []
    subscriptions = []

    class FakeClient:
        def __init__(self, **kwargs):
            connects.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def subscribe(self, topic):
            subscriptions.append(topic)

        async def publish(self, topic, payload, **kw):
            pass

        @property
        def messages(self):
            return self._messages()

        async def _messages(self):
            for message in queue:
                yield message
            drained.set()
            await asyncio.Event().wait()  # a live session just waits for more

    fake_aiomqtt = types.ModuleType("aiomqtt")
    fake_aiomqtt.Client = FakeClient
    fake_aiomqtt.MqttError = type("MqttError", (Exception,), {})

    real_aiomqtt = sys.modules.get("aiomqtt")
    sys.modules["aiomqtt"] = fake_aiomqtt
    real_delay = bridge_module.STARTUP_DELAY_SECONDS
    bridge_module.STARTUP_DELAY_SECONDS = 0
    try:
        task = asyncio.create_task(bridge.start("broker", 1883))
        await asyncio.wait_for(drained.wait(), timeout=5)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    finally:
        bridge_module.STARTUP_DELAY_SECONDS = real_delay
        if real_aiomqtt is None:
            sys.modules.pop("aiomqtt", None)
        else:
            sys.modules["aiomqtt"] = real_aiomqtt

    assert len(connects) == 1, "the malformed frame forced a reconnect"
    assert subscriptions == ["#"], "the malformed frame dropped the subscription"
    assert dev.state["sandPercent"] == 41


# --- live capture (the panel toggle used to need a restart to reach MQTT) ---

async def test_capture_follows_the_live_config_without_a_restart(tmp_path):
    """`capture` was frozen into the bridge at construction, so flipping it in
    the panel started HTTP capture immediately and MQTT capture never."""
    live = {"capture": False, "capture_dir": str(tmp_path)}
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN1")
    bridge = MQTTBridge(reg, live_config=live)

    class _Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = json.dumps(payload).encode()

    msg = _Msg("/sys/pk/dn/thing/event/property/post", {"params": {"x": 1}})

    await bridge._handle_message(msg)
    assert not (tmp_path / "mqtt.jsonl").exists()

    live["capture"] = True
    await bridge._handle_message(msg)
    lines = (tmp_path / "mqtt.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["topic"] == "/sys/pk/dn/thing/event/property/post"


async def test_capture_constructor_argument_still_works_without_a_live_config():
    """The bare `MQTTBridge(reg, ...)` the tests build has no shared config."""
    bridge = MQTTBridge(DeviceRegistry(), capture=True, capture_dir="/somewhere")
    assert bridge._capture is True
    assert bridge._capture_dir == "/somewhere"


# --- the live client attribute, and what depends on it ---------------------

async def test_client_is_cleared_the_moment_the_session_ends(monkeypatch):
    """Not once the reconnect delay is over. `publish_to_device` promises to
    RAISE when there is no session, and that promise is the only thing that
    makes its caller fall back to the heartbeat queue — so a stale attribute
    during the reconnect window silently loses a command instead."""
    import petkit_local.mqtt.bridge as bridge_mod

    seen_during_backoff = []

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def subscribe(self, topic):
            pass

        @property
        def messages(self):
            raise RuntimeError("connection dropped")

    bridge = MQTTBridge(DeviceRegistry())

    real_sleep = asyncio.sleep

    async def _spy_sleep(delay, *a, **kw):
        # This is the reconnect backoff: sample the attribute mid-window.
        if delay == bridge_mod.RECONNECT_DELAY_SECONDS:
            seen_during_backoff.append(bridge._client)
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(bridge_mod.asyncio, "sleep", _spy_sleep)
    monkeypatch.setattr(bridge_mod, "STARTUP_DELAY_SECONDS", 0)
    fake_aiomqtt = types.SimpleNamespace(Client=_Client, MqttError=RuntimeError)
    monkeypatch.setitem(sys.modules, "aiomqtt", fake_aiomqtt)

    with contextlib.suppress(asyncio.CancelledError):
        await bridge.start("127.0.0.1", 1883)

    assert seen_during_backoff == [None]


async def test_the_upstream_relay_survives_a_local_handling_failure():
    """An observation mode that stops observing whenever something else breaks
    is worse than useless."""
    relayed = []

    class _Upstream:
        async def forward_up(self, device, topic, payload):
            relayed.append(topic)
            return True

    reg = DeviceRegistry()
    device = reg.get_or_create(petkit_id=1, device_type="t5", serial_number="SN1")
    bridge = MQTTBridge(reg, upstream=_Upstream())

    async def _boom(*a, **kw):
        raise RuntimeError("event store down")

    bridge._handle_event = _boom

    topic = _event_topic(device)

    class _Msg:
        pass

    msg = _Msg()
    msg.topic = topic
    msg.payload = json.dumps({"params": {"x": 1}}).encode()

    with pytest.raises(RuntimeError):
        await bridge._handle_message(msg)

    # The exception still propagates (so `_consume` logs and drops the frame),
    # but the relay ran anyway.
    assert relayed == [topic]


# --- the bridge's own echo ---

def test_server_published_topics_are_recognised():
    """The bridge subscribes to `#`, so everything it sends comes back to it."""
    from petkit_local.mqtt.topics import is_server_published, parse_topic

    ours = [
        "/sys/pk/dn/thing/service/property/set",
        "/sys/pk/dn/thing/service/start",
        "/pk/dn/user/get",
        "/ota/device/upgrade/pk/dn",
    ]
    theirs = [
        "/sys/pk/dn/thing/event/property/post",
        "/ota/device/inform/pk/dn",
        # The device's acknowledgement of a command travels the opposite way to
        # the command itself, and is the only evidence one was ever received.
        "/sys/pk/dn/thing/service/property/set_reply",
        "/sys/pk/dn/thing/service/start_reply",
    ]
    for t in ours:
        assert is_server_published(parse_topic(t)) is True, t
    for t in theirs:
        assert is_server_published(parse_topic(t)) is False, t


async def test_own_publish_echoed_back_is_not_device_traffic():
    """Publishing a command puts it on a topic the bridge is subscribed to, so
    it arrives right back. Counted as inbound it refreshes the device's liveness
    with our own traffic — which would make a silent device look alive for
    another offline_timeout every time a command was sent."""

    class _Hub:
        def __init__(self):
            self.mqtt = []

        def record_mqtt(self, device_id, topic, payload, **kw):
            self.mqtt.append((topic, kw))

    reg, dev, ble, pub, bridge = _setup()
    dev.mqtt_product_key, dev.mqtt_device_name = "pk", "dn"
    hub = _Hub()
    bridge._hub = hub
    dev.last_mqtt = 0.0
    dev.online = False

    class _Msg:
        def __init__(self, topic, payload):
            self.topic = topic
            self.payload = json.dumps(payload).encode()

    await bridge._handle_message(
        _Msg("/sys/pk/dn/thing/service/property/set", {"params": {"lightMode": 1}})
    )
    assert dev.last_mqtt == 0.0
    assert dev.online is False
    assert hub.mqtt == []

    # A real device event still does all three.
    await bridge._handle_message(
        _Msg("/sys/pk/dn/thing/event/property/post", {"params": {"x": 1}})
    )
    assert dev.last_mqtt > 0
    assert dev.online is True
    assert hub.mqtt and hub.mqtt[0][1]["client"] == "dn"
