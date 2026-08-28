"""Transport-agnostic event/media normalizers — turn a `dev_event_report` POST,
an MQTT `thing/event/*` message, or a `dev_upload_file_info_v2` entry into rows
ready for `EventStore.upsert_event` / `upsert_media`. The visit-session grouping
those stored rows are read back into lives in `events/sessions.py`.

**The protocol knowledge itself lives in `events/codes.py`** — every event
code and MQTT topic, its confidence grade, the firmware function behind it and
the device families that emit it. `events/decode.py` renders values from those
tables. This module owns only the transport work: pulling fields out of a form
body, an MQTT envelope or a file-info entry.

HTTP (`dev_event_report`, from `cloud`) sends a NUMERIC code as a string;
MQTT (`thing/event/*`, from `ctrl`) sends a semantic name. They cannot collide,
so `classify_event_kind` handles both through one `codes.lookup`.

The two namespaces that must never be merged
--------------------------------------------
 (1) The ON-DEVICE `dev_event_report` codes (device -> our server), in
     `codes.HTTP_EVENT_CODES`. Authoritative for our path.
 (2) The CLOUD RECORD API's `LitterRecord.subContent[].eventType` that the
     PetKit *cloud* returns to the app, kept quarantined in
     `codes.CLOUD_RECORD_TYPES`. It overlaps ours on 5/8/10 and is a
     different space; nothing here consults it.

Only the namespace-independent SUB-FIELDS (`result`, `start_reason`, `err`)
are shared between them, and `events/decode.py` owns that decoding.
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.parse
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from petkit_local.devices.state_parsers import (apply_consumable_state,
                                                normalize_property_params,
                                                parse_state_report,
                                                reports_used_times)
from petkit_local.events import codes, decode
from petkit_local.utils.coerce import to_float, to_int
from petkit_local.utils.dicts import first_of

if TYPE_CHECKING:
    from petkit_local.devices.base import Device

log = logging.getLogger(__name__)

# --- event_kind classification --------------------------------------------
# The tables themselves are in events/codes.py.

_warned_event_types: set[str] = set()


def classify_event_kind(event_type: str, content: dict | None = None,
                        device_type: str | None = None) -> str:
    """Best-effort `event_kind` bucket, used for Timeline filtering and
    session grouping.

    `content` flags (`toiletEvent`/`cleanEvent`/`cvrEvent`/`petEvent`, as seen
    on file_info entries -- see media/layout.py) take priority when present,
    since a per-chunk flag is the more specific signal than the event code.
    Otherwise the code is resolved through `codes.lookup`, which spans the
    HTTP and MQTT namespaces at once.

    `device_type` matters: the HTTP codes are per device category, so code 2
    is a cleared fault on a litter box and a completed meal on a feeder.
    Omitting it assumes a litter box.
    """
    content = content or {}
    # `toiletEvent` is the one that means the box was actually USED.
    # `petEvent` only means the pet was seen/recorded -- an episode can carry
    # petEvent=1 with toiletEvent=0 for its whole length (that is exactly
    # what an "appeared" episode is), so it must not imply a toilet visit.
    if content.get("toiletEvent"):
        return codes.KIND_TOILET
    if content.get("cleanEvent"):
        return codes.KIND_CLEANING
    if content.get("cvrEvent"):
        return codes.KIND_MOTION
    if content.get("petEvent"):
        return codes.KIND_PET

    code = codes.lookup(event_type, device_type)
    if code is not None:
        return code.kind

    # Same reasoning as the unknown-moduleType warning: an unrecognised code
    # quietly becomes a bare "Event 12" row on the timeline, so say so once.
    et = str(event_type or "")
    if et and et not in _warned_event_types:
        _warned_event_types.add(et)
        log.warning("Unknown event_type %r from device - shown as a generic event. "
                    "Add it to the table in events/codes.py.", et)
    return codes.KIND_OTHER


def event_type_label(event_type: str, device_type: str | None = None) -> str:
    """Static human label for a raw event_type, with no content to refine it.

    Equivalent to `event_label(event_type, None)`; kept as its own name because
    callers that genuinely have no content read better this way.
    """
    return decode.event_label(event_type, None, device_type)


def cleaning_label(event_type: str, content: dict | None = None,
                   device_type: str | None = None) -> str:
    """The specific label for an event, decoded from its sub-fields.

    A thin alias kept for its callers, and narrower in name than in behaviour:
    `decode.event_label` labels every code, not just the cleaning ones, so new
    code should prefer that directly.
    """
    return decode.event_label(event_type, content, device_type)


def is_detail_event(event_type: str, device_type: str | None = None) -> bool:
    """Whether the Timeline should collapse this step behind the expander.

    Low-level steps the official app never surfaces -- mechanism positioning,
    cycle-start markers, the mid-visit weight sample -- stay stored (they are
    useful for protocol work and HA automations) but fold behind "show N more
    steps" so a visit card reads like the app's: a couple of completion lines
    rather than six internal ones.
    """
    code = codes.lookup(event_type, device_type)
    return bool(code and code.detail)


# The device sometimes emits INVALID JSON: a bare, unquoted token as a value,
# e.g. `{"related_event":3_10000001_1784741818,"count":1}` (confirmed on a real
# T5 for event_type 24). `json.loads` rejects it, which silently threw away the
# whole content — including the parent link that ties the event to its episode.
_BARE_VALUE = re.compile(r'(:\s*)([A-Za-z_0-9][A-Za-z_0-9.\-]*)(\s*[,}\]])')
# Strict JSON number grammar. NOT `float()`: Python accepts underscore digit
# separators, so `4_10000001_1784743819` would look like a valid number and
# the token would be left unquoted — exactly the value that needs quoting.
_JSON_NUMBER = re.compile(r'-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?\Z')


def _repair_bare_values(text: str) -> str:
    """Quote the device's unquoted value tokens so `json.loads` accepts them.

    Genuine JSON literals (`true`/`false`/`null`) and strict JSON numbers are
    left alone — only a token that is neither gets quoted, which is exactly the
    malformed case. The result is still only trusted if it then parses, see
    `_as_dict`.
    """
    def fix(m: re.Match[str]) -> str:
        """Quote one candidate token, or return the match untouched."""
        prefix, token, suffix = m.groups()
        if token in ("true", "false", "null") or _JSON_NUMBER.match(token):
            return m.group(0)
        return f'{prefix}"{token}"{suffix}'
    return _BARE_VALUE.sub(fix, text)


def _as_dict(value: object) -> dict:
    """Decode a `content`/`state` field that may already be a dict or may be a
    JSON string. Kept here rather than in utils/dicts.py: this is JSON decoding
    (including the bare-token repair above), not dict traversal."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            # Only attempt the repair once the strict parse has failed, and
            # only trust it if the result parses cleanly — a bad guess must
            # degrade to "no content", never to made-up content.
            try:
                parsed = json.loads(_repair_bare_values(value))
            except (json.JSONDecodeError, TypeError):
                return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _parent_event_of(content: dict) -> str | None:
    """The **cross-episode** link: a cleaning episode's events carry
    `content.relate_event` (singular "relate" — confirmed on a real T5,
    2026-07-22) holding the `event_id` of the *visit* that triggered the
    cleaning. This is what actually ties a cleaning back to its visit, so
    `group_sessions` can attach sub-events deterministically instead of
    guessing from timestamp proximity. Not to be confused with
    `related_event`, which is this codebase's name for an event's OWN
    episode id."""
    if not isinstance(content, dict):
        return None
    v = content.get("relate_event") or content.get("related_event") or content.get("relatedEvent")
    return str(v) if v else None


