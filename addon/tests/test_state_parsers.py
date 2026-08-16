import time

from petkit_local.devices.state_parsers import (DEODORANT_TOTAL_DAYS, WORK_MODE_IDLE,
                                                _days_left_from_reset, normalize_property_params,
                                                parse_state_report)


def test_mqtt_property_post_normalizes_to_flat_keys():
    # exact nested shape from the real T4 property.post capture
    params = {
        "litter": {"weight": 3119, "usedTimes": 0, "percent": 100, "sandType": 1},
        "box": 0,
        "device": {"sw": 1, "pet_in_time": 0, "k3LightSwitch": 0},
        "wifi": {"ssid": "No Signal", "rsq": -51, "bssid": "82"},
        "boxState": 1,
        "err": {"DC": 0, "mcu": 0, "full": 0},
        "firmware": "1.625",
    }
    flat = normalize_property_params("t4", params)
    assert flat["sandPercent"] == 100
    assert flat["sandWeight"] == 3119
    assert flat["usedTimes"] == 0
    assert flat["rssi"] == -51
    assert flat["petInTime"] == 0
    assert flat["boxState"] == 1
    assert flat["errorMsg"] == ""       # no active error fields
    assert flat["firmware"] == "1.625"


def test_normalize_flags_active_errors():
    # Separator is ", " since the flags began decoding to multi-word labels;
    # a litter box has no label table, so its flags still read raw.
    flat = normalize_property_params("t4", {"err": {"DC": 0, "scale": 1, "OLED": 1}})
    assert set(flat["errorMsg"].split(", ")) == {"scale", "OLED"}


def test_normalize_safe_on_flat_input():
    assert normalize_property_params("t5", {"sandPercent": 50}) == {}


def test_real_t5_state_report():
    # exact nested shape the T5 posts (form field `state=<JSON>`)
    from petkit_local.http.handlers.state_report import _extract_state
    import json as _json
    body = {
        "device": {"sw": 1, "pet_in_time": 0},
        "wifi": {"ssid": "x", "rsq": -55, "bssid": ""},
        "firmware": "943",
        "litter": {"weight": 5469, "usedTimes": 3, "percent": 40, "sandType": 1},
        "box": 0, "boxState": 1, "sprayState": 1, "cameraStatus": 0,
        "err": {"scale": 1, "full": 0},
        "other": 'PowerSRC:0,Ip:"192.0.2.155",disP:0',
    }
    extracted = _extract_state("state=" + _json.dumps(body))
    assert extracted.get("firmware") == "943"
    flat = normalize_property_params("t5", extracted)
    assert flat["sandPercent"] == 40
    assert flat["sandWeight"] == 5469
    assert flat["usedTimes"] == 3
    assert flat["rssi"] == -55
    assert flat["boxState"] == 1
    assert flat["sprayState"] == 1
    assert flat["errorMsg"] == "scale"   # err.scale=1 surfaced
    assert flat["ip"] == "192.0.2.155"  # pulled from `other` for the camera


def test_litter_camera_parses_core_fields():
    body = {
        "workState": 1,
        "sandWeight": 4200,
        "sandPercent": 80,
        "boxFull": 0,
        "usedTimes": 12,
        "deodorantLeftDays": 20,
        "Ip": "192.0.2.55",
        "wifi": {"rsq": -55},
    }
    s = parse_state_report("t5", body)
    assert s["workingState"] == 1
    assert s["sandWeight"] == 4200
    assert s["sandPercent"] == 80
    assert s["usedTimes"] == 12
    assert s["ip"] == "192.0.2.55"
    assert s["rssi"] == -55


def test_a_camera_feeder_reports_its_address_like_a_litter_box_does():
    """`state["ip"]` is read a long way from where it is written, and its
    absence never looks like a missing field: `media/go2rtc.py` quietly skips a
    device without one, and the Patchers tab reports the whole device as
    unsupported. So a D4H had no stream URL and could not be patched, with
    nothing naming the cause (PR #12, @nklmilojevic, confirmed on fw 867).

    Both spellings, because the litter path saw it as a flat key and the MQTT
    path saw it inside `other` — and the feeder parser read neither.
    """
    s = parse_state_report("d4h", {"other": 'PowerSRC:0,Ip:"10.50.0.10",disP:0'})
    assert s["ip"] == "10.50.0.10"
    assert parse_state_report("d4h", {"Ip": "10.50.0.11"})["ip"] == "10.50.0.11"


