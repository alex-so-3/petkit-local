"""Schedules: what `dev_multi_config` serves, and what the panel's editor writes.

Two things this covers that nothing did before. `to_multi_config` used to build
its answer from literals and read no stored value at all, so every device was
handed the same made-up light period and quiet hours on every poll — and a
schedule set through the app in proxy mode was undone by the next one. And a
litter box's `schedule` array carries BOTH its cleaning and its deodorizing
times, told apart only by `type`, so an editor that rewrites its own section has
to keep the other one.
"""
import json
import time

import pytest

from aiohttp.test_utils import TestClient, TestServer

from petkit_local.devices import defaults, payloads
from petkit_local.devices.base import Device, encode_multi_range
from petkit_local.devices.ble import BLERegistry
from petkit_local.devices.registry import DeviceRegistry
from petkit_local.web.hub import EventHub
from petkit_local.web.panel import create_panel_app


def _decode(reply, key):
    """Unwrap one `dev_multi_config` value: a JSON string wrapping its own key."""
    return json.loads(reply["result"][key])[key]


# --- to_multi_config -------------------------------------------------------

def test_the_encoding_wraps_the_key_twice():
    """`"{\\"distrubMultiRange\\":[[1425,585]]}"` — the shape the real cloud
    sends in both directions, captured on a T5 on 2026-08-09."""
    assert encode_multi_range("distrubMultiRange", [[1425, 585]]) == \
        '{"distrubMultiRange":[[1425,585]]}'


def test_a_stored_range_is_what_the_device_is_served():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    assert _decode(payloads.to_multi_config(d), "distrubMultiRange") == []

    d.config.setdefault("multi_config", {})["distrubMultiRange"] = [[1425, 585], [0, 1]]
    assert _decode(payloads.to_multi_config(d), "distrubMultiRange") == [[1425, 585], [0, 1]]
    # Storing one schedule must not touch the others.
    assert _decode(payloads.to_multi_config(d), "lightMultiRange") == [[0, 1440]]


def test_an_unset_range_restricts_nothing():
    """These used to fall back to quiet hours — 00:40-08:40 on every litter box,
    22:00-06:00 for its voice prompts — windows nobody chose, pushed on every
    poll and undoing anything set through PetKit's app in proxy mode.

    The default is now the whole day, which is not the same kind of value: it
    means "always", so it takes no decision away from the owner."""
    for device_type in ("t3", "t5", "d4h"):
        d = Device(device_type=device_type, petkit_id=1, serial_number="SN")
        for key, raw in payloads.to_multi_config(d)["result"].items():
            value = json.loads(raw)[key]
            if key == "distrubMultiRange":
                continue
            times = value[0]["time"] if value and isinstance(value[0], dict) else value
            assert times == [[0, 1440]], f"{device_type}/{key} is not all day"


#: The windows that SILENCE a job rather than enable one. An all-day default
#: here would quietly stop the thing from ever running, which is the opposite
#: of harmless -- so these are the entries that stay empty.
SILENCING_RANGES = {"distrubMultiRange", "awDisturbMultiRange", "wlDisturbMultiRange"}


def test_only_the_silencing_windows_default_to_empty():
    """Every other default is a window during which something is ACTIVE, so all
    day means "always" and restricts nothing. A do-not-disturb window is the
    other way round: all day would disable automatic cleaning on every litter
    box, and water top-up on every fountain, that nobody had given a window to.
    """
    from petkit_local.devices.defaults import MULTI_RANGE_DEFAULTS

    for key, value in MULTI_RANGE_DEFAULTS.items():
        if key in SILENCING_RANGES:
            assert value == [], f"{key} must not decide quiet hours for anyone"
        else:
            assert value, f"{key} must have an all-day default"

    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    assert _decode(payloads.to_multi_config(d), "distrubMultiRange") == []


def test_a_default_is_copied_not_shared():
    """The panel hands these straight to an editor that mutates them in place.
    A shared literal would let one device's edit change every other device's
    default for the life of the process."""
    a = defaults.multi_config_ranges(Device(device_type="t5", petkit_id=1, serial_number="SN"))
    a["lightMultiRange"][0][1] = 60
    b = defaults.multi_config_ranges(Device(device_type="t5", petkit_id=2, serial_number="SN2"))
    assert b["lightMultiRange"] == [[0, 1440]]