def _best_match(content: dict) -> dict | None:
    """The highest-scoring entry of `content.score_info`, or None.

    `score_info` is a LIST of `{id, score}` objects on a real T5 (confirmed
    2026-07-22) and the firmware builds it as an array that may hold several
    matches, with no ordering we could confirm — so "first" is only correct in
    a one-cat household. Falls back to a bare `score`/dict shape in case
    another device model sends it differently.
    """
    # `or`, not an `is None` check: `score_info` is an EMPTY LIST on 31 of 33
    # captured detection results, and an empty list must fall through to the
    # bare `score` a different model might send rather than swallow it.
    raw = content.get("score_info") or content.get("score")
    if isinstance(raw, (int, float)):
        return {"score": raw}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        entries = [e for e in raw if isinstance(e, dict)]
        if entries:
            return max(entries, key=lambda e: to_float(e.get("score"), float("-inf")))
        # A list of bare numbers, which the pre-`score_info` code accepted.
        scalars = [e for e in raw if isinstance(e, (int, float))]
        if scalars:
            return {"score": max(scalars)}
    return None


def _extract_score(content: dict) -> float | int | None:
    """The best match's face-recognition score, or None.

    NOT comparable with `dev_discern_config`'s `score` threshold: that one
    gates BODY detection (whether an episode opens at all) on an entirely
    different scale. Observed values here run 9..1846 against a threshold of
    25, which is why nothing filters on it — see `_extract_pet_ref`.
    """
    match = _best_match(content)
    score = match.get("score") if match else None
    return score if isinstance(score, (int, float)) else None


