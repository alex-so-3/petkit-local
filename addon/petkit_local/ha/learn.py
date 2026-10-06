"""Learning a device's settings from what the real cloud tells it, in proxy mode.

Some devices never say what they are set to. A W7H's `property/post` carries no
settings at all, so its controls show only what was last set from Home
Assistant, or a seeded default, never what the owner chose in PetKit's app.
The owner's real settings live in PetKit's account, and the only time they
cross this add-on is when proxy mode relays them to the device:

* a cloud->device `thing.service.property.set` over MQTT, which is how the
  official app writes a setting (`mqtt/upstream.py`), and
* the cloud's `dev_device_info` reply over HTTP, whose `settings` block is the
  whole configuration (`http/middleware/proxy.py`).

Both are recorded here exactly as a write from Home Assistant would be
(`ha/commands.py::store_setting` / `store_multi_range`), so the next state
publish shows them. This is OBSERVATION ONLY: the frame the device receives is
not touched, and nothing here runs with proxy mode off.

Only fields this device's own entities read are kept — anything else in a cloud
payload is not ours to store, and a stored setting is served back to the device
by `to_device_info`. `timezone` is never learned even if an entity ever claims
it: `Device.timezone_offset` has its own precedence, and a cloud value stored
as the `config["timezone"]` override would outrank the device's own statement.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from petkit_local.devices.base import Device
from petkit_local.devices.defaults import (
    clean_range_list, clean_weekly_list, schedule_targets,
)
from petkit_local.ha.categories import get_entities_for_device
from petkit_local.ha.commands import store_multi_range, store_setting

log = logging.getLogger(__name__)

#: Never learned, whatever the payload or the entity table says. See the module
#: docstring.
NEVER_LEARNED = frozenset({"timezone"})

#: Entity components whose `settings.<field>` is a plain scalar setting.
_SCALAR_COMPONENTS = ("switch", "number", "select", "time")

_RANGE_CLEANERS = {"ranges": clean_range_list, "weekly": clean_weekly_list}


def learnable_scalar_fields(device: Device) -> set[str]:
    """The `settings` fields this device's own entities read.

    Only entities bound to `settings.`: a `local.` or `capabilities.` field has
    the same last segment shape but is state of ours, never the device's.
    """
    return {e.setting_field for e in get_entities_for_device(device)
            if e.component in _SCALAR_COMPONENTS
            and e.value_path.startswith("settings.")
            and e.setting_field} - NEVER_LEARNED


def learnable_range_fields(device: Device) -> dict[str, str]:
    """`*MultiRange` field -> its schedule kind, for the windows this model has.

    The same list the panel's schedule editor offers (`schedule_targets`), so a
    learned window lands in exactly what that editor shows.
    """
    return {t["target"]: t["kind"] for t in schedule_targets(device)
            if t["kind"] in _RANGE_CLEANERS}


def _decode_range(field: str, value: Any) -> Any:
    """A range field as the cloud sends it, unwrapped to its list.

    On the wire it is a JSON STRING wrapping its own key again —
    `"{\\"awDisturbMultiRange\\":[[0,600]]}"` (`devices/base.py::encode_multi_range`).
    A bare list, or a string of one, is accepted too. Anything else is None.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    if isinstance(value, dict):
        value = value.get(field)
    return value if isinstance(value, list) else None


def _scalar(value: Any) -> Any:
    """A setting value fit to store, or None. Only scalars are settings."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float, str)):
        return value
    return None


def learn_settings(device: Device, params: Any, *, source: str,
                   only_missing: bool = False) -> dict[str, Any]:
    """Store the settings in a cloud->device payload, as a local write would.

    Args:
        params: `{field: value}` — a `property.set`'s params, or a
            `dev_device_info` settings block. Anything not a dict is ignored.
        source: Named in the log line, so a learned value says where it came
            from.
        only_missing: Fill only fields nothing has stored yet. For a SNAPSHOT
            (`dev_device_info`), which can be older than a write made here: in
            proxy mode an HA write reaches the device but not PetKit's account,
            so the cloud keeps serving the old value, and learning it would
            revert the HA control. A `property.set` is a fresh write and
            overwrites.

    Returns:
        The fields whose stored value CHANGED, with the value stored. Empty when
        nothing was learned, so the caller can skip the save and the HA publish.
    """
    if not isinstance(params, dict) or not params:
        return {}
    scalars = learnable_scalar_fields(device)
    ranges = learnable_range_fields(device)
    settings = device.config.get("settings")
    settings = settings if isinstance(settings, dict) else {}
    multi = device.config.get("multi_config")
    multi = multi if isinstance(multi, dict) else {}

    learned: dict[str, Any] = {}
    for field, raw in params.items():
        if field in NEVER_LEARNED:
            continue
        if field in ranges:
            value = _RANGE_CLEANERS[ranges[field]](_decode_range(field, raw))
            if value is None:
                log.debug("Device %d: not learning %s from %s, unreadable range %r",
                            device.petkit_id, field, source, raw)
                continue
            if multi.get(field) == value or (only_missing and field in multi):
                continue
            store_multi_range(device, field, value)
        elif field in scalars:
            value = _scalar(raw)
            if value is None:
                log.debug("Device %d: not learning %s from %s, not a scalar: %r",
                            device.petkit_id, field, source, raw)
                continue
            if settings.get(field) == value and type(settings.get(field)) is type(value):
                continue
            if only_missing and field in settings:
                continue
            store_setting(device, field, value)
        else:
            continue
        learned[field] = value
        log.info("Device %d: learned %s=%s from %s",
                 device.petkit_id, field, value, source)
    return learned
