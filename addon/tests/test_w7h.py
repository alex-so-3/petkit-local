"""The W7H (EverSweet Ultra AI) fountain, pinned to a real report.

This model shares a category with the Bluetooth EverSweets and almost no fields
with them, so it is the case where "the fountain parser" and "what this device
sends" are two different things. Everything here is checked against one real
`property/post` captured from a W7H on 2026-07-31, cross-checked field for field
against a reverse-engineered map of the same firmware's `ctrl`.

The payload below is that capture with three substitutions, each of which the
assertions are independent of: the `XDevice` signing credential is removed
(`ingest.telemetry_only` strips it anyway, and it does not belong in a public
repo), and the SSID/BSSID/IP are replaced with placeholders — the IP with a
TEST-NET-1 address so the `other`-string parse is still exercised.
"""
import pytest

from petkit_local.devices import defaults
from petkit_local.devices.base import Device
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.devices.state_parsers import (
    normalize_property_params, parse_state_report,
)
from petkit_local.events import codes

#: A real W7H `property/post`: 42 keys, and every one of them named in the map.
W7H_PROPERTY_POST = {
    "device": {"sw": 1, "drink_time": 0, "pet_time": 1785532410, "pet_close_time": 0},
    "wifi": {"ssid": "ExampleNet", "rsq": -78, "bssid": "000000000000"},
    "hardware": 1,
    "firmware": "456",
    "locale": "",
    "timezone": "0.0",
    "sensor": {"hall_CH": 1, "hall_CL": 1, "hall_CKL": 1, "hall_CKR": 1,
               "hall_DH": 1, "hall_DKL": 1, "hall_DKR": 0, "hall_LTU": 1,
               "hall_LTD": 0, "hall_TY": 1},
    "runtime": 64,
    "mem": 0,
    "cpu": 0,
    "ble_adv": 0,
    "serial_comm": 0,
    "ble_os_run_ms": 183401,
    "reboot_reason": 3,
    "err": {"DC": 0, "mcu": 0, "rtc": 0, "cameraL": 0, "cameraE": 0, "taryD": 0,
            "taryL": 0, "taryF": 0, "taryO": 0, "ptcL": 0, "ptcM": 0,
            "valveL": 0, "valveE": 0, "valveN": 0, "cycL": 0, "cycM": 0,
            "repL": 0, "repM": 0},
    "cameraStatus": 1,
    "ota": 0,
    "heatState": 0,
    "liftValveState": 0,
    "pumpState": 0,
    "waterPumpState": 0,
    "cwtState": 0,
    "wtState": 1,
    "addWaterState": 0,
    "flushState": 0,
    "liftResetState": 0,
    "liftLiveState": 0,
    "stgInstall": 1,
    "stgFullState": 1,
    "cwtInstall": 1,
    "wtInstall": 1,
    "wtLock": 1,
    "heatInstall": 0,
    "disinfectTime": 0,
    "heatLeftTime": 0,
    "heatStatusTime": 0,
    "heatRealTemp": 0,
    "disinfectState": 0,
    "addWaterFrequent": 0,
    "discernPic": [],
    "other": 'PowerSRC:0,CloudUseAcceDomain:0,DnsList:"[192.0.2.1]",Ip:"192.0.2.62",pos_info:0_0',
}


def _flat():
    return normalize_property_params("w7h", W7H_PROPERTY_POST)


# --- the mechanism this device actually reports ----------------------------

def test_the_mechanism_fields_reach_the_entities():
    """The point of the change: 42 keys in, the mechanism readable in HA.

    Before this, a `property/post` reached `normalize_property_params` and
    nothing else — that function knew litter boxes — so the tanks, the pumps and
    the lift were visible only as raw JSON in the panel.
    """
    flat = _flat()
    # `stg*` is the tray and `wt*` is the waste tank — see
    # `test_the_prefixes_mean_what_the_firmware_says` for the evidence.
    assert flat["stgFullState"] == 1      # tray full
    assert flat["stgInstall"] == 1        # tray seated
    assert flat["cwtInstall"] == 1
    assert flat["wtInstall"] == 1         # waste tank seated
    assert flat["wtLock"] == 1
    assert flat["wtState"] == 1
    assert flat["pumpState"] == 0
    assert flat["heatInstall"] == 0
    assert flat["rebootReason"] == 3      # sent as `reboot_reason`
    assert flat["rssi"] == -78
    assert flat["ip"] == "192.0.2.62"