def _extract_pet_ref(content: dict) -> int | None:
    """The pet identity the DEVICE reported, verbatim.

    `content.score_info[].id` is the id we handed out in `dev_discern_pic` —
    the firmware copies it from the outer list entry (confirmed in
    `get_update_face_score_info`), so it is a pet id, not a photo id. The
    legacy `petId`/`pet_id` keys are still read as a fallback; they appear in
    none of our 308 captured event reports, which is precisely why nothing was
    ever attributed to a pet before.

    Deliberately NOT filtered by score. The only threshold the cloud gives us
    belongs to a different metric (see `_extract_score`), so discarding a match
    on that comparison would be a category error dressed up as tuning.

    Zero is NOT an identity — it is the device saying it recognised nobody, and
    it must not be stored as one. A real W7H sent four `pet_discern` events
    (2026-07-31) each carrying `count: 1, pet_id: 0`: a pet was there and was
    not identified. What settles it rather than leaving it a guess is the same
    device's `discernPic: []` — it had downloaded no faces at all, so it could
    not have matched anyone. 0 is also not a value any real id takes: ours are
    SQLite row ids and start at 1, and PetKit's cloud ids are nine digits.

    Left unresolved here: `events/normalize.py` is transport, and this id is
    not necessarily one of ours. `ai/pets.py::PetRegistry.resolve_pet_ref` maps it
    to `events.pet_id`, or to nothing.
    """
    match = _best_match(content)
    if match and match.get("id") is not None:
        return _identity_or_none(match["id"])
    return _identity_or_none(content.get("petId", content.get("pet_id")))


def _identity_or_none(value: object) -> int | None:
    """A reported pet id, or None for the "recognised nobody" sentinel."""
    pet_ref = to_int(value, None)
    return None if pet_ref == 0 else pet_ref


# --- dev_event_report (HTTP) -----------------------------------------------

EVENT_TYPE_KEYS = ("eventType", "event_type", "type")
EVENT_ID_KEYS = ("eventId", "event_id", "id")
CONTENT_KEYS = ("content",)
STATE_KEYS = ("state",)


def parse_event_report_form(text: str) -> dict:
    """Parse the dev_event_report POST body: form-urlencoded (same convention
    as dev_state_report's `state=<JSON>`), tolerating a bare JSON body too.
    Returns a flat dict of {field_name: str | dict}, decoding any field whose
    value looks like JSON."""
    text = (text or "").strip()
    if not text:
        return {}

    if text.startswith("{"):
        try:
            obj = json.loads(text)
            return obj if isinstance(obj, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}

    parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
    out = {}
    for k, values in parsed.items():
        if not values:
            continue
        v = values[0]
        if v[:1] in ("{", "["):
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                pass
        out[k] = v
    return out


def from_event_report(device: Device, form: dict) -> dict:
    """Normalize a dev_event_report POST body into an `events` row. The
    caller reads `row["_state"]` (popped before persisting — EventStore only
    writes known columns) to refresh `device.state` via the existing state
    parsers, so a device that only ever calls dev_event_report (not
    dev_state_report) still keeps HA in sync.

    Confirmed from a real T5 capture (2026-07-22): the top-level `event_id`
    is a **session/episode key shared by multiple distinct event_type
    reports** (e.g. "9" then "10" both carry the same event_id for one
    visit) — it is NOT a report's own unique id, and it is what groups an
    episode, not `content.related_event` (a key that does not exist; the real
    field, seen once, is `content.relate_event` — singular "relate" — and it
    is a *cross-episode* reference, e.g. a cleaning episode pointing back at
    the visit that triggered it). So: `related_event` = the raw `event_id`
    (groups same-episode reports for the Timeline), and `event_uid` (the
    EventStore dedup key) = `event_id + event_type` so distinct reports in
    the same episode don't overwrite each other."""
    event_type = str(first_of(form, *EVENT_TYPE_KEYS, default="") or "")
    episode_id = str(first_of(form, *EVENT_ID_KEYS, default="") or "")
    content = _as_dict(first_of(form, *CONTENT_KEYS))
    state = _as_dict(first_of(form, *STATE_KEYS))

    event_uid = f"{episode_id}:{event_type}" if episode_id else None

    return {
        "event_uid": event_uid,
        "related_event": episode_id or None,
        "parent_event": _parent_event_of(content),
        "device_id": device.petkit_id,
        "device_type": device.device_type,
        "event_type": event_type,
        "event_kind": classify_event_kind(event_type, content, device.device_type),
        "ts": time.time(),
        "source": "http",
        "pet_ref": _extract_pet_ref(content),
        "score": _extract_score(content),
        "content_json": json.dumps(content) if content else None,
        "state_json": json.dumps(state) if state else None,
        "_state": state,
        "_content": content,
    }


