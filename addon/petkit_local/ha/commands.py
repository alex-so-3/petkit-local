"""Translate HA entity commands into device commands.

Routing is driven by the entity definition (its `component` and `value_path`),
not by parsing an opaque payload. Every command that should reach the device
returns ``(service_suffix, mqtt_envelope)`` — the caller publishes it to
``/sys/{pk}/{dn}/thing/service/{suffix}`` (or falls back to the heartbeat queue
if the MQTT bridge is down). This matches the reference localkit stack, which
delivers ALL real-time control over MQTT `thing/service/*` topics, not heartbeat.

- **button**  -> a named action (litter start/end, feed, …).
- **switch/number/select** -> a settings write via `property.set`, with an
  optimistic local update so the HA entity reflects it immediately.
- **text** (schedules) -> raw JSON written to device config, served by
  dev_schedule_get / dev_feed_get.

Action codes and envelopes are cross-checked against pypetkitapi `LBCommand`
and localkit `ServiceStart/End/FeedRealtime/PropertySet`.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

from petkit_local.devices.base import Device, Refused
from petkit_local.devices.state_parsers import record_consumable_reset
from petkit_local.events import codes
from petkit_local.ha.discovery import EntityDef
from petkit_local.utils.coerce import to_bool, to_float, to_int
from petkit_local.utils.const import DEVICE_TYPES_FEEDER_DUAL, DEVICE_TYPES_FEEDER_NEXT_GEN
from petkit_local.utils.timeutil import local_day_start

log = logging.getLogger(__name__)

# What every routed command looks like: the thing/service topic suffix to
# publish under, and the Aliyun envelope to publish there.
Command = tuple[str, dict[str, Any]]


PROPERTY_SET_SUFFIX = "property/set"

# Media capability toggles (ha/entities/switches.py::CAPABILITY_SWITCHES) route
# here instead of the generic settings.<field> path below: they don't push
# anything to the device — the STS response (dev_oss_sts_info_new_v2) is the
# control point, and the device picks the change up on its next poll.
CAPABILITY_VALUE_PREFIX = "capabilities."

# Controls that exist only here. `local.<field>` is stored in
# `device.config["local"]` and NEVER sent anywhere: the per-hopper portion
# counts a dual-hopper feed reads are our intent, not a device setting.
#
# The distinction is load-bearing rather than tidy. `Device.to_device_info`
# serves `config["settings"]` straight back to the device, so a value parked
# there is not a local default — it is pushed to hardware as a setting the
# feeder never had and cannot be talked out of.
LOCAL_VALUE_PREFIX = "local."

# `surplusControl` value -> the `surplusStandard` level it pairs with, from a
# live D4SH capture of the app's own writes (2026-08-08). `0` (off) has no
# entry: the app left `surplusStandard` untouched when switching off.
SURPLUS_CONTROL_TO_STANDARD = {30: 1, 60: 2, 80: 3}

#: Starting values for the `local.` controls, so their entities render a number
#: instead of "unknown" before anyone touches them. Both are 1 because that is
#: what PetKit's app sends for its own default manual feed on a Dual-Hopper,
#: captured through proxy mode in issue #2 as
#: `{"amount1": 1, "amount2": 1, "id": "r_20260801_72849_72849-1"}`.
LOCAL_DEFAULTS: dict[str, Any] = {"feedAmount1": 1, "feedAmount2": 1}


def _envelope(method: str, params: dict, ms_id: bool = False) -> dict[str, Any]:
    """Wrap `params` in the Aliyun IoT service envelope.

    Args:
        ms_id: Use a millisecond timestamp as the message id instead of a
            second one. Only litter `start` does this, matching localkit —
            two starts within the same second would otherwise share an id.

    Returns:
        ``{"method": ..., "id": "<epoch>", "params": {...},
        "version": "1.0.0"}``.
    """
    # Aliyun's `id` is a decimal string, and every id the real cloud sends stays
    # within SIGNED int32 — observed 47214543 to 2144539517, always < 2**31,
    # never the 2**31..2**32-1 half. A raw millisecond timestamp (~1.79e12)
    # overshoots, so wrap it into that same signed-int32 range rather than into
    # full uint32: if the firmware parses `id` with a signed atoi, a value above
    # 2**31 would read negative, which the cloud is never seen to do. Wrapping
    # keeps the sub-second uniqueness `ms_id` exists for; the ~24.9-day period is
    # far longer than any window in which two ids could be confused.
    raw = int(time.time() * 1000) if ms_id else int(time.time())
    return {
        "method": method,
        "id": str(raw % 2**31),
        "params": params,
        "version": "1.0.0",
    }


def make_mqtt_property_set(params: dict) -> dict[str, Any]:
    """Envelope for a settings write: `params` is `{field: value}`."""
    return _envelope("thing.service.property.set", params)


# --- litter: MQTT thing/service/start|end with LBCommand codes ---------------
# LBCommand (pypetkitapi command.py, confirmed by localkit PetkitPuraMax.php):
#   CLEANING=0 DUMPING=1 ODOR_REMOVAL=2 RESETTING=3 LEVELING=4 CALIBRATING=5
#   RESET_DEODOR=6 LIGHT=7 RESET_N50_DEODOR=8 MAINTENANCE=9 RESET_N60_DEODOR=10
#
# Code 10 is now CONFIRMED against the real cloud on a T5. An N60 reset issued
# from PetKit's app arrived as, byte for byte in shape, what `_litter_start(10)`
# builds:
#   topic   /sys/{pk}/{dn}/thing/service/start
#   payload {"method":"thing.service.start","id":"1732816215",
#            "params":{"start_action":10},"version":"1.0.0"}
# The device answered within one second with `work_start` (content `action:10`,
# `reason:2`) then `liquid_reset_over`, and its `sprayResetTime` became the
# moment of the reset. So the N60 countdown is `sprayResetTime` -- note the
# event is named for the liquid while the field is named for the spray.
#
# Code 8 is CONTRADICTED for the N50, and `reset_n50` is very likely a no-op.
# Resetting the N50 from PetKit's app was watched on the wire twice: the cloud
# sent ONLY `thing.service.errState {"show":1,"err_state":1}` -- never a `start`,
# never code 8 -- and the box answered nothing. Sending code 8 ourselves to the
# same box also drew no reply, where code 10 produced two events in a second.
# PetKit's own `dev_device_info` carries no N50 field at all, so the replacement
# date lives in their account database and the box is only told what to display.
# The button is left exposed because the enum comes from pypetkitapi, which
# models PetKit's CLOUD api where an N50 reset is a real call -- but on the
# device protocol it has no evidenced counterpart. Do not read its silence as a
# delivery failure, and do not build the N50 countdown on it.

#: Button key -> the consumable whose replacement date it stamps. Both still
#: send their device command as well; this only adds the record we keep.
CONSUMABLE_BUTTONS = {"reset_n50": "n50", "reset_n60": "n60"}


def _litter_start(code: int) -> Command:
    """Begin an LBCommand action on a litter box."""
    return ("start", _envelope("thing.service.start", {"start_action": code}, ms_id=True))


def _litter_end(code: int) -> Command:
    """End an LBCommand action on a litter box."""
    return ("end", _envelope("thing.service.end", {"end_action": code}))


def _litter_ctrl(action_key: str, code: int) -> Command:
    """Interrupt a running litter action (pause/resume).

    Delivered over the `start` service with a different params key, because
    pause/resume have no localkit reference to copy — best-effort verbs.
    """
    return ("start", _envelope("thing.service.start", {action_key: code}))


LITTER_ACTIONS = {
    # reference-confirmed (localkit PetkitPuraMax dispatchSync codes)
    "cleaning_start": lambda device: _litter_start(0),
    "dump_litter": lambda device: _litter_start(1),
    "deodorize": lambda device: _litter_start(2),
    "maintenance_start": lambda device: _litter_start(9),
    "maintenance_stop": lambda device: _litter_end(9),
    # enum-consistent (pypetkitapi codes; delivery via start, best-effort)
    "level_litter": lambda device: _litter_start(4),
    "reset_n50": lambda device: _litter_start(8),
    "reset_n60": lambda device: _litter_start(10),
    # no localkit reference; best-effort control verbs
    "pause": lambda device: _litter_ctrl("stop_action", 0),
    "resume": lambda device: _litter_ctrl("continue_action", 0),
    "reset": lambda device: _litter_end(0),
}


def _feed_id(now: float | None = None) -> str:
    """The feed's own identifier, which is not the envelope id.

    Shape `r_{yyyymmdd}_{n}_{n}-1`, and the number appears TWICE:
    `r_20260802_882_882-1` and `r_20260802_4057_4057-1` off PetKit's cloud
    talking to a D4 (PR #10), `r_20260801_72849_72849-1` and
    `r_20260801_72906_72906-1` off it talking to a D4SH (issue #2). localkit's
    `FeedRealtime` has the number once; four captures across two models settle
    the doubling against a reimplementation.

    `n` is SECONDS SINCE LOCAL MIDNIGHT, which those same captures give away:
    72849 s is 20:14:09 and the log line beside it is timestamped 8:14:09 PM,
    72906 s is 20:15:06 against 8:15:06 PM. It is the same clock the device
    puts in its own `feed_over` content as `time`, next to a `day` of
    `20260801` — so both halves of the id are local, and a UTC one would
    disagree with the device's own reading of the same feed for most of the
    world. This used to be `random.randint(1000, 9999)`, which is the right
    shape and the wrong number.
    """
    now = time.time() if now is None else now
    n = int(now - local_day_start(now))
    return f"r_{time.strftime('%Y%m%d', time.localtime(now))}_{n}_{n}-1"


def _feed(device: Device, amount1: int, amount2: int = 0) -> Command:
    """Dispense now, from one hopper or from both.

    Which field carries the portion is per model, and getting it wrong is
    silent — the device accepts the command, runs a feed cycle and dispenses
    nothing. From `parse_service_invoke_msg` in a D4SH 867 `ctrl`:

    - it compares its own model string against `"D4SH"`, and that branch reads
      `amount1` and `amount2` verbatim, one per hopper, no scaling. PetKit's
      app sends `1` for a single portion, so the unit here is PORTIONS;
    - the next branch compares `"D4H"` and reads `amount`, then DIVIDES it by a
      constant held in the device's own configuration. So `amount` is not in
      portions, and the 10 the single-hopper path has always sent is a value
      that path is entitled to — it stays untouched;
    - a model matching neither reads no portion field at all.

    Both are stored with `sb`, a single byte, so anything above 255 wraps on
    the device. `amount1`/`amount2` are clamped here rather than sent as-is:
    the panel and HA bound their own controls, but neither binds a raw API
    call, and a wrapped byte would dispense a quantity nobody asked for.
    """
    if device.device_type.lower() in DEVICE_TYPES_FEEDER_DUAL:
        params = {"amount1": _clamp_byte(amount1), "amount2": _clamp_byte(amount2)}
    else:
        params = {"amount": _clamp_byte(amount1)}
    feed_id = _feed_id()
    # Remembered so `feed_realtime_cancel` has something to name. Nothing else
    # knows it: the device echoes it back in `feed_start`/`feed_over`, but a
    # cancel is wanted precisely when neither has arrived yet.
    device.config.setdefault("local", {})["lastFeedId"] = feed_id
    return ("feed_realtime", _envelope("thing.service.feed_realtime", {
        **params,
        "id": feed_id,
    }))


def _clamp_byte(value: int) -> int:
    """0..255, the range the device's own `sb` store can hold."""
    return max(0, min(255, int(value)))


def _feed_amounts(device: Device) -> tuple[int, int]:
    """The per-hopper portion counts the two `number` entities hold.

    Defaults are 1 and 1 because that is what PetKit's app sends for its own
    default manual feed (issue #2, captured through proxy mode). They live in
    `config["local"]`, NOT in `config["settings"]` — `to_device_info` serves
    settings straight back to the device, so a value parked there would be
    pushed to hardware as a setting the feeder never had.
    """
    local = device.config.get("local") or {}
    return (to_int(local.get("feedAmount1"), LOCAL_DEFAULTS["feedAmount1"]),
            to_int(local.get("feedAmount2"), LOCAL_DEFAULTS["feedAmount2"]))


def _cancel_feed(device: Device) -> Command:
    """Cancel a manual feed.

    `feed_realtime_cancel` is its own service in the same dispatch that handles
    `feed_realtime`, and it reads `id`, `amount1` and `amount2` with NO model
    check — so it is the cancel on both hoppered shapes. We send the id of the
    last feed we issued, which is the only one we could be cancelling.

    Gated on the models whose firmware we have actually read. The ESP32 feeders
    run something else entirely, and for them this stays what it always was:
    `feed_realtime` with a zero amount, which is localkit's cancel and the only
    evidence covering those models.
    """
    if device.device_type.lower() not in DEVICE_TYPES_FEEDER_NEXT_GEN:
        return ("feed_realtime", _envelope("thing.service.feed_realtime", {
            "amount": 0,
            "id": _feed_id(),
        }))
    last = (device.config.get("local") or {}).get("lastFeedId") or _feed_id()
    return ("feed_realtime_cancel", _envelope("thing.service.feed_realtime_cancel", {
        "id": last,
        "amount1": 0,
        "amount2": 0,
    }))


#: What the single-hopper path has always sent, and must keep sending.
#:
#: NOT one portion. A D4H divides `amount` by a constant held in its own
#: configuration before using it, so the unit here is whatever that constant is
#: denominated in -- unknown to us, and not the portions a Dual-Hopper counts.
#: The value comes from localkit, whose feeders fall back to 10 when the
#: device's own settings carry no amount. Nothing in issue #2 speaks to it: the
#: reporter has a Dual-Hopper, which never reads this field at all. Changing it
#: would silently change the meal size on somebody's working feeder.
SINGLE_HOPPER_AMOUNT = 10


def _feed_both(device: Device) -> Command:
    """The plain Feed button: every hopper this feeder has."""
    if device.device_type.lower() in DEVICE_TYPES_FEEDER_DUAL:
        n1, n2 = _feed_amounts(device)
        return _feed(device, n1, n2)
    return _feed(device, SINGLE_HOPPER_AMOUNT)


def _feed_hopper(device: Device, which: int) -> Command:
    """Dispense from one hopper by asking the other for zero.

    Exactly what PetKit's app does: its single-hopper feed was captured as
    `{"amount1": 0, "amount2": 1}` (issue #2), not as a different service.
    """
    n1, n2 = _feed_amounts(device)
    return _feed(device, n1, 0) if which == 1 else _feed(device, 0, n2)


FEEDER_ACTIONS = {
    "feed": _feed_both,
    "feed_hopper_1": lambda device: _feed_hopper(device, 1),
    "feed_hopper_2": lambda device: _feed_hopper(device, 2),
    "cancel_manual_feed": _cancel_feed,
    # HTTP endpoints in the cloud API; delivered as property.set locally (best-effort)
    "reset_desiccant": lambda device: (
        PROPERTY_SET_SUFFIX, make_mqtt_property_set({"desiccantTime": 0})),
    "food_replenished": lambda device: (
        PROPERTY_SET_SUFFIX, make_mqtt_property_set({"food": 1})),
}

def _fountain_start(code: int) -> Command:
    """Begin a water-treatment job on a W7H.

    The same `thing.service.start` envelope a litter box uses -- one service,
    one `start_action`, and the device tells them apart by which model it is.
    The values are NOT the litter ones (`codes.FOUNTAIN_W7H_START_ACTIONS`);
    2 is "refill" here and "deodorize" on a Purobot.
    """
    if code not in codes.FOUNTAIN_W7H_START_ACTIONS:
        raise ValueError(f"start_action {code} is not one the W7H accepts")
    return ("start", _envelope("thing.service.start", {"start_action": code}, ms_id=True))


FOUNTAIN_ACTIONS = {
    "reset_filter": lambda device: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"filterPercent": 100})),
    "pause_fountain": lambda device: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"power": 0})),
    "resume_fountain": lambda device: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"power": 1})),
    # W7H only; no other fountain publishes these buttons.
    "fountain_flush": lambda device: _fountain_start(1),
    "fountain_refill": lambda device: _fountain_start(2),
    "fountain_water_change": lambda device: _fountain_start(5),
}

