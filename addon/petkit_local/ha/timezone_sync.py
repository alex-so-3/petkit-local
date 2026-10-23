"""Keep a device's UTC offset in step with its zone across DST.

A PetKit device has no notion of DST. It holds ONE number, hours east of UTC,
given at BLE provisioning (`timezone:%f`) or by a runtime `property.set`, and
burns it into its video watermarks. We, on the other hand, usually know the
zone NAME it lives in (`config["locale"]`, e.g. `Europe/Rome`), and
`Device.timezone_offset` derives the offset from that name at the moment it is
asked — so the number WE serve is right in both halves of the year.

What was missing is telling the device. `dev_signup` and `dev_device_info`
carry the derived number, but the firmware does not take its clock from those
answers, and a device on MQTT may not ask again for months. The only thing
that has been seen to move the runtime offset is a `property.set{timezone}`
carrying a JSON STRING (`web/api/devices.py::api_timezone`, verified on a live
T5), and until now that was only sent when somebody POSTed an override. So on
the last Sunday of October a T5 in Europe/Rome kept stamping its recordings
with +2 while the rest of the house moved to +1, and stayed an hour off until
the next spring — observed running 2.1.0, worked around with a daily cron that
POSTed the offset by hand.

This module closes that gap with the smallest thing that can: every few
minutes, for each device whose offset comes from its locale, compare the
offset the zone has NOW with the last one the device was told (or, if we never
told it anything, the last one it reported), and push only when they differ.
The push is `push_timezone`, which is also what `api_timezone` uses, so there
is still exactly one place that knows the value must be a string and that a
device on HTTP gets it through the heartbeat queue.

Three things it is careful not to do:

* **Overrule an operator.** A manual override (`config["timezone"]`) means
  "this box is somewhere else than its locale says"; it is never touched.
* **Repeat itself.** The device's readback is its own `%.1f` — `5.75` comes
  back as `5.8` — so a comparison against the readback alone would push
  Kathmandu's offset forever. What we last pushed is remembered in
  `config[TIMEZONE_PUSHED_KEY]` (persisted with the rest of `config`, so a
  restart does not re-push either), and comparisons use a tolerance well
  under the quarter-hour granularity real offsets come in.
* **Reach anything but the device.** No PetKit endpoint is involved; the push
  goes to our own broker or our own heartbeat queue, like every other command.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from petkit_local.devices.base import TIMEZONE_PUSHED_KEY
from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, make_mqtt_property_set
from petkit_local.utils.coerce import to_float
from petkit_local.utils.timeutil import offset_hours_for_locale

if TYPE_CHECKING:  # pragma: no cover - typing only
    from petkit_local.devices.base import Device
    from petkit_local.devices.registry import DeviceRegistry
    from petkit_local.web.hub import EventHub

log = logging.getLogger(__name__)

#: Two offsets closer than this are the same offset. Real ones come in quarter
#: hours (Kathmandu +5.75, Chatham +12.75), and the device reads its value back
#: as `%.1f`, which is at most 0.05 off — so 0.1 absorbs the rounding and still
#: tells any two real offsets apart.
SAME_OFFSET_TOLERANCE = 0.1

#: How often the loop looks. A DST change is twice a year and a few minutes
#: late on a video watermark is invisible; the check is a zoneinfo lookup per
#: device, so this could be much shorter, but there is nothing to gain.
DEFAULT_INTERVAL_SECONDS = 300.0


def _same(a: float, b: float) -> bool:
    return abs(a - b) < SAME_OFFSET_TOLERANCE


def _last_known_offset(device: Device) -> float | None:
    """The offset the device most plausibly holds right now, or None.

    What we last pushed wins: it is the newest statement either side has made,
    because `http/handlers/signup.py` forgets it the moment the device makes a
    newer one — and then the number from that signup is the answer.

    The live MQTT echo (`state["timezone"]`) is deliberately NOT consulted. It
    carries no timestamp, is not persisted, and arrives on an irregular
    `property/post`: right after a signup it may still be the value from before
    the reboot, and right after a push it is the value from before the push.
    Either way it would be read as the device disagreeing, and pushed at again.
    """
    for raw in (device.config.get(TIMEZONE_PUSHED_KEY),
                device.config.get("reported_timezone")):
        value = to_float(raw, None)
        if value is not None:
            return value
    return None


def timezone_push_due(device: Device, when: float | None = None) -> float | None:
    """The offset to push to `device` now, or None if nothing should be sent.

    Only a device whose offset comes from its locale qualifies: with a manual
    override the operator owns the value (and `api_timezone` pushed it), and
    with no usable zone name there is nothing that could have changed — a
    frozen number does not observe DST, which is the whole problem.

    Never raises: `locale` and every stored number arrive from the device and
    go through `offset_hours_for_locale` / `to_float`, which answer None for
    anything unusable.

    Args:
        when: Epoch to evaluate the zone at. For tests; defaults to now.
    """
    try:
        if to_float(device.config.get("timezone"), None) is not None:
            return None
        target = offset_hours_for_locale(device.config.get("locale"), when)
    except Exception:  # noqa: BLE001 - device input never raises
        return None
    if target is None:
        return None
    known = _last_known_offset(device)
    if known is not None and _same(known, target):
        return None
    return target


async def push_timezone(device: Device, bridge: Any, hub: EventHub | None,
                        offset: float | None = None) -> str:
    """Send `offset` (default: the device's effective one) as a `property.set`.

    The one place the timezone push is built, shared by the panel's override
    endpoint and the DST loop below so the two can never disagree about the
    two details that make it work:

    * **The value is a JSON STRING.** `parse_recv_property_set_normal` reads
      `cJSON.valuestring` and calls `atof()`, so a JSON number leaves
      `valuestring` NULL and the write is a silent no-op — verified on a live
      T5, where `5.75` changed nothing and `"5.75"` moved the watermark in ~30s.
    * **A device on HTTP gets it from the heartbeat queue.** Publishing over
      MQTT only was once a silent no-op for every feeder that never reaches the
      broker.

    Records what was sent in `config[TIMEZONE_PUSHED_KEY]`; persisting it is
    the caller's job (`reg.save()` / `mark_dirty()`).

    Returns:
        `"mqtt"` or `"heartbeat-queue"` — what actually happened.
    """
    value = device.timezone_offset if offset is None else offset
    text = f"{value:g}"
    envelope = make_mqtt_property_set({"timezone": text})
    mqtt_live = (device.mqtt_connected and bridge is not None
                 and getattr(bridge, "_client", None))
    delivered = "mqtt"
    if mqtt_live:
        try:
            await bridge.publish_to_device(device, PROPERTY_SET_SUFFIX, envelope)
        except Exception as exc:  # noqa: BLE001 - transport failure is not a rejection
            log.warning("timezone push failed for device %d: %s", device.petkit_id, exc)
            mqtt_live = False
    if not mqtt_live:
        envelope["_service_suffix"] = PROPERTY_SET_SUFFIX
        device.command_queue.append(envelope)
        delivered = "heartbeat-queue"
    device.config[TIMEZONE_PUSHED_KEY] = value
    if hub is not None:
        hub.record_command(device.petkit_id, delivered,
                           f"{PROPERTY_SET_SUFFIX} timezone={text}")
    return delivered


async def sync_timezones(registry: DeviceRegistry, bridge: Any,
                         hub: EventHub | None, when: float | None = None) -> int:
    """One pass over every device; returns how many were pushed.

    A failure on one device is logged and skipped, never allowed to stop the
    pass or the loop: a background task that dies takes the fix with it for
    every device until the next restart.
    """
    pushed = 0
    for device in registry.all():
        offset = timezone_push_due(device, when)
        if offset is None:
            continue
        try:
            delivered = await push_timezone(device, bridge, hub, offset)
        except Exception:  # noqa: BLE001 - one device must not stop the others
            log.exception("timezone sync failed for device %d", device.petkit_id)
            continue
        pushed += 1
        log.info("Device %d: zone %s is now UTC%+g, pushed via %s",
                 device.petkit_id, device.config.get("locale"), offset, delivered)
    if pushed:
        registry.mark_dirty()
    return pushed


async def timezone_sync_loop(registry: DeviceRegistry, bridge: Any,
                             hub: EventHub | None,
                             interval: float = DEFAULT_INTERVAL_SECONDS,
                             clock: Callable[[], float] = time.time) -> None:
    """`sync_timezones` every `interval` seconds, forever.

    Sleeps FIRST: at startup no device has reconnected yet, so every push would
    land in a heartbeat queue rather than on a live MQTT session — harmless, but
    the next pass a few minutes later does it properly. Started by
    `main/lifecycle.py` through `_spawn`, which is what cancels it at shutdown.
    """
    while True:
        await asyncio.sleep(interval)
        try:
            await sync_timezones(registry, bridge, hub, clock())
        except Exception:  # noqa: BLE001 - the loop must outlive any one pass
            log.exception("timezone sync pass failed")