# --- state only an event can supply ------------------------------------

#: `done_word`s of the cleaning completions that mean the box actually ran a
#: cycle. Every other `cleaning`/`done` row is a different completion sharing
#: the bucket -- deodorizing, sand correction, the LED illuminator ("light
#: cycle"), a consumable reset -- and dating "Last Clean" from one of those
#: would report a clean that never happened.
_CLEAN_DONE_WORDS = frozenset({"cleaning", "litter empty", "reset"})

#: HA `event` entity per event_kind. The MQTT path can key on its own semantic
#: names, but the HTTP path cannot: there `event_type` is a numeric code whose
#: meaning depends on the device category, so both go through `codes.lookup`
#: and dispatch on the resulting kind instead.
KIND_TO_ENTITY = {
    codes.KIND_TOILET: "toilet_event",
    codes.KIND_CLEANING: "cleaning_event",
    codes.KIND_ERROR: "error_event",
    codes.KIND_FEEDING: "feeding_event",
    # Only the W7H produces this kind, and only it publishes the entity.
    codes.KIND_DRINKING: "drinking_event",
}


def entity_for_event(event_type: str, device_type: str | None = None) -> str | None:
    """The HA `event` entity an event fires, or None if it maps to none."""
    code = codes.lookup(event_type, device_type)
    return KIND_TO_ENTITY.get(code.kind) if code else None


#: Transport envelope, not telemetry. Every MQTT `params` carries these
#: alongside the device's actual readings -- confirmed on a live T5, where 186
#: of 186 `property` posts included `XDevice`.
#:
#: They must be stripped before `params` is merged into `device.state`, which is
#: a dict of what entities read and is rendered verbatim in the panel's raw-state
#: view. `XDevice` in particular is the signed request credential
#: (`id=...&nonce=...&sign=...`), and it has no business being displayed or kept.
MQTT_ENVELOPE_KEYS = frozenset({"XDevice", "event_id", "timestamp", "content", "state"})


def telemetry_only(params: dict) -> dict:
    """`params` with the transport envelope removed."""
    return {k: v for k, v in params.items() if k not in MQTT_ENVELOPE_KEYS}


def apply_state_snapshot(device: Device, state: Any) -> bool:
    """Refresh `device.state` from the snapshot an event report carries with it.

    Both transports embed a full state blob in an event -- over HTTP as the
    form's `state=<JSON>`, over MQTT as `params.state` (a JSON string) -- and
    both must apply it, because an event is sometimes the ONLY carrier of a
    value that changed. Confirmed on a T5: an N60 reset from PetKit's app moved
    `sprayResetTime` and announced it inside `liquid_reset_over`, while the
    `property` stream stayed silent for 74 minutes either side of it.

    The raw blob is merged FIRST and the parsers overlaid on top, the same order
    `bridge.py` uses for a `property` post. That matters beyond the debug view:
    no parser passes `sprayResetTime` through -- it is consumed to derive a
    countdown -- yet `payloads.to_device_info` echoes `state["sprayResetTime"]`
    back to the device. Applying only the parsed keys would leave the raw stamp
    frozen at whatever a state report last happened to carry, and we would hand
    the box back a reset date older than the one it just told us about.

    Returns:
        True if a decodable snapshot was applied, so the caller can decide
        whether to persist and re-publish. Anything undecodable is skipped
        rather than raised on -- the event itself is still worth recording.
    """
    if isinstance(state, str):
        try:
            state = json.loads(state)
        except (json.JSONDecodeError, TypeError):
            return False
    if not isinstance(state, dict) or not state:
        return False

    # The RAW merge lands first, for the panel's Debug info and the echoes, and
    # it is not filtered: it is what the device said. That makes it the one path
    # that can still overwrite the derived visit count with a firmware zero, so
    # the count is lifted over it. No T4 report seen carries a flat `usedTimes`
    # (it is nested under `litter`), but a model that does would silently undo
    # the whole fix, and the failure would look exactly like the bug it fixes.
    derived_used_times = None
    if not reports_used_times(device.device_type):
        derived_used_times = device.state.get("usedTimes")

    device.state.update(state)  # raw, for the panel's Debug info and the echoes
    device.state.update(parse_state_report(device.device_type, state))
    device.state.update(normalize_property_params(device.device_type, state))
    if derived_used_times is not None:
        device.state["usedTimes"] = derived_used_times
    apply_consumable_state(device)
    device.last_state_report = time.time()
    return True


