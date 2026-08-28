"""Last Clean / Last Visit / Last Feed / Pet Weight, on BOTH transports.

These four sensors have no field in any state report — they exist only as a
consequence of an event. The derivation used to live in `mqtt/bridge.py` alone,
so every device reporting over HTTP (each ESP32 model, and every Ingenic device
until the `mqtt` patcher is applied) showed all four as unknown forever. The
tests here pin the fix on both paths, because one path having it is exactly the
state that shipped.
"""
import json
import tempfile
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.base import Device
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events.ingest import apply_derived_state
from petkit_local.events.store import EventStore
from petkit_local.http.server import create_app


def _device(device_type="t5") -> Device:
    return Device(petkit_id=1, device_type=device_type, serial_number="SN")


# --- the derivation itself, in both namespaces ------------------------------

@pytest.mark.parametrize("mqtt_name, http_code", [
    ("clean_over", "5"),      # cleaning done
    ("dump_over", "6"),       # litter emptied
    ("reset_over", "7"),      # reset done
])
def test_a_completed_cleaning_cycle_dates_last_clean_on_either_transport(mqtt_name, http_code):
    for event_type in (mqtt_name, http_code):
        d = _device()
        apply_derived_state(d, event_type, {})
        assert d.state.get("lastClean"), f"{event_type} did not set lastClean"


@pytest.mark.parametrize("event_type", ["8", "11", "17", "21"])
def test_the_other_cleaning_completions_do_not_claim_the_box_was_cleaned(event_type):
    """Deodorizing, sand correction, the LED illuminator and a consumable reset
    all sit in the cleaning/done bucket. Dating Last Clean from one of them
    would report a cleaning cycle that never ran."""
    d = _device()
    apply_derived_state(d, event_type, {})
    assert "lastClean" not in d.state


@pytest.mark.parametrize("event_type", ["pet_out", "10"])
def test_a_finished_visit_dates_last_visit_and_records_the_weight(event_type):
    d = _device()
    apply_derived_state(d, event_type, {"pet_weight": 4200})
    assert d.state.get("lastVisit")
    assert d.state.get("petWeight") == 4200


@pytest.mark.parametrize("event_type", ["pet_in", "9"])
def test_entering_the_box_is_not_a_finished_visit(event_type):
    """`pet_in` and the weight-check step are parts of a visit, not its end."""
    d = _device()
    apply_derived_state(d, event_type, {"pet_weight": 4200})
    assert "lastVisit" not in d.state


def test_a_visit_without_a_weight_still_dates_the_visit():
    d = _device()
    apply_derived_state(d, "10", {})
    assert d.state.get("lastVisit")
    assert "petWeight" not in d.state


def test_a_completed_feed_dates_last_feed_on_either_transport():
    for event_type in ("feed_over", "2"):
        d = _device("d4h")
        apply_derived_state(d, event_type, {})
        assert d.state.get("lastFeed"), f"{event_type} did not set lastFeed"


def test_an_unknown_event_changes_nothing():
    d = _device()
    apply_derived_state(d, "not-a-real-event", {})
    assert d.state == {}


def test_the_code_is_read_per_device_category():
    """`2` is a completed feed on a feeder and an error on a litter box, so a
    litter box must not date Last Feed from it."""
    litter = _device("t5")
    apply_derived_state(litter, "2", {})
    assert "lastFeed" not in litter.state


# --- Times Used, on the boxes that never report their own --------------------
#
# A T4 sends `litter.usedTimes` hard-coded to zero: 209 stored state snapshots
# spanning 2026-08-16..29 carry that value and no other, against 41 completed
# visits in the same window. So the field is dropped on that model and the
# counter is derived from the visits, the way PetKit's own service derives it.


@pytest.mark.parametrize("event_type", ["pet_out", "10"])
def test_a_finished_visit_counts_towards_times_used_on_either_transport(event_type):
    d = _device("t4")
    apply_derived_state(d, event_type, {"pet_weight": 4200})
    assert d.state["usedTimes"] == 1
    apply_derived_state(d, event_type, {"pet_weight": 4100})
    assert d.state["usedTimes"] == 2


@pytest.mark.parametrize("event_type", ["pet_in", "9"])
def test_a_visit_step_does_not_count_as_a_use(event_type):
    """One visit stores several rows — the entry and the mid-visit weight
    checks share the toilet kind with the summary. Counting those would report
    three uses for every one."""
    d = _device("t4")
    apply_derived_state(d, event_type, {"pet_weight": 4200})
    assert "usedTimes" not in d.state


def test_the_count_starts_over_on_the_next_local_day():
    """The rollover is carried by the day key stored beside the count, not by a
    scheduled reset — nothing has to be running at midnight for it to happen."""
    d = _device("t4")
    yesterday = time.time() - 24 * 3600
    apply_derived_state(d, "10", {"time_in": yesterday})
    apply_derived_state(d, "10", {"time_in": yesterday})
    assert d.state["usedTimes"] == 2
    apply_derived_state(d, "10", {"time_in": time.time()})
    assert d.state["usedTimes"] == 1


