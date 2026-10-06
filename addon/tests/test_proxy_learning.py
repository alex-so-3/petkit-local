"""Learning a device's settings from what proxy mode relays to it (`ha/learn.py`).

A W7H reports no settings of its own, so when the official app changes one the
cloud's `thing.service.property.set` downlink (MQTT) and its `dev_device_info`
reply (HTTP) are the only places the value ever crosses this add-on. Both must be
recorded the way a write from Home Assistant is — and only while proxy mode is
on, and never by touching what the device receives.
"""
import json

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices import defaults
from petkit_local.devices.base import Device, encode_multi_range
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha.learn import learn_settings, learnable_scalar_fields
from petkit_local.http.proxy import close_proxy_session
from petkit_local.http.redact import RedactionPolicy
from petkit_local.http.server import create_app
from petkit_local.mqtt.upstream import UpstreamCredentials, UpstreamMQTT
from petkit_local.web.hub import EventHub

REAL = {"product_key": "realpk", "device_name": "realdn",
        "device_secret": "realsecret", "mqtt_host": "realpk.iot-as-mqtt.example"}
SET_TOPIC = "/sys/realpk/realdn/thing/service/property/set"
PROXY_ON = {"proxy_mode": True, "proxy_mqtt_bridge": True}


class _FakePublisher:
    def __init__(self):
        self.states = []

    async def publish_state(self, device):
        self.states.append(device.petkit_id)


class _FakeMessage:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = json.dumps(payload).encode()
        self.qos = 0
        self.retain = False


def _upstream(tmp_path, *, live, device_type="w7h"):
    registry = DeviceRegistry()
    device = registry.get_or_create(petkit_id=100, device_type=device_type,
                                    serial_number="SN1")
    device.config["settings"] = {}
    registry.dirty_marks = 0

    def mark_dirty():
        registry.dirty_marks += 1
    registry.mark_dirty = mark_dirty
    creds = UpstreamCredentials(tmp_path / "proxy_upstream.json")
    creds.put(100, REAL)
    local = []

    async def publish_local(topic, payload):
        local.append((topic, payload))

    def policy(dev):
        return RedactionPolicy(device=dev, api_url="http://192.0.2.199:8080/6/",
                               mqtt_host="192.0.2.199")

    publisher = _FakePublisher()
    up = UpstreamMQTT(registry, creds, policy, publish_local, live_config=live,
                      ha_publisher=publisher)
    return up, device, registry, publisher, local


def _set(params):
    return _FakeMessage(SET_TOPIC, {"method": "thing.service.property.set",
                                    "id": "1", "params": params, "version": "1.0.0"})


# --- MQTT: a relayed property.set -------------------------------------------

async def test_a_relayed_property_set_is_stored_and_published(tmp_path):
    up, device, registry, publisher, local = _upstream(tmp_path, live=PROXY_ON)

    await up._on_upstream(device, REAL, _set({"waterChangeCycle": 4, "fountainMode": 2}))

    assert device.config["settings"]["waterChangeCycle"] == 4
    assert device.config["settings"]["fountainMode"] == 2
    assert publisher.states == [100]
    assert registry.dirty_marks == 1
    # The relay itself is untouched.
    assert len(local) == 1
    assert json.loads(local[0][1])["params"] == {"waterChangeCycle": 4, "fountainMode": 2}


async def test_a_multi_range_string_lands_where_the_schedule_editor_reads(tmp_path):
    up, device, _, publisher, _ = _upstream(tmp_path, live=PROXY_ON)
    wire = encode_multi_range("awDisturbMultiRange", [[1140, 660]])
    assert isinstance(wire, str)

    await up._on_upstream(device, REAL, _set({"awDisturbMultiRange": wire}))

    assert device.config["multi_config"]["awDisturbMultiRange"] == [[1140, 660]]
    assert "awDisturbMultiRange" not in device.config["settings"]
    served = {t["target"]: t["value"] for t in defaults.schedule_targets(device)}
    assert served["awDisturbMultiRange"] == [[1140, 660]]
    assert publisher.states == [100]