def test_something_that_is_not_an_address_does_not_become_one():
    """It ends up as a go2rtc source and an SSH target, and the pattern used to
    be `[0-9.]+` — which matches `....` as happily as an address. No `ip` at
    all is a state every caller already handles; a malformed one is not."""
    for junk in ('Ip:1.2.3.4.5,x:1', "Ip:....", "Ip:not-an-ip", "PowerSRC:0"):
        assert "ip" not in parse_state_report("d4h", {"other": junk}), junk
    assert "ip" not in parse_state_report("t5", {"Ip": "999.999.999.999"})


def test_litter_content_string_is_merged():
    import json
    body = {"workState": 0, "content": json.dumps({"sandPercent": 42})}
    s = parse_state_report("t5", body)
    assert s["sandPercent"] == 42


def test_feeder_feedstate_nested():
    body = {
        "workState": 0,
        "desiccantLeftDays": 5,
        "feedState": {"times": 3, "realAmountTotal": 30, "eatAmountTotal": 25},
    }
    s = parse_state_report("d4h", body)
    assert s["desiccantLeftDays"] == 5
    assert s["feedState"]["times"] == 3
    assert s["feedState"]["realAmountTotal"] == 30


def test_fountain_electricity_nested():
    body = {"workState": 1, "electricity": {"battery_percent": 88}}
    s = parse_state_report("w5", body)
    assert s["batteryPercent"] == 88


def test_litter_workstate_object_extracts_mode():
    # Real workState is an object; the status sensor wants the mode int.
    body = {"workState": {"workMode": 2, "workProcess": 10}, "sandPercent": 30}
    s = parse_state_report("t5", body)
    assert s["workingState"] == 2
    assert s["workState"]["workProcess"] == 10


def test_fountain_real_field_names():
    body = {"workState": 0, "lackWarning": 1, "heatRealTemp": 24, "drinkTime": 5}
    s = parse_state_report("w7h", body)
    assert s["lackWarning"] == 1   # was waterLack
    assert s["heatRealTemp"] == 24  # was temperature
    assert s["drinkTime"] == 5      # was drinkTimes


def test_purifier_fields():
    body = {"workState": 1, "liquid": 60, "battery": 90, "temp": 21}
    s = parse_state_report("k3", body)
    assert s["liquid"] == 60
    assert s["battery"] == 90
    assert s["temp"] == 21  # was temperature


def test_explicit_nulls_do_not_crash_or_invent_values():
    # A device may send a key with an explicit null instead of omitting it.
    # `dig` returns that null (it reports key presence, not truthiness), so
    # every consumer here has to keep its own isinstance guard.
    body = {"workState": None, "wifi": None, "litter": None, "err": None}
    s = parse_state_report("t5", body)
    # A null `workState` reads the same as an absent one: no cycle is running.
    # This used to assert 0, which is `WORK_MODES[0]` -- "cleaning".
    assert s["workingState"] == WORK_MODE_IDLE
    assert "sandPercent" not in s


def test_an_idle_box_is_not_reported_as_cleaning():
    """`workState` is sent ONLY while a cycle runs -- absent from 988 of 1254
    captured snapshots. Defaulting it to 0 meant `WORK_MODES[0] == "cleaning"`,
    so an idle box claimed to be cleaning about 79% of the time."""
    idle = {"litter": {"percent": 40}, "sprayState": 1}
    assert parse_state_report("t5", idle)["workingState"] == WORK_MODE_IDLE
    assert normalize_property_params("t5", idle)["workingState"] == WORK_MODE_IDLE

    running = dict(idle, workState={"workMode": 9, "workProcess": 13})
    assert parse_state_report("t5", running)["workingState"] == 9
    assert normalize_property_params("t5", running)["workingState"] == 9

    # A fragment that was never going to carry `workState` says nothing, so
    # neither do we -- inventing "idle" there would be the same mistake again.
    assert "workingState" not in normalize_property_params("t5", {"sandPercent": 50})


