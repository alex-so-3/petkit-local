"""The schedule editors' one write endpoint, and what a valid schedule is.

Four shapes, one per `defaults.schedule_targets()` kind: a range list, a weekly
list, the litter box's timed jobs, and the feeder's meals. Each cleaner is
deliberately permissive about the CONTENT and strict about the SHAPE, because
the odd-looking values are the ones that came out of the real app.
"""
from __future__ import annotations

import datetime
import json
import time
from typing import Any

from aiohttp import web

from petkit_local.devices import defaults
from petkit_local.devices.base import encode_multi_range
from petkit_local.ha.commands import PROPERTY_SET_SUFFIX, make_mqtt_property_set
from petkit_local.http.handlers.feed import _build_latest, _compute_next_tick
from petkit_local.utils.coerce import to_int
from petkit_local.utils.const import DEVICE_TYPES_FEEDER_DUAL
from petkit_local.web.api._common import _deliver, _device_or_404, _json_body


#: Minutes in a day. A schedule range runs 0..1440 inclusive — 1440 is the end
#: of the day and appears in every "entire day" payload PetKit sends.
DAY_MINUTES = 24 * 60

#: Seconds in a day — the unit of a feeder meal's `t`, alone among the
#: schedule shapes (see `_clean_feed_schedule`).
DAY_SECONDS = 24 * 3600


def _clean_range_list(value: Any) -> list[list[int]] | None:
    """`[[start, end], ...]` in minutes, or None if it is not that.

    Deliberately permissive about the CONTENT and strict about the SHAPE. An
    end below its start crosses midnight and is normal; a one-minute window
    ([0, 1]) came straight out of the app; several windows at once are what the
    do-not-disturb capture contained. So nothing here sorts, merges, or drops a
    range for looking odd — the only rejections are values that are not minutes
    in a day, because those are the ones the firmware cannot mean.
    """
    if not isinstance(value, list):
        return None
    cleaned: list[list[int]] = []
    for pair in value:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            return None
        start, end = to_int(pair[0], None), to_int(pair[1], None)
        if start is None or end is None:
            return None
        if not (0 <= start <= DAY_MINUTES and 0 <= end <= DAY_MINUTES):
            return None
        cleaned.append([start, end])
    return cleaned


