from petkit_local.devices.base import Device
import pytest

from petkit_local.devices.registry import get_entities_for_device
from petkit_local.ha.commands import (
    ALL_ACTIONS,
    Refused,
    _coerce_number,
    _coerce_switch,
    handle_ha_command,
)


def _settable_index(device):
    return {e.unique_id_suffix: e for e in get_entities_for_device(device) if e.is_settable}


def _litter():
    d = Device(device_type="t5", petkit_id=1, serial_number="SN")
    d.config.setdefault("settings", d.default_settings())
    return d, _settable_index(d)


def _feeder():
    d = Device(device_type="d4sh", petkit_id=1, serial_number="SN")
    d.config.setdefault("settings", d.default_settings())
    return d, _settable_index(d)


def test_switch_updates_settings_and_returns_mqtt():
    d, idx = _litter()
    res = handle_ha_command(d, idx["auto_work"], "OFF")
    assert res is not None
    suffix, payload = res
    assert suffix == "property/set"
    assert payload["params"] == {"autoWork": 0}
    assert d.config["settings"]["autoWork"] == 0

    handle_ha_command(d, idx["auto_work"], "ON")
    assert d.config["settings"]["autoWork"] == 1


def test_number_coercion():
    d, idx = _litter()
    _, payload = handle_ha_command(d, idx["volume"], "7")
    assert payload["params"] == {"volume": 7}
    assert d.config["settings"]["volume"] == 7


def test_coerce_switch_returns_int_not_bool():
    # The value is JSON-encoded straight into a property.set params dict and
    # the device's settings fields are integers — `true` is not the same wire
    # value as `1`.
    for on in ("ON", "on", "TRUE", "true", "1", 1, True):
        assert _coerce_switch(on) == 1
        assert type(_coerce_switch(on)) is int
    for off in ("OFF", "off", "FALSE", "0", 0, False, "garbage", "", None):
        assert _coerce_switch(off) == 0
        assert type(_coerce_switch(off)) is int


def test_coerce_number_stays_polymorphic():
    # An integral value must come back as int so the device sees {"volume": 7},
    # never {"volume": 7.0}; a fractional one keeps its float.
    assert _coerce_number("21.0") == 21 and type(_coerce_number("21.0")) is int
    assert _coerce_number("21") == 21 and type(_coerce_number("21")) is int
    assert _coerce_number("21.5") == 21.5 and type(_coerce_number("21.5")) is float
    assert _coerce_number(" -3 ") == -3 and type(_coerce_number(" -3 ")) is int
    assert _coerce_number("2e3") == 2000 and type(_coerce_number("2e3")) is int


def test_coerce_number_rejects_non_finite_setpoints():
    # Behaviour change vs. the bare float() this used to call: inf/NaN are no
    # longer accepted. json.dumps renders them as bare Infinity/NaN, which is
    # invalid JSON that the device cannot read back, and no device setpoint is
    # non-finite. The caller drops the command instead.
    for bad in ("nan", "NaN", "inf", "-inf", "Infinity", "1e400"):
        assert _coerce_number(bad) is None, bad


def test_coerce_number_rejects_underscore_digit_separators():
    assert _coerce_number("1_0") is None


def test_non_numeric_number_payload_is_dropped_not_written():
    d, idx = _litter()
    before = dict(d.config["settings"])
    for bad in ("nan", "not a number", ""):
        assert handle_ha_command(d, idx["volume"], bad) is None, bad
    assert d.config["settings"] == before


def test_number_maps_to_correct_field():
    d, idx = _litter()
    # HA key 'cleaning_delay' writes device field 'stillTime'
    _, payload = handle_ha_command(d, idx["cleaning_delay"], "120")
    assert payload["params"] == {"stillTime": 120}


def test_select_index_default():
    d, idx = _litter()
    _, payload = handle_ha_command(d, idx["sand_type"], "tofu")
    assert payload["params"] == {"sandType": 2}  # option_values [1,2,3]


def test_select_explicit_values():
    d, idx = _litter()
    _, payload = handle_ha_command(d, idx["cleaning_interval"], "1h")
    assert payload["params"] == {"autoIntervalMin": 60}