def test_presence_signalled_fields_can_go_back_down():
    """`refreshState` is an object the box sends only while deodorizing, and
    `device.state` is merged into and never pruned -- so the binary sensor that
    read it directly latched ON at the first spray and never returned. Absence
    in a full snapshot has to mean 0."""
    spraying = {"litter": {"percent": 40}, "refreshState": {"workReason": 0, "workProcess": 1}}
    done = {"litter": {"percent": 40}}
    for parse in (parse_state_report, normalize_property_params):
        assert parse("t5", spraying)["deodorizing"] == 1
        assert parse("t5", done)["deodorizing"] == 0
    # Not a snapshot -> no claim either way.
    assert "deodorizing" not in normalize_property_params("t5", {"sandPercent": 50})


def test_scalar_where_a_sub_object_was_expected_is_ignored():
    s = parse_state_report("t5", {"workState": 2, "wifi": "not an object"})
    assert s["workingState"] == 2
    assert s.get("rssi") is None


def test_snake_case_work_state_is_used_as_a_fallback():
    assert parse_state_report("t5", {"work_state": 3})["workingState"] == 3
    # the camelCase spelling wins when both are present
    assert parse_state_report("t5", {"workState": 1, "work_state": 3})["workingState"] == 1


def test_fountain_and_feeder_nested_fallback_spellings():
    s = parse_state_report("d4h", {"feed_state": {"times": 4}})
    assert s["feedState"]["times"] == 4
    s = parse_state_report("w7h", {"status": {"detect_status": 1}})
    assert s["detectStatus"] == 1


def test_unknown_type_passthrough():
    body = {"foo": 1}
    assert parse_state_report("zzz", body) == body


def test_empty_body():
    assert parse_state_report("t5", {}) == {}


def test_days_left_from_reset_survives_non_finite_device_value():
    # json.loads accepts bare Infinity/NaN by default, so a device really can
    # put one in sprayResetTime. The old isinstance((int, float)) guard let it
    # through and int(total_days - days_since) raised OverflowError, taking the
    # whole dev_state_report handler down with it.
    assert _days_left_from_reset(float("inf"), DEODORANT_TOTAL_DAYS) is None
    assert _days_left_from_reset(float("-inf"), DEODORANT_TOTAL_DAYS) is None
    assert _days_left_from_reset(float("nan"), DEODORANT_TOTAL_DAYS) is None
    # int is unbounded; time.time() - 10**400 raises OverflowError.
    assert _days_left_from_reset(10 ** 400, DEODORANT_TOTAL_DAYS) is None
    assert _days_left_from_reset("garbage", DEODORANT_TOTAL_DAYS) is None
    assert _days_left_from_reset(None, DEODORANT_TOTAL_DAYS) is None
    assert _days_left_from_reset(0, DEODORANT_TOTAL_DAYS) is None


def test_days_left_from_reset_computes_from_a_real_timestamp():
    # 10.5 days in, 19.5 of 30 left -> 20: the countdown rounds UP, because a
    # part-used day is still a day you have. The offset deliberately sits away
    # from a whole-day boundary so the assertion does not depend on which side
    # of it the clock lands.
    reset = time.time() - 10.5 * 86400
    assert _days_left_from_reset(reset, DEODORANT_TOTAL_DAYS) == 20
    # The field sometimes arrives as a numeric string; the old isinstance
    # guard silently returned None for it.
    assert _days_left_from_reset(str(int(reset)), DEODORANT_TOTAL_DAYS) == 20


def test_mqtt_property_post_derives_the_consumable_countdowns():
    # The regression this guards: the derivation lived only in the HTTP state
    # report parser, and a T5 stops polling the HTTP heartbeat once it is on
    # MQTT. A box that had never sent a single dev_state_report reported a
    # perfectly good sprayResetTime in all 685 captures and still showed
    # "N60 Spray Days Left" empty in HA and the panel.
    params = {"sprayResetTime": time.time() - 10.5 * 86400, "liquidReset": time.time() - 5.5 * 86400}
    flat = normalize_property_params("t5", params)
    assert flat["sprayLeftDays"] == 35      # ceil(45 - 10.5)
    assert flat["deodorantLeftDays"] == 25  # ceil(30 - 5.5)


