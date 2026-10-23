"""The DST push: a device whose offset comes from its zone name is told when
that zone's offset changes (`ha/timezone_sync.py`).

The device has no DST of its own — it holds one number and burns it into its
video watermarks — so before this, a T5 in Europe/Rome kept +2 through the
whole winter unless somebody POSTed an override. Every instant here is a fixed
epoch either side of a real transition, so nothing depends on today's date.
"""
import asyncio
from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices.base import TIMEZONE_PUSHED_KEY
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.ha.timezone_sync import (
    sync_timezones, timezone_push_due, timezone_sync_loop,
)
from petkit_local.http.server import create_app
from petkit_local.web.hub import EventHub

# Europe/Rome leaves CEST at 2026-10-25 01:00 UTC (03:00 local -> 02:00).
BEFORE_DST_END = datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc).timestamp()   # +2
AFTER_DST_END = datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc).timestamp()    # +1
A_WEEK_LATER = AFTER_DST_END + 7 * 86400
# ...and enters it again at 2027-03-28 01:00 UTC.
AFTER_DST_START = datetime(2027, 3, 28, 1, 30, tzinfo=timezone.utc).timestamp()   # +2


class FakeBridge:
    """The two things `push_timezone` touches on the real bridge."""

    def __init__(self, connected: bool = True, fail: bool = False) -> None:
        self._client = object() if connected else None
        self.fail = fail
        self.sent: list[tuple[int, str, dict]] = []

    async def publish_to_device(self, device, suffix, payload):
        if self.fail:
            raise ConnectionError("broker went away")
        self.sent.append((device.petkit_id, suffix, payload))


def _registry(locale="Europe/Rome", reported=2.0, mqtt=True, device_type="t5"):
    reg = DeviceRegistry()
    d = reg.get_or_create(petkit_id=1, device_type=device_type, serial_number="SN")
    if locale is not None:
        d.config["locale"] = locale
    if reported is not None:
        d.config["reported_timezone"] = reported
    d.mqtt_connected = mqtt
    return reg, d


def _sent_values(bridge: FakeBridge) -> list:
    return [env["params"]["timezone"] for _, _, env in bridge.sent]


async def test_the_end_of_DST_pushes_exactly_once_and_as_a_string():
    """The case observed on a live T5 running 2.1.0 in Europe/Rome."""
    reg, d = _registry()
    bridge = FakeBridge()
    hub = EventHub()

    # Summer: the zone and the device agree, nothing to say.
    assert await sync_timezones(reg, bridge, hub, BEFORE_DST_END) == 0
    assert bridge.sent == []

    # 03:00 becomes 02:00: one push, the new offset, as a JSON STRING —
    # a number is a silent no-op in `parse_recv_property_set_normal`.
    assert await sync_timezones(reg, bridge, hub, AFTER_DST_END) == 1
    (_, suffix, envelope), = bridge.sent
    assert suffix == "property/set"
    assert envelope["method"] == "thing.service.property.set"
    assert envelope["params"] == {"timezone": "1"}
    assert isinstance(envelope["params"]["timezone"], str)
    assert d.config[TIMEZONE_PUSHED_KEY] == 1.0

    # Every later pass in the same half of the year is silent — including one
    # after the device has echoed the new value back in a property/post.
    await sync_timezones(reg, bridge, hub, AFTER_DST_END + 300)
    d.state["timezone"] = 1.0
    await sync_timezones(reg, bridge, hub, A_WEEK_LATER)
    assert len(bridge.sent) == 1

    # And spring sends it back the other way, once.
    await sync_timezones(reg, bridge, hub, AFTER_DST_START)
    await sync_timezones(reg, bridge, hub, AFTER_DST_START + 300)
    assert _sent_values(bridge) == ["1", "2"]


async def test_nothing_is_sent_while_the_offset_is_unchanged():
    reg, d = _registry(reported=2.0)
    bridge = FakeBridge()
    for when in (BEFORE_DST_END, BEFORE_DST_END - 3600, BEFORE_DST_END - 86400):
        assert await sync_timezones(reg, bridge, EventHub(), when) == 0
    assert bridge.sent == [] and d.command_queue == []
    assert TIMEZONE_PUSHED_KEY not in d.config


async def test_a_manual_override_is_never_touched():
    """An override means "this box is not where its locale says"; the operator
    owns that number, DST or not."""
    reg, d = _registry(reported=2.0)
    d.config["timezone"] = 2.0
    bridge = FakeBridge()
    assert timezone_push_due(d, AFTER_DST_END) is None
    assert await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END) == 0
    assert bridge.sent == [] and d.command_queue == []


async def test_a_lossy_readback_does_not_cause_repeated_pushes():
    """The device reads its offset back as `%.1f`, so `5.75` returns as `5.8`.
    Comparing the zone against that alone would push Kathmandu forever."""
    # A box given only a locale reports 0 — the D4SH case — so it IS pushed once.
    reg, d = _registry(locale="Asia/Kathmandu", reported=0.0)
    bridge = FakeBridge()
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END)
    assert _sent_values(bridge) == ["5.75"]

    d.state["timezone"] = 5.8          # the echo, ~30s later
    for i in range(1, 6):
        await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END + i * 300)
    assert len(bridge.sent) == 1

    # Same when the device's own word is all there is: a reboot re-signs it up,
    # which forgets the push, and it states the value it holds in its own lossy
    # form. 5.8 IS 5.75 here, so nothing goes out.
    del d.config[TIMEZONE_PUSHED_KEY]
    d.config["reported_timezone"] = 5.8
    assert timezone_push_due(d, AFTER_DST_END) is None
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END + 3600)
    assert len(bridge.sent) == 1