async def test_unknown_fields_and_timezone_are_not_learned(tmp_path):
    up, device, _, publisher, local = _upstream(tmp_path, live=PROXY_ON)

    await up._on_upstream(device, REAL, _set({"timezone": "3.0", "notAField": 7}))

    assert device.config["settings"] == {}
    assert "timezone" not in device.config
    assert publisher.states == []
    # Still relayed exactly as before.
    assert len(local) == 1


async def test_nothing_is_learned_with_proxy_mode_off(tmp_path):
    up, device, _, publisher, _ = _upstream(tmp_path, live={"proxy_mode": False})

    await up._on_upstream(device, REAL, _set({"waterChangeCycle": 4}))

    assert device.config["settings"] == {}
    assert publisher.states == []


async def test_a_repeat_of_the_stored_value_publishes_nothing(tmp_path):
    up, device, _, publisher, _ = _upstream(tmp_path, live=PROXY_ON)
    device.config["settings"]["volume"] = 5

    await up._on_upstream(device, REAL, _set({"volume": 5}))

    assert publisher.states == []


async def test_other_services_teach_nothing(tmp_path):
    up, device, _, publisher, _ = _upstream(tmp_path, live=PROXY_ON)
    msg = _FakeMessage("/sys/realpk/realdn/thing/service/start",
                       {"method": "thing.service.start",
                        "params": {"start_action": 2, "volume": 3}})

    await up._on_upstream(device, REAL, msg)

    assert device.config["settings"] == {}
    assert publisher.states == []


# --- the allow-list itself --------------------------------------------------

def test_only_settings_bound_fields_are_learnable():
    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    fields = learnable_scalar_fields(device)
    assert {"waterChangeCycle", "flushCycle", "waterChangeTime", "fountainMode",
            "volume", "awDisturbMode"} <= fields
    assert "timezone" not in fields
    assert "notAField" not in fields


def test_an_unreadable_range_is_skipped_not_stored():
    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    learned = learn_settings(device, {"awDisturbMultiRange": "not json",
                                      "wlDisturbMultiRange": [[0, 9999]]},
                             source="test")
    assert learned == {}
    assert "multi_config" not in device.config


def test_a_non_scalar_setting_is_skipped():
    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    assert learn_settings(device, {"volume": {"x": 1}}, source="test") == {}


# --- HTTP: a proxied dev_device_info ----------------------------------------

W7H_HDR = {"X-Device": "id=100&sn=SN100&type=W7H"}


async def _http(cloud_settings, *, proxy):
    async def cloud(request):
        return web.json_response({"result": {"id": 100, "settings": cloud_settings}})

    capp = web.Application()
    capp.router.add_route("*", "/{path:.*}", cloud)
    up = TestClient(TestServer(capp))
    await up.start_server()
    base = str(up.make_url("")).rstrip("/")

    config = {
        "api_url": "http://192.0.2.199:8080/6/", "mqtt_port": 1883,
        "bucket_endpoint": "https://192.0.2.199:9000", "data_dir": "/tmp",
        "capture": False, "capture_dir": "/tmp/capture", "proxy_mode": False,
        "proxy_upstream": base, "proxy_block_run_cmd": True,
        "proxy_block_ota": True, "proxy_media_real_oss": False,
    }
    registry = DeviceRegistry()
    app = create_app(registry, config)
    app["event_hub"] = EventHub()
    publisher = _FakePublisher()
    app["ha_publisher"] = publisher
    client = TestClient(TestServer(app))
    await client.start_server()
    r = await client.post("/6/w7h/dev_signup", headers=W7H_HDR)
    assert r.status == 200
    # A device nothing has set yet: a snapshot only fills gaps, and signup
    # seeds the fountain family's defaults.
    registry.get(100).config["settings"] = {}
    client.app["config"]["proxy_mode"] = proxy
    return up, client, registry, publisher


async def _close(up, client):
    await close_proxy_session(client.app)
    await client.close()
    await up.close()