def test_the_hall_switches_are_flattened_out_of_the_sensor_block():
    flat = _flat()
    assert flat["hall_CH"] == 1
    assert flat["hall_TY"] == 1
    # The one that is not seated. This is the whole reason the halls are
    # published next to the derived flag: `wtInstall`, the waste tank's, reads
    # 1 at the same moment, because the left side alone satisfies it.
    assert flat["hall_DKR"] == 0
    assert flat["wtInstall"] == 1


def test_a_property_post_and_an_event_snapshot_agree():
    """Both transports must produce the same keys from the same payload.

    A `property/post` goes through `normalize_property_params` alone, while the
    snapshot embedded in a `drink_start` also goes through `parse_state_report`.
    A mapping added to one of them works on some frames and silently not on
    others — the failure mode this module's docstring names.
    """
    from_property = normalize_property_params("w7h", W7H_PROPERTY_POST)
    from_snapshot = parse_state_report("w7h", W7H_PROPERTY_POST)
    for key in ("stgFullState", "wtLock", "cwtState", "hall_DKR", "lastPetDetect"):
        assert from_snapshot[key] == from_property[key], key


# --- values the device does NOT send ---------------------------------------

def test_no_work_mode_is_invented():
    """`workState` is absent from this payload, and 0 is a real mode.

    The litter box's version of this bug had an idle box reporting itself as
    cleaning 79% of the time, because `WORK_MODES[0] == "cleaning"`.
    """
    assert "workingState" not in _flat()
    assert "workingState" not in parse_state_report("w7h", W7H_PROPERTY_POST)


@pytest.mark.parametrize("key", [
    "batteryPercent", "detectStatus", "filterPercent", "filterLeftDays",
    "lackWarning", "lowBattery", "filterWarning",
])
def test_absent_fields_are_left_absent_rather_than_written_as_null(key):
    """An explicit None publishes "unknown" — a claim about a field the device
    never mentioned. Leaving the key out lets the entity stay untouched."""
    assert key not in _flat()


# --- timestamps, not counters ----------------------------------------------

def test_a_zero_timestamp_does_not_become_1970():
    """`drink_time` is 0 here, meaning it has not happened. An HA timestamp
    sensor renders epoch 0 as a real date in 1970, which reads as data."""
    flat = _flat()
    assert "lastDrink" not in flat
    assert "lastPetLeft" not in flat        # pet_close_time is 0 too


def test_a_real_timestamp_becomes_iso():
    flat = _flat()
    assert flat["lastPetDetect"].startswith("2026-07-")
    assert flat["lastPetDetect"].endswith("+00:00")


def test_drink_time_is_not_published_as_a_drink_count():
    """It is the unix time of the last drink. Published as `drinkTime` it would
    have rendered 1785531049 behind a sensor named "Drink Times"."""
    payload = {**W7H_PROPERTY_POST,
               "device": {**W7H_PROPERTY_POST["device"], "drink_time": 1785531049}}
    flat = normalize_property_params("w7h", payload)
    assert "drinkTime" not in flat
    assert flat["lastDrink"].startswith("2026-")


# --- errors -----------------------------------------------------------------

def test_no_active_fault_reads_empty():
    assert _flat()["errorMsg"] == ""


def test_active_faults_are_decoded_to_words():
    payload = {**W7H_PROPERTY_POST,
               "err": {**W7H_PROPERTY_POST["err"], "taryF": 1, "cycL": 1}}
    message = normalize_property_params("w7h", payload)["errorMsg"]
    assert set(message.split(", ")) == {"Tray full", "Circulation pump stalled"}


def test_an_unknown_fault_bit_still_shows_up():
    """Falling back to the raw name keeps a new firmware's fault visible."""
    payload = {**W7H_PROPERTY_POST, "err": {"somethingNew": 1}}
    assert normalize_property_params("w7h", payload)["errorMsg"] == "somethingNew"


def test_a_family_with_no_flag_table_reads_raw():
    """Litter and feeder flags have no source naming them, so they must pass
    through untranslated rather than borrow the fountain's table."""
    assert codes.error_flag_label("taryF", "t5") == "taryF"
    assert codes.error_flag_label("taryF", "w7h") == "Tray full"


def test_the_fault_vocabulary_belongs_to_the_w7h_not_to_fountains():
    """A Bluetooth EverSweet has no tray, no lift valve and no second tank.

    Keying the table on the CATEGORY meant a W5 sending anything shaped like
    `taryF` would have been answered in the vocabulary of hardware it does not
    have — the same failure as HTTP `event_type` being read as global, one
    level further down.
    """
    for esp32 in ("w4", "w5", "ctw2", "ctw3"):
        assert codes.error_flag_label("taryF", esp32) == "taryF"


