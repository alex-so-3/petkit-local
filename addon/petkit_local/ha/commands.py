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
import random
import time
from typing import Any

from petkit_local.devices.base import Device
from petkit_local.devices.state_parsers import record_consumable_reset
from petkit_local.ha.discovery import EntityDef
from petkit_local.utils.coerce import to_bool, to_float

log = logging.getLogger(__name__)

# What every routed command looks like: the thing/service topic suffix to
# publish under, and the Aliyun envelope to publish there.
Command = tuple[str, dict[str, Any]]


class Refused(ValueError):
    """The write was understood and rejected, and nothing was changed.

    Distinct from a `None` return, which means "applied, but there is nothing to
    send to the device" -- a capability toggle, a schedule write. Both used to be
    `None`, so the panel answered `{"ok": true}` to a value it had just thrown
    away, which is the silent failure that refusing was supposed to avoid.
    """

PROPERTY_SET_SUFFIX = "property/set"

# Media capability toggles (ha/entities/switches.py::CAPABILITY_SWITCHES) route
# here instead of the generic settings.<field> path below: they don't push
# anything to the device — the STS response (dev_oss_sts_info_new_v2) is the
# control point, and the device picks the change up on its next poll.
CAPABILITY_VALUE_PREFIX = "capabilities."


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
    "cleaning_start": lambda: _litter_start(0),
    "dump_litter": lambda: _litter_start(1),
    "deodorize": lambda: _litter_start(2),
    "maintenance_start": lambda: _litter_start(9),
    "maintenance_stop": lambda: _litter_end(9),
    # enum-consistent (pypetkitapi codes; delivery via start, best-effort)
    "level_litter": lambda: _litter_start(4),
    "reset_n50": lambda: _litter_start(8),
    "reset_n60": lambda: _litter_start(10),
    # no localkit reference; best-effort control verbs
    "pause": lambda: _litter_ctrl("stop_action", 0),
    "resume": lambda: _litter_ctrl("continue_action", 0),
    "reset": lambda: _litter_end(0),
}


def _feed(amount: int = 10) -> Command:
    """Dispense `amount` portions now; amount=0 cancels a pending manual feed.

    LOCAL PATCH (see CHANGELOG-LOCAL.md): the id's shape was corrected to match
    real cloud traffic, confirmed twice on a D4 (`r_20260802_882_882-1`,
    `r_20260802_4057_4057-1`) - the random number appears TWICE before `-1`,
    not once as the original `r_{date}_{nnnn}-1` assumed.
    """
    return ("feed_realtime", _envelope("thing.service.feed_realtime", {
        "amount": amount,
        "id": (lambda n: f"r_{time.strftime('%Y%m%d')}_{n}_{n}-1")(random.randint(1000, 9999)),
    }))


FEEDER_ACTIONS = {
    "feed": lambda: _feed(10),
    "cancel_manual_feed": lambda: _feed(0),
    # HTTP endpoints in the cloud API; delivered as property.set locally (best-effort)
    "reset_desiccant": lambda: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"desiccantTime": 0})),
    "food_replenished": lambda: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"food": 1})),
}

FOUNTAIN_ACTIONS = {
    "reset_filter": lambda: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"filterPercent": 100})),
    "pause_fountain": lambda: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"power": 0})),
    "resume_fountain": lambda: (PROPERTY_SET_SUFFIX, make_mqtt_property_set({"power": 1})),
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
        return action()

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