def test_an_unstored_range_still_answers_with_a_well_formed_body():
    """The firmware reads a 4xx or a broken body as a server fault and retries
    forever, so "nothing set" has to be an empty schedule and not a missing
    key or an error."""
    for device_type in ("t3", "t5", "d4h"):
        d = Device(device_type=device_type, petkit_id=1, serial_number="SN")
        result = payloads.to_multi_config(d)["result"]
        assert result, f"{device_type} served no keys at all"
        for key, raw in result.items():
            assert json.loads(raw).keys() == {key}


def test_a_malformed_stored_range_is_ignored_not_served():
    """`devices.json` is hand-editable and the firmware is not the place to find
    out that somebody typed a string where a list goes."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config["multi_config"] = {"lightMultiRange": "08:00-20:00"}
    assert _decode(payloads.to_multi_config(d), "lightMultiRange") == [[0, 1440]]

    d.config["multi_config"] = "not even a dict"
    assert _decode(payloads.to_multi_config(d), "lightMultiRange") == [[0, 1440]]


def test_the_multi_config_shape_is_unchanged():
    """Every value is a JSON STRING that wraps its own key, and the misspelling
    is PetKit's — correcting it drops the do-not-disturb schedule silently."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    result = payloads.to_multi_config(d)["result"]
    assert "distrubMultiRange" in result and "disturbMultiRange" not in result
    for key, raw in result.items():
        assert isinstance(raw, str)
        assert json.loads(raw).keys() == {key}


def test_the_editor_and_the_device_see_the_same_values():
    """The panel renders `schedule_targets`, the device is served
    `to_multi_config`, and both come from `multi_config_ranges`. Two sources
    would let the panel show a period the box is not running."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config.setdefault("multi_config", {})["toneMultiRange"] = [[1140, 360]]
    served = {k: _decode(payloads.to_multi_config(d), k)
              for k in payloads.to_multi_config(d)["result"]}
    for target in defaults.schedule_targets(d):
        if target["kind"] in ("points", "feed"):
            continue
        assert target["value"] == served[target["target"]], target["target"]


def test_a_fountain_is_served_the_ranges_its_firmware_reads():
    """Nine exist in the W7-262863 image and seven are sent.

    Five are confirmed by watching PetKit's own cloud write them to a W7H; the
    other two default to something that restricts nothing. The two
    `*AssistMultiRange` fields are held back on purpose -- real fields, but no
    capture shows a value, and this reply is re-sent on every poll, so an
    invented window would overwrite the owner's on repeat.
    """
    d = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    served = payloads.to_multi_config(d)["result"]
    assert set(served) == {
        "lightMultiRange", "toneMultiRange", "distrubMultiRange",
        "detectMultiRange", "cameraMultiRange",
        "awDisturbMultiRange", "wlDisturbMultiRange",
    }
    assert "lightAssistMultiRange" not in served
    assert "wifiLightAssistMultiRange" not in served
    # The camera-gating field is the object form here too -- W7H has no
    # `cameraMultiNew` at all, so this is the one that decides.
    assert isinstance(_decode(payloads.to_multi_config(d), "cameraMultiRange")[0], dict)
    # And the editor now has something to offer.
    assert {t["target"] for t in defaults.schedule_targets(d)} == set(served)


def test_every_model_offers_only_schedules_it_has():
    litter = defaults.schedule_targets(Device(device_type="t5", petkit_id=1, serial_number="SN"))
    kinds = {t["target"]: t["kind"] for t in litter}
    assert kinds["schedule"] == "points"
    assert kinds["cameraMultiRange"] == "weekly"
    assert kinds["distrubMultiRange"] == "ranges"

    plain = defaults.schedule_targets(Device(device_type="t3", petkit_id=1, serial_number="SN"))
    assert "cameraMultiRange" not in {t["target"] for t in plain}

    feeder = defaults.schedule_targets(Device(device_type="d4sh", petkit_id=1, serial_number="SN"))
    assert "feed_schedule" in {t["target"] for t in feeder}
    assert "schedule" not in {t["target"] for t in feeder}


# --- the panel's write path ------------------------------------------------

class FakeBridge:
    def __init__(self, connected=True):
        self._client = object() if connected else None
        self.sent = []

    async def publish_to_device(self, device, suffix, payload):
        self.sent.append((device.petkit_id, suffix, payload))


def _panel(device_type="t5"):
    reg = DeviceRegistry()
    reg.get_or_create(petkit_id=1, device_type=device_type, serial_number="SN")
    reg.get(1).mqtt_connected = True
    bridge = FakeBridge()
    app = create_panel_app(reg, BLERegistry(), EventHub(),
                           {"api_url": "http://x/6/", "mqtt_tls": True,
                            "mqtt_tls_port": 443, "capture": False,
                            "capture_dir": "/nope"},
                           bridge)
    return app, reg, bridge


async def _client(app):
    c = TestClient(TestServer(app))
    await c.start_server()
    return c


async def _save(c, target, value):
    r = await c.post("/api/devices/1/schedule",
                     data=json.dumps({"target": target, "value": value}))
    return r.status, await r.json()


async def test_saving_a_range_stores_it_and_pushes_the_doubled_encoding():
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        status, out = await _save(c, "distrubMultiRange", [[1425, 585], [0, 1]])
        assert status == 200 and out["ok"]

        assert reg.get(1).config["multi_config"]["distrubMultiRange"] == [[1425, 585], [0, 1]]
        _did, suffix, envelope = bridge.sent[0]
        assert suffix == "property/set"
        assert envelope["params"] == {
            "distrubMultiRange": '{"distrubMultiRange":[[1425,585],[0,1]]}'}
    finally:
        await c.close()


async def test_saving_the_schedule_pushes_a_plain_string_not_a_wrapped_one():
    """`schedule` travels the same way and is NOT the same shape: a plain JSON
    string of the array, with no wrapping key. Captured on a T5."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        entries = [{"id": 103382, "repeats": "1,2,3,4,5,6,7", "time": 585, "type": 0}]
        status, out = await _save(c, "schedule", entries)
        assert status == 200 and out["ok"]

        _did, _suffix, envelope = bridge.sent[0]
        assert envelope["params"]["schedule"] == json.dumps(entries, separators=(",", ":"))
        assert json.loads(envelope["params"]["schedule"]) == entries
        assert reg.get(1).config["schedule"] == entries
    finally:
        await c.close()


