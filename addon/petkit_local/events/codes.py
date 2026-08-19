"""The PetKit protocol knowledge base: every event code, topic and enum we know.

This module is DATA, not behaviour. `events/normalize.py` normalizes transports,
`events/decode.py` renders values for humans, and both read their meaning from
here. Keeping it separate is what lets the panel's Debug view show *why* a
label says what it says: the confidence grade and the firmware provenance are
fields, not comments.

Seven namespaces, deliberately never merged
-------------------------------------------
NS1 `HTTP_EVENT_CODES`  -- numeric `event_type` on `POST /dev_event_report`.
NS2 `CLOUD_RECORD_TYPES`-- the PetKit *cloud*'s `LitterRecord.subContent[]
                           .eventType`. QUARANTINED: it overlaps NS1 on 5/8/10
                           with DIFFERENT meanings. Recorded so the collision
                           is documented; never consulted when labelling a
                           device report.
NS3 `MQTT_EVENT_TOPICS` -- `/sys/{pk}/{dn}/thing/event/{name}/post` suffixes.
NS4 `RECORD_TYPES`      -- media classification strings in cloud records.
NS5 `WORK_MODES` etc.   -- `state.workState.workMode`, the CURRENT operation.
NS6 `FEED_SRC` etc.     -- feeder dispensing sub-fields.
NS7 `ERROR_FLAGS`       -- the named bits inside a state report's `err{}`
                           object. Per device family, like NS1.

Where the two evidence sources disagree
---------------------------------------
Two independent sources feed this table, and they are authoritative for
different things:

  * A **firmware RE pass** over the T5's `/app/bin/{cloud,ctrl}` binaries is
    authoritative for a code's MEANING and NAME. Its `a0`/`v0` values are
    internal dispatch ids, NOT wire codes -- note that a0=4 is shared by four
    different operations -- so a mismatch in content SHAPE does not undermine
    the naming.
  * A **wire capture** (268 events, real T5 id=10000001 firmware 943,
    2026-07-22..27) is authoritative for content SHAPE on this firmware.

Every `EventCode.grade` records which of those two backs the row, and
`EventCode.note` records the disagreement verbatim when there is one. Nothing
here is inferred from a reference project alone; `grade="unverified"` marks a
row we have never seen on the wire.

The corpus that grades these rows
---------------------------------
Episode composition across all 268 captured events, which is what pins the
mechanism codes to the operation they belong to::

    23 x ('3','8')                          action=2  -> deodorizing cycle
    21 x ('17','3','5')                     action=0  -> cleaning cycle
    23 x ('17',)                                      -> solo cycle step
    23 x ('10','9')                                   -> the visit itself
    31 x ('20',)   31 x ('24',)                       -> appeared + result
     8 x ('1','2')                                    -> error raised/cleared
     1 x ('13','17','3','4','5')            action=0
     1 x ('13','14','15','16','3','4','7')  action=9  -> maintenance session

`action` therefore carries NS5's work mode: the action=0 episode terminates in
code 5 (cleaning done), the action=9 episode in code 7 (reset done), and
action=2 occurs exactly 23 times against 23 toilet visits. Code 8 co-occurs
with action=2 in 23 of 23 episodes and never with any other mode, which is why
it is "Deodorizing" and not "Cleaning started".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from petkit_local.utils.const import (
    DEVICE_TYPES_ALL, DEVICE_TYPES_FEEDER, DEVICE_TYPES_LITTER,
    DEVICE_TYPES_PURIFIER, DEVICE_TYPES_WATER_FOUNTAIN,
)

# --- device families -------------------------------------------------------
# Codenames come from utils/const.py so this table and ha/categories.py
# can never disagree about which models exist.

ANY_DEVICE = frozenset(DEVICE_TYPES_ALL)
LITTER = frozenset(DEVICE_TYPES_LITTER)
FEEDER = frozenset(DEVICE_TYPES_FEEDER)
FOUNTAIN = frozenset(DEVICE_TYPES_WATER_FOUNTAIN)
PURIFIER = frozenset(DEVICE_TYPES_PURIFIER)

#: Ingenic-based litter boxes. The older T3/T4 report only the handful of
#: completion codes the cloud API also exposes; everything mechanism-level is
#: new with the T5 generation.
LITTER_NEXT_GEN = frozenset({"t5", "t6", "t7"})

#: Embedded-Linux fountains — today just the W7H (EverSweet Ultra AI).
#:
#: Sharing the `fountain` category with the Bluetooth EverSweets says almost
#: nothing about shared protocol. The W7H has two tanks, a tray, a lift valve,
#: two pumps, a heater, ten hall switches, a camera and an NPU; a W5 has a pump
#: and a filter. Every table below that came out of the W7H's own `ctrl` is
#: therefore keyed on THIS set and not on the category, because a W4 or W5 that
#: ever sent a colliding name would otherwise be answered in a vocabulary
#: describing hardware it does not have — which is the same failure as the
#: HTTP `event_type` codes being per-category rather than global, one level
#: further down.
FOUNTAIN_NEXT_GEN = frozenset({"w7h"})

#: Models carrying the N60 liquid deodorizer. PetKit sells the N60 for the
#: Purobot Max Pro and Max Pro 2, the Purobot Ultra and the Purobot Crystal Duo
#: — `t5`, `t6` and `t7` here, plus the plain "Max Pro" which maps to no
#: codename in this repo (see `utils/const.py`). This read `{"t5"}` and claimed
#: the T6/T7 do not have it, which is wrong and came from no source; it silently
#: withheld the deodorizer event codes from two thirds of the models that have
#: the hardware. Deliberately NOT written as `LITTER_NEXT_GEN` even though the
#: two sets coincide today: "has an N60" and "is the newer generation" are
#: different claims and only one of them is about a consumable.
#:
#: NOT having an N60 does not mean not spraying. A model without the built-in
#: unit can take an optional K3 (Pura Air) over BLE, which provides the same
#: deodorizing function — consistent with `k3LightSwitch` appearing in a real
#: T4 property post, the T4 having no N60. So do not narrow anything
#: spray-shaped to this set on the assumption that the rest cannot spray; this
#: set is about the built-in consumable only. A K3's own consumables arrive as
#: `battery`/`liquid` LEVELS on its parent's report (`bridge._update_linked_k3`),
#: not as the reset date the N60 countdown uses, so the two are not
#: interchangeable sources even where the function is the same.
LITTER_N60 = frozenset({"t5", "t6", "t7"})
#: T6 introduced the wander/melt/package cycles.
LITTER_T6_PLUS = frozenset({"t6", "t7"})
#: Feeders with a camera; the eat/move/pet detection topics are theirs alone.
FEEDER_CAMERA = frozenset({"d4h", "d4sh"})

# --- grades ----------------------------------------------------------------

#: Firmware RE and the wire capture agree, or one of them is decisive alone.
CONFIRMED = "confirmed"
#: Derived from content shape and episode composition; no firmware name.
INFERRED = "inferred"
#: Named by firmware RE but never observed on our wire. Rendered normally,
#: badged in the Debug view so an unproven payload shape is visible.
UNVERIFIED = "unverified"
#: The two sources disagree and the disagreement is not resolved. `note` says
#: how. The label follows the firmware (meaning); the shape follows capture.
CONFLICTED = "conflicted"

GRADES = frozenset({CONFIRMED, INFERRED, UNVERIFIED, CONFLICTED})

# --- event_kind buckets ----------------------------------------------------
# The values `classify_event_kind` may return. `web/panel.py` filters and
# `ha/publisher.py` fan-out both switch on these, so the set is a contract.

KIND_TOILET = "toilet_visit"
KIND_CLEANING = "cleaning"
KIND_PET = "pet"
KIND_MOTION = "motion"
KIND_ERROR = "error"
KIND_FEEDING = "feeding"
KIND_DRINKING = "drinking"
KIND_SYSTEM = "system"
KIND_OTHER = "other"

EVENT_KINDS = frozenset({
    KIND_TOILET, KIND_CLEANING, KIND_PET, KIND_MOTION, KIND_ERROR,
    KIND_FEEDING, KIND_DRINKING, KIND_SYSTEM, KIND_OTHER,
})

# --- roles -----------------------------------------------------------------
# What a code does within its episode. `group_sessions` uses `anchor`/`role`
# to decide which event heads a Timeline card.

ROLE_VISIT_SUMMARY = "visit_summary"
ROLE_START = "start"
ROLE_DONE = "done"
ROLE_STEP = "step"
ROLE_STOP = "stop"
ROLE_ERROR_START = "error_start"
ROLE_ERROR_OVER = "error_over"
ROLE_DETECTION = "detection"


@dataclass(frozen=True)
class EventCode:
    """One event type, in either the HTTP (NS1) or MQTT (NS3) namespace.