def test_the_faults_that_only_ever_arrive_as_an_event_are_named_too():
    """`tankCU`/`tankDU`/`tankCL`/`tankDF`/`ptcU` are not `err{}` bits.

    The payload builder's key list is closed at eighteen names and none of
    these is among them; they live in a second run of literals and reach us as
    the content of an `error_start`. Same sensor, so the same table.
    """
    assert codes.error_flag_label("tankDF", "w7h") == "Waste tank full"
    assert codes.error_flag_label("tankCL", "w7h") == "Clean water tank low"
    assert codes.error_flag_label("tankCU", "w7h") == "Clean water tank not installed"
    assert codes.error_flag_label("tankDU", "w7h") == "Waste tank not installed"
    assert codes.error_flag_label("ptcU", "w7h") == "Heater not installed"


def test_an_error_event_reads_the_same_as_the_matching_bit():
    """The bug: the same fault said "Tray full" arriving on a property post and
    `taryF` arriving as an event, because one path went through the table and
    the other wrote the device's abbreviation straight into the sensor."""
    from petkit_local.mqtt.bridge import _error_text

    assert _error_text("taryF", "w7h") == "Tray full"
    assert _error_text("taryF,cycL", "w7h") == "Tray full, Circulation pump stalled"
    assert _error_text({"taryF": 1, "cycL": 0}, "w7h") == "Tray full"
    assert _error_text("brandNew", "w7h") == "brandNew"


# --- the fountain branch must not touch other models ------------------------

def test_a_litter_box_is_not_parsed_as_a_fountain():
    """A T5 sends a `sensor{}` block of its own, so payload shape cannot be what
    selects this branch — it was, for one commit, and would have run the
    fountain mapping over every litter box."""
    t5_like = {"litter": {"percent": 50}, "sensor": {"open_hall": 1, "prox_raw": 99},
               "cameraStatus": 1, "reboot_reason": 0}
    flat = normalize_property_params("t5", t5_like)
    assert flat["open_hall"] == 1          # the T5's own halls still map

    # The gate has to be the codename. Asserting only on a realistic T5 payload
    # is not enough — it carries no W7H field, so a shape-based gate passes that
    # check while still running the wrong branch. Feeding a payload that has
    # BOTH a `sensor` block and a W7H-only key is what tells the two apart.
    ambiguous = {**t5_like, "stgFullState": 1, "wtLock": 1}
    as_litter = normalize_property_params("t5", ambiguous)
    assert "stgFullState" not in as_litter
    assert "wtLock" not in as_litter
    assert normalize_property_params("w7h", ambiguous)["stgFullState"] == 1


def test_the_t5_hall_block_is_flattened():
    """Read live off a running T5 (firmware 943) on 2026-07-31."""
    t5_sensor = {"weight": 0, "stdby_hall": 0, "smooth_hall": 1, "dump_hall": 1,
                 "open_hall": 1, "close_hall": 0, "top_hall": 0,
                 "prox_raw": 99, "around_pos": 0}
    flat = parse_state_report("t5", {"litter": {"percent": 50}, "sensor": t5_sensor})
    assert flat["dump_hall"] == 1
    assert flat["close_hall"] == 0
    # Not the raw ADC or the position code: no source gives either a scale.
    assert "prox_raw" not in flat
    assert "around_pos" not in flat


# --- what HA ends up publishing --------------------------------------------

def _keys(device_type):
    return {e.key for e in get_entities_for_device(
        Device(device_type=device_type, petkit_id=1, serial_number="SN"))}


def test_the_w7h_publishes_its_own_mechanism():
    keys = _keys("w7h")
    assert {"tray_full", "clean_tank_installed", "waste_lock_closed",
            "flushing", "disinfecting", "last_drink", "reboot_reason",
            "hall_waste_right", "drink_detection"} <= keys


def test_the_prefixes_mean_what_the_firmware_says():
    """`stg*` is the TRAY and `wt*` is the WASTE tank, not the reverse.

    Published the other way round from 1.1.0 to 1.3.0, so four entities named
    the wrong part. Settled in W7H 456 `ctrl` by following the writes rather
    than the prefixes: `set_prop(0x0d)` -> `stgFullState` takes the return of
    the reader that names itself `pk_hmi_get_water_tary_full_sta`, and
    `set_prop(0x0a)` -> `wtInstall` takes the predicate behind the "Not work
    dirty tank unstall" refusal.
    """
    bound = {e.key: e.value_path for e in get_entities_for_device(
        Device(device_type="w7h", petkit_id=1, serial_number="SN"))}
    assert bound["tray_full"] == "state.stgFullState"
    assert bound["tray_installed"] == "state.stgInstall"
    assert bound["waste_tank_installed"] == "state.wtInstall"
    assert bound["waste_tank_state"] == "state.wtState"
    # The one `wt*` entity that was right all along, and the reason the mix-up
    # survived review: a "waste lock" next to a "drinking tray installed"
    # sharing one prefix should have read as a contradiction.
    assert bound["waste_lock_closed"] == "state.wtLock"