def test_surplus_level_writes_the_pair():
    """`surplusControl` alone carries on/off + level (0/30/60/80); the level
    is mirrored into `surplusStandard` too, per a live D4SH capture."""
    d, idx = _feeder()
    _, payload = handle_ha_command(d, idx["surplus_level"], "moderate")
    assert payload["params"] == {"surplusControl": 60, "surplusStandard": 2}
    assert d.config["settings"]["surplusControl"] == 60
    assert d.config["settings"]["surplusStandard"] == 2


def test_surplus_level_disabled_leaves_standard_untouched():
    d, idx = _feeder()
    d.config["settings"]["surplusStandard"] = 3
    _, payload = handle_ha_command(d, idx["surplus_level"], "disabled")
    assert payload["params"] == {"surplusControl": 0}
    assert d.config["settings"]["surplusStandard"] == 3


def test_button_returns_mqtt_service_envelope():
    d, idx = _litter()
    suffix, env = handle_ha_command(d, idx["cleaning_start"], "")
    assert suffix == "start"
    assert env["method"] == "thing.service.start"
    assert env["params"] == {"start_action": 0}


def test_litter_action_codes_match_reference():
    d, idx = _litter()
    expected = {
        "cleaning_start": ("start", "start_action", 0),
        "dump_litter": ("start", "start_action", 1),
        "deodorize": ("start", "start_action", 2),
        "maintenance_start": ("start", "start_action", 9),
        "maintenance_stop": ("end", "end_action", 9),
        "level_litter": ("start", "start_action", 4),
        "reset_n60": ("start", "start_action", 10),
    }
    for key, (suffix, akey, code) in expected.items():
        s, env = handle_ha_command(d, idx[key], "")
        assert s == suffix, key
        assert env["params"] == {akey: code}, key


