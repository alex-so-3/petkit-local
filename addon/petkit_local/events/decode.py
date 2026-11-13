"""Render a device event's `content` for humans, from the tables in `codes.py`.

`codes.py` holds what a field MEANS; this module decides how it READS. One
decoder serves three consumers -- the Timeline card label, the panel's Debug
view, and the HA "Device Status" sensor -- so a value can never be explained
one way in the UI and another way in an entity.

Two properties this module guarantees, both of which the Debug view depends on:

* **`decode_content` is total.** Every key present in `content` produces a row,
  including keys we have no table entry for (rendered raw, graded `unknown`).
  A firmware that starts sending a new field must become *visible*, not vanish.
* **Nothing raises.** Device input reaches here unvalidated, so every renderer
  degrades to the raw value rather than aborting a request.

What is deliberately NOT interpreted
------------------------------------
`box`, `pos`, `item_id`, `components`, `current` and `interval` are shown raw.
`ph_reason` is labelled but its VALUES are not decoded: urine pH detection was
off for the whole capture, so 5 and 4 are its "not measured" codes and a real
reading has never been seen. So is `auto_clear`, which looks boolean until you see
the value 7 it carried once. So is an integer `err`: 256 and 128 look like a
bitmask (1<<8, 1<<7) sitting next to code 7's separate `components` field, but
"looks like" is not evidence, so no bit meanings are invented.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from petkit_local.events import codes
from petkit_local.utils.coerce import to_float, to_int

# --- value tables ----------------------------------------------------------

#: `start_reason` on completion codes, and `reason` on mechanism codes. Both
#: use this space: the maintenance episode's code 3 carried reason=3 and its
#: closing code 7 carried start_reason=3.
TRIGGER: dict[int, str] = {
    0: "Auto",
    1: "Periodic",
    2: "Manual (app)",
    3: "Manual (button)",
}

#: The same space collapsed for use inside a sentence, where "Manual (app)
#: cleaning canceled" reads worse than "Manual cleaning canceled". The Debug
#: table uses `TRIGGER` and keeps the distinction.
TRIGGER_SHORT: dict[int, str] = {
    0: "Auto",
    1: "Periodic",
    2: "Manual",
    3: "Manual",
}

#: How a CLEANING cycle ended, and only a cleaning cycle -- see
#: `_result_field` for why this table must not be read as a global enum.
#:
#: `4` is the bin stopping the cycle: it appears in a capture of four device
#: families ONLY alongside `err: "full"` (T5 twice, T6 once) and in no other
#: context. It used to read "canceled (kitten mode)", which was never a wire
#: meaning at all -- the slot was borrowed as a lookup constant for a DIFFERENT
#: case, `result == 3` with a `kitten` flag, and then a real `4` arrived from a
#: box with a full bin and inherited the wrong label. Kitten mode now has
#: `RESULT_KITTEN` and this table only describes values devices actually send.
RESULT: dict[int, str] = {
    0: "completed",
    1: "terminated",
    2: "failed",
    3: "canceled",
    4: "stopped (bin full)",
}

#: Not a wire value: the label for `result == 3` when `content.kitten` is set.
#: Kept out of `RESULT` so a real `3` still reads "canceled" and a real `4`
#: cannot pick this up by accident.
RESULT_KITTEN = "canceled (kitten mode)"

#: Hall-sensor and bin faults. `hallB` is ours -- 16 captures, present in no
#: reference table -- and is graded a step lower than its documented siblings.
ERR_TEXT: dict[str, str] = {
    "full": "bin full",
    "hallL": "hall sensor (L)",
    "hallT": "hall sensor (T)",
    "hallB": "hall sensor (B)",
}

#: Sentinels the device uses for "no error". `NULL` is a literal four-character
#: string, not JSON null, and it is what all 22 captured code-5 events carry --
#: so treating it as a fault would put a false cause on every clean cycle.
ERR_NONE = frozenset({"", "null", "none", "0"})

#: Grades a decoded field can carry. Mirrors `codes.GRADES` plus `UNKNOWN` for
#: a value we pass through without claiming to understand.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class DecodedField:
    """One row of the Debug view's decoded table.

    `raw` is the value exactly as the device sent it (JSON-safe, so it can go
    straight into the API response); `text` is the rendered form. Keeping both
    is the point: the reader can always check our interpretation against the
    original without opening the raw JSON block.
    """

    key: str
    label: str
    raw: Any
    text: str
    grade: str
    note: str = ""


# --- primitive renderers ---------------------------------------------------
# Each takes an untrusted value and returns display text. None of them raise.

def _enum(table: dict[int, str], value: Any, unknown: str) -> tuple[str, str]:
    """Render `value` through an int-keyed table.

    Returns (text, grade). An out-of-table value renders as `unknown` with the
    number interpolated rather than being dropped -- the point of surfacing it
    is that we can see a code we have not mapped yet.
    """
    number = to_int(value, None)
    if number is None:
        return _raw_text(value), UNKNOWN
    name = table.get(number)
    if name is None:
        return unknown.format(n=number), UNKNOWN
    return name, codes.CONFIRMED


def _grams(value: Any) -> tuple[str, str]:
    """Weight in grams, promoted to kg once it stops reading naturally.

    The device mixes scales freely: a code-10 `pet_weight` of 2320 is a 2.32 kg
    cat while its `shit_weight` of 10 is ten grams. One threshold covers both.
    """
    number = to_float(value, None)
    if number is None:
        return _raw_text(value), UNKNOWN
    if abs(number) < 1000:
        return f"{number:g} g", codes.CONFIRMED
    return f"{number / 1000:.2f} kg", codes.CONFIRMED


def _percent(value: Any) -> tuple[str, str]:
    """A percentage. Graded down: every capture read 100, so the scale and
    even the direction (remaining vs. used) are unconfirmed."""
    number = to_float(value, None)
    if number is None:
        return _raw_text(value), UNKNOWN
    return f"{number:g}%", codes.INFERRED


def _yes_no(value: Any) -> tuple[str, str]:
    """A confirmed-boolean flag."""
    number = to_int(value, None)
    if number is None:
        return _raw_text(value), UNKNOWN
    return ("yes" if number else "no"), codes.CONFIRMED


def _seconds(value: Any) -> tuple[str, str]:
    """A duration in seconds."""
    number = to_float(value, None)
    if number is None:
        return _raw_text(value), UNKNOWN
    return f"{number:g} s", codes.INFERRED


def _epoch(value: Any) -> tuple[str, str]:
    """A UNIX timestamp, rendered in the server's local timezone.

    The device's epochs are correct UTC; rendering them local is what makes
    the Debug view comparable with the Timeline row above it.
    """
    number = to_float(value, None)
    if number is None or number <= 0:
        return _raw_text(value), UNKNOWN
    try:
        stamp = datetime.fromtimestamp(number).astimezone()
    except (OSError, OverflowError, ValueError):
        return _raw_text(value), UNKNOWN
    return stamp.strftime("%Y-%m-%d %H:%M:%S %z"), codes.CONFIRMED


def _voice_time(value: Any) -> tuple[str, str]:
    """`[{"start": epoch, "end": epoch}]` -> "12s at 14:22", or "none".

    Rendered as durations rather than raw epochs because the useful fact is how
    long the cat was vocal, not when the span happened to start.
    """
    if not isinstance(value, list) or not value:
        return "none", codes.CONFIRMED
    parts = []
    for span in value:
        if not isinstance(span, dict):
            parts.append(_raw_text(span))
            continue
        start, end = span.get("start"), span.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            when, grade = _epoch(start)
            clock = when.split(" ")[1][:5] if grade == codes.CONFIRMED and " " in when else when
            parts.append(f"{int(end - start)}s at {clock}")
        else:
            parts.append(_raw_text(span))
    return "; ".join(parts), codes.CONFIRMED


def _score_info(value: Any) -> tuple[str, str]:
    """The AI recognition result: a list of {id, score} pairs.

    `id` is the pet id we handed the device in `dev_discern_pic` -- the
    firmware copies it verbatim from the outer list entry
    (`get_update_face_score_info`), so it is a pet id, not a photo id. Bound to
    a row by `ai/pets.py::PetRegistry.resolve_pet_ref`, which leaves it unbound
    if the box is still matching against faces cached from PetKit's cloud.
    """
    if not isinstance(value, list) or not value:
        return "none", codes.CONFIRMED
    parts = []
    for entry in value:
        if isinstance(entry, dict):
            parts.append(f"id {entry.get('id')}, score {entry.get('score')}")
        else:
            parts.append(_raw_text(entry))
    return "; ".join(parts), codes.CONFIRMED


def _shortened(value: Any) -> tuple[str, str]:
    """A long opaque string (a URL, a key) trimmed to its tail.

    The full value stays in `raw` and in the raw-JSON block; this only keeps
    the table readable inside the panel's narrow column.
    """
    text = _raw_text(value)
    if len(text) <= 48:
        return text, codes.CONFIRMED
    return "..." + text[-45:], codes.CONFIRMED


def _raw_text(value: Any) -> str:
    """Last-resort rendering for a value we do not interpret."""
    if value is None:
        return "null"
    if isinstance(value, str):
        # An empty string is a real observation -- every captured error event
        # carries msg="" and detail="" -- and rendering it as nothing leaves a
        # blank table cell that reads like a bug in the panel.
        return value if value else "(empty)"
    if isinstance(value, (list, dict)):
        try:
            return json.dumps(value, ensure_ascii=True)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _raw(value: Any) -> tuple[str, str]:
    """Pass a value through unexplained, graded so the UI can say so."""
    return _raw_text(value), UNKNOWN


# --- shared sub-field decoders ---------------------------------------------

def decode_err(value: Any) -> tuple[str | None, str]:
    """Decode a polymorphic `err` field into (cause, grade).

    Returns `(None, grade)` when the field means "no error" -- which is most
    of the time, and getting that wrong would stamp a fault on every healthy
    cycle. The field is genuinely polymorphic in the captures: the empty
    string and the literal `"NULL"` both mean success, `"hallB"` is a real
    fault code, and integers 256/128 appear on mechanism steps.
    """
    if value is None:
        return None, codes.CONFIRMED
    if isinstance(value, bool):
        return (None, codes.CONFIRMED) if not value else (_raw_text(value), UNKNOWN)
    if isinstance(value, str):
        text = value.strip()
        if text.lower() in ERR_NONE:
            return None, codes.CONFIRMED
        known = ERR_TEXT.get(text)
        if known is not None:
            # hallB is capture-only; its documented siblings are not.
            grade = codes.INFERRED if text == "hallB" else codes.CONFIRMED
            return known, grade
        return text, UNKNOWN
    number = to_int(value, None)
    if number is not None:
        if number == 0:
            return None, codes.CONFIRMED
        return f"code {number} (0x{number:x})", UNKNOWN
    return None, UNKNOWN


def work_mode_name(value: Any, device_type: str | None = None) -> str | None:
    """NS5 work-mode name for a raw `action`/`workMode` value, or None.

    Shared with the HA "Device Status" sensor so `state.workingState` stops
    publishing a bare integer. Per device category, because a fountain's
    `action` is a different enum in the same field.
    """
    number = to_int(value, None)
    if number is None:
        return None
    return codes.work_modes_for(device_type).get(number)


def _err_field(value: Any) -> tuple[str, str]:
    """`decode_err` shaped for the decoded table, where "no error" is a row."""
    cause, grade = decode_err(value)
    return (cause or "none"), grade


def _result_field(value: Any) -> tuple[str, str]:
    """`result`, keeping unmapped values visible instead of blank.

    `RESULT` describes a CLEANING cycle. `result` is NOT one enum across the
    protocol — the same field carries a different vocabulary per event, exactly
    as `event_type` does per device category. Observed in one capture of four
    families::

        clean_over      0, 4          ble_relay_over   0, 1, 2, 6
        feed_over       0, 7, 10      add_water_over   0, 5, 18
        reset_over      2 (T6), 5 (T5)

    So `ble_relay_over result=1` is not "terminated" and `2` is not "failed" —
    those labels come from the litter table and nothing supports them here.
    Values outside `RESULT` render as `result {n}` and are graded UNVERIFIED
    rather than being dressed up in a cleaning cycle's vocabulary. Splitting
    this per event needs a source for each family's meanings; until then the
    raw number is the honest answer.
    """
    return _enum(RESULT, value, "result {n}")


# --- the field table -------------------------------------------------------

_Renderer = Callable[[Any], "tuple[str, str]"]


@dataclass(frozen=True)
class _FieldSpec:
    """How one `content` key is labelled and rendered."""

    label: str
    render: _Renderer
    note: str = ""


# Local aliases so the field table below reads uniformly against NS5/NS6.
_FEED_SRC = codes.FEED_SRC
_FEED_RESULT = codes.FEED_RESULT


_FIELDS: dict[str, _FieldSpec] = {
    # -- operation identity
    "action": _FieldSpec(
        "Work mode", lambda v: _enum(codes.WORK_MODES, v, "mode {n}"),
        "NS5 work mode, per device family. On a litter box only 0, 2 and 9 "
        "have been observed here; a fountain uses a different enum entirely."),
    "reason": _FieldSpec(
        "Trigger", lambda v: _enum(TRIGGER, v, "reason {n}"),
        "Per-STEP trigger, not per-cycle: one maintenance episode carried "
        "3, 0, 3, 2, 0, 3 across its six steps."),
    "start_reason": _FieldSpec(
        "Start reason", lambda v: _enum(TRIGGER, v, "reason {n}")),
    "result": _FieldSpec("Result", _result_field),
    "err": _FieldSpec("Error", _err_field),
    # -- weights and levels
    "pet_weight": _FieldSpec("Pet weight", _grams),
    "petWeight": _FieldSpec("Pet weight", _grams),
    "shit_weight": _FieldSpec("Waste weight", _grams),
    "dump_weight": _FieldSpec("Dumped weight", _grams),
    "clean_weight": _FieldSpec(
        "Cleaned weight", _grams, "Read 0 in every capture; unit unconfirmed."),
    "litter_weight": _FieldSpec(
        "Litter weight", _grams, "Read 0 in every capture; unit unconfirmed."),
    "litter_percent": _FieldSpec(
        "Litter level", _percent,
        "Read 100 in every capture, so neither the scale nor the direction "
        "(remaining vs. used) is confirmed."),
    # -- visit facts
    "is_shit": _FieldSpec("Waste detected", _yes_no),
    "toiletDetection": _FieldSpec("Toilet detection", _yes_no),
    # Not event content — a setting, decoded here so the panel's state view
    # stops showing a bare `2` for something with a name. The whole enum came
    # out of a controlled run through the app's picker; there is no 0.
    "sandType": _FieldSpec(
        "Litter type", lambda v: _enum(codes.SAND_TYPES, v, "litter type {n}")),
    "count": _FieldSpec(
        "Detections", lambda v: (_raw_text(v), codes.CONFIRMED),
        "How many animals were detected. Independent of `score_info`: count 1 "
        "with an empty score_info means something was seen but nobody was "
        "identified, which is 31 of 33 captured 'appeared' episodes."),
    "area": _FieldSpec(
        "Detection area", _raw,
        "On a LITTER BOX the detected animal's bounding box in detector "
        "pixels: 2444..810810 across 429 detections, never above 921600, so "
        "the detector appears to run on a 1280x720 frame. "
        "dev_discern_config's `area` (6000) is NOT a hard floor -- a T5 "
        "reported 5605 and 2444 -- so it gates something earlier than what "
        "arrives here. "
        "On a W7H it is a different quantity entirely: 0 or 100 and nothing "
        "else, in all 442 fountain detections, which is not a pixel count. "
        "Whatever it measures there has no source, so it is shown raw."),
    "score_info": _FieldSpec(
        "Recognition", _score_info,
        "The pet id we served in dev_discern_pic, plus a face-match score. NOT "
        "comparable with dev_discern_config's `score`, which is the "
        "body-detection floor on a different scale."),
    "score": _FieldSpec(
        "Score", _raw, "Face-match similarity, observed 9..1846 on a litter "
                       "box. Not the same quantity as dev_discern_config's "
                       "`score` threshold. A W7H's `tracker_info[].pet_score` "
                       "runs to 48609 over 577 entries -- whether that is the "
                       "same scale is not established, so the two are not "
                       "compared."),
    # -- times
    "time_in": _FieldSpec("Entered", _epoch),
    "time_out": _FieldSpec("Left", _epoch),
    "start_time": _FieldSpec("Started", _epoch),
    "over_time": _FieldSpec("Finished", _epoch),
    "mark": _FieldSpec("Episode mark", _epoch),
    "voice_time": _FieldSpec(
        "Yowling", _voice_time,
        "Spans of the cat vocalising during the visit, produced by the AI "
        "camera's Yowling Detection (settings.voice). One capture in 27 "
        "carried a single 12-second span; the rest were empty."),
    "interval": _FieldSpec("Interval", _seconds),
    # -- links and media
    "relate_event": _FieldSpec(
        "Linked episode", lambda v: (_raw_text(v), codes.CONFIRMED)),
    "related_event": _FieldSpec(
        "Linked episode", lambda v: (_raw_text(v), codes.CONFIRMED)),
    "img": _FieldSpec("Preview URL", _shortened),
    "aesKey": _FieldSpec("AES key", _shortened),
    "upload": _FieldSpec("Upload requested", _yes_no),
    "media": _FieldSpec("Media attached", _yes_no),
    # -- error detail
    "msg": _FieldSpec(
        "Message", _raw, "Empty in all 16 captured error events."),
    "detail": _FieldSpec(
        "Detail", _raw, "Empty in all 16 captured error events."),
    # -- feeder (NS6)
    "src": _FieldSpec("Source", lambda v: _enum(_FEED_SRC, v, "source {n}")),
    "err_code": _FieldSpec(
        "Feed result", lambda v: _enum(_FEED_RESULT, v, "error {n}")),
    # -- surfaced but deliberately unexplained
    "ph_reason": _FieldSpec(
        "Urine pH", _raw,
        "Produced by Urine pH Detection (settings.phDetection), which reads "
        "PetKit's own indicator litter. Observed values 5 and 4 -- but that "
        "feature was OFF for the whole capture, so those are its 'not "
        "measured' codes and the meaning of a real reading is unknown."),
    "auto_clear": _FieldSpec(
        "auto_clear", _raw,
        "Not a boolean: one capture carried 7."),
    "from_clear": _FieldSpec("from_clear", _raw),
    "clean_flag": _FieldSpec("clean_flag", _raw),
    "pos": _FieldSpec("pos", _raw, "Read 0 in all 54 captures."),
    "item_id": _FieldSpec("item_id", _raw, "Read 0 in every capture."),
    "box": _FieldSpec("box", _raw, "Read 0 in every capture."),
    "current": _FieldSpec("current", _raw),
    "components": _FieldSpec("components", _raw),
    "key": _FieldSpec(
        "key", _raw,
        "Named by the firmware RE for codes 8 and 17 but absent from all 68 "
        "such captures -- see those codes' notes."),
    "petVoice": _FieldSpec(
        "Cat vocalised", _yes_no,
        "Whether Yowling Detection heard anything this visit; the spans are in "
        "voice_time."),
    "voice_reason": _FieldSpec(
        "voice_reason", _raw, "Read 0 in all 27 captures, including the one "
                              "visit that did record yowling."),
}

#: Interesting keys first, in reading order; everything else follows
#: alphabetically. Ordering is cosmetic but stops the important fields from
#: being buried under `upload`/`media` noise.
_FIELD_ORDER: tuple[str, ...] = (
    "action", "reason", "start_reason", "result", "err",
    "is_shit", "shit_weight", "pet_weight", "petWeight",
    "time_in", "time_out", "interval",
    "litter_percent", "litter_weight", "clean_weight", "dump_weight",
    "score_info", "score", "count", "area", "toiletDetection",
    "start_time", "over_time", "mark",
    "relate_event", "related_event",
)


#: Renderers whose vocabulary depends on the device model, keyed by field name.
#:
#: Separate from `_FIELDS` because a `_FieldSpec.render` sees only the value —
#: and the one field that needs more than the value is exactly the one that was
#: being read in the wrong language. Kept to a table rather than an `if` in the
#: loop so adding the next such field is one line, in the obvious place.
_DEVICE_AWARE: dict[str, Any] = {
    "action": lambda v, dt: _enum(codes.work_modes_for(dt), v, "mode {n}"),
}


def decode_content(event_type: str | None,
                   content: dict[str, Any] | None,
                   device_type: str | None = None) -> list[DecodedField]:
    """Every field of `content`, decoded and ordered for display.

    Total by contract: a key with no `_FieldSpec` still produces a row, graded
    `unknown`. `event_type` is accepted for future per-code disambiguation and
    to keep the signature stable; the field table is otherwise shared.

    `device_type` is not decoration: `action` is a work-mode enum whose
    vocabulary is per model, so without it the Debug view named a fountain's
    jobs out of the litter table — the same field, a different language.
    """
    if not isinstance(content, dict) or not content:
        return []

    ordered = [k for k in _FIELD_ORDER if k in content]
    ordered += sorted(k for k in content if k not in _FIELD_ORDER)

    fields: list[DecodedField] = []
    for key in ordered:
        value = content[key]
        spec = _FIELDS.get(key)
        if spec is None:
            text, grade = _raw(value)
            fields.append(DecodedField(key=key, label=key, raw=value,
                                       text=text, grade=grade))
            continue
        renderer = _DEVICE_AWARE.get(key)
        try:
            if renderer is not None:
                text, grade = renderer(value, device_type)
            else:
                text, grade = spec.render(value)
        except Exception:  # noqa: BLE001 - a renderer must never break a request
            text, grade = _raw_text(value), UNKNOWN
        fields.append(DecodedField(key=key, label=spec.label, raw=value,
                                   text=text, grade=grade, note=spec.note))
    return fields


#: Keys whose value is a UNIX timestamp, derived from the field table rather
#: than listed again so the two cannot drift.
#:
#: The panel needs to know which rows these are because it renders them itself:
#: `_epoch` formats in the SERVER's timezone, and the Timeline card headers
#: format in the BROWSER's, so a container in UTC and a browser in CEST showed
#: the same instant twice, two hours apart, in adjacent rows of one table.
#: One page, one timezone — the reader's.
EPOCH_FIELDS = frozenset(k for k, s in _FIELDS.items() if s.render is _epoch)


def summary_bits(event_type: str | None,
                 content: dict[str, Any] | None) -> list[str]:
    """Short facts worth putting on the Timeline card itself.

    Only values with a confirmed meaning appear here; everything else stays in
    the Debug view. Returns [] when the event carries nothing worth saying.
    """
    if not isinstance(content, dict) or not content:
        return []

    bits: list[str] = []
    if to_int(content.get("is_shit"), 0):
        weight = to_float(content.get("shit_weight"), None)
        bits.append(f"waste {_grams(weight)[0]}" if weight else "waste")
    level = to_float(content.get("litter_percent"), None)
    if level is not None and to_int(content.get("result"), None) is not None:
        bits.append(f"litter {level:g}%")
    # Yowling. PetKit frames meowing in the box as a possible sign of physical
    # discomfort, so it belongs on the card rather than buried in Debug — and
    # `events/sessions.py::_filter_buckets` also files such a visit under the
    # Health alert chip. The duration comes from the spans when they parse;
    # `petVoice` alone still says it happened.
    if to_int(content.get("petVoice"), 0):
        spans = content.get("voice_time")
        total = 0
        if isinstance(spans, list):
            for span in spans:
                if isinstance(span, dict):
                    a_, b_ = to_float(span.get("start"), None), to_float(span.get("end"), None)
                    if a_ is not None and b_ is not None and b_ > a_:
                        total += int(b_ - a_)
        bits.append(f"yowled {total}s" if total else "yowled")
    # The fault cause is deliberately NOT repeated here: `event_label` already
    # appends it, so adding it would print "Error - hall sensor (B)" beside a
    # "hall sensor (B)" chip on the same card.
    return bits


# --- label building --------------------------------------------------------

def event_label(event_type: str | None,
                content: dict[str, Any] | None = None,
                device_type: str | None = None,
                state: dict[str, Any] | None = None) -> str:
    """The human label for one event, as specific as its content allows.

    `device_type` disambiguates the HTTP namespace, whose numeric codes mean
    different things per device category -- pass it whenever you have it.

    `state` is the device snapshot attached to the report. It matters only for
    a code whose CONTENT cannot say which way the event went: the light fires
    the same payload switching on as switching off, and only the state tells
    them apart. Omitting it degrades to the generic wording, never to a wrong
    direction.

    Layers, each applied only when the data supports it, so an event that
    carries nothing extra still reads correctly:

    1. the code's static label, or ``Event <n>`` for a code we do not know;
    2. qualified by its work mode when it carries one, so a code 3 reads
       "Odor removal - mechanism started" rather than a flat "Cleaning";
    3. rewritten as "<trigger> <operation> <outcome>" for completion codes,
       e.g. "Manual cleaning canceled";
    4. suffixed with the failure cause whenever the device names one.

    Step 4 is not gated on ``result == 2``, unlike the implementation this
    replaces: that condition held in zero of 268 captured events, so every
    error cause the device reported was invisible.
    """
    raw_type = str(event_type or "")
    code = codes.lookup(raw_type, device_type)
    if code is None:
        base = f"Event {raw_type}" if raw_type else "Event"
        return _with_cause(base, content)

    label = code.label
    result = to_int((content or {}).get("result"), None)
    noun = _directional_noun(code, state) or code.done_word

    if code.role == codes.ROLE_DONE and noun and result is not None:
        trigger = TRIGGER_SHORT.get(
            to_int((content or {}).get("start_reason"), None))
        if noun != code.done_word:
            label = f"{trigger} {noun}" if trigger else noun
        else:
            if code.kind == codes.KIND_FEEDING:
                outcome, _ = _enum(_FEED_RESULT, result, "result {n}")
            elif code.done_word == codes.DEEP_CLEAN_DONE_WORD:
                outcome, _ = _enum(codes.FOUNTAIN_W7H_DISINFECT_RESULTS,
                                   result, "result {n}")
            else:
                outcome, _ = _result_field(result)
                if result == 3 and (content or {}).get("kitten"):
                    outcome = RESULT_KITTEN
            label = f"{trigger} {noun} {outcome}" if trigger \
                else f"{noun} {outcome}"
    elif code.mode_from:
        raw_mode = (content or {}).get(code.mode_from)
        if to_int(raw_mode, None) is not None:
            # An unmapped mode renders as "mode 99" rather than being dropped:
            # a firmware that adds one should show up in the timeline, not
            # quietly collapse every step to the same generic label.
            mode, _ = _enum(codes.work_modes_for(device_type), raw_mode,
                            "mode {n}")
            label = f"{mode} - {code.label.lower()}"

    return _with_cause(_capitalise(label), content)


def _directional_noun(code: codes.EventCode,
                      state: dict[str, Any] | None) -> str | None:
    """The direction this event went, read from the device state, or None.

    Only meaningful for a code carrying `state_label`. The light reports the
    same payload switching on as switching off; the attached state does say
    which -- `lightState` is present while the light is on and gone once it is
    off. Without a state we return None and the caller keeps the generic
    wording, so a missing snapshot can never invent a direction.
    """
    if not code.state_label or state is None:
        return None
    key, when_present, when_absent = code.state_label
    return when_present if state.get(key) is not None else when_absent


def _with_cause(label: str, content: dict[str, Any] | None) -> str:
    """Append the device-named failure cause, when there is one we can name.

    Only a RECOGNISED cause reaches the label. An integer `err` decodes to
    something like "code 256 (0x100)", and every captured code-4 carried
    exactly that -- appending it would put an alarming suffix on a routine
    mechanism step while asserting a meaning we have no evidence for. Those
    values stay in the Debug table, where they read as data rather than as a
    diagnosis.
    """
    cause, grade = decode_err((content or {}).get("err"))
    if cause and grade != UNKNOWN:
        return _capitalise(f"{label} - {cause}")
    return _capitalise(label)


def _capitalise(text: str) -> str:
    """Upper-case the first character, leaving the rest alone.

    `str.capitalize()` would lower-case the tail and turn "Manual cleaning" and
    the acronyms in an error cause into mush.
    """
    return text[:1].upper() + text[1:] if text else text