def test_the_w7h_does_not_publish_hardware_it_does_not_have():
    """Each of these reads unknown forever on this model — which is
    indistinguishable from a device that has not reported yet."""
    keys = _keys("w7h")
    assert not keys & {"filter_percent", "filter_days", "battery", "low_battery",
                       "replace_filter", "reset_filter", "device_status",
                       "water_lack", "drink_times", "pet_detected"}


def test_the_w7h_does_not_publish_buttons_its_firmware_ignores():
    """`power` is not among the set handlers in this firmware's `ctrl`, so both
    buttons wrote a field nothing reads and reported success. It IS a service
    (`type: "power"`, `power_action` 0/1) — a different envelope, and device
    on/off rather than a running job paused, so neither button comes back."""
    assert not _keys("w7h") & {"pause_fountain", "resume_fountain"}


def test_the_esp32_fountains_are_untouched():
    """Gating one model must not quietly edit the others' entity set."""
    for device_type in ("w4", "w5", "ctw2", "ctw3"):
        keys = _keys(device_type)
        assert {"filter_percent", "battery", "drink_times", "pause_fountain"} <= keys
        assert not keys & {"tray_full", "hall_tray", "last_drink",
                           "fountain_flush", "drinking_event"}


# --- events -----------------------------------------------------------------

def test_a_discern_result_links_back_to_the_detection_that_opened_it():
    """`content.related_event` is the PARENT, not this row's own episode.

    Taking it as the row's own id is what left every MQTT card unparented: the
    discern stole the detection's id and the detection got none, so the two
    could never group into one Timeline card.
    """
    from petkit_local.events import ingest

    device = Device(device_type="w7h", petkit_id=30000369, serial_number="SN")
    row = ingest.from_mqtt(device, "pet_discern", {
        "event_id": "30000369_1785531925",
        "content": '{"related_event":"1_30000369_1785531685","count":1,'
                   '"area":0,"pet_id":0,"tracker_info":[],"vomit_info":[]}',
    })
    assert row["event_kind"] == "pet"
    assert row["related_event"] == "30000369_1785531925"
    assert row["parent_event"] == "1_30000369_1785531685"


def test_an_unrecognised_pet_is_not_stored_as_pet_zero():
    """`pet_id: 0` means the device matched nobody, not that it matched pet 0.

    All four `pet_discern` events in the capture carry `count: 1, pet_id: 0`,
    from a device whose `discernPic` is empty — it had no faces to match
    against. Stored as an identity it would be resolvable by anyone who binds
    the alias 0, and every unidentified visit would land on that pet.
    """
    from petkit_local.events import ingest

    device = Device(device_type="w7h", petkit_id=30000369, serial_number="SN")
    unknown = ingest.from_mqtt(device, "pet_discern", {
        "event_id": "e1", "content": '{"count":1,"pet_id":0}'})
    assert unknown["pet_ref"] is None

    known = ingest.from_mqtt(device, "pet_discern", {
        "event_id": "e2", "content": '{"count":1,"pet_id":7}'})
    assert known["pet_ref"] == 7


def test_the_fountain_events_a_real_w7h_sends_are_classified():
    """All three appear in the 2026-07-31 capture. An event the table does not
    know is stored as `other` and rendered as its raw name."""
    for name, kind in [("drink_start", codes.KIND_DRINKING),
                       ("pet_detect", codes.KIND_PET),
                       ("pet_discern", codes.KIND_PET)]:
        code = codes.lookup(name, "w7h")
        assert code is not None, name
        assert code.kind == kind, name
        assert "w7h" in code.families, name


def test_a_fountain_job_is_not_named_out_of_the_litter_enum():
    """`work_start` carries `action`, and `action` is a different enum here.

    A refill rendered as "Odor removal - work started" — litter-box vocabulary
    on a device with no litter — because one global `WORK_MODES` was applied to
    whatever sent the field.
    """
    from petkit_local.events import decode

    assert decode.event_label("work_start", {"action": 1}, "w7h") \
        == "Flush - work started"
    assert decode.event_label("work_start", {"action": 5}, "w7h") \
        == "Water change - work started"
    # The litter box keeps its own, unchanged.
    assert decode.event_label("work_start", {"action": 2}, "t5") \
        == "Odor removal - work started"
    # And the Debug view reads the same language as the card.
    action = next(f for f in decode.decode_content("work_start", {"action": 1}, "w7h")
                  if f.key == "action")
    assert action.text == "flush"