async def test_a_proxied_device_info_teaches_its_settings():
    up, client, registry, publisher = await _http(
        {"fountainMode": 3, "flushCycle": 2, "timezone": 3.0, "bogus": 1}, proxy=True)
    try:
        r = await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        body = await r.json()
        # The device still gets the cloud's reply, as before.
        assert body["result"]["settings"]["fountainMode"] == 3

        device = registry.get(100)
        assert device.config["settings"]["fountainMode"] == 3
        assert device.config["settings"]["flushCycle"] == 2
        assert "bogus" not in device.config["settings"]
        assert "timezone" not in device.config["settings"]
        assert "timezone" not in device.config
        assert publisher.states == [100]
    finally:
        await _close(up, client)


async def test_device_info_teaches_nothing_with_proxy_off():
    up, client, registry, publisher = await _http({"fountainMode": 3}, proxy=False)
    try:
        await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        assert "fountainMode" not in registry.get(100).config.get("settings", {})
        assert publisher.states == []
    finally:
        await _close(up, client)


# --- learning never costs the device anything -------------------------------

def _boom(*_a, **_kw):
    raise RuntimeError("learning broke")


async def test_a_failing_learner_relays_the_same_bytes(tmp_path, monkeypatch):
    up, device, _, _, local = _upstream(tmp_path, live=PROXY_ON)
    await up._on_upstream(device, REAL, _set({"volume": 3}))
    monkeypatch.setattr("petkit_local.mqtt.upstream.learn_settings", _boom)
    await up._on_upstream(device, REAL, _set({"volume": 3}))
    assert len(local) == 2 and local[0] == local[1]


async def test_the_bridge_switched_off_learns_nothing(tmp_path):
    up, device, _, publisher, _ = _upstream(
        tmp_path, live={"proxy_mode": True, "proxy_mqtt_bridge": False})
    await up._on_upstream(device, REAL, _set({"volume": 3}))
    assert device.config["settings"] == {} and publisher.states == []


async def test_a_failing_learner_or_publisher_still_answers_the_same(monkeypatch):
    up, client, _, publisher = await _http({"fountainMode": 3}, proxy=True)
    try:
        r = await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        normal = await r.read()

        monkeypatch.setattr("petkit_local.http.middleware.proxy.learn_settings", _boom)
        r = await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        assert r.status == 200 and await r.read() == normal
        monkeypatch.undo()

        async def raising(_device):
            raise RuntimeError("HA gone")
        publisher.publish_state = raising
        client.app["registry"].get(100).config["settings"].pop("fountainMode")
        r = await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        assert r.status == 200 and await r.read() == normal
    finally:
        await _close(up, client)


_CLOUDS = []


async def _base_of(handler):
    capp = web.Application()
    capp.router.add_route("*", "/{path:.*}", handler)
    c = TestClient(TestServer(capp))
    await c.start_server()
    _CLOUDS.append(c)
    return str(c.make_url("")).rstrip("/")


async def test_a_refused_device_info_teaches_nothing():
    async def cloud(request):
        return web.json_response({"error": {"code": 704, "msg": "nope"}})

    up, client, registry, publisher = await _http({}, proxy=True)
    client.app["config"]["proxy_upstream"] = await _base_of(cloud)
    try:
        await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        assert "fountainMode" not in registry.get(100).config.get("settings", {})
        assert publisher.states == []
    finally:
        await _close(up, client)
        await _CLOUDS.pop().close()


async def test_device_info_does_not_revert_a_value_set_locally():
    """In proxy mode an HA write never reaches PetKit's account, so its
    `dev_device_info` keeps serving the old value: a snapshot only fills gaps."""
    up, client, registry, publisher = await _http(
        {"fountainMode": 1, "flushCycle": 2}, proxy=True)
    try:
        registry.get(100).config.setdefault("settings", {})["fountainMode"] = 3
        await client.post("/6/w7h/dev_device_info", headers=W7H_HDR)
        settings = registry.get(100).config["settings"]
        assert settings["fountainMode"] == 3
        assert settings["flushCycle"] == 2
    finally:
        await _close(up, client)