async def test_both_jobs_survive_a_write_to_the_schedule_array():
    """One array holds the cleaning AND the deodorizing times. Whatever the
    editor sends is stored whole, so a section that dropped the other job's
    rows would be visible here."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        entries = [
            {"id": 1, "repeats": "1,2,3,4,5,6,7", "time": 585, "type": 0},
            {"id": 2, "repeats": "1,3,4,5,6,7", "time": 890, "type": 1},
        ]
        await _save(c, "schedule", entries)
        stored = reg.get(1).config["schedule"]
        assert [e["type"] for e in stored] == [0, 1]
        assert stored[1]["repeats"] == "1,3,4,5,6,7"  # every day but Monday
    finally:
        await c.close()


async def test_an_entry_with_an_unknown_type_is_kept():
    """An editor grouping by `type` must not delete what it cannot group."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        await _save(c, "schedule",
                    [{"id": 9, "repeats": "1", "time": 60, "type": 7}])
        assert reg.get(1).config["schedule"][0]["type"] == 7
    finally:
        await c.close()


@pytest.mark.parametrize("bad", [
    "not a list",
    [[0]],                    # not a pair
    [[0, 1441]],              # past the end of the day
    [[-1, 60]],
    [["08:00", "20:00"]],     # clocks, not minutes
    [{"start": 0, "end": 60}],
])
async def test_a_malformed_range_is_refused_and_stores_nothing(bad):
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        status, out = await _save(c, "lightMultiRange", bad)
        assert status == 400, out
        assert "multi_config" not in reg.get(1).config
        assert not bridge.sent
    finally:
        await c.close()


async def test_the_odd_looking_ranges_the_app_itself_writes_are_accepted():
    """A window that ends before it starts crosses midnight; a one-minute
    window came straight out of the app; several at once is what the captured
    do-not-disturb payload contained. Nothing may tidy these away."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        status, _ = await _save(c, "lightMultiRange", [[1425, 585], [0, 1], [0, 1440]])
        assert status == 200
        assert reg.get(1).config["multi_config"]["lightMultiRange"] == \
            [[1425, 585], [0, 1], [0, 1440]]
    finally:
        await c.close()


async def test_a_weekly_entry_is_rebuilt_rather_than_passed_through():
    """`rpt` is split on commas by the firmware, so it is reassembled from
    parsed weekday numbers instead of being forwarded as somebody's string."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        status, _ = await _save(c, "cameraMultiRange", [
            {"enable": 1, "rpt": " 7 , 2 , 2 ", "time": [[480, 1200]]}])
        assert status == 200
        stored = reg.get(1).config["multi_config"]["cameraMultiRange"]
        assert stored == [{"enable": 1, "rpt": "2,7", "time": [[480, 1200]]}]
    finally:
        await c.close()