def test_a_box_that_keeps_its_own_counter_is_left_alone():
    """A real T5 reports `usedTimes: 3`. Ours must never overwrite the
    device's own number with a count of what we happened to observe."""
    d = _device("t5")
    d.state["usedTimes"] = 3
    apply_derived_state(d, "10", {"pet_weight": 4200})
    assert d.state["usedTimes"] == 3
    assert "usedTimesDay" not in d.state


def test_a_state_report_cannot_reset_the_count_it_does_not_carry():
    """The whole point. A T4 heartbeats every ~11s and every one of those
    reports says `usedTimes: 0`; before the suppression, the derived count
    survived for exactly one heartbeat."""
    from petkit_local.events.ingest import apply_state_snapshot

    d = _device("t4")
    apply_derived_state(d, "10", {"pet_weight": 4200})
    assert d.state["usedTimes"] == 1
    # The exact nested shape a real T4 posts, zero and all.
    apply_state_snapshot(d, {"litter": {"weight": 3119, "usedTimes": 0,
                                        "percent": 100, "sandType": 1}})
    assert d.state["usedTimes"] == 1
    # And again for a hypothetical firmware that also states it flat, which the
    # unfiltered raw merge would otherwise let through.
    apply_state_snapshot(d, {"usedTimes": 0, "sandPercent": 100})
    assert d.state["usedTimes"] == 1


def test_a_camera_box_still_takes_the_count_from_its_report():
    from petkit_local.events.ingest import apply_state_snapshot

    d = _device("t5")
    apply_state_snapshot(d, {"litter": {"weight": 5469, "usedTimes": 3,
                                        "percent": 40, "sandType": 1}})
    assert d.state["usedTimes"] == 3


# --- end to end over HTTP ---------------------------------------------------

CONFIG = {"api_url": "http://server/6/", "mqtt_port": 1883, "proxy_mode": False,
          "proxy_upstream": "", "proxy_block_run_cmd": True, "capture": False}
HDR = {"X-Device": "id=100&sn=SN100"}


async def test_an_http_event_report_populates_the_derived_sensors():
    """The whole point: a device that only ever speaks HTTP gets these too."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = DeviceRegistry()
        device = reg.get_or_create(petkit_id=100, device_type="t5", serial_number="SN100")
        app = create_app(reg, dict(CONFIG))
        app["event_store"] = EventStore(Path(tmp) / "petkit.db")
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            await client.post("/6/t5/dev_event_report", headers=HDR,
                              data={"event_type": "10", "event_id": "e1",
                                    "content": '{"pet_weight": 3900}'})
            assert device.state.get("lastVisit")
            assert device.state.get("petWeight") == 3900

            await client.post("/6/t5/dev_event_report", headers=HDR,
                              data={"event_type": "5", "event_id": "e2", "content": "{}"})
            assert device.state.get("lastClean")
        finally:
            await client.close()
            await app["event_store"].close()


def test_an_mqtt_event_applies_the_state_snapshot_it_carries():
    """The N60 reset that exposed this: the device announced a changed
    sprayResetTime only inside liquid_reset_over, and the property stream was
    silent for 74 minutes either side, so dropping the snapshot meant HA showed
    a stale countdown for over an hour."""
    from petkit_local.devices.base import Device
    from petkit_local.events.ingest import apply_state_snapshot

    from petkit_local.devices.state_parsers import SPRAY_TOTAL_DAYS

    dev = Device(device_type="t5", petkit_id=1, serial_number="SN")
    # Relative stamps, so the test cannot rot as real dates recede.
    dev.state["sprayResetTime"] = int(time.time() - 14.5 * 86400)   # pre-reset
    assert apply_state_snapshot(dev, json.dumps(dev.state)) is True
    assert dev.state["sprayLeftDays"] == 31                        # ceil(45 - 14.5)

    reset_at = int(time.time())
    # exactly the shape params.state arrives in: a JSON *string*
    snapshot = json.dumps({"sprayResetTime": reset_at, "liquidReset": 0,
                           "litter": {"percent": 42}})

    assert apply_state_snapshot(dev, snapshot) is True
    # The raw stamp must move too, not just the derived key: to_device_info
    # echoes it back to the device.
    assert dev.state["sprayResetTime"] == reset_at
    assert dev.state["sandPercent"] == 42              # went through the parsers
    # Countdown restarted to the full interval: the countdown rounds up, so a
    # cartridge replaced a moment ago reads 45 rather than 44.
    assert dev.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS
    assert dev.last_state_report > 0


def test_a_state_snapshot_that_is_junk_is_skipped_not_raised():
    # Device input never raises: an event carrying an undecodable snapshot is
    # still worth recording, so this reports False instead of failing.
    from petkit_local.devices.base import Device
    from petkit_local.events.ingest import apply_state_snapshot

    dev = Device(device_type="t5", petkit_id=1, serial_number="SN")
    for junk in ("not json", "", None, "[1,2,3]", "null", {}, 7):
        assert apply_state_snapshot(dev, junk) is False
    assert dev.last_state_report == 0.0