async def test_bad_locales_and_numbers_never_raise():
    """`locale` and both numbers come from the device ("Device input never
    raises"). Anything unusable means "no zone, nothing to follow"."""
    bridge = FakeBridge()
    for locale in ("Mars/Olympus_Mons", "en-US", "", "/", "../../etc/passwd",
                   "Europe/Rome\x00", 42, 4.2, None, ["Europe/Rome"], {"tz": 1}):
        reg, d = _registry(locale=None)
        d.config["locale"] = locale
        assert timezone_push_due(d, AFTER_DST_END) is None, locale
        assert await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END) == 0
    assert bridge.sent == []

    # A garbage stored number is "unknown", not a crash: the zone is still good,
    # so the device is told the offset it should have.
    reg, d = _registry(reported=None)
    d.config["reported_timezone"] = "not a number"
    d.config[TIMEZONE_PUSHED_KEY] = {"weird": True}
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END)
    assert _sent_values(bridge) == ["1"]


async def test_a_device_on_http_gets_it_through_the_heartbeat_queue():
    """Same path as the panel's override: a feeder that never reaches the broker
    is told on its next heartbeat. Its `dev_device_info` carries the derived
    number too, but the firmware does not take its clock from that."""
    reg, d = _registry(mqtt=False, device_type="d4sh")
    bridge = FakeBridge(connected=False)
    assert await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END) == 1
    assert bridge.sent == []
    queued, = d.command_queue
    assert queued["_service_suffix"] == "property/set"
    assert queued["params"] == {"timezone": "1"}

    # Queued once, not once per pass while it waits for the device to poll.
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END + 300)
    assert len(d.command_queue) == 1


async def test_a_failed_publish_falls_back_to_the_queue_and_is_not_retried_hot():
    reg, d = _registry()
    bridge = FakeBridge(fail=True)
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END)
    assert d.command_queue[0]["params"] == {"timezone": "1"}
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END + 300)
    assert len(d.command_queue) == 1


async def test_a_new_signup_makes_the_device_be_told_again():
    """Nobody has shown that a runtime `property.set{timezone}` survives a
    reboot, and a rebooted device signs up again stating what it holds. That
    statement is newer than our last push, so it is what the zone is compared
    with — and a device that came back on the provisioned +2 is corrected."""
    reg, d = _registry(reported=2.0, device_type="d4")
    d.serial_number = "20241223G11497"
    bridge = FakeBridge()
    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END)
    assert _sent_values(bridge) == ["1"]

    app = create_app(reg, {"api_url": "http://server/6/", "mqtt_port": 1883,
                           "proxy_mode": False, "proxy_upstream": "",
                           "proxy_block_run_cmd": True})
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        body = ("hardware=1&firmware=1.267&mac=c05d89d25204&timezone=2.0"
                "&locale=Europe/Rome&id=1&sn=20241223G11497")
        r = await client.post("/6/d4/dev_signup", data=body,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert r.status == 200
    finally:
        await client.close()
    assert TIMEZONE_PUSHED_KEY not in d.config

    await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END + 300)
    assert _sent_values(bridge) == ["1", "1"]


async def test_the_panel_override_records_what_it_pushed():
    """`api_timezone` and the loop share `push_timezone`, so clearing an
    override (which pushes the locale-derived offset) leaves nothing for the
    loop to repeat."""
    from petkit_local.devices.ble import BLERegistry
    from petkit_local.web.panel import create_panel_app

    reg, d = _registry()
    bridge = FakeBridge()
    app = create_panel_app(reg, BLERegistry(), EventHub(),
                           {"api_url": "http://x/6/", "mqtt_tls": True,
                            "mqtt_tls_port": 443, "capture": False,
                            "capture_dir": "/nope"}, bridge)
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        r = await client.post("/api/devices/1/timezone", json={"timezone": 5.75})
        out = await r.json()
        assert out["delivered"] == "mqtt" and out["pushed"] == 5.75
    finally:
        await client.close()
    assert d.config[TIMEZONE_PUSHED_KEY] == 5.75
    # With the override in place the loop stays out of it.
    assert await sync_timezones(reg, bridge, EventHub(), AFTER_DST_END) == 0
    assert _sent_values(bridge) == ["5.75"]


async def test_the_loop_survives_a_failing_pass_and_keeps_going():
    reg, d = _registry()
    bridge = FakeBridge()
    calls = 0

    def clock() -> float:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("a pass that blows up")
        return AFTER_DST_END

    task = asyncio.create_task(timezone_sync_loop(reg, bridge, EventHub(),
                                                  interval=0.01, clock=clock))
    try:
        for _ in range(200):
            if bridge.sent:
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    assert calls >= 2
    assert _sent_values(bridge) == ["1"]