ALL_ACTIONS = {**LITTER_ACTIONS, **FEEDER_ACTIONS, **FOUNTAIN_ACTIONS}


def _coerce_switch(payload: str) -> int:
    """Coerce an HA switch payload ("ON"/"OFF") to the device's 0/1 form.

    Returns an `int`, not a `bool`: the value goes straight into a
    `property.set` params dict, and the device's settings fields are integers.
    An unrecognised payload is treated as OFF rather than rejected — a switch
    has no third state, so `default=False` reproduces the previous behaviour.
    """
    return int(to_bool(payload, False))


def _coerce_number(payload: str) -> int | float | None:
    """Coerce an HA number/select payload to the numeric form the device wants.

    Deliberately polymorphic: an integral value comes back as an `int` so the
    JSON reaching the device reads `{"volume": 7}` and not `{"volume": 7.0}`.
    A non-integral one stays a `float`.

    Returns:
        None for anything non-numeric — the caller logs and drops the command.
        Since moving to `to_float`, this now also covers infinities and NaN,
        which bare `float()` used to accept: they serialise as bare
        `Infinity`/`NaN`, which is not valid JSON, and no setpoint the device
        understands is non-finite.
    """
    number = to_float(payload, None)
    if number is None:
        return None
    return int(number) if number.is_integer() else number