def apply_derived_state(device: Device, event_type: str, content: dict) -> None:
    """Fold into `device.state` the values that only an EVENT ever carries.

    Four entities -- Last Clean, Last Visit, Last Feed and Pet Weight -- have no
    field in any state report; they exist only as a consequence of something
    happening. BOTH transports call this, and it has to stay that way: living on
    the MQTT path alone, it would leave every device that reports over HTTP --
    each ESP32 model, and every Ingenic device until the `mqtt` patcher is
    applied -- with all four reading unknown forever.

    Dispatch goes through `codes.lookup`, which resolves either namespace, so
    the two transports cannot drift apart.
    """
    code = codes.lookup(event_type, device.device_type)
    if code is None:
        return

    if code.kind == codes.KIND_CLEANING and code.role == codes.ROLE_DONE:
        if code.done_word in _CLEAN_DONE_WORDS:
            device.state["lastClean"] = _now_iso()

    elif code.kind == codes.KIND_TOILET and code.role == codes.ROLE_VISIT_SUMMARY:
        device.state["lastVisit"] = _now_iso()
        # Weight rides in the content, never in params or the state block.
        weight = to_float(content.get("pet_weight", content.get("petWeight")), None)
        if weight is not None:
            device.state["petWeight"] = weight
        _accumulate_visit_count(device, content)

    elif code.kind == codes.KIND_FEEDING and code.role == codes.ROLE_DONE:
        device.state["lastFeed"] = _now_iso()
        _accumulate_feed_totals(device, content)


def visit_day_key(ts: float | None = None) -> str:
    """The `YYYY-MM-DD` local day a visit belongs to, for the daily counter.

    A litter box's visit summary carries no `day` field — unlike a feeder's
    `feed_over`, which is why the feeder counter can follow the device's own
    reading of the date and this one cannot. `time_in` is the device clock, and
    the device is handed our locale at provisioning (`utils/timeutil.py`), so
    localizing it here puts the two clocks back in the same day. Falls back to
    now for a summary that omits the stamp.

    Cut at LOCAL midnight, matching `EventStore.pet_visit_stats` and the panel's
    Timeline. Cutting at UTC would file an after-midnight visit under yesterday
    on every install east of Greenwich, and the sensor would read one low every
    morning until the offset had passed.
    """
    moment = datetime.fromtimestamp(ts) if ts else datetime.now()
    return moment.date().isoformat()


def _accumulate_visit_count(device: Device, content: dict) -> None:
    """Keep today's "Times Used" running for a box that never reports it.

    `state.usedTimes` is a field the T4 sends pinned at zero (see
    `utils/const.py::DEVICE_TYPES_LITTER_USED_TIMES_UNREPORTED`), so on those
    models `state_parsers` drops it and this is the only writer. PetKit's own
    service counts the same thing from the same visit records; being the cloud
    means doing the same.

    A model whose counter IS live keeps it: this returns without touching
    `state` there, so a T5's own number is never overwritten by ours.

    `usedTimesDay` rides alongside the count so the rollover is explicit rather
    than a scheduled reset — the first visit of a new local day sees a stale day
    key and starts from one. `main/lifecycle.py` seeds both at startup from the
    event store, because `Device.to_dict` excludes `state` and a restart would
    otherwise drop the day's count back to zero until the next visit.
    """
    if reports_used_times(device.device_type):
        return
    day = visit_day_key(to_float(content.get("time_in"), None))
    count = 0
    if device.state.get("usedTimesDay") == day:
        count = int(to_float(device.state.get("usedTimes"), 0) or 0)
    device.state["usedTimesDay"] = day
    device.state["usedTimes"] = count + 1