def test_feed_uses_feed_realtime_topic():
    d = Device(device_type="d4h", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    suffix, env = handle_ha_command(d, idx["feed"], "")
    assert suffix == "feed_realtime"
    assert env["method"] == "thing.service.feed_realtime"
    assert env["params"]["amount"] == 10
    assert env["params"]["id"].startswith("r_")


def test_the_feed_id_carries_its_number_twice():
    """`r_20260802_882_882-1` and `r_20260802_4057_4057-1`, both captured off
    PetKit's cloud talking to a D4 (PR #10). This was written from localkit's
    `FeedRealtime`, which has the number once; two captures agreeing settles it
    against a reimplementation. Two independent random numbers would match by
    chance about once in eighty million times."""
    import re

    d = Device(device_type="d4h", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    for _ in range(5):
        _, env = handle_ha_command(d, idx["feed"], "")
        m = re.fullmatch(r"r_(\d{8})_(\d+)_(\d+)-1", env["params"]["id"])
        assert m, env["params"]["id"]
        assert m.group(2) == m.group(3)


def test_the_feed_id_counts_seconds_since_local_midnight():
    """`r_20260801_72849_72849-1` was logged at 8:14:09 PM and 72849 s is
    20:14:09; `r_20260801_72906_72906-1` at 8:15:06 PM against 72906 s (issue
    #2). It is the same clock the device puts in its own `feed_over` content as
    `time`, beside a local `day`, so a UTC count would disagree with the
    device's own reading of the feed it just ran."""
    import time

    from petkit_local.utils.timeutil import local_day_start

    d = Device(device_type="d4sh", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    before = int(time.time() - local_day_start())
    _, env = handle_ha_command(d, idx["feed"], "")
    after = int(time.time() - local_day_start())

    seconds = int(env["params"]["id"].split("_")[2])
    assert before <= seconds <= after
    assert env["params"]["id"].startswith(time.strftime("r_%Y%m%d_"))


# --- dual hopper (issue #2) --------------------------------------------------

def test_a_dual_hopper_is_asked_per_hopper_and_never_with_plain_amount():
    """The bug in issue #2. A D4SH's firmware compares its own model string
    against "D4SH" and that branch reads ONLY `amount1`/`amount2`; the plain
    `amount` we sent is not looked at, so the device ran a feed cycle and
    dispensed nothing."""
    d = Device(device_type="d4sh", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    suffix, env = handle_ha_command(d, idx["feed"], "")

    assert suffix == "feed_realtime"
    assert "amount" not in env["params"], "the field a Dual-Hopper ignores"
    # 1 and 1 is what PetKit's app sends for its own default manual feed.
    assert env["params"]["amount1"] == 1
    assert env["params"]["amount2"] == 1


def test_a_single_hopper_feeder_is_unchanged():
    """The other half of the same fix, and the one with a live blast radius.

    A D4H DIVIDES `amount` by a constant from its own configuration, so 10 is
    not ten portions and the number is not ours to reinterpret. Nothing in
    issue #2 speaks to it — the reporter has a Dual-Hopper, which never reads
    this field. Changing it would silently resize the meal on working
    hardware."""
    for codename in ("d4h", "d4", "d3", "feedermini"):
        d = Device(device_type=codename, petkit_id=2, serial_number="F")
        idx = _settable_index(d)
        _, env = handle_ha_command(d, idx["feed"], "")
        assert env["params"]["amount"] == 10, codename
        assert "amount1" not in env["params"], codename


def test_the_portion_numbers_drive_the_feed_and_are_never_sent_as_settings():
    """They are our intent, not a device setting. `to_device_info` serves
    `config["settings"]` straight back to the device, so a value parked there
    would be pushed to the feeder as a setting it never had."""
    d = Device(device_type="d4sh", petkit_id=2, serial_number="F")
    idx = _settable_index(d)

    assert handle_ha_command(d, idx["hopper1_portions"], "3") is None
    assert handle_ha_command(d, idx["hopper2_portions"], "2") is None
    assert d.config["local"] == {"feedAmount1": 3, "feedAmount2": 2}
    assert "feedAmount1" not in d.config.get("settings", {})

    _, env = handle_ha_command(d, idx["feed"], "")
    assert (env["params"]["amount1"], env["params"]["amount2"]) == (3, 2)


def test_feeding_one_hopper_asks_the_other_for_zero():
    """What PetKit's app does, and through the same service rather than a
    different one: its single-hopper feed was captured as
    `{"amount1": 0, "amount2": 1}` (issue #2)."""
    d = Device(device_type="d4sh", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    handle_ha_command(d, idx["hopper1_portions"], "4")
    handle_ha_command(d, idx["hopper2_portions"], "5")

    _, one = handle_ha_command(d, idx["feed_hopper_1"], "")
    assert (one["params"]["amount1"], one["params"]["amount2"]) == (4, 0)

    _, two = handle_ha_command(d, idx["feed_hopper_2"], "")
    assert (two["params"]["amount1"], two["params"]["amount2"]) == (0, 5)


def test_a_portion_count_above_the_byte_the_device_stores_is_refused():
    """The firmware stores the amount with `sb`, a single byte, so 256 would
    wrap to 0 on the device — a feed that asks for a lot and delivers
    nothing."""
    d = Device(device_type="d4sh", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    with pytest.raises(Refused):
        handle_ha_command(d, idx["hopper1_portions"], "300")
    assert d.config.get("local", {}).get("feedAmount1") is None


def test_cancel_uses_the_service_the_firmware_has_for_it():
    """`feed_realtime_cancel` is its own service in the same dispatch, reading
    `id`/`amount1`/`amount2` with no model check. We used to send
    `feed_realtime` with `amount: 0`, which on a Dual-Hopper lands in a field
    the device does not read at all."""
    d = Device(device_type="d4sh", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    _, fed = handle_ha_command(d, idx["feed"], "")

    suffix, env = handle_ha_command(d, idx["cancel_manual_feed"], "")
    assert suffix == "feed_realtime_cancel"
    assert env["method"] == "thing.service.feed_realtime_cancel"
    # The feed it is cancelling, which nothing else knows: the device echoes
    # the id back, but a cancel is wanted before either echo has arrived.
    assert env["params"]["id"] == fed["params"]["id"]


def test_cancel_on_an_esp32_feeder_is_left_alone():
    """That service was read out of the embedded-Linux `ctrl`. The D4, D3 and
    Feeder Mini run different firmware entirely, so for them this stays
    localkit's zero-amount cancel — the only evidence covering them."""
    d = Device(device_type="d4", petkit_id=2, serial_number="F")
    idx = _settable_index(d)
    suffix, env = handle_ha_command(d, idx["cancel_manual_feed"], "")
    assert suffix == "feed_realtime"
    assert env["params"]["amount"] == 0


def test_every_button_maps_to_an_action():
    # Coherence: every button entity across all device types must resolve to a
    # known action, else pressing it silently does nothing.
    for dtype in ("t5", "t4", "d4h", "d4", "w7h", "w5"):
        d = Device(device_type=dtype, petkit_id=99, serial_number="X")
        for e in get_entities_for_device(d):
            if e.component == "button":
                assert e.key in ALL_ACTIONS, f"{dtype}: button '{e.key}' has no action"


def test_capability_switch_writes_config_not_settings_and_pushes_nothing():
    d, idx = _litter()
    res = handle_ha_command(d, idx["capability_full_video"], "OFF")
    assert res is None  # no MQTT/heartbeat push — STS is the control point
    assert d.config["capabilities"]["fullVideo"] is False
    assert "fullVideo" not in d.config.get("settings", {})

    handle_ha_command(d, idx["capability_full_video"], "ON")
    assert d.config["capabilities"]["fullVideo"] is True


def test_every_settable_control_has_settings_path():
    # Coherence: every switch/number/select must write to settings.<field>,
    # EXCEPT capability toggles — those write to config["capabilities"] and
    # are never pushed to the device (the STS response is the control point,
    # see ha/commands.py::CAPABILITY_VALUE_PREFIX), so they're exempt.
    for dtype in ("t5", "t4", "d4h", "d4sh", "d4", "w7h", "w5", "k3"):
        d = Device(device_type=dtype, petkit_id=99, serial_number="X")
        for e in get_entities_for_device(d):
            if e.component in ("switch", "number", "select"):
                # `local.` is the same kind of exemption for the same kind of
                # reason: the per-hopper portion counts are our intent, not a
                # device setting, and nothing pushes them anywhere.
                if e.value_path.startswith(("capabilities.", "local.")):
                    continue
                assert e.value_path.startswith("settings."), \
                    f"{dtype}: {e.component} '{e.key}' value_path={e.value_path!r}"
                assert e.setting_field, f"{dtype}: {e.key} has empty setting_field"


def test_a_command_id_stays_within_signed_int32():
    """`id` must stay < 2**31. Every id observed from the real cloud is signed-
    int32 (47214543..2144539517, never the 2**31..2**32-1 half), and a raw ms
    timestamp overflows it — so the envelope wraps into the signed range so a
    firmware signed-atoi never reads it negative."""
    from petkit_local.ha.commands import ALL_ACTIONS
    device = Device(device_type="d4sh", petkit_id=1, serial_number="F")
    for name, make in ALL_ACTIONS.items():
        result = make(device)
        if result is None:
            continue
        _suffix, envelope = result
        if not isinstance(envelope, dict) or "id" not in envelope:
            continue
        assert envelope["id"].isdigit(), name
        assert int(envelope["id"]) < 2**31, f"{name} id out of signed-int32 range"


def test_a_number_outside_its_range_is_refused_not_clamped():
    """`min_value`/`max_value` bound Home Assistant's control and the panel's
    spinner; neither binds a raw API call. And the value does not merely render:
    it lands in `config["settings"]`, which `to_device_info` serves back to the
    device, so an out-of-range number would be pushed to hardware.

    Refused rather than clamped, because writing a number nobody asked for is
    the failure this project avoids everywhere else.
    """
    from petkit_local.devices.base import Device
    from petkit_local.devices.registry import get_entities_for_device

    dev = Device(device_type="t5", petkit_id=1, serial_number="SN")
    volume = next(e for e in get_entities_for_device(dev) if e.key == "volume")
    before = dict(dev.config.get("settings", {}))

    from petkit_local.ha.commands import Refused

    # `Refused`, not None: None already means "applied, nothing to send to the
    # device", so returning it here made the panel answer {"ok": true} to a
    # value it had just thrown away.
    for bad in (volume.max_value + 1, volume.min_value - 1):
        with pytest.raises(Refused):
            handle_ha_command(dev, volume, str(bad))
    assert dev.config.get("settings", {}) == before, "a refused write must change nothing"

    # The bounds themselves are valid, and a value inside them still works.
    assert handle_ha_command(dev, volume, str(volume.max_value)) is not None
    assert dev.config["settings"][volume.setting_field] == volume.max_value