def _select_value(entity: EntityDef, payload: str) -> int | float | str | None:
    """Map an HA select LABEL back to the value the device expects.

    HA always sends a label, since that is what it was given as `options` (see
    ha/discovery.py::_select_value_template). The numeric fallback exists for
    an option list that was later renamed while HA still holds the old retained
    command payload, and for anyone publishing a raw value by hand.

    Returns:
        The `option_values` entry for the label, or its index when the entity
        has none; None if the payload is neither a known label nor a number.
    """
    label = str(payload).strip()
    if label in entity.options:
        idx = entity.options.index(label)
        if entity.option_values:
            return entity.option_values[idx]
        return idx
    return _coerce_number(label)


def handle_ha_command(device: Device, entity: EntityDef, payload: str) -> Command | None:
    """Route one HA command for `entity`, mutating `device` where it applies.

    Settings writes are applied to `device.config` optimistically BEFORE the
    device confirms anything, so the HA control stops bouncing back to its old
    position while the command is in flight; the device's next property post
    overwrites it either way.

    Returns:
        ``(service_suffix, envelope)`` for the caller to deliver to the device,
        or None when nothing needs to be sent — because the command was handled
        entirely locally (capability toggles, schedule text) or because it
        could not be routed at all (unknown action, uncoercible payload, an
        entity with no settings field). Both are logged; neither raises.
    """
    if entity is None:
        return None

    comp = entity.component

    if comp == "switch" and entity.value_path.startswith(CAPABILITY_VALUE_PREFIX):
        cap = entity.value_path[len(CAPABILITY_VALUE_PREFIX):]
        value = bool(_coerce_switch(payload))
        device.config.setdefault("capabilities", {})[cap] = value
        log.info("Capability %s=%s for device %d (STS is the control point - "
                "takes effect on the next dev_oss_sts_info_new_v2 poll)",
                cap, value, device.petkit_id)
        return None

    # `surplus_level` writes a PAIR, not a single field: `surplusControl`
    # carries on/off and level together (0/30/60/80), `surplusStandard`
    # mirrors the level (1/2/3) and is captured live to be left untouched when
    # switching off — see ha/entities/selects.py::FEEDER_SELECTS.
    if comp == "select" and entity.key == "surplus_level":
        value = _select_value(entity, payload)
        if value is None:
            log.warning("Could not coerce payload %r for entity '%s'", payload, entity.key)
            return None
        settings = device.config.setdefault("settings", {})
        settings["surplusControl"] = value
        params = {"surplusControl": value}
        level = SURPLUS_CONTROL_TO_STANDARD.get(value)
        if level is not None:
            settings["surplusStandard"] = level
            params["surplusStandard"] = level
        log.info("Setting surplusControl=%s (surplusStandard=%s) for device %d",
                 value, level, device.petkit_id)
        return (PROPERTY_SET_SUFFIX, make_mqtt_property_set(params))

    if comp == "button":
        # A consumable reset is recorded HERE, not inferred from the device's
        # answer, because for the N50 there will never be one: it has no field
        # in any report and PetKit keeps its replacement date in their own
        # account database. Being the cloud is the point of this add-on, and
        # this is where that has to mean remembering something ourselves. The
        # device command is still sent where one exists -- on the N60 it really
        # does reset the box's own stamp.
        which = CONSUMABLE_BUTTONS.get(entity.key)
        if which:
            ts = record_consumable_reset(device, which)
            log.info("Recorded %s replacement for device %d at %s",
                     which.upper(), device.petkit_id, ts)

        action = ALL_ACTIONS.get(entity.key)
        if not action:
            log.warning("Unknown button action '%s' for device %d", entity.key, device.petkit_id)
            return None
        log.info("Action '%s' for device %d", entity.key, device.petkit_id)
        # Every action takes the device, whether or not it reads it: which
        # field a feed puts its portion in is per model, and a table of
        # zero-argument lambdas is exactly what made that impossible to express.
        return action(device)

    if comp == "text":
        key = entity.value_path
        if not key:
            return None
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            log.warning("text '%s' payload is not valid JSON - storing raw", entity.key)
            parsed = payload
        device.config[key] = parsed
        log.info("Set config[%s] for device %d", key, device.petkit_id)
        return None

    if comp not in ("switch", "number", "select"):
        return None

    field = entity.setting_field
    if not field:
        log.warning("Entity '%s' has no settings field (value_path=%r)", entity.key, entity.value_path)
        return None

    if entity.value_path.startswith(LOCAL_VALUE_PREFIX):
        value = _coerce_number(payload) if comp != "switch" else _coerce_switch(payload)
        if value is None:
            log.warning("Could not coerce payload %r for entity '%s'", payload, entity.key)
            return None
        if comp == "number" and not (entity.min_value <= value <= entity.max_value):
            raise Refused(
                f"{entity.name} must be between "
                f"{entity.min_value} and {entity.max_value}")
        device.config.setdefault("local", {})[field] = value
        log.info("Local %s=%s for device %d (no device command)",
                 field, value, device.petkit_id)
        return None

    if comp == "switch":
        value = _coerce_switch(payload)
    elif comp == "number":
        value = _coerce_number(payload)
        # REFUSED, not clamped. `min_value`/`max_value` bound HA's own control
        # and the panel's spinner, but neither binds a raw API call, and the
        # accepted value does not just render: it is stored in
        # `config["settings"]`, which `to_device_info` serves straight back to
        # the device. So an out-of-range number would be pushed to hardware.
        #
        # Clamping would silently write a value nobody asked for, which is the
        # thing this project does not do anywhere else. Refusing leaves the
        # setting as it was and says why.
        if value is not None and not (entity.min_value <= value <= entity.max_value):
            log.warning("Refusing %s=%s for device %d: outside %s..%s",
                        field, value, device.petkit_id,
                        entity.min_value, entity.max_value)
            raise Refused(
                f"{entity.name} must be between "
                f"{entity.min_value} and {entity.max_value}")
    else:
        value = _select_value(entity, payload)

    if value is None:
        log.warning("Could not coerce payload %r for entity '%s'", payload, entity.key)
        return None

    device.config.setdefault("settings", {})[field] = value
    log.info("Setting %s=%s for device %d (optimistic + MQTT)", field, value, device.petkit_id)
    return (PROPERTY_SET_SUFFIX, make_mqtt_property_set({field: value}))