def _accumulate_feed_totals(device: Device, content: dict) -> None:
    """Keep today's "Times Dispensed" and "Total Dispensed" running.

    Both sensors read `state.feedState.*`, and no feeder state report carries
    those totals — on PetKit's own service the cloud sums them from the feed
    events, so being the cloud means doing the same. Without this the two
    entities existed and could never hold a value.

    Per DAY, which the device's own vocabulary settles: the same block carries
    `planAmountTotal` and `eatAvg`, neither of which means anything as a
    lifetime figure. `content.day` (YYYYMMDD) is the device's own reading of
    which day it is, so the rollover follows its clock rather than ours.

    A D4SH reports the amount PER HOPPER, `real_amount1` and `real_amount2`;
    single-hopper firmware uses `real_amount`. Reading only the unsuffixed
    name would leave a dual-hopper feeder's counter permanently at zero. A
    feed that dispensed nothing — a jam, an outlet block — is not a dispense,
    so it neither counts nor adds.

    NOT persisted: `Device.to_dict` deliberately excludes `state`, and this
    stays inside it. The totals are lost on restart and rebuilt from the day's
    remaining feeds, which is the same trade every other `state` key makes.
    """
    grams = sum(
        to_float(content.get(key), 0) or 0
        for key in ("real_amount", "real_amount1", "real_amount2",
                    "realAmount", "realAmount1", "realAmount2")
        if key in content
    )
    if grams <= 0:
        return
    day = content.get("day")
    totals = device.state.get("feedState")
    if not isinstance(totals, dict) or (day is not None and totals.get("day") != day):
        totals = {"day": day, "times": 0, "realAmountTotal": 0}
    totals["times"] = int(to_float(totals.get("times"), 0) or 0) + 1
    totals["realAmountTotal"] = round(
        (to_float(totals.get("realAmountTotal"), 0) or 0) + grams, 1)
    device.state["feedState"] = totals


def _now_iso() -> str:
    """Current time as an ISO-8601 UTC string, for HA timestamp sensors."""
    return datetime.now(timezone.utc).isoformat()


# --- MQTT thing/event/* ------------------------------------------------

def from_mqtt(device: Device, event_type: str, params: dict) -> dict:
    """Normalize an MQTT thing/event/* message (already parsed by
    mqtt/bridge.py) into an `events` row. `params` is the raw event params;
    the nested JSON-string `content` field is decoded the same way the bridge
    already does for pet_out weight / error text (see bridge._event_content).

    Confirmed on a live T5 (2026-07-29): an MQTT event carries **the same
    envelope as the HTTP form** — `{XDevice, event_id, timestamp, content,
    state}` — so every field below is read exactly as `from_event_report`
    reads it, `event_id` included. All 24 non-`property` frames in that
    capture carried both `event_id` and `state`; `property` carries neither,
    which is why mqtt/bridge.py never persists it as an event.

    Taking `content.related_event` as this row's OWN episode — the pre-capture
    guess — is what left every MQTT card unparented in the Timeline. It is the
    *parent* link (`_parent_event_of`), so a `pet_discern` pointing back at its
    `pet_detect` took the parent's id as its own while the `pet_detect` anchor
    got no id at all, and the two could never group; the three steps of a
    cleaning cycle likewise became three cards instead of one. `event_id` is
    the session key on both transports, and now means that on both.
    """
    params = params if isinstance(params, dict) else {}
    content = _as_dict(params.get("content"))
    state = _as_dict(params.get("state"))
    episode_id = str(first_of(params, *EVENT_ID_KEYS, default="") or "")

    return {
        # Same dedup key as HTTP, for the same reason: one episode reports
        # several event types under one id, so deduping on the id alone would
        # keep only the last of them.
        "event_uid": f"{episode_id}:{event_type}" if episode_id else None,
        "related_event": episode_id or None,
        "parent_event": _parent_event_of(content),
        "device_id": device.petkit_id,
        "device_type": device.device_type,
        "event_type": event_type,
        "event_kind": classify_event_kind(event_type, content, device.device_type),
        "ts": time.time(),
        "source": "mqtt",
        "pet_ref": _extract_pet_ref(content),
        "score": _extract_score(content),
        "content_json": json.dumps(content) if content else None,
        # Kept for the panel's Debug info so an MQTT row shows what an HTTP one
        # shows. Not returned as `_state`, because on this path `bridge.py`
        # applies the snapshot itself via `apply_state_snapshot` before the row
        # is even built. Dropping it on the theory that the `property` stream
        # refreshes everything anyway does not hold: on a T5 an N60 reset moved
        # `sprayResetTime` and said so only inside `liquid_reset_over`, with no
        # `property` post for 74 minutes around it.
        "state_json": json.dumps(state) if state else None,
    }