def _clean_weekly_list(value: Any) -> list[dict[str, Any]] | None:
    """`[{enable, rpt, time: [[s, e]]}]` — ranges plus weekdays and a switch.

    `rpt` is a comma-separated list of weekday numbers where SUNDAY IS 1
    (`events/codes.py::WEEKDAY_NAMES`). It is rebuilt from the parsed numbers
    rather than passed through, so a client cannot smuggle a string into a field
    the firmware splits on commas.
    """
    if not isinstance(value, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        times = _clean_range_list(entry.get("time"))
        if times is None:
            return None
        days = []
        for part in str(entry.get("rpt", "")).split(","):
            day = to_int(part, None)
            if day is None or not (1 <= day <= 7):
                return None
            days.append(day)
        if not days:
            return None
        cleaned.append({
            "enable": int(bool(to_int(entry.get("enable", 1), 1))),
            "rpt": ",".join(str(d) for d in sorted(set(days))),
            "time": times,
        })
    return cleaned


def _clean_point_list(value: Any) -> list[dict[str, Any]] | None:
    """`[{id, repeats, time, type}]` — the litter box's timed jobs.

    ONE array carries both of them: `type` 0 is a cleaning and 1 a deodorizing,
    confirmed on a T5 whose array gained a `type: 1` entry when a periodic
    deodorizing time was added. An unrecognised `type` is kept as it is rather
    than rejected — the editor groups by it, and a value nobody has seen is not
    a reason to delete somebody's entry.
    """
    if not isinstance(value, list):
        return None
    cleaned: list[dict[str, Any]] = []
    for entry in value:
        if not isinstance(entry, dict):
            return None
        minute = to_int(entry.get("time"), None)
        kind = to_int(entry.get("type"), None)
        entry_id = to_int(entry.get("id"), None)
        if minute is None or kind is None or entry_id is None:
            return None
        if not 0 <= minute < DAY_MINUTES:
            return None
        days = []
        for part in str(entry.get("repeats", "")).split(","):
            day = to_int(part, None)
            if day is None or not (1 <= day <= 7):
                return None
            days.append(day)
        if not days:
            return None
        cleaned.append({
            "id": entry_id,
            "repeats": ",".join(str(d) for d in sorted(set(days))),
            "time": minute,
            "type": kind,
        })
    return cleaned


def _clean_meal_amounts(meal: dict, dual: bool) -> dict[str, Any] | None:
    """A meal's dispensed amount, validated, in the model's own shape.

    A Dual-Hopper carries `a1`/`a2`, one per hopper (`a2` defaults 0); a
    single-hopper D4H/D4H2 carries a lone `a`, which is the field its
    `pk_schmg_parse_schedule` reads — sent `a1`/`a2` it dispenses nothing
    (`err_code: 8`, the mirror of the Dual-Hopper's issue #2). Which key a
    feeder reads is per model, exactly as `feed_realtime` is (`ha/commands.py::
    _feed`), and the two amounts never convert: `a` is the device's own unit,
    not the portions a hopper counts.

    For a single hopper a legacy `a1` is accepted as `a` — a schedule the
    dual-only editor saved before this split is read, not silently emptied —
    but never the other way. All are a single byte on the device, so 0..255.
    """
    if dual:
        first = to_int(meal.get("a1"), None)
        second = to_int(meal.get("a2"), 0)
        if first is None or second is None:
            return None
        if not (0 <= first <= 255 and 0 <= second <= 255):
            return None
        return {"a1": first, "a2": second}
    amount = to_int(meal.get("a"), None)
    if amount is None:
        amount = to_int(meal.get("a1"), None)  # legacy dual-only editor save
    if amount is None or not 0 <= amount <= 255:
        return None
    return {"a": amount}


def _clean_feed_schedule(value: Any, dual: bool) -> dict[str, Any] | None:
    """`{schedule: [{re, it, itemJsonString}], nextTick, latest}` — the feeder's.

    The shape comes from a D4SH 867 `ctrl` (`pk_schmg_parse_schedule`), which
    reads `re` and `it` per group and `id`/`t` per meal, then the amount in the
    model's own key: `a1`/`a2` on a Dual-Hopper, a lone `a` on the single-hopper
    D4H/D4H2 (`dual` selects which; see `events/codes.py::FEED_SCHEDULE_ITEM_KEYS`
    and `_clean_meal_amounts`). Unlike every other schedule here, a meal's `t`
    counts SECONDS since local midnight — the cloud's `n_46560` fires at 12:56:00
    (D4SH capture, 2026-08-12) — and the id is the cloud's `n_<seconds>` scheme,
    regenerated whenever a client sends an int.

    `itemJsonString` is rebuilt from `it` rather than trusted: the real cloud
    sends both, they are the same list twice, and two copies of one value in one
    payload is exactly the pair that drifts. Key order in it is the cloud's —
    alphabetical. `nextTick` and `latest` are recomputed at serve time, and the
    `v: 2` stamp marks the schedule as seconds-based so the one-time minute
    migration (`feed.migrate_minute_schedule`) never touches a current save.
    """
    if not isinstance(value, dict):
        return None
    groups_in = value.get("schedule")
    if not isinstance(groups_in, list):
        return None

    groups: list[dict[str, Any]] = []
    for group in groups_in:
        if not isinstance(group, dict):
            return None
        days = []
        for part in str(group.get("re", "")).split(","):
            day = to_int(part, None)
            if day is None or not (1 <= day <= 7):
                return None
            days.append(day)
        if not days:
            return None

        meals: list[dict[str, Any]] = []
        for meal in group.get("it") or []:
            if not isinstance(meal, dict):
                return None
            second_of_day = to_int(meal.get("t"), None)
            if second_of_day is None or not 0 <= second_of_day < DAY_SECONDS:
                return None
            amounts = _clean_meal_amounts(meal, dual)
            if amounts is None:
                return None
            raw_id = meal.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                raw_id = f"n_{second_of_day}"
            meals.append({"id": raw_id, "t": second_of_day, **amounts})

        groups.append({
            "re": ",".join(str(d) for d in sorted(set(days))),
            "it": meals,
            "itemJsonString": json.dumps(
                meals, separators=(",", ":"), sort_keys=True),
        })

    cleaned = dict(value)
    cleaned["schedule"] = groups
    cleaned["v"] = 2
    return cleaned


async def api_save_schedule(request: web.Request) -> web.Response:
    """Store one schedule and push it to the device.

    Body: `{"target": <field>, "value": <parsed JSON>}`, where `target` is one
    of the names `defaults.schedule_targets()` gave out. Anything else is refused
    rather than stored, so the panel cannot invent a field.

    The write goes to the device as `property.set`, which is how the real cloud
    does it — captured going through this add-on to a T5 on 2026-08-09. The two
    shapes on the wire are NOT the same and that is the trap:

      * a range field carries a JSON string that wraps its own key again
        (`encode_multi_range`),
      * `schedule` carries a plain JSON string of the array, no wrapper.

    Storing happens either way, because `dev_multi_config` and
    `dev_schedule_get` are what the device actually reads on its own clock; the
    push only saves it the wait. A feeding schedule is stored and NOT pushed:
    no capture shows the cloud writing one, and `dev_feed_get` serves it.
    """
    reg = request.app["registry"]
    hub = request.app["hub"]
    bridge = request.app["bridge"]
    d = _device_or_404(request)

    body = await _json_body(request)

    target = body.get("target")
    known = {t["target"]: t["kind"] for t in defaults.schedule_targets(d)}
    if target not in known:
        return web.json_response({"error": f"unknown schedule {target}"}, status=400)
    kind = known[target]
    raw = body.get("value")

    if kind == "feed":
        dual = d.device_type.lower() in DEVICE_TYPES_FEEDER_DUAL
        feed = _clean_feed_schedule(raw, dual)
        if feed is None:
            return web.json_response({"error": "not a valid feeding schedule"}, status=400)
        d.config["feed_schedule"] = feed
        reg.save()
        _push_feed_get(d, hub, bridge, feed)
        now = time.time()
        latest = _build_latest(feed, now)
        wire_groups = []
        for g in feed.get("schedule", []):
            wire_groups.append({"re": g.get("re", ""), "it": g.get("it", [])})
        wire = {
            "schedule": wire_groups,
            "nextTick": _compute_next_tick(latest),
            "latest": latest,
        }
        mqtt_cmd = make_mqtt_property_set(
            {"feed": json.dumps(wire, separators=(",", ":"))})
        return await _deliver(hub, bridge, d, PROPERTY_SET_SUFFIX, mqtt_cmd)

    cleaner = {"ranges": _clean_range_list, "weekly": _clean_weekly_list,
               "points": _clean_point_list}[kind]
    value = cleaner(raw)
    if value is None:
        return web.json_response(
            {"error": f"not a valid {kind} schedule"}, status=400)

    if kind == "points":
        d.config["schedule"] = value
        params = {"schedule": json.dumps(value, separators=(",", ":"))}
    else:
        d.config.setdefault("multi_config", {})[target] = value
        params = {target: encode_multi_range(target, value)}
    reg.save()

    return await _deliver(hub, bridge, d, PROPERTY_SET_SUFFIX,
                          make_mqtt_property_set(params))


async def api_deferred_feed(request: web.Request) -> web.Response:
    """Add or list deferred (one-off) feeds for a feeder.

    POST ``{"date": "2026-08-13", "time": "17:05", "a1": 0, "a2": 1}``
    (Dual-Hopper) or ``{..., "a": 20}`` (single-hopper D4H/D4H2) adds a deferred
    feed. GET lists pending ones. DELETE with ``{sound_id}`` in path removes one.
    The amount is stored in the model's own key so the feed actually dispenses
    (`_clean_meal_amounts`); a legacy `a1` is read as `a` on a single hopper.

    The device picks this up on its next ``dev_feed_get`` poll, which the
    heartbeat ``feed_get:1`` command triggers immediately.
    """
    reg = request.app["registry"]
    hub = request.app["hub"]
    bridge = request.app["bridge"]
    d = _device_or_404(request)

    feed = d.config.setdefault("feed_schedule", {
        "schedule": [{"re": "1,2,3,4,5,6,7", "it": [], "itemJsonString": "[]"}],
    })
    deferred = feed.setdefault("deferred", [])

    if request.method == "GET":
        return web.json_response({"deferred": deferred})

    if request.method == "DELETE":
        try:
            feed_id = request.match_info["feed_id"]
        except KeyError:
            return web.json_response({"error": "missing feed_id"}, status=400)
        feed["deferred"] = [d2 for d2 in deferred if d2.get("id") != feed_id]
        reg.save()
        _push_feed_get(d, hub, bridge, feed)
        return web.json_response({"ok": True})

    body = await _json_body(request)
    date_str = body.get("date", "")
    time_str = body.get("time", "")
    if d.device_type.lower() in DEVICE_TYPES_FEEDER_DUAL:
        amounts = {"a1": to_int(body.get("a1"), 0), "a2": to_int(body.get("a2"), 0)}
    else:
        amounts = {"a": to_int(body.get("a", body.get("a1")), 0)}

    try:
        dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        dt = dt.replace(tzinfo=datetime.timezone(
            datetime.timedelta(hours=d.timezone_offset)))
    except (ValueError, TypeError):
        return web.json_response({"error": "bad date/time"}, status=400)

    fire_at = dt.timestamp()
    if fire_at <= time.time():
        return web.json_response({"error": "time is in the past"}, status=400)

    secs_since_midnight = dt.hour * 3600 + dt.minute * 60 + dt.second
    feed_id = f"d_{dt.strftime('%Y%m%d')}_{secs_since_midnight}"

    entry = {"id": feed_id, **amounts, "fire_at": fire_at}
    deferred.append(entry)
    reg.save()

    _push_feed_get(d, hub, bridge, feed)
    return web.json_response({"ok": True, "feed": entry})


def _push_feed_get(d, hub, bridge, feed):
    """Queue feed_get:1 heartbeat + MQTT property.set{feed}."""
    d.command_queue.append({"msgType": 1, "payload": {"feed_get": "1"},
                            "timestamp": int(time.time())})
