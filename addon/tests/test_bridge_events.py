"""MQTTBridge <-> EventStore/PetRegistry wiring — the MQTT-transport half of
events/ingest.py (the HTTP half is covered by tests/test_ingest_endpoints.py)."""
import json
import tempfile
from pathlib import Path

from petkit_local.ai.pets import PetRegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.store import EventStore
from petkit_local.mqtt.bridge import MQTTBridge
from petkit_local.web.hub import EventHub


class FakePublisher:
    def __init__(self):
        self.events = []
        self.states = []
        self.pet_discoveries = []
        self.pet_states = []

    async def publish_event(self, device, suffix, event_type, attrs=None):
        self.events.append((device.petkit_id, suffix, event_type, attrs))

    async def publish_state(self, device):
        self.states.append(device.petkit_id)

    async def publish_availability(self, device):
        pass

    async def publish_pet_discovery(self, pet):
        self.pet_discoveries.append(pet["id"])

    async def publish_pet_state(self, pet, store):
        self.pet_states.append(pet["id"])


def _setup(tmp):
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=10, device_type="t5", serial_number="SN")
    pub = FakePublisher()
    store = EventStore(Path(tmp) / "petkit.db")
    hub = EventHub()
    pet_registry = PetRegistry(store, str(Path(tmp) / "faces"))
    bridge = MQTTBridge(reg, pub, event_store=store, hub=hub, pet_registry=pet_registry)
    return reg, dev, pub, store, hub, pet_registry, bridge


async def test_pet_out_event_is_persisted_to_store():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub, store, hub, pet_registry, bridge = _setup(tmp)
        # `params.event_id` is the episode key over MQTT exactly as over HTTP
        # (live T5 capture); `content.related_event` is the cross-episode link.
        await bridge._handle_event(dev, "pet_out", {
            "params": {
                "event_id": "10000001_1785276736",
                "content": json.dumps({"related_event": "r1", "pet_weight": 4200}),
            }
        })
        rows = await store.query_timeline(device_id=10)
        assert len(rows) == 1
        assert rows[0]["event_type"] == "pet_out"
        assert rows[0]["related_event"] == "10000001_1785276736"
        assert rows[0]["parent_event"] == "r1"


async def test_property_event_is_not_persisted():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub, store, hub, pet_registry, bridge = _setup(tmp)
        await bridge._handle_event(dev, "property", {"params": {"sandPercent": 40}})
        assert await store.query_timeline(device_id=10) == []


async def test_event_with_pet_id_triggers_pet_publish():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub, store, hub, pet_registry, bridge = _setup(tmp)
        pet = await pet_registry.create("Mruczek")
        await bridge._handle_event(dev, "pet_out", {
            "params": {"content": json.dumps({"petId": pet["id"], "pet_weight": 4200})}
        })
        assert pub.pet_discoveries == [pet["id"]]
        assert pub.pet_states == [pet["id"]]


async def test_event_without_pet_id_does_not_touch_pets():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub, store, hub, pet_registry, bridge = _setup(tmp)
        await bridge._handle_event(dev, "clean_over", {"params": {}})
        assert pub.pet_discoveries == []
        assert pub.pet_states == []


async def test_event_persistence_emits_hub_event():
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub, store, hub, pet_registry, bridge = _setup(tmp)
        await bridge._handle_event(dev, "error_start", {"params": {}})
        kinds = [e["kind"] for e in hub.recent(20)]
        assert "event" in kinds


async def test_the_request_credential_never_lands_in_device_state():
    """Every MQTT `params` carries the transport envelope beside the telemetry —
    on a live T5, all 186 `property` posts included `XDevice`, which is the
    signed request credential (`id=...&nonce=...&sign=...`).

    `device.state` is merged into and never pruned, and the panel renders it
    verbatim under "Raw parsed state JSON", so merging `params` wholesale both
    displayed the credential and kept it forever.
    """
    with tempfile.TemporaryDirectory() as tmp:
        _, device, _, store, _, _, bridge = _setup(tmp)
        await bridge._handle_event(device, "property", {"params": {
            "XDevice": "id=1&nonce=abc&timestamp=2&type=T5&sign=deadbeef",
            "event_id": "4_1_2", "timestamp": "2",
            "litter": {"percent": 40, "weight": 1200},
            "runtime": 900,
        }})
        await store.close()

    assert "XDevice" not in device.state
    assert not [k for k in device.state if "sign=" in str(device.state[k])]
    # The telemetry beside it still applies, or this would be a silent regression.
    assert device.state["sandPercent"] == 40
    assert device.state["totalTime"] == 900


async def test_ha_event_fires_with_content_not_transport_envelope():
    """Everything in the event payload besides `event_type` becomes an HA
    event attribute, so the bridge hands publish_event the decoded CONTENT.
    Passing raw `params` shipped the transport envelope — `XDevice`, the
    signed request credential, plus the full state snapshot — into HA's
    logbook and recorder."""
    with tempfile.TemporaryDirectory() as tmp:
        reg, dev, pub, store, hub, pet_registry, bridge = _setup(tmp)
        await bridge._handle_event(dev, "clean_over", {
            "params": {
                "XDevice": "id=1&nonce=x&sign=y",
                "event_id": "10000001_1785276736",
                "timestamp": 1785276736,
                "state": json.dumps({"sandPercent": 40}),
                "content": json.dumps({"start_reason": 0, "result": 0}),
            }
        })
        assert pub.events == [
            (10, "cleaning_event", "clean_over", {"start_reason": 0, "result": 0}),
        ]
