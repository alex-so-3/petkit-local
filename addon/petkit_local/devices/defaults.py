"""The per-category seed data: what a device is served before anyone has spoken.

A codename selects a settings block, a set of schedule ranges and the editors
the panel offers for them, and all three are tables rather than behaviour. The
one rule they share is that a default may not decide anything on the owner's
behalf: a seeded value is served straight back to the device as its own
configuration, so an invented one is not a harmless placeholder but an
instruction. Where that rule bites, the comment below the table says so.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from petkit_local.events.codes import FOUNTAIN_NEXT_GEN
from petkit_local.utils.const import DEVICE_TYPES_FEEDER_DUAL

if TYPE_CHECKING:  # `devices.ble` imports `devices.registry`, which imports `devices.base`.
    from petkit_local.devices.base import Device


#: The whole day, in the two shapes a `*MultiRange` comes in. `1440` is the end
#: of the day and is the only place it ever appears — every "entire day" payload
#: PetKit sends is exactly `[0, 1440]`.
_ALL_DAY = [[0, 1440]]
_ALL_DAY_WEEKLY = [{"enable": 1, "rpt": "1,2,3,4,5,6,7", "time": [[0, 1440]]}]

#: What a device is served for a schedule range nobody has set.
#:
#: All day everywhere, with ONE exception, and the exception is the reason this
#: is a table rather than a constant. Every entry here is a window during which
#: some function is active, so "all day" means "always" — the screen is on, the
#: camera records, detection runs. That restricts nothing and decides nothing on
#: the owner's behalf.
#:
#: `distrubMultiRange` is the cleaning do-not-disturb, and there "always" means
#: the box never cleans on its own. An all-day default would quietly disable
#: automatic cleaning on every litter box that has not been given a window,
#: which is the opposite of harmless. It stays empty.
#:
#: `toneMultiRange` is a do-not-disturb too and IS all day, deliberately: it
#: only silences voice prompts, and it does nothing at all until `toneMode` is
#: switched on — which is off by default. Somebody who turns on Do Not Disturb
#: without picking hours means all of them.
MULTI_RANGE_DEFAULTS: dict[str, Any] = {
    "lightMultiRange": _ALL_DAY,
    "toneMultiRange": _ALL_DAY,
    "cameraMultiRange": _ALL_DAY_WEEKLY,
    # Format B, like `cameraMultiRange` and for the same reason: this is a
    # gating field and `pk_parse_cameraMultiNew_func` reads `rpt`/`time` off
    # each element as an object. A bare `[[start, end]]` makes every lookup
    # null, so `cameraRangeTable` stays empty and the D4SH logs "camera not
    # enable" while reporting `camera: 1`.
    #
    # It had the plain shape because the feeder branch was written with one
    # all-day literal for all four of its fields, while the litter branch got
    # Format B from a capture. Our own T5 is the proof the object form is what
    # a gating field wants: it is served `cameraMultiRange` this way and
    # records.
    "cameraMultiNew": _ALL_DAY_WEEKLY,
    "detectMultiRange": _ALL_DAY,
    "distrubMultiRange": [],
    # A W7H's two quiet windows: `aw` is addWater (the firmware keeps
    # `awDisturb*` in the same vocabulary as `addWaterMode`, `addWaterSwitch`
    # and `addWaterTimeAllow`), `wl` the signal lights — the image never says,
    # but the official app's "Signal lights" screen is what writes it
    # (capture 2026-10-04).
    #
    # Empty for the same reason `distrubMultiRange` is: these SILENCE a job,
    # and an empty window silences nothing. Both are gated by their own
    # `awDisturbMode`/`wlDisturbMode` switch as well.
    "awDisturbMultiRange": [],
    "wlDisturbMultiRange": [],
}


def default_settings(device: Device) -> dict[str, Any]:
    """The settings block a device of this category starts life with.

    Seeded into `config["settings"]` at registration and served by
    `to_device_info` until the device or Home Assistant overwrites a key, so
    every switch/number/select entity has a value to render on day one.
    Missing keys are also backfilled on load, see
    `devices/registry.py::_merge_default_settings`. Empty for a codename no
    category claims.

    The litter set is checked against a captured `dev_device_info` from the
    real cloud (2026-07-27). `lightRange`/`disturbRage` are NOT in it — the
    cloud carries those ranges in `dev_multi_config` instead — so they are
    not seeded here. A device that already has them keeps them; nothing
    strips stored settings, because those are the owner's state.

    `sandType` is not seeded either, and it is the clearest case of why a
    seed is not a free default. A controlled 1 -> 2 -> 3 -> 1 run through
    the app gives the whole enum — 1 clay/ore, 2 tofu, 3 mixed — and `0` is
    not in it. Seeding `sandType: 0` therefore has `to_device_info` serve that
    back to the box as the litter it is filled with: a value outside its own
    vocabulary. Our own T5 reports 2, which is where a real value comes from.
    """
    if device.is_litter:
        base = {
            "manualLock": 0, "clickOkEnable": 1,
            "avoidRepeat": 1, "underweight": 1, "kitten": 0,
            "bury": 0, "autoWork": 1,
            "fixedTimeClear": 0, "autoIntervalMin": 0,
            "stillTime": 30, "stopTime": 600, "unit": 0,
            "language": "en_US", "deepClean": 0, "disturbMode": 0,
            "lightest": 1680, "downpos": 0, "sandSaving": 0,
            "lightMode": 0, "lightConfig": 1,
            "lightMultiRange": [],
        }
        if device.is_camera:
            base.update({
                "camera": 1, "microphone": 1, "night": 1,
                "timeDisplay": 1, "tumbling": 0,
                "cameraLight": 1, "highlight": 1,
                "autoProduct": 0, "upload": 1,
                "preLive": 1, "liveEncrypt": 1,
                "toiletDetection": 1, "petDetection": 1,
                "petNotify": 1, "petNotifyInterval": 60,
                "lightAssist": 1, "toiletLight": 0,
                "toneMode": 0, "toneMultiRange": [[1320, 360]], "toneConfig": 2,
                "systemSoundEnable": 1, "volume": 1,
                "deepSpray": 0, "fixedTimeSpray": 1, "autoSpray": 1,
                "autoIntervalSpray": 0,
                "sandFullWeight": [3500, 5800, 3000, 3500, 3500],
                "sandSetUseConfig": [[2, 2, 4]] * 4,
                "deodorantNotify": 1, "sprayNotify": 1,
                "phDetection": 0, "voice": 1, "logSwitch": 1,
            })
        return base
    if device.is_feeder:
        base = {
            "manualLock": 0, "lightMode": 0, "foodWarn": 0,
            "foodWarnRange": [480, 1200],
            "surplusControl": 0, "surplusStandard": 2,
            "numLimit": 5,
        }
        if device.is_camera:
            base.update({
                "camera": 1, "microphone": 1, "night": 1,
                "timeDisplay": 1, "moveDetection": 1, "moveSensitivity": 1,
                "petDetection": 1, "petSensitivity": 3,
                "eatDetection": 1, "eatSensitivity": 3,
                "soundEnable": 0, "systemSoundEnable": 1,
                "volume": 7, "smartFrame": 1,
                "toneMode": 0, "disturbMode": 0,
                "feedSound": 0, "selectedSound": -1,
                "detectInterval": 0,
                "logSwitch": 1,
                # The three enables a camera feeder needs before it will stage
                # and upload a clip. `feedPicture` is the direct gate,
                # `eatVideo` the eat-clip enable, `upload` the master switch —
                # the camera-litter block above has carried `upload: 1` all
                # along, and this branch had none of the three. The device does
                # not report them, and `to_device_info` serves seeded settings
                # back, so an absent key reads to `ctrl` as a zero: it logs
                # "feed not upload pic and video ..." and every event says
                # `media: 0`.
                "feedPicture": 1, "eatVideo": 1, "upload": 1,
            })
        return base
    if device.is_water_fountain:
        if device.device_type.lower() in FOUNTAIN_NEXT_GEN:
            # NOTHING for a W7H — see `RETIRED_W7H_SEED` for what this was and
            # why every value in it was an instruction rather than a default.
            return {}
        return dict(_FOUNTAIN_SEED)
    if device.is_purifier:
        return {
            "lightMode": 0, "manualLock": 0, "sound": 0,
        }
    return {}


#: The fountain family's seed. Never reaches a real device today: the W7H is the
#: only fountain with WiFi and is not seeded, and the rest are Bluetooth-only
#: and never become a `Device` (`utils/const.py::DEVICE_TYPES_BLE_ONLY`). Kept
#: so a WiFi EverSweet that turns up starts where the family always did.
_FOUNTAIN_SEED: dict[str, Any] = {
    "manualLock": 0, "lightMode": 0, "disturbMode": 0,
    "addWaterSwitch": 0, "petDetection": 0, "heaterSwitch": 0,
    "fountainMode": 0, "fountainTime": 12, "sleepTime": 12,
}

#: What a W7H was seeded with until 2026-10-04, and is no longer.
#:
#: A W7H's `property/post` carries no settings at all, so nothing ever replaced
#: these with the device's own values — and `to_device_info` serves the settings
#: block back to the device as its configuration, on every `dev_device_info`,
#: including the one after each reboot. Every entry was therefore a write we
#: made on the owner's behalf, and two were actively harmful:
#:
#:   * `fountainMode: 0` is "do not flow" (`codes.FOUNTAIN_W7H_APP_SET_FIELDS`):
#:     a running fountain told to stop pumping by a value nobody chose;
#:   * `addWaterSwitch: 0` turns auto refill OFF.
#:
#: Not seeding is the only value that is not a guess: the HA entity reads
#: unknown until somebody sets it, as every other W7H setting already does
#: (`UNSEEDED_BY_DESIGN` in tests/test_entity_backing.py). Whether the firmware
#: reads a key ABSENT from that block as 0 or keeps its own value is not known;
#: if it reads 0, leaving the key out is no worse than serving the 0 was.
#:
#: The registry also takes these values BACK from a W7H that was registered
#: while they were seeded (`retire_stale_seed`), because a stored seed is served
#: exactly like a stored choice.
RETIRED_W7H_SEED: dict[str, Any] = dict(_FOUNTAIN_SEED)

#: `config` flag recording that `retire_stale_seed` has run for a device, so a
#: value somebody sets AFTER it — `fountainMode: 0` on purpose — is kept.
RETIRED_SEED_FLAG = "retired_seed_cleared"


#: Stored W7H settings dropped whatever their value: written by entities that
#: no longer exist because the official app has no such control
#: (`codes.FOUNTAIN_W7H_SET_FIELDS_NOT_IN_APP`). Left in place they would keep
#: being served by `to_device_info` with nothing left to change them.
_RETIRED_W7H_FIELDS = frozenset({
    "heaterSwitch", "disturbMode", "vomitDetection", "wifiLightAssist",
})


def retire_stale_seed(device: Device) -> list[str]:
    """Drop the old W7H seed from a device's stored settings, once.

    Only a value still EQUAL to what was seeded is dropped: anything else was
    set by Home Assistant or the panel and stays. A value that happens to equal
    the seed because somebody chose it is dropped too, and that is the price of
    a migration with no record of where a value came from — it costs that
    entity reading unknown until set again, never a write to the device.

    Also dropped, whatever their value: the fields of the retired controls
    (`_RETIRED_W7H_FIELDS`), and a `volume` of 0, which is below the app's
    slider and now outside the entity's own range.

    Returns:
        The keys removed, for the caller to log. Empty for anything but a W7H,
        and on every run after the first.
    """
    if device.device_type.lower() not in FOUNTAIN_NEXT_GEN:
        return []
    if device.config.get(RETIRED_SEED_FLAG):
        return []
    device.config[RETIRED_SEED_FLAG] = True
    settings = device.config.get("settings")
    if not isinstance(settings, dict):
        return []
    removed = [key for key, seeded in RETIRED_W7H_SEED.items()
               if key in settings and settings[key] == seeded]
    removed += sorted(key for key in _RETIRED_W7H_FIELDS
                      if key in settings and key not in removed)
    if "volume" in settings and settings["volume"] == 0:
        removed.append("volume")
    for key in removed:
        del settings[key]
    return removed


def multi_config_ranges(device: Device) -> dict[str, Any]:
    """The `*MultiRange` schedules this model has, resolved to real values.

    A stored range wins. An unset one falls back to `MULTI_RANGE_DEFAULTS`,
    which is "the whole day" for everything except the cleaning
    do-not-disturb — see that table for why the one exception is not an
    inconsistency.

    The fallback is always-on for a reason, and must not be replaced by a
    plausible-looking window. A concrete literal — quiet hours of 00:40-08:40
    or 22:00-06:00, say — is a window nobody chose, pushed to every device on
    every poll and undoing anything set through PetKit's app in proxy mode. An
    always-on range restricts nothing, so it takes no decision away from the
    owner.

    The store is `config["multi_config"]` and NOT `config["settings"]`, even
    though the device writes these fields with `property.set` like any other
    setting. `to_device_info` serves the settings block straight back, and
    the real cloud does not put these keys in `dev_device_info` — it puts
    them here. Parking them in settings would add fields to a payload that a
    capture says does not carry them.

    A stored value that is not a list is IGNORED rather than served:
    `devices.json` is hand-editable, and a malformed schedule reaching the
    firmware is not something to find out about later.
    """
    stored = device.config.get("multi_config")
    stored = stored if isinstance(stored, dict) else {}

    def pick(key: str) -> Any:
        value = stored.get(key)
        if isinstance(value, list):
            return value
        # Copied, not shared: the caller edits what it is given, and these
        # are module-level literals.
        return json.loads(json.dumps(MULTI_RANGE_DEFAULTS[key]))

    keys: tuple[str, ...] = ()
    if device.is_litter:
        keys = ("lightMultiRange", "distrubMultiRange")
        if device.is_camera:
            keys += ("cameraMultiRange", "toneMultiRange")
    elif device.is_feeder and device.is_camera:
        # `cameraMultiNew`, NOT `cameraMultiRange`: the D4SH parser is
        # `pk_parse_cameraMultiNew_func`, which keys on `cameraMultiNew` and
        # saves it into its internal `cameraMultiRange`. Serving the internal
        # name (as PR #18 briefly did) reaches no parser, so the recording
        # window stays empty, the camera never arms (`cameraStatus` 0), and
        # every feed reports `media: 0` — confirmed live on a D4SH: pushing
        # `cameraMultiNew` flips `cameraStatus` to 1 and recording resumes.
        keys = ("detectMultiRange", "cameraMultiNew",
                "toneMultiRange", "lightMultiRange")
    elif device.is_water_fountain:
        # Nine of these exist in the W7-262863 image; SEVEN are sent.
        #
        # Five are confirmed by watching PetKit's own cloud write them to a
        # W7H (capture 2026-08-11): `lightMultiRange`, `toneMultiRange`,
        # `awDisturbMultiRange`, `wlDisturbMultiRange`, `cameraMultiRange`.
        # `distrubMultiRange` and `detectMultiRange` are in the firmware's
        # string table and default to something that restricts nothing, so
        # sending them takes no decision away from the owner.
        #
        # `lightAssistMultiRange` and `wifiLightAssistMultiRange` are held
        # back deliberately. They are real fields, but no capture shows a
        # value, and this reply is re-sent on every poll — an invented window
        # would overwrite whatever the owner set in PetKit's app, on repeat.
        keys = ("lightMultiRange", "toneMultiRange", "distrubMultiRange",
                "detectMultiRange", "cameraMultiRange",
                "awDisturbMultiRange", "wlDisturbMultiRange")
    return {key: pick(key) for key in keys}


def schedule_targets(device: Device) -> list[dict[str, Any]]:
    """Every schedule this device has, as the panel's editor needs them.

    One list so the panel does not re-derive which model has which schedule;
    the ranges come from `multi_config_ranges`, so what is edited is what is
    served. `kind` selects the editor, and there are only four across the
    whole product line:

      * `ranges` — `[[start, end], ...]` in MINUTES since local midnight. An
        end below its start crosses midnight and is valid.
      * `weekly` — `[{enable, rpt, time: [[s, e]]}]`: the same ranges plus
        weekdays and a switch.
      * `points` — `[{id, repeats, time, type}]`: moments, not ranges, and
        `type` says which job each belongs to (`codes.SCHEDULE_TYPES`).
      * `feed` — the feeder's own `{schedule: [{re, it, itemJsonString}]}`.
        Raw-only: `it` was an empty list in every capture and nothing here
        is going to guess what a meal item looks like.

    A W7H gets every range `multi_config_ranges` serves it; its three quiet
    windows are also HA text entities (`FOUNTAIN_W7H_RANGE_TEXT`), which write
    the same store, so both show the same window.
    """
    labels = {
        "lightMultiRange": "Screen Period" if device.is_litter else "Indicator Light Period",
        "distrubMultiRange": "Cleaning Do Not Disturb",
        "toneMultiRange": "Voice Undisturbed Period",
        "cameraMultiRange": "Shooting Period",
        "cameraMultiNew": "Shooting Period",
        "detectMultiRange": "Detection Period",
        # `aw` is addWater, from the firmware's own vocabulary. `wl` is not
        # named anywhere in the image -- three `wl` tokens and none of them
        # says what it abbreviates. The official app does: its "Signal lights"
        # screen writes `wlDisturbMode`/`wlDisturbMultiRange` (capture
        # 2026-10-04), so the label is the app's.
        "awDisturbMultiRange": "Water Top-Up Undisturbed Period",
        "wlDisturbMultiRange": "Signal Lights Undisturbed Period",
    }
    weekly = {"cameraMultiRange", "cameraMultiNew"}

    targets = [
        {"target": key, "name": labels.get(key, key),
         "kind": "weekly" if key in weekly else "ranges", "value": value}
        for key, value in multi_config_ranges(device).items()
    ]

    if device.is_litter:
        targets.append({
            "target": "schedule", "name": "Scheduled Cleaning & Deodorizing",
            # Empty when nobody has set one, and `dev_schedule_get` answers
            # empty too — there is no default cleaning schedule anywhere in
            # this add-on. Inventing times would run somebody's box on a
            # timetable they never chose.
            "kind": "points",
            "value": device.config.get("schedule") or [],
        })
    if device.is_feeder:
        targets.append({
            "target": "feed_schedule", "name": "Feeding Schedule",
            "kind": "feed",
            # `{"schedule": []}` and not `{}`, so the editor always has the
            # shape to render — a feeder with no meals set is a feeder with
            # no groups, not a feeder with no schedule object.
            "value": device.config.get("feed_schedule") or {"schedule": []},
            # A Dual-Hopper dispenses from two hoppers and a meal carries a
            # portion for each (`a1`/`a2`); every other feeder reads one.
            # Decided here rather than in the browser, next to the set that
            # already answers this question for `feed_realtime`.
            "dual": device.device_type in DEVICE_TYPES_FEEDER_DUAL,
        })
    return targets