@pytest.mark.parametrize("bad", [
    [{"enable": 1, "rpt": "0", "time": [[0, 60]]}],     # weekdays run 1..7
    [{"enable": 1, "rpt": "8", "time": [[0, 60]]}],
    [{"enable": 1, "rpt": "", "time": [[0, 60]]}],      # no day at all
    [{"enable": 1, "rpt": "mon", "time": [[0, 60]]}],
    [{"enable": 1, "rpt": "1", "time": "all day"}],
])
async def test_a_malformed_weekly_entry_is_refused(bad):
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        status, _ = await _save(c, "cameraMultiRange", bad)
        assert status == 400
    finally:
        await c.close()


async def test_an_unknown_target_is_refused():
    """The list comes from `schedule_targets`, so the panel cannot invent a
    settings field to write."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        status, out = await _save(c, "somethingMultiRange", [[0, 60]])
        assert status == 400 and "unknown schedule" in out["error"]
        # And a schedule a DIFFERENT model has is just as unknown.
        status, _ = await _save(c, "awDisturbMultiRange", [[0, 60]])
        assert status == 400
    finally:
        await c.close()


async def test_a_feeding_schedule_is_stored_and_pushed():
    """The cloud pushes feed schedules via property.set{feed: "<json>"},
    confirmed in a D4SH MQTT capture (2026-08-12)."""
    app, reg, bridge = _panel("d4sh")
    c = await _client(app)
    try:
        payload = {"schedule": [{"re": "1,2,3,4,5,6,7", "it": [], "itemJsonString": "[]"}],
                   "nextTick": 0, "latest": []}
        status, out = await _save(c, "feed_schedule", payload)
        assert status == 200
        # Stored as sent, plus the v:2 stamp that marks it seconds-based
        # (feed.migrate_minute_schedule).
        assert reg.get(1).config["feed_schedule"] == {**payload, "v": 2}
        assert bridge.sent
        _did, suffix, envelope = bridge.sent[0]
        assert suffix == "property/set"
        wire = json.loads(envelope["params"]["feed"])
        # The wire shape is the cloud's: no itemJsonString in property.set,
        # and an empty latest carries the cloud's 86340 constant.
        assert set(wire.keys()) == {"schedule", "nextTick", "latest"}
        assert all("itemJsonString" not in g for g in wire["schedule"])
        assert wire["nextTick"] == 86340 and wire["latest"] == []
    finally:
        await c.close()


async def test_a_meal_id_is_regenerated_when_not_a_string():
    """The firmware reads `id` with cJSON_GetStringValue — an int id is a NULL
    pointer and a crash. Anything that is not a non-empty string comes back as
    the cloud's own `n_<seconds>` scheme."""
    app, reg, bridge = _panel("d4sh")
    c = await _client(app)
    try:
        payload = {"schedule": [{"re": "1", "it": [
            {"id": 1, "t": 46560, "a1": 1, "a2": 0},
            {"t": 50100, "a1": 1, "a2": 0},
        ]}]}
        status, _ = await _save(c, "feed_schedule", payload)
        assert status == 200
        meals = reg.get(1).config["feed_schedule"]["schedule"][0]["it"]
        assert [m["id"] for m in meals] == ["n_46560", "n_50100"]
    finally:
        await c.close()


async def test_the_device_detail_carries_the_schedules():
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        detail = await (await c.get("/api/devices/1")).json()
        targets = {t["target"]: t for t in detail["schedules"]}
        assert targets["schedule"]["kind"] == "points"
        assert targets["distrubMultiRange"]["value"] == []
        assert targets["lightMultiRange"]["value"] == [[0, 1440]]
    finally:
        await c.close()


async def test_a_saved_schedule_reaches_dev_multi_config():
    """End to end: what the panel writes is what the device is answered with on
    its next poll, which is the whole point of storing it."""
    app, reg, bridge = _panel()
    c = await _client(app)
    try:
        await _save(c, "lightMultiRange", [[417, 117]])
        assert _decode(payloads.to_multi_config(reg.get(1)), "lightMultiRange") == [[417, 117]]
    finally:
        await c.close()