def test_the_esp32_fountains_keep_the_default_work_enum():
    """Narrowed to the W7H, not to the category: nothing says a W5 shares an
    enum read out of one model's firmware."""
    assert codes.work_modes_for("w5") is codes.WORK_MODES
    assert codes.work_modes_for("w7h") is codes.FOUNTAIN_W7H_WORK_MODES
    assert codes.work_modes_for(None) is codes.WORK_MODES


# --- work actions -----------------------------------------------------------

def test_the_job_buttons_send_actions_the_firmware_accepts():
    """A `start_action` outside the whitelist is discarded by the device with
    no reply, no error and no log — indistinguishable from a lost command.

    The values are the official app's (capture 2026-10-04)."""
    from petkit_local.ha.commands import ALL_ACTIONS

    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    for key, expected in [("fountain_flush", 1),
                          ("fountain_refill", 2),
                          ("fountain_drain", 3)]:
        suffix, envelope = ALL_ACTIONS[key](device)
        assert suffix == "start"
        assert envelope["method"] == "thing.service.start"
        assert envelope["params"] == {"start_action": expected}
        assert expected in codes.FOUNTAIN_W7H_START_ACTIONS
        assert codes.FOUNTAIN_W7H_APP_ACTIONS[expected][1] == codes.CONFIRMED


def test_no_button_sends_an_action_that_is_not_a_job():
    """Two of the twenty accepted values are not work at all: 32 is the camera
    in/out handler, and 7 leaves the dispatcher onto a different queue."""
    from petkit_local.ha.commands import ALL_ACTIONS

    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    for key in ("fountain_flush", "fountain_refill", "fountain_drain"):
        _, envelope = ALL_ACTIONS[key](device)
        assert envelope["params"]["start_action"] \
            not in codes.FOUNTAIN_W7H_START_ACTIONS_NOT_WORK


def test_an_action_the_firmware_rejects_cannot_be_built():
    from petkit_local.ha.commands import _fountain_start

    for rejected in (0, 6, 8, 13, 14, 33, 99, 107):
        with pytest.raises(ValueError):
            _fountain_start(rejected)


# --- settings writes --------------------------------------------------------

def test_every_settable_w7h_field_is_one_the_firmware_dispatches():
    """A `property.set` naming a field `ctrl` has no handler for is delivered
    and silently dropped — no error, no reply, no change. From this side that is
    indistinguishable from a device that is not listening, so the only defence
    is to check the name against the firmware's own handler list before
    publishing an entity that writes it.

    This is what retired the pause/resume buttons on this model: they wrote
    `power`, which is absent from that list.
    """
    from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, handle_ha_command

    device = Device(device_type="w7h", petkit_id=1, serial_number="SN")
    device.settings = defaults.default_settings(device)

    checked = 0
    for entity in get_entities_for_device(device):
        if entity.component not in ("switch", "number", "select"):
            continue
        if entity.value_path.startswith("capabilities."):
            continue          # local-only; the STS reply is the control point
        result = handle_ha_command(
            device, entity, "ON" if entity.component == "switch" else "1")
        assert result is not None, f"{entity.key} sends nothing at all"
        suffix, envelope = result
        assert suffix == PROPERTY_SET_SUFFIX, f"{entity.key} -> {suffix}"
        field = next(iter(envelope["params"]))
        assert field in codes.FOUNTAIN_W7H_SET_FIELDS, (
            f"{entity.key} writes {field!r}, which this firmware's ctrl "
            f"registers no set handler for — the device would ignore it")
        checked += 1
    assert checked >= 19, f"only {checked} settable entities checked"


def test_the_settings_envelope_matches_what_the_device_was_seen_to_accept():
    """Shape pinned against a real `thing/service/property/set` captured on the
    wire to this device (2026-07-31)."""
    from petkit_local.ha.commands import make_mqtt_property_set

    envelope = make_mqtt_property_set({"petDetection": 1})
    assert envelope["method"] == "thing.service.property.set"
    assert envelope["version"] == "1.0.0"
    assert envelope["params"] == {"petDetection": 1}
    assert str(int(envelope["id"]))          # an epoch-second string


