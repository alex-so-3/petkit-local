"""HAPublisher.publish_event — the payload contract HA's event platform holds.

HA parses the payload as a JSON OBJECT: `event_type` is validated against the
discovery config's `event_types`, and every other key becomes an attribute of
the fired event, visible in the logbook and stored by the recorder. That is
also why the discovery config carries no value_template (see ha/discovery.py):
HA renders the template first and then requires the result to still be JSON,
so extracting the string made HA reject every event.
"""
import json

from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha.publisher import HAPublisher
from tests._fakes import FakeMqttClient


def _setup():
    reg = DeviceRegistry()
    dev = reg.get_or_create(petkit_id=1, device_type="t6", serial_number="SN")
    pub = HAPublisher(reg, {})
    pub._client = FakeMqttClient()
    pub._connected = True
    return dev, pub


async def test_event_payload_is_json_carrying_event_type():
    dev, pub = _setup()
    await pub.publish_event(dev, "cleaning_event", "clean_over")
    [(topic, payload, kw)] = pub._client.published
    assert topic == "petkit-local/1/event/cleaning_event"
    assert json.loads(payload) == {"event_type": "clean_over"}
    # Momentary: a retained event would re-fire on every HA restart.
    assert kw == {"retain": False}


async def test_event_attrs_become_attributes_and_envelope_is_stripped():
    dev, pub = _setup()
    await pub.publish_event(dev, "toilet_event", "pet_out", {
        "pet_weight": 4200,
        # The transport envelope must never reach HA — `XDevice` is the signed
        # request credential and `state` the full snapshot. Same rule as
        # merging `params` into `device.state` (events/normalize.py).
        "XDevice": "id=1&nonce=x&sign=y",
        "event_id": "10000001_1785276736",
        "timestamp": 1785276736,
        "content": "{}",
        "state": "{}",
    })
    [(_, payload, _)] = pub._client.published
    assert json.loads(payload) == {"event_type": "pet_out", "pet_weight": 4200}


async def test_device_content_cannot_override_event_type():
    dev, pub = _setup()
    await pub.publish_event(dev, "cleaning_event", "clean_over",
                            {"event_type": "spoofed"})
    [(_, payload, _)] = pub._client.published
    assert json.loads(payload)["event_type"] == "clean_over"
