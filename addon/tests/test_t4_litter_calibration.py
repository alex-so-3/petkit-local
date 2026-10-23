"""Verified T4 Finish operation; no device, cloud account or network required."""
import copy
import json
from pathlib import Path
import time

from aiohttp.test_utils import TestClient, TestServer
import pytest

from petkit_local.devices.base import Device, Refused
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.events import codes, decode
from petkit_local.events.normalize import apply_derived_state, apply_state_snapshot
from petkit_local.events.store import EventStore
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.ha.commands import ALL_ACTIONS
from petkit_local.http.server import create_app
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app


@pytest.fixture
def capture() -> dict:
    path = Path(__file__).parent / "fixtures" / "t4_litter_calibration.json"
    return json.loads(path.read_text())


def test_t4_command_and_labels_do_not_change_other_models() -> None:
    device = Device(device_type="t4", petkit_id=1, online=True)
    device.state = {"workingState": -1, "petInTime": 0}
    suffix, envelope = ALL_ACTIONS["finish_litter_calibration"](device)
    assert suffix == "start"
    assert envelope["params"] == {"start_action": 6}
    assert "finish_litter_calibration" in {e.key for e in get_entities_for_device(device)}
    status = next(e for e in get_entities_for_device(device) if e.key == "device_status")
    assert status.options[status.option_values.index(6)] == "litter calibration"
    assert codes.codes_for("t4")["18"].role == codes.ROLE_DONE
    assert codes.work_modes_for("t4")[6] == "litter calibration"
    for model in ("t3", "t5", "t6", "t7", "d4sh"):
        other = Device(device_type=model, petkit_id=2)
        assert "finish_litter_calibration" not in {e.key for e in get_entities_for_device(other)}
        with pytest.raises(Refused):
            ALL_ACTIONS["finish_litter_calibration"](other)
    assert codes.work_modes_for("t5")[6] == "reset deodorant"
    assert codes.lookup("18", "t5").role == codes.ROLE_STEP


@pytest.mark.parametrize("online,state", [
    (False, {"workingState": -1, "petInTime": 0}),
    (True, {"workingState": 0, "petInTime": 0}),
    (True, {"workingState": -1, "petInTime": 20}),
    (True, {}),
])
def test_calibration_refuses_offline_busy_occupied_or_unknown(online: bool, state: dict) -> None:
    device = Device(device_type="t4", petkit_id=1, online=online)
    device.state = state
    with pytest.raises(Refused):
        ALL_ACTIONS["finish_litter_calibration"](device)


def test_calibration_result_is_not_decoded_as_a_cleaning_termination() -> None:
    label = decode.event_label("18", {"result": 1, "start_reason": 2}, "t4")
    assert "completed" in label and "terminated" not in label
    result = next(f for f in decode.decode_content("18", {"result": 1}, "t4")
                  if f.key == "result")
    assert result.text == "completed" and result.grade == codes.CONFIRMED
    assert "terminated" in decode.event_label("5", {"result": 1}, "t4")
    assert "result 0" in decode.event_label("18", {"result": 0}, "t4")


async def test_panel_to_heartbeat_and_real_completion_survive_restart(
    capture: dict, event_store: EventStore, tmp_path: Path,
) -> None:
    """The queue is not success; only actual reports change the fill level."""
    registry = DeviceRegistry(tmp_path / "devices.json")
    device = registry.get_or_create(petkit_id=1, device_type="t4")
    device.online = True
    apply_state_snapshot(device, capture["before"])
    config = {"capture": False, "api_url": "http://localhost/6/", "data_dir": str(tmp_path)}
    hub = EventHub()
    device_app = create_app(registry, config)
    device_app["event_store"] = event_store
    panel_app = create_panel_app(registry, None, hub, config)
    api = TestClient(TestServer(device_app))
    panel = TestClient(TestServer(panel_app))
    await api.start_server()
    await panel.start_server()
    try:
        response = await panel.post("/api/devices/1/command",
                                    json={"action": "finish_litter_calibration"})
        assert response.status == 200
        assert (await response.json())["delivered"] == "heartbeat-queue"
        assert device.state["sandPercent"] == 27
        response = await api.post("/6/poll/t4/heartbeat", headers={"X-Device": "id=1&type=T4"})
        command = json.loads((await response.json())["result"][0]["content"])
        assert command["msgType"] == 2 and command["type"] == "start"
        assert command["payload"] == {"start_action": 6}
        assert device.state["sandPercent"] == 27
        base = int(time.time())
        for recorded in capture["events"]:
            frame = copy.deepcopy(recorded)
            timestamp = base + frame["timestamp"] - 1700000000
            if "start_time" in frame["content"]:
                frame["content"]["start_time"] = base + 1
            response = await api.post("/6/t4/dev_event_report", data={
                "eventType": frame["eventType"], "event_id": f"1_{base}",
                "timestamp": str(timestamp), "content": json.dumps(frame["content"]),
                "state": json.dumps(frame["state"]),
            }, headers={"X-Device": "id=1&type=T4"})
            assert response.status == 200
        assert device.state["sandPercent"] == 100 and device.state["usedTimes"] == 0
        assert device.state["workingState"] == -1
        assert "lastClean" not in device.state
        assert len(await event_store.all_events()) == 2
        await registry.flush()
        restarted = DeviceRegistry(tmp_path / "devices.json").get(1)
        assert restarted.state == {} and not restarted.online
        apply_state_snapshot(restarted, capture["events"][-1]["state"])
        assert restarted.state["workingState"] == -1
        assert restarted.state["sandPercent"] == 100
    finally:
        await panel.close()
        await api.close()
        await registry.stop()


def test_late_completion_cannot_finish_a_newer_calibration(capture: dict) -> None:
    device = Device(device_type="t4", petkit_id=1)
    now = time.time()
    apply_state_snapshot(device, capture["events"][0]["state"])
    apply_derived_state(device, "3", {"action": 6}, event_time=now)
    apply_derived_state(device, "18", {"result": 1}, event_time=now + 2)
    apply_derived_state(device, "3", {"action": 6}, event_time=now + 10)
    assert device.state["workingState"] == 6
    apply_derived_state(device, "18", {"result": 1}, event_time=now + 2)
    assert device.state["workingState"] == 6
    assert device.config["local"]["litter_calibration"]["started_at"] == now + 10


@pytest.mark.parametrize("result", [0, 1])
def test_completion_does_not_invent_full_litter(result: int) -> None:
    device = Device(device_type="t4", petkit_id=1)
    device.state = {"workingState": 6, "sandPercent": 16, "usedTimes": 71}
    apply_derived_state(device, "3", {"action": 6})
    apply_derived_state(device, "18", {"result": result})
    assert device.state["sandPercent"] == 16 and device.state["usedTimes"] == 71
    assert device.state["workingState"] == -1


@pytest.mark.parametrize("timestamp", [float("nan"), float("inf"), -1, "bad", "nan"])
def test_invalid_or_old_device_clock_does_not_update_calibration(timestamp: float | str) -> None:
    device = Device(device_type="t4", petkit_id=1)
    device.last_state_report = time.time()
    device.state = {"workingState": 6}
    apply_derived_state(device, "18", {"result": 1}, event_time=timestamp)
    assert device.state["workingState"] == 6 and "local" not in device.config