# --- dev_upload_file_info_v2 -------------------------------------------

_FILE_ID_KEYS = ("fileId", "file_id", "id")
_EVENT_ID_KEYS_MEDIA = ("eventId", "event_id")
_MODULE_KEYS = ("moduleType", "module_type")
_CYCLE_KEYS = ("cycleType", "cycle_type")  # kept as a fallback; not present on a real T5 (see below)
_FILE_TYPE_KEYS = ("fileType", "file_type")
_START_KEYS = ("startTime", "start_time")
_END_KEYS = ("endTime", "end_time")
_DURATION_KEYS = ("duration", "durationMs", "duration_ms")
_AES_IV_KEYS = ("aesIv", "aes_iv")
_ENCRYPT_KEYS = ("encrypt",)
_SIZE_KEYS = ("size", "fileSize", "file_size")

# The category comes from `moduleType`, because `fileInfos[]` entries have NO
# `cycleType` field on a real T5 (confirmed 2026-07-22 capture). Each mapping
# below was cross-referenced against the *actual* upload path the device used
# once it had re-polled our per-capability STS pathPrefix
# (devices/payloads.py::to_oss_sts): it truncates each cycleType to 4 chars for
# the path segment ("fullVideo" -> ".../full/...", "eventImage" ->
# ".../even/...", "dynamicVideo" -> ".../dyna/..."), and those segments lined up
# exactly with these moduleTypes in the same capture. "highLight" (-> "high")
# was not exercised there — no highlight-worthy visit happened — so it is not in
# the table yet.
#
# The set is exhaustive: the firmware `cloud`/`ctrl` binaries
# (`HEALTH_PRED:local_name(%s) cloud_name(%s)`) emit exactly these six names.
CATEGORY_CLOUD_DOUBLE = "cloudDouble"
CATEGORY_WASTE_CHECK = "wasteCheck"
CATEGORY_HEALTH = "healthPic"

_MODULE_TYPE_TO_CATEGORY = {
    # The main recording: 1056x1056 @25fps with AAC, in ~4s chunks.
    "CLOUD_STORAGE": "fullVideo",
    # A ~4x TIME-LAPSE of the same span CLOUD_STORAGE covers — not a second half
    # of it and not a plain low-res mirror. Measured on real files: 528x528,
    # silent, ~1s of footage per ~4s of wall clock (a stitched pair covered 74s
    # of reality in 20s of video). That is why it looks "sped up": inherent to
    # the stream, not something we do to it. Mapping it to `fullVideo` as well
    # mixes two incompatible streams into one folder, where they concatenate
    # into garbage — hence a category of its own, stitched only against its own
    # kind. It is deliberately NOT one of the four STS capabilities, since the
    # device never asks for it by name (see CATEGORY_TO_CAPABILITY below).
    "CLOUD_DOUBLE": CATEGORY_CLOUD_DOUBLE,
    # ONE poster image per event. Shares the `even`/eventImage path prefix with
    # SHIT_PICTURE and is a different thing entirely.
    "EVENT_PREVIEW": "eventImage",
    "EVENT_VIDEO": "dynamicVideo",
    # The app's **"Check waste" gallery** — ~5 photos per cleaning cycle, the
    # multi-shot set to EVENT_PREVIEW's single poster. Unmapped, all five land in
    # an "Other" folder under one colliding filename and the gallery is
    # invisible in the timeline.
    "SHIT_PICTURE": CATEGORY_WASTE_CHECK,
    # The T5's stool-health-analysis photo; it runs poop analysis on the NPU.
    "HEALTH_PRED": CATEGORY_HEALTH,
}

