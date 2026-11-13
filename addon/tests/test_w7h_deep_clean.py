"""The W7H deep clean: its button, the pump reset it can leave behind, and the
stage and outcome it reports.

All of it from one capture of PetKit's app in proxy mode (W7H, fw 456): the
app's "Deep clean" sends `start_action` 4, a failed run latches `err.cycM`, and
the app's "I've cleaned it. Start circulating" sends a `reset_cycle_pump`
service of its own.
"""

import tempfile
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer
from jinja2 import Template

from petkit_local.devices import defaults
from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.devices.state_parsers import (normalize_property_params,
                                                parse_state_report)
from petkit_local.events import codes
from petkit_local.events.decode import event_label
from petkit_local.events.ingest import apply_derived_state
from petkit_local.events.normalize import classify_event_kind, entity_for_event
from petkit_local.events.store import EventStore
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.ha.commands import handle_ha_command
from petkit_local.ha.discovery import build_discovery_payload
from petkit_local.http.server import create_app
from petkit_local.mqtt import topics

NEW_KEYS = {"fountain_deep_clean", "fountain_reset_circulation_pump",
            "deep_clean_stage", "last_deep_clean", "last_deep_clean_result"}


def _w7h() -> tuple[Device, dict]:
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    d.config.setdefault("settings", defaults.default_settings(d))
    idx = {e.unique_id_suffix: e for e in get_entities_for_device(d) if e.is_settable}
    return d, idx


def _entity(key: str):
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    return next(e for e in get_entities_for_device(d) if e.key == key)


def _tpl(key: str) -> str:
    payload = build_discovery_payload(
        entity=_entity(key), device_id=1, device_type="w7h",
        device_name="dev", serial_number="SN",
        state_topic="petkit-local/1/state",
    )
    return payload["value_template"]


def _render(key: str, state: dict) -> str:
    return Template(_tpl(key)).render(value_json={"state": state})


# --- the two buttons ----------------------------------------------------------

def test_deep_clean_sends_start_action_4():
    d, idx = _w7h()
    suffix, payload = handle_ha_command(d, idx["fountain_deep_clean"], "")
    assert suffix == "start"
    assert set(payload) == {"method", "id", "params", "version"}
    assert payload["method"] == "thing.service.start"
    assert payload["params"] == {"start_action": 4}
    assert payload["version"] == "1.0.0"
    assert int(payload["id"]) < 2**31
    assert 4 in codes.FOUNTAIN_W7H_START_ACTIONS
    assert 4 not in codes.FOUNTAIN_W7H_START_ACTIONS_NOT_WORK


def test_pump_reset_is_its_own_service_not_a_setting():
    """Like `power`: written through `property.set` it would be delivered,
    accepted by the broker and dropped without a word."""
    d, idx = _w7h()
    before = dict(d.config["settings"])
    suffix, payload = handle_ha_command(d, idx["fountain_reset_circulation_pump"], "")
    assert suffix == "reset_cycle_pump"
    assert set(payload) == {"method", "id", "params", "version"}
    assert payload["method"] == "thing.service.reset_cycle_pump"
    assert payload["params"] == {"reset_cycle_pump": 1}
    assert payload["version"] == "1.0.0"
    assert int(payload["id"]) < 2**31
    assert d.config["settings"] == before
    assert "reset_cycle_pump" not in d.config["settings"]


def test_pump_reset_topic_is_one_the_broker_subscribes_the_device_to():
    """A W7H holds only what the broker subscribed it to, so a service topic
    outside `downstream_filters` would be accepted and silently dropped."""
    d, idx = _w7h()
    suffix, _ = handle_ha_command(d, idx["fountain_reset_circulation_pump"], "")
    topic = topics.service_topic("pk_test", "dn_test", suffix)
    assert topic == "/sys/pk_test/dn_test/thing/service/reset_cycle_pump"
    assert "/sys/pk_test/dn_test/thing/service/#" in topics.downstream_filters(
        "pk_test", "dn_test")


# --- who gets the entities ----------------------------------------------------

def test_the_w7h_publishes_the_deep_clean_entities():
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    entities = {e.key: e for e in get_entities_for_device(d)}
    assert NEW_KEYS <= set(entities)
    assert entities["fountain_deep_clean"].component == "button"
    assert entities["fountain_reset_circulation_pump"].component == "button"
    assert entities["deep_clean_stage"].component == "sensor"
    assert entities["last_deep_clean"].device_class == "timestamp"


@pytest.mark.parametrize("device_type", ["w4", "w5", "ctw2", "ctw3", "t5"])
def test_no_other_model_gets_them(device_type):
    d = Device(device_type=device_type, petkit_id=1, serial_number="SN")
    keys = {e.key for e in get_entities_for_device(d)}
    assert not keys & NEW_KEYS
    if device_type == "t5":
        # The litter box's own `deep_clean` switch, which the stage sensor's key
        # was chosen not to collide with.
        assert "deep_clean" in keys


# --- the stage ----------------------------------------------------------------

def test_a_property_post_carries_the_stage():
    params = {"disinfectState": 3, "disinfectTime": 1790000000,
              "workState": {"workMode": 4, "workReason": 2, "workProcess": 10}}
    for parsed in (normalize_property_params("w7h", params),
                   parse_state_report("w7h", params)):
        assert parsed["disinfectState"] == 3
        assert parsed["disinfectTime"] == 1790000000