def test_there_is_no_default_cleaning_schedule():
    """This add-on used to hand any box that had not been given one three
    cleanings a day — 09:45, 13:45, 18:45 — so it ran somebody's litter box on a
    timetable they never chose and could not see anywhere.

    The times were not even invented: PetKit's cloud was later captured sending
    exactly these to a T5. That makes them a real account's schedule, which is
    not the same thing as this account's."""
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    assert not d.config.get("schedule")

    target = next(t for t in defaults.schedule_targets(d) if t["target"] == "schedule")
    assert target["value"] == []

    d.config["schedule"] = [{"id": 9, "repeats": "1", "time": 60, "type": 1}]
    target = next(t for t in defaults.schedule_targets(d) if t["target"] == "schedule")
    assert target["value"] == d.config["schedule"]


# --- the feeder's own shape ------------------------------------------------

async def test_a_feeding_schedule_is_stored_in_the_shape_the_firmware_parses():
    """`pk_schmg_parse_schedule` in a D4SH 867 `ctrl` reads `re` and `it` per
    group and `id`/`t`/`a1`/`a2` per meal — its own log line calls those
    `repeats`, `time`, `amount_l` and `amount_r`. `it` was empty in every
    capture we have, which is why the shape came out of the binary."""
    app, reg, bridge = _panel("d4sh")
    c = await _client(app)
    try:
        status, out = await _save(c, "feed_schedule", {"schedule": [
            {"re": "1,2,3,4,5,6,7",
             "it": [{"id": 1, "t": 510, "a1": 1, "a2": 3}]},
        ]})
        assert status == 200

        group = reg.get(1).config["feed_schedule"]["schedule"][0]
        assert sorted(group["it"][0]) == ["a1", "a2", "id", "t"]
        assert group["it"][0]["t"] == 510  # 08:30, minutes since midnight
        assert bridge.sent
        _did, suffix, envelope = bridge.sent[0]
        assert suffix == "property/set"
        assert "feed" in envelope["params"]
    finally:
        await c.close()


async def test_the_item_json_string_is_rebuilt_rather_than_trusted():
    """The real cloud sends the meals twice — once as `it`, once as a JSON
    string beside it. Two copies of one value in one payload is the pair that
    drifts, so the string is derived here and whatever the client sent for it is
    discarded."""
    app, reg, bridge = _panel("d4sh")
    c = await _client(app)
    try:
        await _save(c, "feed_schedule", {"schedule": [
            {"re": "1", "it": [{"id": 1, "t": 60, "a1": 2, "a2": 0}],
             "itemJsonString": "[]"},
        ]})
        group = reg.get(1).config["feed_schedule"]["schedule"][0]
        assert json.loads(group["itemJsonString"]) == group["it"]
    finally:
        await c.close()


async def test_a_feeding_schedule_keeps_the_devices_own_bookkeeping():
    """`nextTick` and `latest` are the device's, not ours."""
    app, reg, bridge = _panel("d4sh")
    c = await _client(app)
    try:
        await _save(c, "feed_schedule",
                    {"schedule": [], "nextTick": 99, "latest": [{"id": 7}]})
        stored = reg.get(1).config["feed_schedule"]
        assert stored["nextTick"] == 99 and stored["latest"] == [{"id": 7}]
    finally:
        await c.close()


@pytest.mark.parametrize("bad", [
    {"schedule": "not a list"},
    [],
    {"schedule": [{"re": "8", "it": []}]},                          # weekdays run 1..7
    {"schedule": [{"re": "1", "it": [{"id": 1, "t": 86400, "a1": 1}]}]},  # past the day
    {"schedule": [{"re": "1", "it": [{"id": 1, "t": 60, "a1": 256}]}]},   # wraps a byte
    {"schedule": [{"re": "1", "it": [{"id": 1, "t": 60, "a1": -1}]}]},
])
async def test_a_malformed_feeding_schedule_is_refused(bad):
    app, reg, bridge = _panel("d4sh")
    c = await _client(app)
    try:
        status, _ = await _save(c, "feed_schedule", bad)
        assert status == 400
        assert "feed_schedule" not in reg.get(1).config
    finally:
        await c.close()