def test_both_transports_agree_on_uptime():
    # `runtime` -> `totalTime` was mapped in the HTTP parser only, and a T5
    # stops polling the HTTP heartbeat once it is on MQTT -- so the Uptime
    # sensor read unknown forever on exactly the healthiest devices.
    body = {"litter": {"percent": 40}, "runtime": 130364}
    assert parse_state_report("t5", body)["totalTime"] == 130364
    assert normalize_property_params("t5", body)["totalTime"] == 130364


def test_both_transports_agree_on_the_countdowns():
    # The two transports share no field table and are hand-synced, which the
    # module docstring calls the standard bug here. One body, one answer.
    body = {"sprayResetTime": time.time() - 3.5 * 86400,
            "liquidReset": time.time() - 3.5 * 86400}
    http = parse_state_report("t5", body)
    mqtt = normalize_property_params("t5", body)
    for key in ("sprayLeftDays", "deodorantLeftDays"):
        assert http[key] == mqtt[key], key


def test_a_never_reset_consumable_stays_unknown_rather_than_zero():
    # liquidReset was 0 in all 685 captured reports of a box whose N50 had
    # never been replaced. Writing 0 there would claim the cartridge is
    # exhausted; leaving the key unset makes HA show "unknown", which is true.
    for parse in (parse_state_report, normalize_property_params):
        flat = parse("t5", {"liquidReset": 0, "sprayResetTime": 0})
        assert "deodorantLeftDays" not in flat
        assert "sprayLeftDays" not in flat


def test_advertised_spray_days_matches_the_countdown_total():
    # payloads.py told the device sprayDays=45 while the countdown here used 30,
    # so HA burned down a cartridge the device had been told was half again as
    # long. Both now read one constant; this fails if either is re-hardcoded.
    from petkit_local.devices import payloads
    from petkit_local.devices.base import Device
    from petkit_local.devices.state_parsers import SPRAY_TOTAL_DAYS

    dev = Device(device_type="t5", petkit_id=1, serial_number="SN")
    info = payloads.to_device_info(dev)["result"]
    assert info["sprayDays"] == SPRAY_TOTAL_DAYS

    # 10.5 days into the cartridge, off a whole-day boundary so the assertion
    # does not depend on which side of it the clock lands; the countdown
    # rounds up, so 34.5 remaining reads 35.
    used = normalize_property_params("t5", {"sprayResetTime": time.time() - 10.5 * 86400})
    assert used["sprayLeftDays"] == SPRAY_TOTAL_DAYS - 10


# --- D4SH (YumShare Dual-Hopper), issue #2 ----------------------------------

#: The state a real D4SH on firmware 867 reports, verbatim from issue #2. It
#: arrived twice, once inside an event's `state` and once as an HTTP report, and
#: both copies carry these keys.
D4SH_STATE = {
    "wifi": {"ssid": "HomeWIFI", "rsq": -48, "bssid": "60d8a4e83040"},
    "hardware": 1, "firmware": "867", "locale": "",
    "ir_b_1": 1, "ir_b_2": 1, "ir_c": 0,
    "batV": 0, "DCV": 6234, "runtime": 659, "mem": 34448, "cpu": 100, "ubat": 0,
    "cameraStatus": 1, "door": 1, "food1": 2, "food2": 2, "bowl": -1,
    "feeding": 0, "eating": 0, "ota": 0, "ultra_sta": 0,
    "ready": [0, 0, 0, 0, 0],
    "err": {"DC": 0, "sys": 0, "rtc_c": 0, "moto": 0,
            "blk_f": 0, "blk_d": 0, "camera": 0, "serial": 0},
    "sensor": {"left_hall": 1, "home_hall": 1, "right_hall": 1, "left_sub_hall": 0},
    "other": ("PowerSRC:0,CloudUseAcceDomain:0,DnsList:[192.168.1.254]"
              "[114.114.114.114][114.114.115.115],Ip:192.168.1.204,"
              "feed_recoed[0-0-0-0-0]"),
}