@pytest.mark.parametrize("raw, label", [
    (0, "idle"), (1, "preparing"), (2, "preparing"), (3, "soaking"),
    (9, "9"),
])
def test_the_stage_sensor_decodes_each_stage(raw, label):
    assert _render("deep_clean_stage", {"disinfectState": raw}) == label


def test_the_stage_sensor_is_blank_before_anything_is_reported():
    assert _render("deep_clean_stage", {}) == ""


def test_the_stage_sensor_is_not_an_enum_and_disinfecting_still_follows_it():
    """`device_class="enum"` would make HA reject an unmapped stage."""
    assert _entity("deep_clean_stage").device_class != "enum"
    assert _entity("last_deep_clean_result").device_class != "enum"
    for raw, expected in ((0, "OFF"), (1, "ON"), (2, "ON"), (3, "ON")):
        assert _render("disinfecting", {"disinfectState": raw}) == expected


# --- the result ---------------------------------------------------------------

@pytest.mark.parametrize("event_type", ["disinfect_over", "12"])
@pytest.mark.parametrize("result, err, label", [
    (0, "", "completed"),
    (16, "cycM", "aborted (water not hot enough)"),
    (2, "cycM", "refused (pump fault active)"),
])
def test_a_finished_deep_clean_records_how_it_ended(event_type, result, err, label):
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    apply_derived_state(d, event_type, {"start_reason": 2, "result": result, "err": err})
    assert d.state["lastDeepCleanResult"] == result
    assert d.state["lastDeepClean"]
    assert "lastClean" not in d.state, "a deep clean is not a litter clean"
    assert _render("last_deep_clean_result", d.state) == label


@pytest.mark.parametrize("content, stored, rendered", [
    ({"result": 7}, 7, "7"),
    ({"result": "abc"}, None, ""),
    ({}, None, ""),
])
def test_an_unmapped_or_junk_result_never_raises(content, stored, rendered):
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    apply_derived_state(d, "disinfect_over", content)
    assert d.state["lastDeepCleanResult"] == stored
    assert d.state["lastDeepClean"]
    assert _render("last_deep_clean_result", d.state) == rendered


def test_code_12_is_per_category():
    assert codes.lookup("12", "t5") is None
    assert codes.lookup("12", "w7h").done_word == codes.DEEP_CLEAN_DONE_WORD
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    apply_derived_state(d, "12", {"result": 0})
    assert d.state == {}


# --- the timeline -------------------------------------------------------------

@pytest.mark.parametrize("event_type", ["disinfect_over", "12"])
def test_timeline_labels_use_the_deep_clean_vocabulary(event_type):
    """The outcome comes from the deep-clean table: on the litter one 2 is a
    failure and 16 is not in it at all."""
    aborted = event_label(event_type,
                          {"start_reason": 2, "result": 16, "err": "cycM"}, "w7h")
    assert aborted == "Manual deep cleaning aborted (water not hot enough)"
    assert event_label(event_type, {"start_reason": 2, "result": 0, "err": ""},
                       "w7h") == "Manual deep cleaning completed"
    refused = event_label(event_type,
                          {"start_reason": 2, "result": 2, "err": "cycM"}, "w7h")
    assert refused == "Manual deep cleaning refused (pump fault active)"


@pytest.mark.parametrize("event_type", ["work_start", "3"])
def test_the_work_start_of_a_deep_clean_is_named(event_type):
    assert event_label(event_type, {"action": 4, "reason": 2},
                       "w7h") == "Deep clean - work started"


def test_the_fountain_http_error_codes_are_classified():
    assert codes.lookup("1", "w7h").kind == codes.KIND_ERROR
    assert codes.lookup("1", "w7h").role == codes.ROLE_ERROR_START
    assert codes.lookup("2", "w7h").kind == codes.KIND_ERROR
    assert codes.lookup("2", "w7h").role == codes.ROLE_ERROR_OVER
    for code in ("1", "2", "3", "12"):
        assert classify_event_kind(code, {}, "w7h") != codes.KIND_OTHER


def test_disinfect_over_fires_a_declared_event_type():
    """HA silently drops an event_type its entity did not declare."""
    assert entity_for_event("disinfect_over", "w7h") == "cleaning_event"
    assert "disinfect_over" in _entity("cleaning_event").options


# --- end to end over HTTP -----------------------------------------------------

CONFIG = {"api_url": "http://server/6/", "mqtt_port": 1883, "proxy_mode": False,
          "proxy_upstream": "", "proxy_block_run_cmd": True, "capture": False}
HDR = {"X-Device": "id=1&sn=SN"}


async def test_an_http_disinfect_over_populates_the_result():
    """Before the device is up on MQTT the same event arrives as HTTP code 12."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = DeviceRegistry()
        device = reg.get_or_create(petkit_id=1, device_type="w7h", serial_number="SN")
        app = create_app(reg, dict(CONFIG))
        app["event_store"] = EventStore(Path(tmp) / "petkit.db")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.post("/6/w7h/dev_event_report", headers=HDR,
                              data={"event_type": "12", "event_id": "e1",
                                    "content": '{"start_reason": 2, "result": 16,'
                                               ' "err": "cycM"}'})
            assert device.state.get("lastDeepCleanResult") == 16
            assert device.state.get("lastDeepClean")
            assert "lastClean" not in device.state
        finally:
            await client.close()
            await app["event_store"].close()