def test_the_settings_topic_is_covered_by_what_the_broker_subscribes_it_to():
    """The W7H, like the T5, sends no SUBSCRIBE of its own — the broker
    subscribes it on connect. A settings publish landing outside those filters
    would be accepted by the broker and dropped without a trace."""
    from petkit_local.mqtt import topics

    pk, dn = "a1c6dbcb01", "d_w7h_20260205W90005"
    topic = topics.service_topic(pk, dn, "property/set")
    assert topic == f"/sys/{pk}/{dn}/thing/service/property/set"
    filters = topics.downstream_filters(pk, dn)
    # `#` matches every remaining level, which is what makes one filter enough.
    assert f"/sys/{pk}/{dn}/thing/service/#" in filters


def test_the_fountain_events_seen_in_a_live_log_are_not_filed_as_other():
    """From a running W7H (2026-08-01). `add_water_over` had no row at all and
    rendered as `add_water_over (other)`; `work_start` was marked litter-only
    while this fountain plainly sends it."""
    for name in ("work_start", "drink_start", "add_water_over"):
        code = codes.lookup(name, "w7h")
        assert code is not None, f"{name} has no row"
        assert code.kind != codes.KIND_OTHER, name
        assert "w7h" in code.families, f"{name} does not list the fountain"


# --- matched to the official app (capture 2026-10-04, fw 456) ---------------

def _w7h_index():
    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    return device, {e.key: e for e in get_entities_for_device(device)}


def test_start_action_5_is_recorded_as_a_crash_and_can_never_be_built():
    """`start_action: 5` dropped the MQTT session within 8 ms and rebooted the
    device by watchdog, 3 times out of 3. It is still on the firmware's accept
    list — which is what lets it reach the job that kills `ctrl`."""
    from petkit_local.ha.commands import _fountain_start

    assert 5 in codes.FOUNTAIN_W7H_START_ACTIONS
    assert codes.FOUNTAIN_W7H_START_ACTIONS_CRASH[5][1] == codes.CONFIRMED
    assert 5 not in codes.FOUNTAIN_W7H_APP_ACTIONS
    with pytest.raises(ValueError):
        _fountain_start(5)


def test_no_action_anywhere_sends_start_action_5_to_a_w7h():
    """Not just the W7H's own buttons: the panel posts any `ALL_ACTIONS` key by
    name, without asking which family it belongs to."""
    from petkit_local.ha.commands import ALL_ACTIONS

    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    assert "fountain_water_change" not in ALL_ACTIONS
    for key, build in ALL_ACTIONS.items():
        try:
            result = build(device)
        except ValueError:
            continue
        if result is None:
            continue
        _, envelope = result
        params = envelope.get("params") or {}
        assert params.get("start_action") not in codes.FOUNTAIN_W7H_START_ACTIONS_CRASH, key


def test_only_an_app_action_can_be_built():
    """Accepted by the firmware is not enough; 4 is on its list too and no
    screen of the app sends it."""
    from petkit_local.ha.commands import _fountain_start

    for value in codes.FOUNTAIN_W7H_START_ACTIONS - set(codes.FOUNTAIN_W7H_APP_ACTIONS):
        with pytest.raises(ValueError):
            _fountain_start(value)


def test_the_w7h_buttons_are_exactly_the_apps_three_jobs():
    _, idx = _w7h_index()
    buttons = sorted(k for k, e in idx.items() if e.component == "button")
    assert buttons == ["fountain_drain", "fountain_flush", "fountain_refill"]


def test_controls_the_app_does_not_have_are_gone():
    _, idx = _w7h_index()
    for key in ("fountain_water_change", "power_off", "power_on", "heater",
                "disturb_mode", "vomit_detection", "wifi_light_assist"):
        assert key not in idx, key
    # Read-only, and not a control at all.
    assert "heater_installed" in idx
    # And nothing published writes a field the app never offers.
    written = {e.setting_field for e in idx.values() if e.value_path.startswith("settings.")}
    assert not written & codes.FOUNTAIN_W7H_SET_FIELDS_NOT_IN_APP


def test_fountain_and_sleep_time_are_minutes_one_to_sixty():
    """The app's pickers run 1-60 min (15, 28 and 60 captured). Hours 1-24 here
    meant a picked 15 could not even be entered, and 12 meant something
    different to each side."""
    from petkit_local.devices.base import Refused
    from petkit_local.ha.commands import handle_ha_command

    device, idx = _w7h_index()
    for key, field in (("fountain_time", "fountainTime"), ("sleep_time", "sleepTime")):
        entity = idx[key]
        assert (entity.unit, entity.min_value, entity.max_value) == ("min", 1, 60)
        _, env = handle_ha_command(device, entity, "28")
        assert env["params"] == {field: 28}
        with pytest.raises(Refused):
            handle_ha_command(device, entity, "61")
        with pytest.raises(Refused):
            handle_ha_command(device, entity, "0")
    # The override replaced the family entity in place, not beside it.
    keys = [e.key for e in get_entities_for_device(device)]
    assert keys.count("fountain_time") == 1 and keys.count("sleep_time") == 1


