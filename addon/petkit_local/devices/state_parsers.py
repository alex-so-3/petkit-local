"""Normalize a `dev_state_report` body into the flat camelCase state HA reads.

Devices do not agree with each other, or with themselves. The same value
arrives as `sandPercent` on one model and `litter.percent` on another, as
`workState` (an object) here and `work_state` (a scalar) there, and a field may
be absent entirely on a firmware that predates it. Entity definitions cannot
carry one `value_path` per spelling, so every spelling is collapsed here into
one flat key per value, and `state.<key>` is then the only thing the HA
templates, the panel and the MQTT property path all read.

The standard bug in this module is a derivation wired into one transport only.
The HTTP `dev_state_report` and the MQTT `property/post` are parsed by separate
functions, so a mapping added to just one of them works on whichever frames
happen to carry it and silently does nothing on the other — and which transport
a device uses is not a detail, since a device on MQTT stops polling the HTTP
heartbeat entirely. Anything computed the same way from the same fields belongs
in a helper both sides call.

Everything in this module is best-effort by design: an unrecognised device type
passes its body through untouched, an unexpected shape at any level is skipped
rather than raised on. A state report that fails would cost the device its whole
update, so a partial state always beats an exception.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from petkit_local.devices.consumables import (_days_left_from_reset, _extract_consumable_days,
                                              apply_consumable_state, record_consumable_reset)
from petkit_local.devices.state_tables import (CONSUMABLE_RECORD_KEY, CONSUMABLE_TOTALS,
                                               DEODORANT_TOTAL_DAYS, FEEDER_HALLS,
                                               FEEDER_NEXT_GEN_FIELDS, LITTER_CAMERA_HALLS,
                                               LITTER_CAMERA_MODELS, PRESENCE_FLAGS,
                                               SNAPSHOT_MARKER, SPRAY_TOTAL_DAYS,
                                               W7H_DEVICE_TIMESTAMPS, W7H_HALLS, W7H_MODELS,
                                               W7H_STATE_FIELDS, WORK_MODE_IDLE)
from petkit_local.events import codes
from petkit_local.utils.coerce import to_float
from petkit_local.utils.const import DEVICE_TYPES_FEEDER_NEXT_GEN
from petkit_local.utils.dicts import dig

#: The names this module answers to. The evidence tables live in
#: `state_tables.py` and the consumable countdowns in `consumables.py`, and both
#: are re-exported here because this is the module a caller reaching for a state
#: key already imports — listing them is also what marks those imports as used,
#: so the lint gate does not read a re-export as dead code. The two private
#: names are here because the tests reach for them by name.
__all__ = [
    "CONSUMABLE_RECORD_KEY",
    "CONSUMABLE_TOTALS",
    "DEODORANT_TOTAL_DAYS",
    "FEEDER_HALLS",
    "FEEDER_NEXT_GEN_FIELDS",
    "LITTER_CAMERA_HALLS",
    "LITTER_CAMERA_MODELS",
    "PRESENCE_FLAGS",
    "SNAPSHOT_MARKER",
    "SPRAY_TOTAL_DAYS",
    "W7H_DEVICE_TIMESTAMPS",
    "W7H_HALLS",
    "W7H_MODELS",
    "W7H_STATE_FIELDS",
    "WORK_MODE_IDLE",
    "_days_left_from_reset",
    "_extract_consumable_days",
    "apply_consumable_state",
    "normalize_property_params",
    "parse_state_report",
    "record_consumable_reset",
]


def _extract_sensor_block(body: dict[str, Any], state: dict[str, Any],
                          names: tuple[str, ...]) -> None:
    """Copy the named switches out of a report's `sensor{}` block.

    Listed names only, never everything present: an entity may bind to a key
    only if some source names it, and a blanket copy would also drag in the raw
    ADC readings whose scale nobody here knows.
    """
    sensor = body.get("sensor")
    if not isinstance(sensor, dict):
        return
    for key in names:
        if key in sensor:
            state[key] = sensor[key]


def _iso_or_none(value: Any) -> str | None:
    """A device unix timestamp as ISO-8601 UTC, or None if it is not one.

    Zero is the device's "never happened" and must not become 1970 — an HA
    timestamp sensor renders that as a real date 56 years ago rather than as
    unknown.
    """
    seconds = to_float(value, 0.0)
    if seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _extract_fountain_w7h(body: dict[str, Any], state: dict[str, Any],
                          device_type: str = "") -> None:
    """Flatten the W7H-specific parts of a fountain report into `state`.

    ONE helper called from BOTH transports on purpose. A `property/post` never
    reaches `parse_state_report` — `mqtt/bridge.py` sends it to
    `normalize_property_params` alone — while the snapshot embedded in an event
    goes through both. So a mapping added to only one of them works on whichever
    frames happen to carry it and silently does nothing on the other, which is
    the failure this module's docstring calls "the standard bug".

    Everything here is keyed off the presence of the field, never off a default.
    """
    if device_type.lower() not in W7H_MODELS:
        return

    # Via `_extract_camel` rather than a plain copy: the device mixes spellings
    # inside one payload — `stgFullState` beside `reboot_reason` — and this is
    # the helper that already collapses both onto the camelCase key an entity
    # reads.
    _extract_camel(body, list(W7H_STATE_FIELDS), state)
    _extract_sensor_block(body, state, W7H_HALLS)

    device_block = body.get("device")
    if isinstance(device_block, dict):
        for src, dst in W7H_DEVICE_TIMESTAMPS.items():
            if src in device_block:
                stamp = _iso_or_none(device_block[src])
                if stamp:
                    state[dst] = stamp
        if "sw" in device_block:
            state["sw"] = device_block["sw"]


def _extract_feeder_next_gen(body: dict[str, Any], state: dict[str, Any],
                             device_type: str = "") -> None:
    """Flatten the D4H/D4SH-specific parts of a feeder report into `state`.

    ONE helper called from BOTH transports, for the reason spelled out on
    `_extract_fountain_w7h`: a mapping added to only one of them works on
    whichever frames happen to carry it and silently does nothing on the other.
    A D4SH publishes `thing/event/property/post` (the topic is in its firmware),
    and that path reaches `normalize_property_params` alone -- so a feeder field
    missing there is missing from the device's main state channel, taking the
    hopper levels, the bowl and the feeding flags with it.

    Gated on the models whose firmware was actually read. The ESP32 feeders
    report none of these keys, and absence is skipped rather than defaulted.
    """
    if device_type.lower() not in DEVICE_TYPES_FEEDER_NEXT_GEN:
        return
    _extract_camel(body, list(FEEDER_NEXT_GEN_FIELDS), state)
    _extract_sensor_block(body, state, FEEDER_HALLS)


def _derive_food_low(state: dict[str, Any]) -> None:
    """Turn the hopper-presence `food` into an explicit `foodLow` 0/1.

    `food` is presence of food in the hopper, not a shortage flag: a live D4H
    (fw 867) holds a steady 2 with a FULL hopper and reports 0 only when
    nothing is detected -- transiently while food moves through during a
    dispense, and for real once the hopper runs out. The shared "Food Low"
    binary sensor used to read `food` through discovery's generic
    truthy-is-ON template (`ha/discovery.py::_value_template`), so a full
    hopper read as Food Low ON -- backwards. Deriving the flag here keeps the
    raw `food` for the Hopper enum sensor and gives the problem sensor a
    value that is 1 exactly when the device says nothing is there.

    Gated on presence, the module's standing rule: a model that never sends
    `food` (a D4SH says `food1`/`food2`) derives nothing. ONE helper called
    from BOTH transports, for the reason spelled out on
    `_extract_feeder_next_gen` -- today no MQTT table carries a singular
    `food`, so the property/post call site is armed but idle until one does.
    """
    if "food" in state:
        state["foodLow"] = 1 if to_float(state.get("food"), 2) == 0 else 0


def _extract_presence_flags(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Turn the presence-signalled fields into 0/1 in `state`.

    Only for a full snapshot: reading absence as "off" is exactly as wrong as
    reading presence as "on" if the payload was never going to carry the key in
    the first place. `SNAPSHOT_MARKER` is what separates the two cases.
    """
    if SNAPSHOT_MARKER not in body:
        return
    for field_name, flag in PRESENCE_FLAGS.items():
        state[flag] = 1 if body.get(field_name) else 0


