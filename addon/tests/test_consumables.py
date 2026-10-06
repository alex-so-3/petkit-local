"""The N50/N60 replacement dates we record ourselves.

The N50 has no representation anywhere in the device protocol: PetKit keeps its
replacement date in their own account database, and resetting it from their app
sends the box nothing but a display poke. So "N50 Days Left" can only ever read
what WE remember, which is what these tests pin.

The N60 does have a device field (`sprayResetTime`), and the device wins there --
it is resettable from PetKit's app too. Our copy exists so the countdown
survives a restart and so `to_device_info` never echoes a zero over a live reset
date.
"""
import json
import time

from petkit_local.devices import payloads
from petkit_local.devices.base import Device
from petkit_local.devices.state_parsers import (CONSUMABLE_RECORD_KEY, DEODORANT_TOTAL_DAYS,
                                                SPRAY_TOTAL_DAYS, apply_consumable_state,
                                                normalize_property_params,
                                                record_consumable_reset)
from petkit_local.ha.commands import handle_ha_command
from petkit_local.ha.categories import get_entities_for_device


def _dev():
    return Device(device_type="t5", petkit_id=1, serial_number="SN")


def _entity(dev, key):
    return next(e for e in get_entities_for_device(dev) if e.key == key)


def test_pressing_reset_n50_records_the_date_and_fills_the_countdown():
    # The whole point: before the press there is no source for this sensor at
    # all, and no amount of waiting for the device would produce one.
    dev = _dev()
    assert "deodorantLeftDays" not in dev.state

    handle_ha_command(dev, _entity(dev, "reset_n50"), "PRESS")

    assert dev.state["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS
    assert dev.config[CONSUMABLE_RECORD_KEY]["n50"] > 0


def test_pressing_reset_n60_records_the_date_and_still_sends_the_real_command():
    # Unlike the N50, code 10 genuinely works on the box, so the record must be
    # an addition to the command rather than a replacement for it.
    dev = _dev()
    result = handle_ha_command(dev, _entity(dev, "reset_n60"), "PRESS")

    assert result is not None, "the device command must still be sent"
    suffix, envelope = result
    assert suffix == "start"
    assert envelope["params"] == {"start_action": 10}
    assert dev.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS
    assert dev.config[CONSUMABLE_RECORD_KEY]["n60"] > 0


def test_the_record_survives_a_restart_which_wipes_state():
    # `state` is rebuilt from the device's next contact; `config` persists. For
    # the N50 there is no next contact that would ever carry the date, so
    # storing it in state would lose it on every add-on restart.
    dev = _dev()
    record_consumable_reset(dev, "n50", time.time() - 10.5 * 86400)
    record_consumable_reset(dev, "n60", time.time() - 10.5 * 86400)

    restarted = Device.from_dict(json.loads(json.dumps(dev.to_dict())))
    assert restarted.state == {}, "state is not persisted, and must not be"

    apply_consumable_state(restarted)
    # 10.5 days used, so 19.5/34.5 remain -> rounded up, a part-used day counts.
    assert restarted.state["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS - 10
    assert restarted.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS - 10


def test_to_device_info_echoes_the_recorded_stamp_rather_than_zero():
    """The clobber this closes: `ctrl` has `set sprayResetTime (%d)`, so handing
    the box a 0 in the window after a restart would move the N60 countdown's
    origin to now ON THE DEVICE, costing the owner the rest of a cartridge's
    warning. PetKit's own reply carries the true value here."""
    dev = _dev()
    stamp = time.time() - 3 * 86400
    record_consumable_reset(dev, "n60", stamp)

    restarted = Device.from_dict(json.loads(json.dumps(dev.to_dict())))
    assert restarted.state == {}
    assert int(payloads.to_device_info(restarted)["result"]["sprayResetTime"]) == int(stamp)

    # A device we have never heard from still gets 0 -- there is nothing to
    # preserve, and PetKit sends the field rather than omitting it.
    assert payloads.to_device_info(_dev())["result"]["sprayResetTime"] == 0


def test_the_device_stamp_wins_over_ours_and_is_copied_into_the_record():
    # The N60 is resettable from PetKit's app, which moves the box's stamp
    # without telling us, so the device is authoritative. Copying it in is what
    # makes the countdown survive the next restart.
    dev = _dev()
    record_consumable_reset(dev, "n60", time.time() - 20 * 86400)   # ours: stale
    fresh = int(time.time() - 2 * 86400)                            # box: newer

    dev.state.update(normalize_property_params("t5", {"sprayResetTime": fresh}))
    apply_consumable_state(dev)

    assert dev.state["sprayLeftDays"] == SPRAY_TOTAL_DAYS - 2  # exactly 2 days
    assert dev.config[CONSUMABLE_RECORD_KEY]["n60"] == fresh


def test_an_unknown_consumable_name_is_refused_rather_than_guessed():
    dev = _dev()
    assert record_consumable_reset(dev, "n99") is None
    assert not dev.config.get(CONSUMABLE_RECORD_KEY)


def test_every_state_refresh_site_recomputes_the_countdowns():
    """The recurring bug in this codebase is one transport getting a fix the
    other does not -- it has now happened three times on this exact pair of
    fields. Any module that refreshes state from a report must also recompute
    the consumables, or an N50 countdown silently vanishes on that path."""
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent / "petkit_local"
    offenders = []
    for path in root.rglob("*.py"):
        text = path.read_text()
        if "normalize_property_params(" not in text:
            continue
        # The definition site itself, and modules that only re-export it.
        if path.name == "state_parsers.py":
            continue
        if "apply_consumable_state(" not in text:
            offenders.append(str(path.relative_to(root)))
    assert not offenders, (
        "these refresh state but never recompute the consumable countdowns: "
        f"{offenders}"
    )


def test_the_countdowns_are_ready_before_the_device_says_anything():
    """A restart wipes `state`, and the N50 has no device input that would ever
    refill it -- so reading the countdown must not depend on the device
    reporting first, or "N50 Days Left" is unknown after every restart. Both
    document builders recompute, which also keeps the number honest as the
    calendar moves under a device that has gone quiet."""
    from petkit_local.web.api.devices import _state_doc

    dev = _dev()
    record_consumable_reset(dev, "n50", time.time() - 4 * 86400)
    restarted = Device.from_dict(json.loads(json.dumps(dev.to_dict())))
    assert restarted.state == {}

    doc = _state_doc(restarted)
    assert doc["state"]["deodorantLeftDays"] == DEODORANT_TOTAL_DAYS - 4


def test_a_settings_write_does_not_shrink_the_served_block():
    """One changed field must not erase every other default from the answer.

    `handle_ha_command` stores only the key it changed, and both the device
    payload and the HA state document used to SUBSTITUTE the stored dict for
    the defaults rather than merge. So the first change to any setting cut
    `dev_device_info`'s settings block down to that one key — and the device
    reads that block as its whole configuration.

    Pinned on a camera litter box, which has the largest seed. The W7H, which
    this was first seen on, is no longer seeded at all
    (`defaults.RETIRED_W7H_SEED`).
    """
    from petkit_local.devices.defaults import default_settings
    from petkit_local.devices.payloads import to_device_info

    dev = Device(device_type="t5", petkit_id=9)
    full = set(default_settings(dev))
    assert len(full) > 1

    dev.config.setdefault("settings", {})["manualLock"] = 1
    served = to_device_info(dev)["result"]["settings"]

    assert full <= set(served), "defaults were dropped by a single write"
    assert served["manualLock"] == 1, "the stored value must win over the default"


def test_a_camera_feeder_is_told_its_cloud_storage_is_active():
    """`capacity[]`/`cloudProduct` gate cloud storage on EVERY camera.

    They were sent only to camera litter boxes, so a camera feeder recorded a
    feed clip and never staged or uploaded it — `ctrl` logs "feed not upload
    pic and video ..." and the event reports `media: 0`. Found independently
    on a D4H and a D4SH.
    """
    from petkit_local.devices.payloads import to_device_info

    for device_type in ("d4h", "d4sh", "t5"):
        served = to_device_info(Device(device_type=device_type, petkit_id=3))["result"]
        assert served.get("capacity"), f"{device_type} got no capacity block"
        assert served.get("cloudProduct"), f"{device_type} got no cloudProduct"

    # A non-camera feeder has no cloud storage to enable, and must not be told
    # it has: the block would be describing a service the hardware lacks.
    plain = to_device_info(Device(device_type="d4", petkit_id=4))["result"]
    assert "capacity" not in plain and "cloudProduct" not in plain


def test_a_camera_feeder_is_seeded_with_the_upload_enables():
    """The device never reports these, and `to_device_info` serves seeds back,
    so an absent key reads to the firmware as a zero."""
    from petkit_local.devices.defaults import default_settings

    seeded = default_settings(Device(device_type="d4sh", petkit_id=5))
    assert seeded["feedPicture"] == 1
    assert seeded["eatVideo"] == 1
    assert seeded["upload"] == 1


def test_the_camera_gating_schedule_is_sent_as_objects():
    """A camera feeder's recording window is served as `cameraMultiNew`, the
    key its `pk_parse_cameraMultiNew_func` parser reads (it saves the value
    into its internal `cameraMultiRange`). The value is a `weekly` object with
    `rpt`/`time`; a bare `[[start, end]]` makes every lookup null, so the table
    stays empty, the camera never arms (`cameraStatus` 0) and every feed reports
    `media: 0`. Serving the internal `cameraMultiRange` name reaches no parser
    at all — confirmed live on a D4SH — so the KEY matters as much as the shape.
    """
    from petkit_local.devices.defaults import multi_config_ranges

    ranges = multi_config_ranges(Device(device_type="d4sh", petkit_id=6))
    assert "cameraMultiNew" in ranges and "cameraMultiRange" not in ranges
    entries = ranges["cameraMultiNew"]
    assert isinstance(entries[0], dict), "still the bare-range shape"
    assert "rpt" in entries[0] and "time" in entries[0]


def test_a_dual_hopper_feed_counts_toward_the_daily_totals():
    """Both sensors read `feedState`, which no feeder report carries.

    A D4SH reports the amount PER HOPPER (`real_amount1`/`real_amount2`), so
    reading only the unsuffixed `real_amount` would leave a dual-hopper
    feeder's counters permanently at zero.
    """
    from petkit_local.events import normalize

    dev = Device(device_type="d4sh", petkit_id=11)
    normalize.apply_derived_state(dev, "feed_over", {
        "day": 20260808, "real_amount1": 0, "real_amount2": 12})
    normalize.apply_derived_state(dev, "feed_over", {
        "day": 20260808, "real_amount1": 3, "real_amount2": 0})
    assert dev.state["feedState"] == {"day": 20260808, "times": 2,
                                      "realAmountTotal": 15}


def test_a_jammed_feed_dispensed_nothing_and_counts_as_nothing():
    from petkit_local.events import normalize

    dev = Device(device_type="d4sh", petkit_id=12)
    normalize.apply_derived_state(dev, "feed_over", {
        "day": 20260808, "real_amount1": 0, "real_amount2": 0, "err_code": 8})
    assert "feedState" not in dev.state


def test_the_totals_start_over_when_the_device_says_the_day_changed():
    """`day` is the DEVICE's reading of which day it is, so the rollover
    follows its clock rather than the container's."""
    from petkit_local.events import normalize

    dev = Device(device_type="d4h", petkit_id=13)
    normalize.apply_derived_state(dev, "feed_over", {"day": 20260808, "real_amount": 10})
    normalize.apply_derived_state(dev, "feed_over", {"day": 20260809, "real_amount": 4})
    assert dev.state["feedState"] == {"day": 20260809, "times": 1,
                                      "realAmountTotal": 4}


def test_the_feed_totals_are_not_persisted():
    """`Device.to_dict` excludes `state` on purpose and this lives there."""
    from petkit_local.events import normalize

    dev = Device(device_type="d4h", petkit_id=14)
    normalize.apply_derived_state(dev, "feed_over", {"day": 20260808, "real_amount": 10})
    assert "feedState" not in json.dumps(dev.to_dict())


def test_pressing_reset_desiccant_starts_its_countdown():
    """The sensor and the button both existed; nothing connected them, so
    "Desiccant Days Left" could never hold a value."""
    dev = Device(device_type="d4sh", petkit_id=15)
    handle_ha_command(dev, _entity(dev, "reset_desiccant"), "PRESS")
    assert dev.state["desiccantLeftDays"] == 30