# A category is the fine-grained *role*; the STS capability is the coarser slot
# the device negotiates. Several roles share one capability, so the capability
# gate and retention grouping resolve through this rather than assuming the
# category IS a capability.
CATEGORY_TO_CAPABILITY = {
    "fullVideo": "fullVideo",
    CATEGORY_CLOUD_DOUBLE: "fullVideo",
    "dynamicVideo": "dynamicVideo",
    "eventImage": "eventImage",
    CATEGORY_WASTE_CHECK: "eventImage",
    CATEGORY_HEALTH: "eventImage",
    "highLight": "highLight",
}

_warned_module_types: set[str] = set()


def capability_for_category(category: str) -> str | None:
    """The STS capability a media role belongs to, or None if it isn't
    governed by one."""
    return CATEGORY_TO_CAPABILITY.get(category)


def _resolve_category(info: dict) -> str:
    """The media role for one file_info entry, or `""` if it can't be resolved.

    An explicit `cycleType` wins if the entry somehow has one (no real T5 does
    — see `_CYCLE_KEYS`); otherwise the role comes from `moduleType`. An
    unmapped moduleType is warned about ONCE per type and then degrades to an
    uncategorised file rather than being dropped.
    """
    cycle = first_of(info, *_CYCLE_KEYS)
    if cycle:
        return str(cycle)
    module_type = str(first_of(info, *_MODULE_KEYS, default="") or "")
    category = _MODULE_TYPE_TO_CATEGORY.get(module_type, "")
    if not category and module_type and module_type not in _warned_module_types:
        # Loud, once per type: an unmapped moduleType silently became an
        # uncategorised file in an "Other" folder, which is exactly how the
        # SHIT_PICTURE waste gallery went unnoticed.
        _warned_module_types.add(module_type)
        log.warning("Unknown moduleType %r from device - media will be uncategorised. "
                    "Add it to _MODULE_TYPE_TO_CATEGORY (events/normalize.py).", module_type)
    return category


def from_file_info(device: Device, info: dict) -> dict:
    """Normalize one `dev_upload_file_info_v2` `fileInfos[]` entry into a
    `media` row. The capability category (fullVideo/eventImage/highLight/
    dynamicVideo — see devices/payloads.py::to_oss_sts) is derived from
    `moduleType`, see `_MODULE_TYPE_TO_CATEGORY`.

    Raises:
        ValueError: The entry carries no `fileId`. That id is the media
            table's primary key and the only handle on the raw upload, so
            such an entry cannot be recorded at all.
    """
    file_id = str(first_of(info, *_FILE_ID_KEYS, default="") or "")
    if not file_id:
        raise ValueError("file_info entry has no fileId")

    encrypt = first_of(info, *_ENCRYPT_KEYS, default="0")
    encrypted = str(encrypt).strip() in ("1", "true", "True")

    return {
        "file_id": file_id,
        "device_id": device.petkit_id,
        "related_event": str(first_of(info, *_EVENT_ID_KEYS_MEDIA, default="") or "") or None,
        "module_type": str(first_of(info, *_MODULE_KEYS, default="") or ""),
        "category": _resolve_category(info),
        "file_type": str(first_of(info, *_FILE_TYPE_KEYS, default="") or ""),
        "encrypted": 1 if encrypted else 0,
        "aes_iv": str(first_of(info, *_AES_IV_KEYS, default="") or "") or None,
        # Coerced for the same reason start/end are: these columns are read
        # back into arithmetic (media/stitch.py sums duration_ms,
        # media/retention.py sums size_bytes) and SQLite's dynamic typing
        # happily stores "4000ms" in an INTEGER column, so an uncoerced value
        # only blows up later, in a background sweeper, far from this request.
        "duration_ms": to_int(first_of(info, *_DURATION_KEYS), None),
        "start_ts": to_float(first_of(info, *_START_KEYS), None),
        "end_ts": to_float(first_of(info, *_END_KEYS), None),
        "size_bytes": to_int(first_of(info, *_SIZE_KEYS), None),
        # Per-chunk detection confidence, 0-100, alongside the flag it belongs
        # to. This is DETECTION ("an animal is in frame"), a different question
        # from the face-recognition score on an event's `score_info` — a chunk
        # can carry petScore 100 while nobody was identified. `pet_event` keeps
        # the flag as text because that is the column's declared type; the
        # capture only ever shows 0/1.
        "pet_score": to_float(info.get("petScore"), None),
        "pet_event": str(info["petEvent"]) if info.get("petEvent") is not None else None,
        "status": "pending",
    }