def test_volume_runs_one_to_nine():
    from petkit_local.devices.base import Refused
    from petkit_local.ha.commands import handle_ha_command

    device, idx = _w7h_index()
    assert (idx["volume"].min_value, idx["volume"].max_value) == (1, 9)
    with pytest.raises(Refused):
        handle_ha_command(device, idx["volume"], "0")


def test_the_flow_mode_enum_is_the_apps():
    _, idx = _w7h_index()
    assert idx["flow_mode"].options == [
        "do_not_flow", "continuous", "intermittent", "motion_activated"]


@pytest.mark.parametrize("key,field", [
    ("refill_disturb_period", "awDisturbMultiRange"),
    ("signal_lights_disturb_period", "wlDisturbMultiRange"),
    ("voice_disturb_period", "toneMultiRange"),
])
def test_a_quiet_window_is_written_in_the_shape_the_app_sends(key, field):
    """Captured: `{"awDisturbMultiRange": "{\\"awDisturbMultiRange\\":[[0,600]]}"}`
    — a JSON STRING wrapping its own key, minutes since midnight."""
    from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, handle_ha_command

    device, idx = _w7h_index()
    suffix, env = handle_ha_command(device, idx[key], "00:00-10:00")
    assert suffix == PROPERTY_SET_SUFFIX
    assert env["method"] == "thing.service.property.set"
    assert env["params"] == {field: '{"%s":[[0,600]]}' % field}
    # Stored where `dev_multi_config` serves it from, not in settings.
    assert device.config["multi_config"][field] == [[0, 600]]
    assert field not in device.config.get("settings", {})
    assert defaults.multi_config_ranges(device)[field] == [[0, 600]]


def test_a_quiet_window_crossing_midnight_round_trips():
    """`[[1140, 660]]` is the app's 19:00-11:00."""
    from petkit_local.ha.commands import format_ranges, parse_ranges

    assert format_ranges([[1140, 660]]) == "19:00-11:00"
    assert parse_ranges("19:00-11:00") == [[1140, 660]]
    assert format_ranges([[0, 1440]]) == "00:00-24:00"
    assert parse_ranges("00:00-24:00") == [[0, 1440]]
    assert parse_ranges(" 22:00-07:00 , 12:00-13:30 ") == [[1320, 420], [720, 810]]
    assert parse_ranges("") == []
    assert format_ranges([]) == ""
    # The weekly object a camera window takes has no text form.
    assert format_ranges([{"enable": 1, "rpt": "1", "time": [[0, 1440]]}]) is None


@pytest.mark.parametrize("bad", [
    "19:00", "25:00-01:00", "24:30-01:00", "10:60-11:00", "a-b", "7-8", "10:5-11:00",
    "19:00-11:00,", "-", "²:00-03:00", "10:00-10:00", "123:00-01:00",
])
def test_a_quiet_window_that_is_not_ranges_is_refused(bad):
    from petkit_local.devices.base import Refused
    from petkit_local.ha.commands import handle_ha_command

    device, idx = _w7h_index()
    with pytest.raises(Refused):
        handle_ha_command(device, idx["refill_disturb_period"], bad)
    assert "awDisturbMultiRange" not in (device.config.get("multi_config") or {})


async def test_the_state_document_shows_the_window_the_device_is_served():
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.ha.discovery import build_discovery_payload
    from petkit_local.ha.publisher import HAPublisher

    reg = DeviceRegistry()
    device = reg.get_or_create(petkit_id=1, device_type="w7h", serial_number="W")
    device.config["multi_config"] = {"awDisturbMultiRange": [[1140, 660]]}
    doc = HAPublisher(reg, {})._build_state(device)["multi_config"]
    assert doc["awDisturbMultiRange"] == "19:00-11:00"
    assert doc["wlDisturbMultiRange"] == ""          # unset: no window
    assert doc["toneMultiRange"] == "00:00-24:00"    # served default
    assert "cameraMultiRange" not in doc

    entity = next(e for e in get_entities_for_device(device)
                  if e.key == "refill_disturb_period")
    payload = build_discovery_payload(
        entity=entity, device_id=1, device_type="w7h", device_name="W",
        serial_number="W", state_topic="petkit-local/1/state")
    assert "multi_config" in payload["value_template"]
    assert payload["max"] == 255


