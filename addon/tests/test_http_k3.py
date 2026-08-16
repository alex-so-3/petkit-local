"""HTTP-side K3 piggyback: a T4 (unpatchable ESP32) never talks MQTT, so its
`property/post` — and with it the K3's `battery`/`liquid` that ride along —
never reaches the bridge. `dev_state_report` and `dev_event_report` carry the
same top-level keys, so the same helper must fire on both HTTP paths for a
linked K3 to publish anything to Home Assistant.

Live evidence (T4 firmware 1.652, 2026-08-16): every one of 24 state reports
and 26 event reports carried `k3Id`, `liquid`, `battery` at the top of the
`state` object — same shape the MQTT bridge already extracts, different
transport.
"""
from __future__ import annotations

import json
from urllib.parse import urlencode

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.ble import BLERegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.http.server import create_app
from petkit_local.mqtt.ble_relay import update_linked_k3

CONFIG = {
    "api_url": "http://server/6/",
    "mqtt_port": 1883,
    "proxy_mode": False,
    "proxy_upstream": "",
    "proxy_block_run_cmd": True,
    # capture writes to disk; keep it off here.
    "capture": False,
}

# The specific number does not matter, only that it resolves to the parent the
# K3 is linked to.
HDR = {"X-Device": "id=100050447&sn=SN"}


class _FakePublisher:
    def __init__(self) -> None:
        self.states: list[int] = []
        self.ble_states: list[int] = []
        self.ble_discovery: list[int] = []

    async def publish_state(self, device):
        self.states.append(device.petkit_id)

    async def publish_availability(self, device):
        pass

    async def publish_ble_discovery(self, ble_dev):
        self.ble_discovery.append(ble_dev.petkit_id)

    async def publish_ble_state(self, ble_dev):
        self.ble_states.append(ble_dev.petkit_id)

    async def publish_event(self, *_a, **_k):
        pass


def _setup():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=100050447, device_type="t4", serial_number="SN")
    ble = BLERegistry()
    ble.register(ble_type="k3", petkit_id=555, mac="AA:BB", link_with=100050447)
    return reg, ble, _FakePublisher()


async def _client(reg, ble, pub) -> TestClient:
    app = create_app(reg, dict(CONFIG))
    app["ble_registry"] = ble
    app["ha_publisher"] = pub
    app["event_store"] = EventStore(":memory:")
    # Mirror wiring.py: the HTTP handler treats a missing hook as no-op, so
    # installing exactly the same closure keeps behaviour production-shaped.
    async def on_state_report(device, body):
        await pub.publish_state(device)
        await pub.publish_availability(device)
        k3 = update_linked_k3(device, body, ble)
        if k3:
            await pub.publish_ble_discovery(k3)
            await pub.publish_ble_state(k3)
    app["on_state_report"] = on_state_report
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --- the helper is the whole point: assert it directly first ---------------

def test_helper_updates_battery_and_liquid_from_top_level_state():
    reg, ble, _pub = _setup()
    dev = reg.get(100050447)
    changed = update_linked_k3(dev, {"battery": 88, "liquid": 60}, ble)
    assert changed is not None and changed.petkit_id == 555
    assert ble.get(555).state["consumables"] == {"battery": 88, "liquid": 60}


def test_helper_returns_none_without_a_linked_k3():
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=100050447, device_type="t4", serial_number="SN")
    dev = reg.get(100050447)
    assert update_linked_k3(dev, {"battery": 88, "liquid": 60}, BLERegistry()) is None


def test_helper_returns_none_when_neither_key_is_present():
    """`sandType` is present in every real state block; only battery/liquid
    should trigger a K3 update."""
    reg, ble, _pub = _setup()
    assert update_linked_k3(reg.get(100050447), {"sandType": 1}, ble) is None


# --- the HTTP paths: dev_state_report and dev_event_report -----------------

async def test_state_report_updates_linked_k3_from_http():
    reg, ble, pub = _setup()
    client = await _client(reg, ble, pub)
    try:
        # Verbatim shape from a live T4 (2026-08-16 capture, trimmed).
        state = {
            "litter": {"weight": 1944, "percent": 0, "sandType": 0},
            "k3Id": 555, "liquid": 60, "battery": 88,
        }
        r = await client.post(
            "/6/t4/dev_state_report",
            headers={**HDR, "Content-Type": "application/x-www-form-urlencoded"},
            data=f"state={json.dumps(state)}",
        )
        assert r.status == 200
        assert ble.get(555).state["consumables"] == {"battery": 88, "liquid": 60}
        assert 555 in pub.ble_states
        assert 555 in pub.ble_discovery
    finally:
        await client.close()


async def test_event_report_state_block_updates_linked_k3():
    reg, ble, pub = _setup()
    client = await _client(reg, ble, pub)
    try:
        # A cleaning event (eventType 5) — every real T4 event report carries
        # this exact state envelope; the wire form is x-www-form-urlencoded.
        form = {
            "eventType": "5",
            "event_id": "100050447_1786877430",
            "timestamp": "1786877529",
            "content": json.dumps({"result": 0, "err": None}),
            "state": json.dumps({
                "litter": {"weight": 1943, "percent": 0, "sandType": 0},
                "k3Id": 555, "liquid": 42, "battery": 77,
            }),
        }
        r = await client.post(
            "/6/t4/dev_event_report",
            headers={**HDR, "Content-Type": "application/x-www-form-urlencoded"},
            data=urlencode(form),
        )
        assert r.status == 200
        assert ble.get(555).state["consumables"] == {"battery": 77, "liquid": 42}
        assert 555 in pub.ble_states
    finally:
        await client.close()