def _extract_shared(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Everything derived identically from either transport's payload.

    ONE call site per transport instead of one per derivation. Hand-syncing two
    lists is what the module docstring calls "the standard bug in this module",
    and the consumables alone have been through it repeatedly. Anything computed
    the same way from the same fields belongs in here, not copied into both
    parsers.
    """
    # runtime (seconds of uptime) -> totalTime. A derivation kept in the HTTP
    # parser alone is missing on exactly the healthiest devices: a T5 STOPS
    # polling the HTTP heartbeat once it is on MQTT, so the Uptime sensor would
    # read unknown for as long as the MQTT session lives.
    if "runtime" in body:
        state["totalTime"] = body["runtime"]
    _extract_consumable_days(body, state)
    _extract_presence_flags(body, state)


def parse_state_report(device_type: str, body: dict[str, Any]) -> dict[str, Any]:
    """Flatten a state report for `device_type` into the keys HA entities read.

    Returns:
        A flat dict of camelCase keys (plus the nested `feedState` / `workState`
        sub-objects a few entities index into). An unknown device type gets its
        body back UNCHANGED rather than an empty dict, so a model we have not
        classified yet still shows whatever it happens to name correctly.
    """
    if not body:
        return {}

    if device_type in ("t5", "t6", "t7"):
        return _parse_litter_camera(body)
    if device_type in ("t3", "t4"):
        return _parse_litter_esp32(body)
    if device_type in ("d4h", "d4sh", "d4", "d3", "d4s", "feeder", "feedermini"):
        return _parse_feeder(body, device_type)
    if device_type in ("w4", "w5", "ctw2", "ctw3", "w7h"):
        return _parse_water_fountain(body, device_type)
    if device_type in ("k2", "k3"):
        return _parse_purifier(body)

    return body


def _extract_camel(body: dict[str, Any], keys: list[str], state: dict[str, Any]) -> None:
    """Copy `keys` from `body` into `state`, accepting either spelling.

    Each key is looked up as written and again in snake_case, because a device
    mixes the two spellings within one payload. The snake_case hit is applied
    second and therefore WINS if a payload somehow carries both; no device has
    been observed doing that, so the precedence is arbitrary, not meaningful.
    """
    for key in keys:
        if key in body:
            state[key] = body[key]
        snake = _to_snake(key)
        if snake != key and snake in body:
            state[key] = body[snake]


def _extract_litter_nested(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Flatten the nested sub-objects a real litter box sends into `state`.

    Confirmed against a real T5. Sets, when the source is present: `sandWeight`,
    `sandPercent`, `usedTimes`, `sandType` (from `litter`), `errorMsg` and
    `boxFull` (from `err`), `petInTime` (from `device`), `totalTime` (from
    `runtime`), and the two countdowns derived from reset timestamps,
    `sprayLeftDays` and `deodorantLeftDays`. Absent sources leave `state`
    untouched rather than writing a zero, so a missing field stays unknown in HA
    instead of reading as a real measurement of nothing.
    """
    # litter{weight, percent, usedTimes, sandType}
    litter = body.get("litter")
    if isinstance(litter, dict):
        if "weight" in litter:
            state["sandWeight"] = litter["weight"]
        if "percent" in litter:
            state["sandPercent"] = litter["percent"]
        if "usedTimes" in litter:
            state["usedTimes"] = litter["usedTimes"]
        if "sandType" in litter:
            state["sandType"] = litter["sandType"]

    _extract_error_flags(body, state)

    # device{sw, pet_in_time}
    dev = body.get("device")
    if isinstance(dev, dict):
        if "pet_in_time" in dev:
            state["petInTime"] = dev["pet_in_time"]

    _extract_shared(body, state)


def _extract_error_flags(body: dict[str, Any], state: dict[str, Any],
                         device_type: str = "") -> None:
    """`err{DC:0, taryF:1, ...}` -> a readable `errorMsg`, plus litter's boxFull.

    One helper for both transports, because an untranslated flag reaches the
    Error sensor as `taryF,cycL` — the firmware's own abbreviations, and its
    spelling of "tray" at that. `codes.error_flag_label` translates per device
    family and falls back to the raw name, so a family with no table (litter,
    feeder) still reads out exactly what the device sent.

    `full` is excluded from the message on purpose: it has its own `boxFull`
    entity, and listing it as an error made a full waste bin look like a fault.
    """
    err = body.get("err")
    if not isinstance(err, dict):
        return
    active = [codes.error_flag_label(flag, device_type)
              for flag, value in err.items() if value and flag != "full"]
    state["errorMsg"] = ", ".join(active) if active else ""
    if "full" in err:
        state["boxFull"] = err["full"]


#: The device's LAN address inside the free-form `other` string, e.g.
#: `"...,Ip:10.50.0.10,..."`. Quoted on some firmware and bare on others. The
#: run of digits and dots is grabbed whole and judged afterwards, so a longer
#: one is REJECTED rather than silently truncated to its first four groups.
_IP_IN_OTHER = re.compile(r'Ip:"?([\d.]+)"?')


def _looks_like_ipv4(value: Any) -> bool:
    """Four dotted octets in range. Judged here rather than in the pattern.

    A regex that merely counts groups accepts `999.999.999.999`, and one that
    stops after four accepts `1.2.3.4.5` by taking the first four — an address
    the device never reported.
    """
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return len(parts) == 4 and all(p.isdigit() and len(p) <= 3 and int(p) <= 255
                                   for p in parts)


def _extract_ip(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Set `state["ip"]` from a flat key or from the `other` string.

    Worth one helper rather than a copy per parser, because the field is read
    much further away than it is written and its absence never looks like a
    missing field. `media/go2rtc.py` skips a device with no `ip` when it builds
    its stream config, and the whole Patchers tab reports the device as
    unsupported — so a parser that drops this leaves a camera device with no
    stream URL and no way to patch it, with nothing anywhere naming the cause.
    Every parser must go through here rather than reading the field its own way.

    The value is checked for shape rather than trusted: a bare run of digits and
    dots matches `....` as readily as an address, and this becomes a go2rtc
    source and an SSH target, so one cheap look is worth having. A device
    reporting something else is left with no `ip` — a state every caller
    already handles.
    """
    ip = body.get("Ip") or body.get("ip") or ""
    if not ip:
        other = body.get("other")
        if isinstance(other, str):
            m = _IP_IN_OTHER.search(other)
            if m:
                ip = m.group(1)
    if _looks_like_ipv4(ip):
        state["ip"] = ip


def _extract_wifi_rssi(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Pull signal strength out of `wifi`, which spells it `rsq` or `rssi`.

    Falls back to whatever is already in `state` so calling this after a flat
    top-level `rssi` was extracted cannot blank it.
    """
    wifi = dig(body, "wifi", default={})
    if isinstance(wifi, dict):
        state["rssi"] = wifi.get("rsq", wifi.get("rssi", state.get("rssi")))


def _parse_content_field(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Merge the `content` sub-document, which arrives as a dict OR a JSON string.

    Unparseable content is dropped silently: the rest of the report is still
    worth publishing, and this field is not where the primary values live.
    """
    if "content" not in body:
        return
    content = body["content"]
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            return
    if isinstance(content, dict):
        state.update(content)


def _extract_work_mode(body: dict[str, Any], state: dict[str, Any]) -> None:
    """Set `workingState` (the mode int) and, when sent as one, `workState` (the object)."""
    # `workState` is an object {workMode, workProcess, ...}, not a scalar. The
    # HA "Device Status" sensor wants the mode int.
    ws = dig(body, "workState", default=dig(body, "work_state"))
    if isinstance(ws, dict):
        state["workState"] = ws
        state["workingState"] = ws.get("workMode", ws.get("work_mode", 0))
    elif ws is not None:
        state["workingState"] = ws
    elif SNAPSHOT_MARKER in body:
        # A litter box sends `workState` ONLY while a cycle is running: it is
        # absent from 988 of 1254 captured snapshots, present in 166, and the
        # payload is otherwise a fixed 29-key dump. Defaulting to 0 here is not
        # neutral: `WORK_MODES[0] == "cleaning"`, so a default would have an
        # idle box reporting itself as cleaning about 79% of the time. Absence
        # means idle, but only in a payload that would have carried the key had
        # it applied.
        state["workingState"] = WORK_MODE_IDLE
    # else: nothing is known, so nothing is written and HA reads unknown --
    # the same rule the rest of this module follows.


def _parse_litter_camera(body: dict[str, Any]) -> dict[str, Any]:
    """State for the Ingenic camera litters (T5/T6/T7).

    The ESP32 litter set plus the camera, spray and package fields, and the
    `content` / `Ip` extras only these models send.
    """
    state: dict[str, Any] = {}
    _extract_work_mode(body, state)
    _parse_content_field(body, state)
    _extract_camel(body, [
        "sandWeight", "sandPercent", "boxFull",
        "petInTime", "deodorantLeftDays", "sprayLeftDays",
        "errorMsg", "rssi", "usedTimes", "totalTime",
        "boxState", "sprayState", "refreshState",
        "cameraStatus", "power",
        # The waste-BAGGING mechanism, which only the Purobot Ultra has and
        # which nothing here read until now: a T6 sends `packageState` in all
        # 3475 reports of one 67-hour capture, alongside these. Published raw
        # on purpose -- the values observed are -1/1, 0/1 and 0/2, and no
        # source names what any of them mean, so a label here would be
        # invented. `ha/entities/sensors.py` publishes them for `t6` only.
        "packageState", "packState", "baggingState",
        "sealDoorState", "boxStoreState", "packageCount",
        # The raw reset stamps, not just the countdowns derived from them:
        # `to_device_info` echoes `sprayResetTime` straight back to the device,
        # and `apply_consumable_state` needs to see what the box reported to
        # know whether to prefer it over the date we recorded ourselves.
        "sprayResetTime", "liquidReset",
        # `discernPic` is the READBACK of on-device facial recognition: the
        # `discern[].id` values whose photos the device downloaded and
        # feature-extracted (features persist in /system/feature.bin there).
        # It is the only way to see whether a dev_discern_pic payload was
        # accepted, which is why it is worth a state field of its own.
        # `aiAnalyse` sits beside it and was 0 in all 733 captured reports;
        # what turns it on is not known.
        "discernPic", "aiAnalyse",
    ], state)
    # Extract from nested sub-objects (real T5 format). Placed AFTER
    # _extract_camel so nested values override any flat keys.
    _extract_litter_nested(body, state)
    _extract_sensor_block(body, state, LITTER_CAMERA_HALLS)
    _extract_ip(body, state)
    _extract_wifi_rssi(body, state)
    return state


def _parse_litter_esp32(body: dict[str, Any]) -> dict[str, Any]:
    """State for the ESP32 litters (T3/T4), which have no camera or spray fields."""
    state: dict[str, Any] = {}
    _extract_work_mode(body, state)
    _extract_camel(body, [
        "sandWeight", "sandPercent", "boxFull",
        "petInTime", "deodorantLeftDays", "errorMsg", "rssi",
        "usedTimes", "totalTime", "boxState", "power",
        "sprayResetTime", "liquidReset",
    ], state)
    # Extract from nested sub-objects (same nested format as camera models).
    # Placed AFTER _extract_camel so nested values override any flat keys.
    _extract_litter_nested(body, state)
    _extract_wifi_rssi(body, state)
    return state


def _parse_feeder(body: dict[str, Any], device_type: str = "") -> dict[str, Any]:
    """State for every feeder, camera or not.

    One parser for both: the camera models add camera fields to the same
    payload, and the camera-only keys are simply absent on the ESP32 ones, so
    asking for them costs nothing. `feedState` is kept as a NESTED dict because
    the feeder entities address it as `feedState.<key>`.
    """
    state: dict[str, Any] = {}
    # Only when the device actually said so. A real D4SH report (issue #2, both
    # transports) carries no `workState` at all, so a `, 0)` default would
    # publish a Device Status of 0 the device never sent -- the same trap the
    # W7H falls into, and the one that has an idle litter box calling itself
    # "cleaning".
    if "workState" in body or "work_state" in body:
        state["workingState"] = body.get("workState", body.get("work_state"))
    # The family's shared names, which come from the reference integration's
    # CLOUD model. `food1`/`food2` are deliberately NOT on it: they are the
    # dual-hopper hardware's, they appear in no single-hopper cloud model, and
    # they have a real source of their own below.
    _extract_camel(body, [
        "errorMsg", "rssi", "desiccantLeftDays",
        "batteryPower", "batteryStatus",
        "door", "bowl", "weight", "food",
        "cameraStatus", "feeding", "eating",
    ], state)
    # Deliberately overlaps the list above on `door`, `bowl`, `feeding` and
    # `eating`. `_extract_camel` copying a key twice is a no-op, and the
    # alternative is worse: this helper is the ONLY one the MQTT path runs, so
    # a key left solely to the list above would reach a next-gen feeder over
    # HTTP and vanish on the transport it actually reports state on.
    _extract_feeder_next_gen(body, state, device_type)
    # The `err{}` fault block, on both transports. Parse it on one only and the
    # Error sensor reads whatever the last transport to arrive had to say --
    # exactly the asymmetry this module's docstring calls the standard bug.
    _extract_error_flags(body, state, device_type)
    # Explicit Food Low, derived from `food` presence -- see _derive_food_low
    # for why the raw value cannot back a problem sensor directly.
    _derive_food_low(state)

    feed_state = dig(body, "feedState", default=dig(body, "feed_state", default={}))
    if isinstance(feed_state, dict) and feed_state:
        parsed_fs: dict[str, Any] = {}
        _extract_camel(feed_state, [
            "times", "realAmountTotal", "eatAmountTotal", "addAmountTotal",
            "planAmountTotal", "planRealAmountTotal", "eatAvg", "eatCount",
            "addAmountTotal1", "addAmountTotal2",
            "planAmountTotal1", "planAmountTotal2",
            "realAmountTotal1", "realAmountTotal2",
        ], parsed_fs)
        state["feedState"] = parsed_fs

    _extract_ip(body, state)
    _extract_wifi_rssi(body, state)
    return state


def _parse_water_fountain(body: dict[str, Any], device_type: str = "") -> dict[str, Any]:
    """State for the water fountains (W4/W5/CTW2/CTW3/W7H).

    Checked against a real W7H `property/post` (2026-07-31). Its payload carries
    no `workState` at all, so a `body.get("workState", …, 0)` default would have
    every W7H report Device Status 0 — a value the device never sent. Same trap
    as the litter box's, where 0 is `WORK_MODES[0] == "cleaning"` and an idle
    box calls itself busy. Absent stays absent.

    The W4/W5/CTW2/CTW3 field names below come from the reference integration's
    cloud model and none of them appear in a W7H report; the W7H's own fields
    are handled by `_extract_fountain_w7h`. The two sets are disjoint, which is
    why one parser can serve both without either inventing the other's values.
    """
    state: dict[str, Any] = {}
    if "workState" in body or "work_state" in body:
        state["workingState"] = body.get("workState", body.get("work_state"))
    _extract_camel(body, [
        "errorMsg", "rssi", "filterLeftDays", "filterPercent",
        # real fountain field names (pypetkitapi water_fountain_container):
        "lackWarning", "heatRealTemp", "drinkTime",
        "batteryPercent", "lowBattery", "filterWarning", "detectStatus",
        "pumpState", "waterPumpState", "cwtState", "wtState",
        "addWaterState", "flushState", "disinfectState",
        "heatInstall", "stgFullState", "runStatus", "powerStatus",
    ], state)

    # These two blocks are absent on a W7H, so the key is written only when the
    # payload actually carried it: a `.get(...)` falling all the way through to
    # None publishes an explicit "unknown" rather than leaving the entity alone.
    electricity = dig(body, "electricity", default={})
    if isinstance(electricity, dict):
        battery = electricity.get("battery_percent", electricity.get("batteryPercent"))
        if battery is not None:
            state["batteryPercent"] = battery

    status = dig(body, "status", default={})
    if isinstance(status, dict):
        detect = status.get("detect_status", status.get("detectStatus"))
        if detect is not None:
            state["detectStatus"] = detect

    _extract_fountain_w7h(body, state, device_type)
    _extract_error_flags(body, state, device_type)
    _extract_wifi_rssi(body, state)
    return state


def _parse_purifier(body: dict[str, Any]) -> dict[str, Any]:
    """State for a purifier reporting over HTTP.

    K2/K3 are BLE-only in every shipping product, so this path is only reached
    if a WiFi purifier ever exists; the K3 values we actually see arrive
    piggybacked on its parent litter's report instead.
    """
    state: dict[str, Any] = {}
    state["workingState"] = body.get("workState", body.get("work_state", 0))
    _extract_camel(body, [
        "errorMsg", "humidity", "temp", "refresh",
        "liquid", "battery", "power", "mode",
        "refreshing", "liquidLack", "leftDay",
    ], state)
    _extract_wifi_rssi(body, state)
    return state


def _to_snake(s: str) -> str:
    """`sandPercent` -> `sand_percent`. Naive by design; only used to widen a lookup."""
    return re.sub(r'([A-Z])', r'_\1', s).lower().lstrip('_')


def normalize_property_params(device_type: str, params: dict[str, Any]) -> dict[str, Any]:
    """Flatten an MQTT `thing/event/property/post` into the HA state keys.

    Produces the same flat camelCase keys as `parse_state_report`
    (`sandPercent`, `rssi`, `errorMsg`, ...), so one entity definition serves
    both transports.

    The MQTT property post nests data differently from the HTTP state_report
    (verified against a real T4 capture): litter under params.litter, signal
    under params.wifi.rsq, errors under params.err. Only keys that are present
    get mapped, so this is safe to run on any device type.

    A W7H reaches this function and NOT `parse_state_report`: `mqtt/bridge.py`
    handles a `property` post here alone, and only the state snapshot embedded
    in an event goes through both. That asymmetry is why `_extract_fountain_w7h`
    is called from here as well — a mapping added to the other parser only would
    work on `drink_start` frames and do nothing on the device's main state
    channel.

    Args:
        device_type: Selects the per-model branch and the `err{}` flag table.
    """
    if not isinstance(params, dict):
        return {}
    flat: dict[str, Any] = {}

    litter = params.get("litter")
    if isinstance(litter, dict):
        for src, dst in (("percent", "sandPercent"), ("weight", "sandWeight"),
                         ("usedTimes", "usedTimes"), ("sandType", "sandType")):
            if src in litter:
                flat[dst] = litter[src]

    wifi = params.get("wifi")
    if isinstance(wifi, dict):
        rssi = wifi.get("rsq", wifi.get("rssi"))
        if rssi is not None:
            flat["rssi"] = rssi

    dev = params.get("device")
    if isinstance(dev, dict):
        if "pet_in_time" in dev:
            flat["petInTime"] = dev["pet_in_time"]

    _extract_fountain_w7h(params, flat, device_type)
    _extract_feeder_next_gen(params, flat, device_type)
    _derive_food_low(flat)
    if device_type.lower() in LITTER_CAMERA_MODELS:
        _extract_sensor_block(params, flat, LITTER_CAMERA_HALLS)

    # Flat state fields the device reports at the top level.
    # Kept in step with `_parse_litter_camera`'s list by hand — the two
    # transports share no table, and forgetting the second is the standard bug
    # in this module.
    for key in ("cameraStatus", "sprayState", "boxState", "weightState",
                "refreshState", "ota",
                "discernPic", "aiAnalyse",
                "sprayResetTime", "liquidReset"):
        if key in params:
            flat[key] = params[key]

    if "box" in params and isinstance(params.get("box"), (int, bool)):
        flat["boxFull"] = int(params["box"])

    # Everything both transports derive the same way: the consumable
    # countdowns, the presence flags and uptime. The fields sit at the top level
    # of a property post exactly as they do in a state report, so one helper
    # serves both -- which is the point, see `_extract_shared`.
    _extract_shared(params, flat)

    _extract_error_flags(params, flat, device_type)

    ws = params.get("work_state", params.get("workState"))
    if isinstance(ws, dict):
        flat["workingState"] = ws.get("work_mode", ws.get("workMode", 0))
    elif SNAPSHOT_MARKER in params:
        # Same presence rule as `_extract_work_mode`: no work cycle means idle.
        # Gated on a full litter snapshot so this cannot invent a work mode for
        # a feeder or fountain, whose own parsers own that key.
        flat["workingState"] = WORK_MODE_IDLE

    if "firmware" in params:
        flat["firmware"] = params["firmware"]

    # The `other` free-form string carries the device IP (needed for the camera
    # and for every patcher). Same reader as the HTTP parsers use.
    _extract_ip(params, flat)

    return flat