def test_both_transports_read_the_same_d4sh_report_the_same_way():
    """A D4SH publishes `thing/event/property/post` — the topic is in its own
    firmware — and that path reaches `normalize_property_params` ALONE, while
    the snapshot inside an event goes through both parsers.

    So a feeder mapping added to one of them works on whichever frames happen
    to carry it and silently does nothing on the other. That is what happened
    here: `normalize_property_params` carried not one feeder field, so the
    device's main state channel dropped every hopper level, the bowl and the
    feeding flags."""
    http_side = parse_state_report("d4sh", D4SH_STATE)
    mqtt_side = normalize_property_params("d4sh", D4SH_STATE)

    for key in ("food1", "food2", "bowl", "door", "feeding", "eating",
                "ir_b_1", "ir_b_2", "ir_c", "DCV", "left_hall", "right_hall"):
        assert key in http_side, f"{key} missing on the HTTP path"
        assert key in mqtt_side, f"{key} missing on the MQTT path"
        assert http_side[key] == mqtt_side[key], key


def test_a_feeder_that_reports_no_work_state_is_not_given_one():
    """The payload has no `workState` at all, and the parser used to default it
    to 0 — so Device Status displayed a value the device never sent. Same
    mistake the W7H's parser was fixed for, and the same one that had an idle
    litter box calling itself "cleaning"."""
    assert "workState" not in D4SH_STATE
    assert "workingState" not in parse_state_report("d4sh", D4SH_STATE)
    assert "workingState" not in normalize_property_params("d4sh", D4SH_STATE)


def test_the_feeder_fault_block_reaches_both_paths():
    """`_extract_error_flags` was wired into the MQTT path only, so the Error
    sensor said whatever the last transport to arrive had to say."""
    faulted = {**D4SH_STATE, "err": {**D4SH_STATE["err"], "blk_f": 1}}
    assert parse_state_report("d4sh", faulted)["errorMsg"] == "Food outlet blocked"
    assert normalize_property_params("d4sh", faulted)["errorMsg"] == "Food outlet blocked"
    assert parse_state_report("d4sh", D4SH_STATE)["errorMsg"] == ""


def test_an_esp32_feeder_is_not_given_fields_its_hardware_never_sends():
    """The next-gen keys are gated on the models whose firmware was read. A D4
    runs something else entirely, and inventing a hopper level for it would be
    extrapolation dressed as support."""
    flat = parse_state_report("d4", D4SH_STATE)
    for key in ("food1", "food2", "ir_b_1", "DCV", "left_hall"):
        assert key not in flat, key


def test_food_low_is_derived_from_presence_not_truthiness():
    """`food` is presence: a live D4H (fw 867) holds a steady 2 with a FULL
    hopper and reports 0 only when nothing is detected. The Food Low problem
    sensor used to read the raw value through discovery's generic truthy-is-ON
    template, so a full hopper read as Food Low ON — backwards. It now reads
    the derived `foodLow`, which is 1 exactly when the device says empty."""
    assert parse_state_report("d4h", {"food": 2})["foodLow"] == 0
    assert parse_state_report("d4h", {"food": 0})["foodLow"] == 1
    # No `food`, no verdict: a D4SH reports `food1`/`food2` and no singular
    # `food`, so nothing may invent a flag for it — absence stays absent.
    assert "foodLow" not in parse_state_report("d4sh", D4SH_STATE)
    assert "foodLow" not in normalize_property_params("d4sh", D4SH_STATE)


def test_the_device_ip_still_comes_out_of_the_other_string():
    """Everything downstream of the camera needs it — the stream URL and the
    whole Patchers tab go quiet without one."""
    assert parse_state_report("d4sh", D4SH_STATE)["ip"] == "192.168.1.204"
    assert normalize_property_params("d4sh", D4SH_STATE)["ip"] == "192.168.1.204"