# --- the single-hopper feeder (D4H/D4H2) ------------------------------------
# A single hopper's `pk_schmg_parse_schedule` reads one `a` per meal, not the
# Dual-Hopper's a1/a2 (events/codes.py::FEED_SCHEDULE_ITEM_KEYS). Sent a1/a2 it
# runs the feed and dispenses nothing (err_code 8). So on a D4H the stored meal,
# the itemJsonString and the pushed latest must all carry `a`.

async def test_a_single_hopper_schedule_is_stored_and_pushed_with_a():
    app, reg, bridge = _panel("d4h")
    c = await _client(app)
    try:
        now = time.time()
        lt = time.localtime(now)
        secs = lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
        future_t = (secs + 7200) % 86400
        status, _ = await _save(c, "feed_schedule", {"schedule": [
            {"re": "1,2,3,4,5,6,7", "it": [{"id": 1, "t": future_t, "a": 20}]},
        ]})
        assert status == 200

        group = reg.get(1).config["feed_schedule"]["schedule"][0]
        assert sorted(group["it"][0]) == ["a", "id", "t"]
        assert group["it"][0]["a"] == 20
        assert json.loads(group["itemJsonString"]) == group["it"]
        assert '"a":20' in group["itemJsonString"]

        _did, suffix, envelope = bridge.sent[0]
        assert suffix == "property/set"
        wire = json.loads(envelope["params"]["feed"])
        assert wire["latest"]
        assert all(e["a"] == 20 and "a1" not in e for e in wire["latest"])
    finally:
        await c.close()


async def test_a_single_hopper_meal_migrates_a_legacy_a1_to_a():
    """A schedule the dual-only editor saved carries a1; on a single hopper it
    is read as `a` rather than silently emptied — but never the other way."""
    app, reg, bridge = _panel("d4h")
    c = await _client(app)
    try:
        status, _ = await _save(c, "feed_schedule", {"schedule": [
            {"re": "1", "it": [{"id": 1, "t": 510, "a1": 3}]},
        ]})
        assert status == 200
        meal = reg.get(1).config["feed_schedule"]["schedule"][0]["it"][0]
        assert meal == {"id": "n_510", "t": 510, "a": 3}
    finally:
        await c.close()


async def test_a_single_hopper_deferred_feed_stores_a():
    app, reg, bridge = _panel("d4h")
    c = await _client(app)
    try:
        r = await c.post("/api/devices/1/deferred-feed",
                         data=json.dumps({"date": "2099-06-01", "time": "12:00", "a": 20}))
        assert r.status == 200
        out = await r.json()
        assert out["feed"]["a"] == 20 and "a1" not in out["feed"]
        stored = reg.get(1).config["feed_schedule"]["deferred"][0]
        assert stored["a"] == 20 and "a1" not in stored
    finally:
        await c.close()


@pytest.mark.parametrize("bad", [
    {"schedule": [{"re": "1", "it": [{"id": 1, "t": 60, "a": 256}]}]},  # wraps a byte
    {"schedule": [{"re": "1", "it": [{"id": 1, "t": 60, "a": -1}]}]},
    {"schedule": [{"re": "1", "it": [{"id": 1, "t": 60}]}]},            # no amount at all
])
async def test_a_malformed_single_hopper_schedule_is_refused(bad):
    app, reg, bridge = _panel("d4h")
    c = await _client(app)
    try:
        status, _ = await _save(c, "feed_schedule", bad)
        assert status == 400
        assert "feed_schedule" not in reg.get(1).config
    finally:
        await c.close()


def test_only_a_dual_hopper_is_offered_two_portions():
    """A meal carries `a1` and `a2`, and every feeder but the Dual-Hopper reads
    one hopper. The panel is told which so it does not draw a control for a
    hopper that is not there."""
    dual = Device(device_type="d4sh", petkit_id=1, serial_number="SN")
    single = Device(device_type="d4h", petkit_id=2, serial_number="SN2")
    esp32 = Device(device_type="d4", petkit_id=3, serial_number="SN3")

    for device, expected in ((dual, True), (single, False), (esp32, False)):
        target = next(t for t in defaults.schedule_targets(device) if t["kind"] == "feed")
        assert target["dual"] is expected, device.device_type
        # And every feeder gets the object shape, so the editor has something
        # to render before any meal exists.
        assert target["value"] == {"schedule": []}