async def test_retired_entities_are_cleared_from_home_assistant():
    """Leaving an entity out stops announcing it; HA keeps the retained config
    and the entity — a button among them stays pressable. An empty retained
    payload on the same topic is how HA is told it is gone."""
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.ha.discovery import EntityDef, discovery_topic
    from petkit_local.ha.publisher import HAPublisher
    from tests._fakes import FakeMqttClient

    reg = DeviceRegistry()
    device = reg.get_or_create(petkit_id=7, device_type="w7h", serial_number="W")
    pub = HAPublisher(reg, {})
    pub._client = FakeMqttClient()
    pub._connected = True
    await pub.publish_discovery(device)

    cleared = {topic for topic, payload, kw in pub._client.published
               if payload == "" and kw.get("retain")}
    for component, key in (("button", "fountain_water_change"), ("button", "power_off"),
                           ("button", "power_on"), ("switch", "heater"),
                           ("switch", "disturb_mode"), ("switch", "vomit_detection"),
                           ("switch", "wifi_light_assist")):
        topic = discovery_topic(EntityDef(component=component, key=key, name=key), 7,
                                pub._prefix)
        assert topic in cleared, key
    # Nothing still published is cleared.
    for entity in get_entities_for_device(device):
        assert discovery_topic(entity, 7, pub._prefix) not in cleared, entity.key


def test_retirement_is_per_model():
    """A T5 keeps its power buttons; only the W7H retired them."""
    from petkit_local.ha.categories import get_retired_entities_for_device

    t5 = Device(device_type="t5", petkit_id=1, serial_number="T")
    assert get_retired_entities_for_device(t5) == []
    keys = {e.key for e in get_entities_for_device(t5)}
    assert {"power_off", "power_on"} <= keys


# --- seeding ----------------------------------------------------------------

def test_a_w7h_is_served_no_invented_settings():
    """Its `property/post` carries no settings, so a seed is never replaced by
    the device's own value, and `to_device_info` serves the block back as its
    configuration. `fountainMode: 0` is "do not flow"."""
    from petkit_local.devices.payloads import to_device_info

    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    assert defaults.default_settings(device) == {}
    served = to_device_info(device)["result"]["settings"]
    for key in ("fountainMode", "addWaterSwitch", "heaterSwitch", "disturbMode"):
        assert key not in served, key

    # A value somebody chose is still served.
    device.config["settings"] = {"fountainMode": 1}
    assert to_device_info(device)["result"]["settings"]["fountainMode"] == 1


def test_the_old_seed_is_taken_back_once_and_only_where_unchanged():
    from petkit_local.devices.registry import DeviceRegistry

    stored = {**defaults.RETIRED_W7H_SEED, "fountainMode": 1, "volume": 5,
              "vomitDetection": 1, "wifiLightAssist": 0, "heaterSwitch": 1}
    reg = DeviceRegistry()
    reg._restore({"3": Device(device_type="w7h", petkit_id=3, serial_number="W",
                              config={"settings": dict(stored)}).to_dict()})
    settings = reg.get(3).config["settings"]
    # Set by somebody (differs from the seed) — kept.
    assert settings == {"fountainMode": 1, "volume": 5}

    # Once: a 0 chosen after the cleanup survives the next load.
    settings["fountainMode"] = 0
    reg2 = DeviceRegistry()
    reg2._restore(reg._serialize())
    assert reg2.get(3).config["settings"]["fountainMode"] == 0


def test_other_families_keep_their_seed():
    t5 = Device(device_type="t5", petkit_id=1, serial_number="T")
    assert defaults.retire_stale_seed(t5) == []
    assert defaults.default_settings(t5)


def test_a_zero_volume_is_dropped_with_the_old_seed():
    device = Device(device_type="w7h", petkit_id=4, serial_number="W",
                    config={"settings": {"volume": 0}})
    assert defaults.retire_stale_seed(device) == ["volume"]
    assert device.config["settings"] == {}


def test_the_cycle_numbers_run_one_to_seven_days_like_the_app():
    """Both of the app's cycle pickers offer 1-7 days (W7H fw 456, 2026-10-04)."""
    device = Device(device_type="w7h", petkit_id=1, serial_number="W")
    numbers = {e.key: e for e in get_entities_for_device(device) if e.component == "number"}
    for key in ("water_change_cycle", "flush_cycle"):
        assert (numbers[key].min_value, numbers[key].max_value, numbers[key].unit) == (1, 7, "d")