One row per code, carrying everything known about it — which is what
    makes completeness testable. Split across parallel membership sets instead,
    a code that belongs to none of them classifies as "other" and renders as
    "Event 13", and finding it means cross-checking every set by hand.

    Attributes:
        kind: The `event_kind` bucket, from `EVENT_KINDS`.
        label: Static human label, used when `content` decodes to nothing.
        grade: Evidence grade, from `GRADES`.
        detail: Collapse behind the Timeline's "show N more steps" expander.
        anchor: May head a Timeline card of its own.
        role: Position within its episode; see the `ROLE_*` constants.
        done_word: Noun for the rich completion label ("cleaning" ->
            "Auto cleaning completed"). Only meaningful when `role` is
            `ROLE_DONE`.
        mode_from: `content` key holding an NS5 work mode that qualifies the
            label, e.g. `action` -> "Odor removal - mechanism started".
        firmware: RE function name and dispatch id, verbatim, or "".
        families: Device codenames that emit this code.
        note: Caveat or source disagreement, rendered in the Debug view.
    """

    kind: str
    label: str
    grade: str = CONFIRMED
    detail: bool = False
    anchor: bool = False
    role: str = ""
    done_word: str = ""
    mode_from: str = ""
    firmware: str = ""
    families: frozenset[str] = field(default=ANY_DEVICE)
    note: str = ""
    #: For an event whose CONTENT cannot say which way it went. Holds
    #: `(state key, label when present, label when absent)`: the device state
    #: attached to the report is consulted instead. Code 17 is the case this
    #: exists for -- the switch-on and the switch-off are byte-identical
    #: payloads and only `state.lightState` tells them apart.
    state_label: tuple[str, str, str] | None = None


# --- NS1: HTTP dev_event_report numeric codes ------------------------------
# Keys are strings because the wire sends `event_type=10` inside a form body
# and `from_event_report` never coerces it -- the stored column is TEXT.
#
# THE CODES ARE PER DEVICE CATEGORY, NOT GLOBAL. This is the single most
# dangerous property of the namespace: code 2 is `err_over` on a litter box and
# `feed_over` on a D4H feeder (firmware RE of both binaries). A flat
# code -> meaning table would confidently label a feeder's completed meal as a
# cleared fault. Hence one table per category and a `lookup` that takes the
# device codename.

LITTER_HTTP_CODES: dict[str, EventCode] = {
    "1": EventCode(
        kind=KIND_ERROR, label="Error", anchor=True, role=ROLE_ERROR_START,
        firmware="err_start_event_report (a0=1)", families=LITTER_NEXT_GEN,
        note="Observed 8 times, always paired with a code 2 sharing its "
             "event_id. Content {err, msg, detail}; msg and detail were "
             "empty in every capture, so the cause comes from err alone.",
    ),
    "2": EventCode(
        kind=KIND_ERROR, label="Error cleared", role=ROLE_ERROR_OVER,
        firmware="err_over_event_report (a0=2)", families=LITTER_NEXT_GEN,
        note="Its start_time equals the episode id suffix, i.e. it points "
             "back at when the fault began.",
    ),
    "3": EventCode(
        kind=KIND_CLEANING, label="Mechanism started", detail=True,
        role=ROLE_START, mode_from="action",
        firmware="pk_event_pack_work_start", families=LITTER_NEXT_GEN,
        note="Opens a work cycle of whatever mode `action` names -- cleaning, "
             "odor removal or maintenance. It is NOT cleaning-specific: a flat "
             "'Cleaning (mechanism)' label misreads every other mode.",
    ),
    "4": EventCode(
        kind=KIND_CLEANING, label="Mechanism step", grade=CONFLICTED,
        detail=True, role=ROLE_STEP, mode_from="action",
        firmware="dump/reset/smooth/spray_over_event_report (a0=4)",
        families=LITTER_NEXT_GEN,
        note="A SHARED completion code, dispatched by an internal v0 sub-type: "
             "0x86 dump (dump_weight), 0x8d reset and 0x99 smooth "
             "(litter_weight), 0xaa/0xad spray (key), and on the T6 also 248 "
             "melt (content is only {start_time}) and 256 package "
             "({start_time, sn, secret, type, start_reason, result, err}). "
             "CONFLICT: both T5 captures carry none of those shapes but the "
             "stop-family {pos, reason, action, err}, so the sub-type set is "
             "incomplete. Labelled from `action` until a capture settles it.",
    ),
    "5": EventCode(
        kind=KIND_CLEANING, label="Cleaning done", role=ROLE_DONE,
        done_word="cleaning", firmware="pk_event_pack_clean_over",
        families=LITTER,
        note="Co-occurs with action=0 in all 22 captures. Carries the waste "
             "report (img, aesKey, clean_weight, litter_percent, box, "
             "ph_reason) that the RE's shorter shape omits.",
    ),
    "6": EventCode(
        kind=KIND_CLEANING, label="Litter emptied", grade=UNVERIFIED,
        role=ROLE_DONE, done_word="litter empty", families=LITTER,
        note="Never seen on our wire. Shape {start_time, over_time, "
             "start_reason, pos, current, result, err, components, "
             "dump_weight} per the reference.",
    ),
    "7": EventCode(
        kind=KIND_CLEANING, label="Reset done", role=ROLE_DONE,
        done_word="reset", families=LITTER,
        note="Seen once, terminating the action=9 maintenance session, and "
             "its shape matched the documented one exactly -- which is what "
             "upgraded this row from a low-confidence cross-namespace guess.",
    ),
    "8": EventCode(
        kind=KIND_CLEANING, label="Deodorizing", role=ROLE_DONE,
        done_word="deodorizing",
        firmware="pk_event_pack_spray_and_liquid_reset_over", families=LITTER,
        note="Co-occurs with action=2 (odor removal) in 23 of 23 episodes and "
             "never with any other mode, which rules out the tempting "
             "'Cleaning started' reading. The RE's `key` field is absent from "
             "all 23 captures.",
    ),
    "9": EventCode(
        kind=KIND_TOILET, label="Weight check", detail=True, role=ROLE_STEP,
        firmware="pk_event_pack_pet_in", families=LITTER_NEXT_GEN,
        note="Mid-visit sample; shares the visit's event_id with code 10.",
    ),
    "10": EventCode(
        kind=KIND_TOILET, label="Toilet visit", anchor=True,
        role=ROLE_VISIT_SUMMARY, firmware="pk_event_pack_pet_out",
        families=LITTER,
        note="The authoritative visit record: time_in/time_out, is_shit, "
             "shit_weight, pet_weight, count, area, score_info, img.",
    ),
    "11": EventCode(
        kind=KIND_CLEANING, label="Litter correction done", grade=UNVERIFIED,
        role=ROLE_DONE, done_word="litter correction",
        firmware="correct_over_event_report (a0=0xb)",
        families=LITTER_NEXT_GEN,
    ),
    "13": EventCode(
        kind=KIND_CLEANING, label="Mechanism step", grade=INFERRED,
        detail=True, role=ROLE_STEP, mode_from="action",
        families=LITTER_NEXT_GEN,
        note="Emitted by T5 firmware 943 (2 captures) although the RE lists "
             "12/13/19/22/23 as absent. Shape {pos, reason, action} puts it "
             "in the stop family beside 14/15/16; no firmware name exists, so "
             "none is invented.",
    ),
    "14": EventCode(
        kind=KIND_CLEANING, label="Motor stop requested", detail=True,
        role=ROLE_STOP, mode_from="action",
        firmware="stop_start_event_report (a0=0xe)", families=LITTER_NEXT_GEN,
    ),
    "15": EventCode(
        kind=KIND_CLEANING, label="Motor stop paused", detail=True,
        role=ROLE_STOP, mode_from="action",
        firmware="stop_suspend_event_report (a0=0xf)",
        families=LITTER_NEXT_GEN,
    ),
    "16": EventCode(
        kind=KIND_CLEANING, label="Motor stop resumed", detail=True,
        role=ROLE_STOP, mode_from="action",
        firmware="stop_continue_event_detect (a0=0x10)",
        families=LITTER_NEXT_GEN,
    ),
    "17": EventCode(
        kind=KIND_CLEANING, label="Light cycle", detail=True,
        role=ROLE_DONE, done_word="light cycle",
        state_label=("lightState", "light on", "light off"),
        firmware="light_over_event_report (a0=0x11)",
        families=LITTER_NEXT_GEN,
        note="The LED illuminator, literally. `ctrl` drives white and IR LEDs "
             "over PWM (pk_pwm_ctrl_whiteLight_*, pk_pwm_mode_force_irLight_*), "
             "raises this with an open reason (pk_set_event_light_open_reason, "
             "'set light on reason = %d'), tracks it beside cleaning "
             "(clean_light_sta with auto_clear_sta and lightAssist) and closes "
             "it on pk_toilet_over_judge_light_off. LBCommand.LIGHT is action "
             "7, matching NS5 workMode 7.\n\n"
             "It fires TWICE per use -- once as the light comes on and once as "
             "it goes off -- and the two payloads are IDENTICAL apart from "
             "start_time. What separates them is the attached device state: "
             "`lightState` is present with workProcess=1 while the light is "
             "on, and absent once it is off. Across 47 captures, 45 of 46 "
             "consecutive reports alternate on/off, giving 23 clean pairs, "
             "with a 115s automatic cycle. The manual pair recorded from the "
             "panel ran 14s because it was switched off by hand.\n\n"
             "The 3.8s median of `ts - start_time` is REPORT LATENCY, not the "
             "cycle length; its closeness to the firmware's 'need wait 3s' is "
             "a coincidence, not a reading of the cycle. "
             "`from_clear` is 1 on 44 of 47 and may be the `light_open_reason` "
             "the RE calls `key`, but it discriminates nothing we can check, "
             "so it stays a guess.",
    ),
    "18": EventCode(
        kind=KIND_CLEANING, label="Litter correction", grade=UNVERIFIED,
        detail=True, role=ROLE_STEP,
        firmware="sand_correct_event_report (a0=0x12)",
        families=LITTER_NEXT_GEN,
    ),
    "20": EventCode(
        kind=KIND_PET, label="Appeared", anchor=True,
        firmware="pk_event_pack_pet_and_wander", families=LITTER_NEXT_GEN,
        note="A pet was seen and recorded but never used the box: petEvent=1 "
             "with toiletEvent=0 and no cleaning follows. The app counts it "
             "under 'Pet', NEVER under 'Toileting'. The T6's wander events "
             "(pk_event_pack_wander_event_msg / wander_end) share this code "
             "through a thin wrapper with flag=0, so a wander arrives here "
             "rather than under a code of its own.",
    ),
    "21": EventCode(
        kind=KIND_CLEANING, label="Deodorant reset done", grade=UNVERIFIED,
        role=ROLE_DONE, done_word="deodorant reset",
        firmware="pk_event_pack_liquid_reset_over (v0=0x15)",
        families=LITTER_N60,
        note="N60 liquid deodorizer. Not T5-only: see LITTER_N60.",
    ),
    "24": EventCode(
        kind=KIND_PET, label="Detection result", detail=True,
        role=ROLE_DETECTION,
        firmware="package_pet_discern_event_report (v0=0x18)",
        families=LITTER_NEXT_GEN,
        note="Closes a code 20 episode, linked by content.related_event. Its "
             "score_info is usually EMPTY -- 31 of 33 captures -- while code 10 "
             "carries an id/score pair in 25 of 27, so the pet identity "
             "normally arrives on the visit summary rather than here. Not "
             "never, though: 2 of the 33 did carry one, and both were the "
             "largest detections in the set. Content is {related_event, count, "
             "area, score_info}, and the device emits it as INVALID JSON (the "
             "related_event value is an unquoted bare token) -- see "
             "ingest._repair_bare_values.",
    ),
    "51": EventCode(
        kind=KIND_SYSTEM, label="BLE relay started", detail=True,
        families=LITTER_NEXT_GEN,
        note="Confirmed on a T5 (fw 943, HTTP capture). Content "
             "{start_time, start_reason, action, device: {mac, type}}. "
             "The MQTT equivalent is ble_relay_start/post.",
    ),
    "53": EventCode(
        kind=KIND_SYSTEM, label="BLE relay data", detail=True,
        families=LITTER_NEXT_GEN,
        note="Confirmed on a T5 (fw 943, HTTP capture). Content "
             "{payload: [{cmd, data}], device: {type, mac}}. "
             "The MQTT equivalent is ble_response/post.",
    ),
}

#: Codes absent from both the firmware RE and our wire. Left unmapped on
#: purpose so `classify_event_kind` logs them once if a T6/T7 ever sends one,
#: rather than silently bucketing them into a family they may not belong to.
UNKNOWN_HTTP_CODES = frozenset({"12", "19", "22", "23"})


#: Feeder HTTP codes. Code 2 came from the D4H firmware RE; codes 3 and 4
#: were confirmed on a live D4SH (fw 248, HTTP). The D4H's `ctrl` has no
#: `*_event_report` wrappers except `pk_event_report` itself, so further codes
#: are set inside a `pk_event_pack_*` body behind a dispatch that the
#: disassembly does not resolve to a constant.
#:
#: Known pack functions without a recovered code: eat_start, eat_over,
#: pet_event, err_start, err_over, relay_start, relay_over, relay_response.
FEEDER_HTTP_CODES: dict[str, EventCode] = {
    "2": EventCode(
        kind=KIND_FEEDING, label="Feeding done", anchor=True, role=ROLE_DONE,
        done_word="feeding", firmware="pk_event_pack_feed_over_event_msg (a0=2)",
        families=FEEDER,
        note="The collision that proves these codes are per-category: 2 is "
             "`feed_over` here and `err_over` on a litter box. Content is "
             "{id, day, manual, time, online_state, eat_video, state}.",
    ),
    "3": EventCode(
        kind=KIND_FEEDING, label="Feeding started", detail=True,
        role=ROLE_START, families=FEEDER,
        note="Confirmed on a live D4SH (fw 248, HTTP). Content "
             "{id, day, manual, time, online_state} matches MQTT "
             "feed_start/post from the same model (fw 867). "
             "state.feeding is 1 or 2 while the motor runs.",
    ),
    "4": EventCode(
        kind=KIND_FEEDING, label="Feeding done", anchor=True,
        role=ROLE_DONE, done_word="feeding", families=FEEDER,
        note="Confirmed on a live D4SH (fw 248, HTTP). Content "
             "{mark, start_time, id, day, manual, time, real_amount1, "
             "real_amount2, completed_at, result, err_code, media} "
             "matches MQTT feed_over/post. Coexists with code 2 "
             "(same kind, simpler shape, from the D4H firmware RE); "
             "which code a given model sends is not yet settled.",
    ),
    "5": EventCode(
        kind=KIND_FEEDING, label="Eating started", detail=True,
        role=ROLE_START, grade=INFERRED, families=FEEDER_CAMERA,
        note="Snapshot event on a live D4SH (fw 248, HTTP), content "
             "{img, aesKey, mark, start_time} — same shape as codes 7 and 8. "
             "Read as `eat_start` by the device's OWNER from the attached "
             "recordings (proxy was off, so not from the app's own labels): "
             "the clip shows eating beginning. Seen once, ~9 s before a "
             "feed_over (code 4) at 18:21. The matching firmware pack function "
             "is pk_event_pack_eat_start_event_msg, but its HTTP event_type "
             "is not literal in the binary, so the number-to-name link is the "
             "owner's, not the RE's. NOTE codes are PER CATEGORY: 5 is "
             "`drink_start` on a fountain and something else again on a litter "
             "box.",
    ),
    "7": EventCode(
        kind=KIND_MOTION, label="Motion detected", anchor=True,
        families=FEEDER_CAMERA,
        note="Confirmed on a live D4SH (fw 248, HTTP). Content "
             "{img, aesKey, mark, start_time} — a snapshot event, "
             "same shape as litter code 20 without the detection flags. "
             "Six occurrences in quick succession (55-190s apart) point to "
             "motion detection rather than pet identification. "
             "The firmware has both pk_event_pack_move_event_msg and "
             "pk_event_pack_pet_event_msg; which one produces this code "
             "is not resolved.",
    ),
    "8": EventCode(
        kind=KIND_PET, label="Pet detected", anchor=True, grade=INFERRED,
        families=FEEDER_CAMERA,
        note="Snapshot event on a live D4SH (fw 248, HTTP), content "
             "{img, aesKey, mark, start_time} — same shape as codes 5 and 7. "
             "Read as `pet` (pet appeared) by the device's OWNER from the "
             "attached recordings (proxy was off, not the app's labels): the "
             "clip shows the cat. Seen both around feeds (18:21) and standing "
             "alone well after them (18:29, 21:06), which fits pet appearance "
             "rather than the feed sequence. Firmware sibling is "
             "pk_event_pack_pet_event_msg; the HTTP event_type is not literal "
             "in the binary, so the number-to-name link is the owner's, not "
             "the RE's. Code 8 is `err_over`-adjacent on other categories — "
             "these numbers are per-category.",
    ),
}

#: Fountain HTTP codes. Sparse until more families have HTTP captures: the W7H
#: is the only embedded-Linux fountain, and a Bluetooth EverSweet has no HTTP
#: path at all, so the codes are keyed on `FOUNTAIN_NEXT_GEN`.
FOUNTAIN_HTTP_CODES: dict[str, EventCode] = {
    "5": EventCode(
        kind=KIND_DRINKING, label="Drinking started", detail=True,
        role=ROLE_START, families=FOUNTAIN_NEXT_GEN,
        note="Confirmed on a W7H (fw 456, HTTP capture). Content "
             "{event_start} matches MQTT drink_start/post shape.",
    ),
    "6": EventCode(
        kind=KIND_DRINKING, label="Drinking done", anchor=True,
        role=ROLE_DONE, done_word="drinking", families=FOUNTAIN_NEXT_GEN,
        note="Confirmed on a W7H (fw 456, HTTP capture). Content "
             "{img, aesKey, mark, start_time, event_start, event_end, "
             "drink_time, media, upload, drinkDetection, pet_id, "
             "tracker_info, vomit_info} matches MQTT drink_over/post.",
    ),
    "20": EventCode(
        kind=KIND_PET, label="Appeared", anchor=True,
        families=FOUNTAIN_NEXT_GEN,
        note="Same role as litter code 20 but a DIFFERENT category table. "
             "Confirmed on a W7H (fw 456, HTTP capture). Content "
             "{img, aesKey, mark, start_time, upload, media, petDetection}.",
    ),
    "24": EventCode(
        kind=KIND_PET, label="Detection result", detail=True,
        role=ROLE_DETECTION, families=FOUNTAIN_NEXT_GEN,
        note="Same role as litter code 24. Confirmed on a W7H (fw 456). "
             "Content {related_event, count, area, pet_id, tracker_info, "
             "vomit_info}. Emitted as INVALID JSON (bare related_event "
             "token) -- same quirk as litter code 24.",
    ),
}

#: Category name -> its HTTP code table. Categories with no recovered codes map
#: to an empty table rather than being absent, so `lookup` never has to guess
#: which family a codename belongs to.
HTTP_CODES_BY_CATEGORY: dict[str, dict[str, EventCode]] = {
    "litter": LITTER_HTTP_CODES,
    "feeder": FEEDER_HTTP_CODES,
    "fountain": FOUNTAIN_HTTP_CODES,
    "purifier": {},
}

#: The category assumed when a caller has no device to hand. Litter is the only
#: family we have ever captured on the HTTP path, and it is what every stored
#: event in the reference corpus came from.
DEFAULT_HTTP_CATEGORY = "litter"

_CATEGORY_DEVICE_TYPES: tuple[tuple[str, frozenset[str]], ...] = (
    ("litter", LITTER),
    ("feeder", FEEDER),
    ("fountain", FOUNTAIN),
    ("purifier", PURIFIER),
)


def category_of(device_type: str | None) -> str | None:
    """Behavioural category for a device codename, or None if unrecognised.

    Mirrors `ha/categories.py::spec_for_device` without importing it --
    that module pulls in the whole HA entity tree, and this one must stay a
    leaf so anything can read the protocol tables.
    """
    if not device_type:
        return None
    codename = device_type.lower()
    for name, members in _CATEGORY_DEVICE_TYPES:
        if codename in members:
            return name
    return None


# --- NS2: cloud record eventType (QUARANTINED) -----------------------------
# The PetKit cloud's LitterRecord.subContent[].eventType. Recorded ONLY so the
# collision with NS1 is documented. Nothing reads this when labelling a device
# report, and nothing should: NS2's 8 is deodorization while NS1's 8 is also
# deodorization but NS2's 5/10 do not line up with ours. Merging the two
# namespaces is a documented invariant violation.

CLOUD_RECORD_TYPES: dict[int, str] = {
    5: "Cleaning event",
    6: "Litter empty / dump",
    7: "Reset",
    8: "Deodorization / spray",
    10: "Pet used box",
}


# --- NS3: MQTT thing/event/{name}/post topics ------------------------------

MQTT_EVENT_TOPICS: dict[str, EventCode] = {
    # -- litter: work cycle
    # A real W7H sends this too (live log, 2026-08-01), so the fountain is in
    # `families` — the set says who EMITS a code, and leaving it litter-only
    # while a fountain sends it is the sort of gap that makes a table look
    # complete when it is not.
    #
    # `mode_from="action"` was annotated here as not resolving on a fountain.
    # It resolves: the W7H's `work_start` content is
    # `{"pos","reason","item_id","action"}`, so every fountain job was being
    # named out of the litter enum. The fix is `work_modes_for`, not dropping
    # the qualifier — the action IS the job and is the most useful thing on the
    # card.
    "work_start": EventCode(
        kind=KIND_CLEANING, label="Work started", role=ROLE_START,
        mode_from="action", families=LITTER | FOUNTAIN_NEXT_GEN),
    "work_suspend": EventCode(
        kind=KIND_CLEANING, label="Work paused", detail=True, role=ROLE_STOP,
        families=LITTER),
    "work_continue": EventCode(
        kind=KIND_CLEANING, label="Work resumed", detail=True, role=ROLE_STOP,
        families=LITTER),
    "clean_over": EventCode(
        kind=KIND_CLEANING, label="Cleaning done", role=ROLE_DONE,
        done_word="cleaning", families=LITTER),
    "dump_over": EventCode(
        kind=KIND_CLEANING, label="Dumping done", role=ROLE_DONE,
        done_word="litter empty", families=LITTER),
    "reset_over": EventCode(
        kind=KIND_CLEANING, label="Reset done", role=ROLE_DONE,
        done_word="reset", families=LITTER),
    "smooth_over": EventCode(
        kind=KIND_CLEANING, label="Leveling done", grade=CONFIRMED,
        role=ROLE_DONE, done_word="leveling", families=LITTER_NEXT_GEN),
    "correct_over": EventCode(
        kind=KIND_CLEANING, label="Litter correction done", grade=UNVERIFIED,
        role=ROLE_DONE, done_word="litter correction", families=LITTER_NEXT_GEN),
    "sand_correct_over": EventCode(
        kind=KIND_CLEANING, label="Litter level corrected", grade=UNVERIFIED,
        role=ROLE_DONE, done_word="litter correction", families=LITTER_NEXT_GEN),
    "light_over": EventCode(
        kind=KIND_CLEANING, label="Light cycle done", grade=CONFIRMED,
        detail=True, role=ROLE_DONE, done_word="light cycle",
        state_label=("lightState", "light on", "light off"),
        families=LITTER_NEXT_GEN,
        note="The MQTT twin of code 17, and it behaves the same way: two "
             "reports per use whose payloads are identical apart from "
             "start_time, told apart only by the attached device state. "
             "Confirmed on a live T5 (2026-07-29): the report inside a "
             "cleaning episode carried lightState={workReason:0,"
             "workProcess:1} and the one 114s later carried none -- the "
             "on/off pattern and the ~115s automatic cycle that code 17's "
             "note records. The second report is a SEPARATE episode with its "
             "own event_id and no link back, so it heads its own card."),
    "spray_over": EventCode(
        kind=KIND_CLEANING, label="Deodorizing done", grade=CONFIRMED,
        role=ROLE_DONE, done_word="deodorizing", families=LITTER_N60,
        note="N60 liquid deodorizer. Present on every model that takes an N60 "
             "refill, not the T5 alone -- see LITTER_N60."),
    "liquid_reset_over": EventCode(
        kind=KIND_CLEANING, label="Deodorant reset done", grade=CONFIRMED,
        role=ROLE_DONE, done_word="deodorant reset", families=LITTER_N60,
        firmware="pk_event_pack_liquid_reset_over_event_msg",
        note="Observed end to end on a T5: an N60 reset from PetKit's app "
             "arrives as thing.service.start {\"start_action\":10}, the box "
             "answers work_start (content action:10, reason:2) and then this, "
             "~1s later, content {start_time, over_time, start_reason, pos, "
             "current, result, err, components, litter_weight}. It moves "
             "`sprayResetTime` to the moment of the reset -- the event is named "
             "for the liquid while the field it moves is named for the spray, "
             "so do not read the names as identifying different consumables. "
             "Deliberately NOT in ingest._CLEAN_DONE_WORDS: a consumable reset "
             "is not a clean and must not date Last Clean."),
    "melt_over": EventCode(
        kind=KIND_CLEANING, label="Melt cycle done", grade=CONFIRMED,
        role=ROLE_DONE, done_word="melt cycle", families=LITTER_T6_PLUS),
    "package_over": EventCode(
        kind=KIND_CLEANING, label="Bagging done", grade=CONFIRMED,
        role=ROLE_DONE, done_word="bagging", families=LITTER_T6_PLUS),
    # -- litter: pet
    "pet_in": EventCode(
        kind=KIND_TOILET, label="Pet entered", detail=True, role=ROLE_START,
        families=LITTER),
    "pet_out": EventCode(
        kind=KIND_TOILET, label="Pet left", anchor=True,
        role=ROLE_VISIT_SUMMARY, families=LITTER),
    # No longer unverified: a real W7H sent four of these (2026-07-31), each
    # with `content.related_event` pointing back at its `pet_detect` and the
    # `count`/`area`/`pet_id`/`tracker_info`/`vomit_info` shape this row
    # assumed. The fountain was missing from `families`, so every one of them
    # would have classified as "other" on the model that actually sends them.
    "pet_discern": EventCode(
        kind=KIND_PET, label="Detection result", detail=True,
        role=ROLE_DETECTION, families=LITTER_NEXT_GEN | FOUNTAIN_NEXT_GEN),
    "pet_wander": EventCode(
        kind=KIND_PET, label="Pet nearby", grade=UNVERIFIED, anchor=True,
        families=LITTER_T6_PLUS),
    "wander_over": EventCode(
        kind=KIND_PET, label="Pet left the area", grade=UNVERIFIED,
        detail=True, families=LITTER_T6_PLUS),
    # -- motor stop (litter)
    "stop_start": EventCode(
        kind=KIND_CLEANING, label="Motor stop requested", grade=CONFIRMED,
        detail=True, role=ROLE_STOP, families=LITTER_NEXT_GEN),
    "stop_suspend": EventCode(
        kind=KIND_CLEANING, label="Motor stop paused", grade=CONFIRMED,
        detail=True, role=ROLE_STOP, families=LITTER_NEXT_GEN),
    "stop_continue": EventCode(
        kind=KIND_CLEANING, label="Motor stop resumed", grade=CONFIRMED,
        detail=True, role=ROLE_STOP, families=LITTER_NEXT_GEN),
    # -- feeder
    "feed_start": EventCode(
        kind=KIND_FEEDING, label="Feeding started", detail=True,
        role=ROLE_START, families=FEEDER),
    "feed_over": EventCode(
        kind=KIND_FEEDING, label="Feeding done", anchor=True, role=ROLE_DONE,
        done_word="feeding", families=FEEDER),
    "feed_stop": EventCode(
        kind=KIND_FEEDING, label="Feeding cancelled", role=ROLE_STOP,
        families=FEEDER),
    "eat_start": EventCode(
        kind=KIND_FEEDING, label="Eating started", detail=True,
        role=ROLE_START, families=FEEDER_CAMERA),
    "eat_over": EventCode(
        kind=KIND_FEEDING, label="Eating done", anchor=True, role=ROLE_DONE,
        done_word="eating", families=FEEDER_CAMERA),
    # -- fountain
    # CONFIRMED on a real W7H (capture 2026-07-31): two `drink_start` frames,
    # each carrying `content.event_start` and the standard state snapshot. The
    # matching `drink_over` has still never been seen, so it keeps its grade —
    # the pair is not evidence for itself.
    "drink_start": EventCode(
        kind=KIND_DRINKING, label="Drinking started", detail=True,
        role=ROLE_START, families=FOUNTAIN_NEXT_GEN),
    # CONFIRMED on a real W7H (live log, 2026-08-01), where it arrived one
    # second after a `drink_start`. The meaning is the device's own vocabulary
    # rather than a guess: the reverse-engineered field map names
    # `addWaterState` "add-water (refill) procedure active", alongside the
    # `addWaterSwitch` / `addWaterMode` / `addWaterFrequent` settings this same
    # fountain accepts. Without a row it rendered as "add_water_over (other)".
    #
    # Graded on the SHAPE being seen and the name being sourced. Whether it also
    # reports a result or a volume in `content` is not known — no capture of the
    # payload itself, only of the topic.
    "add_water_over": EventCode(
        kind=KIND_CLEANING, label="Refill done", grade=INFERRED, anchor=True,
        role=ROLE_DONE, done_word="refilling", families=FOUNTAIN_NEXT_GEN),
    "drink_over": EventCode(
        kind=KIND_DRINKING, label="Drinking done", grade=CONFIRMED,
        anchor=True, role=ROLE_DONE, done_word="drinking",
        families=FOUNTAIN_NEXT_GEN,
        note="188 in one W7H capture (2026-08-11), with content "
             "{event_start, img, aesKey, upload, media, mark}. The note that "
             "this had never been seen is gone with them."),
    # The two water-treatment jobs, from the same capture: 3 and 2 frames,
    # content `{img, aesKey, mark, upload, media, event_start, event_end}` --
    # the `add_water_over` shape, which is what they are siblings of.
    "flush_over": EventCode(
        kind=KIND_CLEANING, label="Flush done", anchor=True, role=ROLE_DONE,
        done_word="flushing", families=FOUNTAIN_NEXT_GEN),
    "water_change_over": EventCode(
        kind=KIND_CLEANING, label="Water change done", anchor=True,
        role=ROLE_DONE, done_word="changing the water",
        families=FOUNTAIN_NEXT_GEN),
    # -- shared detection
    "move_detect": EventCode(
        kind=KIND_MOTION, label="Motion detected", anchor=True,
        families=FEEDER_CAMERA | LITTER_NEXT_GEN),
    "pet_detect": EventCode(
        kind=KIND_PET, label="Pet detected", anchor=True,
        families=FEEDER_CAMERA | LITTER_NEXT_GEN | FOUNTAIN_NEXT_GEN),
    "cvr_event": EventCode(
        kind=KIND_MOTION, label="Recording event", grade=UNVERIFIED,
        anchor=True, families=LITTER_NEXT_GEN | FEEDER_CAMERA),
    "appeared": EventCode(
        kind=KIND_PET, label="Appeared", grade=UNVERIFIED, anchor=True,
        families=LITTER_NEXT_GEN),
    # -- errors
    "error_start": EventCode(
        kind=KIND_ERROR, label="Error", anchor=True, role=ROLE_ERROR_START),
    "error_over": EventCode(
        kind=KIND_ERROR, label="Error cleared", role=ROLE_ERROR_OVER),
    # -- transport chatter. These are handled by mqtt/bridge.py for their side
    # effects and never persisted as events, but they are listed so a reader
    # of this table sees the complete topic set rather than an edited one.
    "property": EventCode(
        kind=KIND_SYSTEM, label="State update", detail=True),
    "property_post": EventCode(
        kind=KIND_SYSTEM, label="State update", detail=True,
        families=FEEDER_CAMERA),
    "data_get": EventCode(
        kind=KIND_SYSTEM, label="Config requested", detail=True),
    "ble_response": EventCode(
        kind=KIND_SYSTEM, label="BLE response", detail=True),
    "ble_relay_start": EventCode(
        kind=KIND_SYSTEM, label="BLE relay started", grade=CONFIRMED,
        detail=True),
    "ble_relay_over": EventCode(
        kind=KIND_SYSTEM, label="BLE relay finished", grade=CONFIRMED,
        detail=True),
}

#: Topics the bridge handles purely for their side effects; they never become
#: timeline rows. `mqtt/bridge.py` owns this exclusion, listed here so the two
#: stay in sync.
MQTT_TRANSPORT_TOPICS = frozenset({"property", "property_post", "data_get",
                                   "ble_response", "ble_relay_start",
                                   "ble_relay_over"})


# --- NS4: RecordType media classification ----------------------------------
# Strings the cloud record API uses to classify recorded media. Distinct from
# our moduleType -> category mapping in events/ingest.py: this namespace comes
# from the app side and is what a future cloud-record importer would speak.

RECORD_TYPES: dict[str, str] = {
    "eat": "Eating",
    "feed": "Feeding",
    "move": "Motion",
    "pet": "Pet",
    "toileting": "Toileting",
    "waste_check": "Waste check",
    "dish_before": "Bowl before",
    "dish_after": "Bowl after",
    "drink_over": "Drinking",
    "pet_detect": "Pet detected",
}


# --- NS5: workMode / workProcess / safeWarn --------------------------------
# state.workState.* -- the CURRENT operation, not a historical event. Also the
# table the HTTP codes' `action` field decodes against (see module docstring).

WORK_MODES: dict[int, str] = {
    0: "cleaning",
    1: "dumping",
    2: "odor removal",
    3: "resetting",
    4: "leveling",
    5: "calibrating",
    6: "reset deodorant",
    7: "light cycle",
    8: "reset max deodorant",
    9: "maintenance",
}

#: Only these three have been observed in `action`: 0 (24x), 2 (23x), 9 (6x).
#: The rest of the enum comes from the state-side reference and is rendered
#: but graded lower by `events/decode.py`.
WORK_MODES_OBSERVED = frozenset({0, 2, 9})

#: What a litter box's `thing.service.start {"start_action": N}` asks it to do,
#: as the APP's own buttons — which is not the same question `WORK_MODES` above
#: answers. That table decodes a mode the box reports; this one records what
#: was seen going the other way.
#:
#: Every entry is a single isolated tap in PetKit's app on a T6, captured and
#: mapped 2026-08-09. Light and Level were each pressed several times to check
#: they were not toggling through a sequence.
#:
#: Read alongside `WORK_MODES`, four values agree outright (0, 1, 3, 4) and one
#: more agrees with pypetkitapi's LBCommand (7 LIGHT). **8 does not.** LBCommand
#: calls it RESET_N50_DEODOR; the tap that produced it was Pack, the
#: waste-packing cycle. The disagreement is not resolvable from here and it is
#: not cosmetic — one reading is a bookkeeping no-op and the other moves the
#: mechanism — so the row is graded `conflicted` and `ha/entities/buttons.py`
#: publishes the code under the name the tap gives it, on that model only.
#:
#: NOT a whitelist, unlike `FOUNTAIN_W7H_START_ACTIONS`: that set is the literal
#: `cmp` chain out of the firmware, so a value outside it is known to be
#: ignored. This is a list of taps somebody made. 2, 9 and 10 are absent here
#: and are all real (deodorize, maintenance, N60 reset — 10 confirmed against
#: the real cloud on a T5), because the T6 action sheet does not offer them.
#: Nothing may validate against this table.
LITTER_START_ACTIONS: dict[int, tuple[str, str]] = {
    0: ("scoop", CONFIRMED),
    1: ("empty", CONFIRMED),
    3: ("reset", CONFIRMED),
    4: ("level", CONFIRMED),
    7: ("light", CONFIRMED),
    8: ("pack waste", CONFLICTED),
    11: ("open sealed door", CONFIRMED),
}

#: The litter a box is told it is filled with (`settings.sandType`).
#:
#: The whole enum, from a controlled 1 -> 2 -> 3 -> 1 run through the app's own
#: picker. There is no 0: it was seeded as a default here for a long time and
#: served back to devices, which is a value outside their vocabulary — see
#: `devices/defaults.py::default_settings`.
SAND_TYPES: dict[int, str] = {
    1: "clay/ore",
    2: "tofu",
    3: "mixed",
}

#: What a `schedule[]` entry schedules.
#:
#: One array carries BOTH of a litter box's timed jobs and `type` is the only
#: thing telling them apart. Captured on our own T5 through proxy mode
#: (2026-08-09): the array held four `type: 0` entries, and adding a periodic
#: DEODORIZING time appended a fifth with `type: 1` — same array, same write.
#:
#: Worth writing down precisely because every source up to now, ours included,
#: recorded `type` as "observed as 0; semantics unknown". Anything that edits
#: this array must filter by `type` and merge back, or it silently deletes the
#: other job's schedule.
SCHEDULE_TYPES: dict[int, str] = {
    0: "cleaning",
    1: "deodorizing",
}

# Time units in the settings block, because they are not one unit and the field
# names do not tell you which is which:
#
#   * MINUTES since midnight -- every `*MultiRange` (`lightMultiRange`,
#     `toneMultiRange`, `distrubMultiRange`, `awDisturbMultiRange`,
#     `wlDisturbMultiRange`, `cameraMultiRange[].time`) and `schedule[].time`.
#     A range whose end is below its start crosses midnight and is valid; a
#     one-minute range ([0, 1]) is valid too, so nothing may "tidy" these.
#   * SECONDS since midnight -- `flushTime` and `waterChangeTime` on a W7H.
#     The same settings block, the other unit.
#   * SECONDS as a duration -- `stillTime` (120 is the app's "2 min") and
#     `autoIntervalMin`, WHOSE NAME SAYS MINUTES AND MEANS SECONDS: PetKit's
#     app writes 300 for the UI's 5-minute minimum and 7200 for its 2-hour
#     maximum. See `ha/entities/selects.py`, where reading the name cost every
#     litter box a repeat-cleaning interval 60x shorter than the one chosen.
#
# All of them are LOCAL time; a T5 entry set at 14:50 in UTC+2 came out as 890.

#: A feeder's scheduled meals, which are a shape of their own.
#:
#: `dev_feed_get` answers with `{schedule: [...], nextTick, latest}`, where each
#: `schedule[]` entry is a GROUP of meals sharing weekdays:
#:
#:     {"re": "1,2,3,4,5,6,7", "it": [{"id": .., "t": .., "a1": .., "a2": ..}],
#:      "itemJsonString": "<the same it, as a string>"}
#:
#: Read out of a D4SH 867 `ctrl`, `pk_schmg_parse_schedule` in
#: `schedule_ctrl.c`: it looks up `result`, `schedule`, `latest`, then per group
#: `re` and `it`, then per item `id`, `t`, `a1` and `a2`. Its own log line names
#: what those become — `schdl_item[%d]:id = %7s,amount_l=%d,amount_r=%d,
#: time=%d,repeats=%d` — so `a1`/`a2` are the per-hopper portions, the same pair
#: a `feed_realtime` carries on this model, and `re` is the group's weekdays.
#:
#: The single-hopper D4H/D4H2 carries `a` instead — one amount, no per-hopper
#: split — exactly as it reads `amount` where the Dual-Hopper reads
#: `amount1`/`amount2` from a `feed_realtime` (`utils/const.py`,
#: `ha/commands.py::_feed`). Confirmed on a D4H2 (867): a cloud-made schedule
#: with `a` in both `it[]` and `latest[]` dispensed, while `a1`/`a2` ran the
#: cycle and put out nothing — `real_amount: 0, err_code: 8` (FEED_RESULT[8],
#: "failed (nothing dispensed)"). So the amount key is
#: PER MODEL and never both: `a` on a single hopper, `a1`/`a2` on a dual. The
#: two never convert — `a` is the device's own unit (÷ a config constant, like
#: realtime `amount`), not the portions a hopper counts.
#:
#: `it` arrived POPULATED on 2026-08-12 — 21 proxied `dev_feed_get` responses
#: from the real cloud to a D4SH with meals set. They settle what the earlier
#: firmware read could not:
#:
#:   * The UNIT OF `t` IS SECONDS since local midnight — `n_46560` fired at
#:     12:56:00 — NOT the minutes every other schedule time on these devices
#:     counts (the litter box's `schedule[].time`, confirmed on our T5, is
#:     minutes). The id is `n_<that same seconds value>`.
#:   * `latest[]` holds the concrete feeds firing TODAY OR TOMORROW, id
#:     `s_YYYYMMDD_<secs>` (an instance of a recurring meal) or `d_...` (a
#:     one-off), with `t` a countdown in seconds from now, floored, recomputed
#:     every poll. `nextTick` is the countdown of the last `latest` entry, and
#:     the constant 86340 when `latest` is empty.
#:   * `itemJsonString` is NOT read by this parser. It is written alongside
#:     `it` anyway, because that is what the real cloud sends (keys in
#:     alphabetical order) and there is no reason to hand the device a payload
#:     a shape narrower than the one it knows.
FEED_SCHEDULE_ITEM_KEYS = ("id", "t", "a", "a1", "a2")

#: The weekday numbering PetKit uses everywhere a schedule names days:
#: `schedule[].repeats`, `cameraMultiRange[].rpt` and their siblings.
#:
#: SUNDAY IS 1. Confirmed twice over: a controlled CRUD run on a T6 (01:11
#: Sunday -> `"1"`, 02:22 Monday -> `"2"`, 03:33 Saturday -> `"7"`), and on our
#: own T5, where a deodorizing entry set for every day EXCEPT Monday came out as
#: `"1,3,4,5,6,7"`. Every schedule this add-on serves by default repeats on all
#: seven days, so the convention has never mattered and has never been checked —
#: which is exactly how it would have been got wrong the first time a UI let
#: somebody pick one day.
WEEKDAY_NAMES: dict[int, str] = {
    1: "Sunday",
    2: "Monday",
    3: "Tuesday",
    4: "Wednesday",
    5: "Thursday",
    6: "Friday",
    7: "Saturday",
}

#: A FOUNTAIN's `action` is a different enum in the same field.
#:
#: This table exists because the one above was being applied to it. A W7H
#: `work_start` carries `action` (its content is
#: `{"pos","reason","item_id","action"}`), so `mode_from="action"` resolved,
#: and a refill rendered as "Odor removal - work started" — litter-box
#: vocabulary on a device that has no litter.
#:
#: Read out of W7H 456 `ctrl`. `work_start_event_detect` branches on the action
#: byte and each branch names itself, both in its log line and in the still it
#: grabs: `cmp #1` -> "get start event ---> flush water", `/tmp/fPro_flushStart.jpeg`;
#: `cmp #5` -> `/tmp/fPro_changeStart.jpeg`; everything else falls through to
#: "get start event ---> add water", `/tmp/fPro_addStart.jpeg`.
#:
#: So 1 and 5 are named by the firmware. 2 is not: it is accepted by the
#: command whitelist below but reaches the event detector's default branch, so
#: "refill" is the app's meaning attached to a value the firmware only lets
#: through. Graded accordingly — it is the one entry here that could be wrong.
FOUNTAIN_W7H_WORK_MODES: dict[int, str] = {
    1: "flush",
    2: "refill",
    5: "water change",
}

#: Every `start_action` a W7H accepts, and nothing else.
#:
#: `_pki_deal_app_ctrl_start_evt` (W7H 456 `ctrl`) opens with an explicit
#: whitelist — a `cmp`/`beq`/`bgt` chain over exactly these twenty values. A
#: value outside it returns without touching the work queue: no reply, no
#: error, no log. That is worth writing down precisely because it is
#: unobservable from here; a command with a wrong action looks identical to a
#: device that never got it.
#:
#: Only `start` is filtered this way. `stop_action`, `end_action` and
#: `continue_action` are forwarded whatever they carry (the one exception being
#: `continue_action == 103`, which the firmware rewrites into an end), and
#: `power_action` takes only 0 and 1 down a path of its own.
FOUNTAIN_W7H_START_ACTIONS = frozenset({
    1, 2, 3, 4, 5, 7, 9, 10, 11, 12, 15, 16, 32, 100, 101, 102, 103, 104, 105, 106,
})

#: The two of those twenty that are not work at all.
#:
#: 32 is the camera: its branch calls the camera in/out handler and logs
#: "camera in/out runing". 7 leaves `pk_hmi_set_ctrl_evt_2_app` by a different
#: exit than every other value (`cmp r4,#7`), onto a different message queue.
#: Neither belongs behind a button labelled with a cleaning cycle.
FOUNTAIN_W7H_START_ACTIONS_NOT_WORK = frozenset({7, 32})


def work_modes_for(device_type: str | None = None) -> dict[int, str]:
    """The `action`/`workMode` enum for this device model.

    Per family for the same reason NS1's numeric codes are per category: the
    field name is shared, the vocabulary is not. Keyed on `FOUNTAIN_NEXT_GEN`
    rather than the whole fountain category, because these values came out of
    one model's firmware and a Bluetooth EverSweet has none of those jobs.

    Defaults to the litter table, which is what every caller assumed back when
    there was only one.
    """
    if (device_type or "").lower() in FOUNTAIN_NEXT_GEN:
        return FOUNTAIN_W7H_WORK_MODES
    return WORK_MODES

#: workProcess is two digits: the tens digit is the phase, the units digit a
#: detail (22 means paused with a safeWarn reason).
WORK_PROCESS_PHASE: dict[int, str] = {
    1: "active",
    2: "paused",
    3: "resetting",
    4: "paused",
}

SAFE_WARN: dict[int, str] = {
    0: "pet approaching",
    1: "pet entered",
    3: "cover opened",
}


# --- NS6: feeder dispensing sub-fields -------------------------------------

FEED_SRC: dict[int, str] = {
    1: "scheduled",
    3: "manual (app)",
    4: "manual (button)",
}

#: Keyed by `err_code`; the parallel `result` values are 0, 8 and "other".
#:
#: 8 is INFERRED and rests on a single observation, so it is worded for what was
#: seen rather than for what it might mean. A D4SH answered a `feed_realtime`
#: carrying `amount` — a field that model does not read — with a completed feed
#: cycle, `real_amount1: 0`, `real_amount2: 0` and `err_code: 8` (issue #2).
#: The owner then watched every later feed and never saw 8 again, including the
#: ones that dispensed normally. So it accompanies a feed that put out nothing;
#: whether it names the cause or only the outcome is not settled.
#: Keyed by `content.result` in a feed_over event. NOT `err_code` — the two are
#: parallel fields that carry different things (`err_code` names the fault,
#: `result` names the outcome).
#:
#: Grading per value:
#:   0  confirmed — normal completion, amounts > 0
#:   3  inferred — co-occurs with err_code=4 and zero amounts (capture 2026-08-11)
#:   7  inferred — co-occurs with err_code=7 (two captures)
#:   8  inferred — co-occurs with err_code=8, single observation (issue #2)
#:   9  inferred — firmware sets real_amount=-1 and skips the motor (disasm
#:      0x426e40), every capture has food1=0/food2=0
#:  10  inferred — every capture carries surplus_standard=1, firmware reads a
#:      config field before the branch (disasm 0x427be0), likely surplus control
#:  11  unverified — observed on a live D4SH, not yet traced in disassembly
FEED_RESULT: dict[int, str] = {
    0: "completed",
    3: "blocked (outlet obstructed)",
    7: "failed (error 7)",
    8: "failed (nothing dispensed)",
    9: "skipped (hoppers empty)",
    10: "skipped (bowl not empty)",
    11: "failed (empty run)",
}


# --- NS7: err{} fault bits -------------------------------------------------
# A state report carries an `err` OBJECT of named 0/1 bits, and the names are
# per device family exactly as NS1's numeric codes are. Without a table the
# Error sensor reads `taryF,cycL` — the device's own abbreviations, which say
# nothing to the person whose fountain has stopped.
#
# Only what a source names is listed. A bit with no entry falls back to its raw
# name rather than being dropped, so an unknown fault is still visible.

#: W7H (EverSweet Ultra AI). Two groups, one table, because both reach the same
#: Error sensor.
#:
#: The first eighteen are the `err{}` bits of a `property/post`. That set is
#: closed: the key list the payload builder walks is a single literal run in
#: W7H 456 `ctrl`, and it holds exactly these eighteen names — matching, name
#: for name, the `err` block of a real capture from the same device.
#:
#: The last seven never appear in `err{}` at all. They live in a separate run
#: (`cameraN|tankCU|tankDU|tankCL|tankDF|ptcU|tankL`) and are the content
#: strings an `error_start` EVENT carries. All twenty-five are referenced from
#: one code-to-name mapper, which is what says they are one vocabulary.
#:
#: The suffixes are regular, which is how the tank ones are readable at all:
#: L = missing/not detected, M = malfunction, U = not installed, F = full,
#: O = removed, D = detection. C is the clean tank and D the dirty one — the
#: same C/D split as the hall switches.
#:
#: Note the firmware's own spelling: "tary", not "tray". Kept verbatim, because
#: the key has to match what the device sends.
FOUNTAIN_W7H_ERROR_FLAGS: dict[str, str] = {
    # `err{}` bits — the closed set a property/post can carry
    "DC": "DC power fault",
    "mcu": "MCU communication fault",
    "rtc": "Clock fault",
    "cameraL": "Camera offline",
    "cameraE": "Camera error",
    "taryD": "Tray detection fault",
    "taryL": "Tray missing",
    "taryF": "Tray full",
    "taryO": "Tray removed",
    "ptcL": "Heater not detected",
    "ptcM": "Heater fault",
    "valveL": "Valve did not arrive",
    "valveE": "Valve error",
    "valveN": "Valve timeout",
    "cycL": "Circulation pump stalled",
    "cycM": "Circulation pump fault",
    "repL": "Refill pump stalled",
    "repM": "Refill pump fault",
    # `error_start` content strings — never bits, only events
    "tankCU": "Clean water tank not installed",
    "tankDU": "Waste tank not installed",
    "tankCL": "Clean water tank low",
    "tankDF": "Waste tank full",
    "ptcU": "Heater not installed",
    # Same table, no suffix that the others explain. Rendered rather than
    # dropped, but named as little as the evidence allows.
    "cameraN": "Camera fault (cameraN)",
    "tankL": "Tank level low (tankL)",
}

#: Wire names the W7H's `ctrl` registers a settings handler for, from the
#: reverse-engineered map supplied 2026-07-31 (`wire_to_set_handler`).
#:
#: This is the SET side of the protocol, and it is worth writing down because
#: nothing on our side can detect getting it wrong. A `property.set` naming a
#: field the firmware has no handler for is accepted by the broker, delivered,
#: and silently dropped by the device — no error, no reply, no change. From
#: here that is indistinguishable from a device that is not listening, which is
#: exactly the report this list exists to make impossible.
#:
#: It is also why `pause_fountain`/`resume_fountain` are not published for a
#: W7H: they wrote `power`, which is absent here.
#:
#: The map gives the wire name and the handler it dispatches to; only the wire
#: name matters to us, so only that is kept. Five differ from the handler
#: (`timeDisplay`->`timestamp_enable`, `microphone`->`mic_enable`,
#: `camera`->`camera_enable`, `night`->`irlight_enable`,
#: `logSwitch`->`log_upload`), which is precisely the trap: the handler name is
#: NOT what goes on the wire.
FOUNTAIN_W7H_SET_FIELDS = frozenset({
    "addWaterMode", "addWaterSwitch", "autoFlush", "autoWaterChange",
    "awDisturbMode", "camera", "cleanWaterEmptyLight", "cleanWaterLackLight",
    "disturbMode", "drinkDetection", "flushCycle", "flushIntensity",
    "flushTime", "fountainMode", "fountainTime", "heaterSwitch", "language",
    "lightAssist", "lightMode", "logSwitch", "manualLock", "microLight",
    "microphone", "night", "petDetection", "sleepTime", "smartFrame",
    "systemSoundEnable", "timeDisplay", "timezone", "toneMode", "upload",
    "volume", "vomitDetection", "wasteWaterFullLight", "waterChangeCycle",
    "waterChangeTime", "wifiLightAssist", "wlDisturbMode",
})

#: Family -> that family's flag names, most specific first.
#:
#: Keyed on a codename set, not a category. The one table we have describes the
#: W7H's mechanism — tray, lift valve, two tanks, two pumps — so answering a
#: W5's `err{}` out of it would name parts that fountain does not have. Litter
#: boxes and feeders send an `err` object too, but no source names their bits,
#: so they are absent here and their flags render raw: the honest state, and
#: the reason the lookup below falls back rather than raising.
#: D4SH (camera feeder) error flags, from the `err{}` object in a state report.
#: Confirmed on a live D4SH (fw 248, capture 2026-08-11): every state report
#: carries these 7 keys (plus `serial` when the MCU serial link faults).
FEEDER_CAMERA_ERROR_FLAGS: dict[str, str] = {
    "DC": "DC power fault",
    "sys": "System fault",
    "rtc_c": "Clock fault",
    "moto": "Motor fault",
    "blk_f": "Food outlet blocked",
    "blk_d": "Dispensing blocked",
    "camera": "Camera fault",
    "serial": "Serial communication fault",
}

ERROR_FLAGS: tuple[tuple[frozenset[str], dict[str, str]], ...] = (
    (FEEDER_CAMERA, FEEDER_CAMERA_ERROR_FLAGS),
    (FOUNTAIN_NEXT_GEN, FOUNTAIN_W7H_ERROR_FLAGS),
)


def error_flag_label(flag: str, device_type: str | None = None) -> str:
    """Human label for one `err{}` bit, or the raw flag name if none is known.

    Per device family, for the same reason NS1 is per category: nothing
    guarantees two models spell the same fault the same way, and a wrong label
    is worse than an untranslated one.
    """
    codename = (device_type or "").lower()
    for members, table in ERROR_FLAGS:
        if codename in members:
            return table.get(flag, flag)
    return flag


# --- derived views ---------------------------------------------------------
# Computed once at import so hot paths do set membership rather than dataclass
# attribute walks. Only the four below have readers; a set nothing asks for is
# a table that can drift from `EventCode` without anything noticing, so derive
# a new one where it is needed rather than pre-computing it here.

def _codes_where(table: dict[str, EventCode], **match: object) -> frozenset[str]:
    """Keys of `table` whose `EventCode` matches every attribute in `match`."""
    return frozenset(
        key for key, code in table.items()
        if all(getattr(code, attr) == value for attr, value in match.items())
    )


#: Every key in either namespace, for the derived sets below and for callers
#: that only need "is this code a detail step?" rather than its meaning.
#:
#: MQTT names cannot collide with numeric codes, but HTTP codes DO collide
#: across categories, so the litter table wins here -- see
#: `DEFAULT_HTTP_CATEGORY`. Anything that needs a code's meaning must go
#: through `lookup(event_type, device_type)` instead of reading this.
ALL_EVENT_CODES: dict[str, EventCode] = {
    **MQTT_EVENT_TOPICS,
    **FEEDER_HTTP_CODES,
    **FOUNTAIN_HTTP_CODES,
    **LITTER_HTTP_CODES,
}

DETAIL_CODES = _codes_where(ALL_EVENT_CODES, detail=True)
ANCHOR_CODES = _codes_where(ALL_EVENT_CODES, anchor=True)
DONE_CODES = _codes_where(ALL_EVENT_CODES, role=ROLE_DONE)
#: Completion steps a user actually sees. A cycle emits several `role=DONE`
#: steps (a cleaning cycle closes with both a light cycle and the cleaning
#: itself), so "the completion" means the one that is not folded away as
#: detail -- it is the line that heads an unparented cycle's card and the one
#: that carries the episode's waste photos.
PRIMARY_DONE_CODES = DONE_CODES - _codes_where(ALL_EVENT_CODES, detail=True)


def lookup(event_type: str | None,
           device_type: str | None = None) -> EventCode | None:
    """The `EventCode` for a raw event_type, or None if we do not know it.

    Args:
        event_type: A numeric HTTP code as a string, or an MQTT topic name.
            The two cannot collide, so one call resolves either namespace.
        device_type: The reporting device's codename. **Supply it whenever you
            have one.** HTTP codes mean different things per category -- 2 is
            `feed_over` on a feeder and `err_over` on a litter box -- so
            omitting it falls back to `DEFAULT_HTTP_CATEGORY`, which is right
            for a litter box and wrong for a feeder.

    An unrecognised codename does NOT drop the event: it falls back to the
    default table, because labelling a device we have not classified is better
    than showing its owner a bare code.
    """
    if not event_type:
        return None
    key = str(event_type)

    category = category_of(device_type) or DEFAULT_HTTP_CATEGORY
    code = HTTP_CODES_BY_CATEGORY.get(category, {}).get(key)
    if code is not None:
        return code
    # A category with a sparse table (the feeder's, mostly unrecovered) still
    # gets MQTT names, which ARE global.
    return MQTT_EVENT_TOPICS.get(key.lower())


def codes_for(device_type: str | None) -> dict[str, EventCode]:
    """Every code a given device codename can emit, both namespaces.

    Used by the panel to describe what a device *could* report, and by the
    tests to assert the tables stay complete. Unlike `lookup`, this filters
    strictly by family -- it answers "what should I expect?", not "what is
    this?", and a wrong family guess must never cost a user their event.
    """
    if not device_type:
        return dict(ALL_EVENT_CODES)
    codename = device_type.lower()
    category = category_of(codename) or DEFAULT_HTTP_CATEGORY
    table = {**MQTT_EVENT_TOPICS, **HTTP_CODES_BY_CATEGORY.get(category, {})}
    return {key: code for key, code in table.items()
            if codename in code.families}
