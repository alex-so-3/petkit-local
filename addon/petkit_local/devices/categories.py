"""Catalogue mapping each device category to its HA entities and MQTT topics.

PetKit ships many device codenames but only four behavioural families: litter
box, feeder, water fountain, Pura Air spray. Everything a family contributes to
Home Assistant -- which entity lists it publishes, which extra ones a
camera-equipped model adds, and which MQTT event topics carry its state -- used
to live in four near-identical modules (`litter.py`, `feeder.py`,
`water_fountain.py`, `purifier.py`) that had already drifted apart in ordering
and signature. This module replaces them with one table so that supporting a
new family is a `CATEGORY_SPECS` entry, not a fifth copy of the same skeleton.

The table is deliberately data, not code: the entity set it produces is the
user's Home Assistant state. A reordered or renamed entry orphans entities in
live installations and loses their recorded history, so `CategorySpec` composes
existing lists in a fixed order and never rewrites them.
"""
from __future__ import annotations

from dataclasses import dataclass

from petkit_local.devices.base import Device
from petkit_local.ha.discovery import EntityDef
from petkit_local.ha.entities.buttons import (
    FEEDER_BUTTONS, FEEDER_DUAL_BUTTONS,
    FOUNTAIN_BUTTONS, FOUNTAIN_W7H_BUTTONS, LITTER_BUTTONS,
)
from petkit_local.ha.entities.camera import CAMERA_ENTITIES
from petkit_local.ha.entities.events import (
    FEEDER_EVENTS, FOUNTAIN_W7H_EVENTS, LITTER_EVENTS,
)
from petkit_local.ha.entities.numbers import (
    FEEDER_CAMERA_NUMBERS, FEEDER_DUAL_NUMBERS, FEEDER_NUMBERS, FOUNTAIN_NUMBERS,
    LITTER_CAMERA_NUMBERS, LITTER_NUMBERS,
)
from petkit_local.ha.entities.selects import FEEDER_SELECTS, FOUNTAIN_SELECTS, LITTER_SELECTS
from petkit_local.ha.entities.sensors import (
    FEEDER_BINARY_SENSORS, FEEDER_NEXT_GEN_HALL_SENSORS,
    FEEDER_NEXT_GEN_SENSORS, FEEDER_SENSORS,
    FOUNTAIN_BINARY_SENSORS, FOUNTAIN_SENSORS,
    FOUNTAIN_W7H_BINARY_SENSORS, FOUNTAIN_W7H_HALL_SENSORS, FOUNTAIN_W7H_SENSORS,
    LITTER_BINARY_SENSORS, LITTER_CAMERA_HALL_SENSORS, LITTER_CAMERA_SENSORS,
    LITTER_SENSORS,
    PURIFIER_BINARY_SENSORS, PURIFIER_SENSORS,
)
from petkit_local.ha.entities.switches import (
    CAPABILITY_SWITCHES,
    FEEDER_CAMERA_SWITCHES, FEEDER_SWITCHES,
    FOUNTAIN_SWITCHES, FOUNTAIN_W7H_SWITCHES,
    LITTER_CAMERA_SWITCHES, LITTER_SWITCHES,
    PURIFIER_SWITCHES,
)
from petkit_local.ha.entities.text import FEEDER_SCHEDULE_TEXT, LITTER_SCHEDULE_TEXT
from petkit_local.utils.const import (
    DEVICE_TYPES_FEEDER, DEVICE_TYPES_LITTER, DEVICE_TYPES_PURIFIER,
    DEVICE_TYPES_WATER_FOUNTAIN,
)

#: Every camera-capable category ends its camera bundle with the same trio of
#: camera/snapshot/clip entities plus the media-capability toggles; only the
#: leading per-category switches differ. Order is load-bearing (see module
#: docstring), which is why this is appended, never merged in.
_COMMON_CAMERA_ENTITIES: tuple[EntityDef, ...] = (*CAMERA_ENTITIES, *CAPABILITY_SWITCHES)


@dataclass(frozen=True)
class CategorySpec:
    """What one device category publishes to Home Assistant.

    Invariant: `entities` and `camera_entities` are in HA discovery order and
    that order is part of the contract -- entity identity is derived from
    `EntityDef.key`, so appending is safe but reordering or renaming breaks
    existing installations.

    `device_types` is the set of device codenames belonging to this category,
    shared with `utils/const.py` so there is exactly one such list per family.
    """

    device_types: frozenset[str]
    entities: tuple[EntityDef, ...]
    state_topics: tuple[str, ...]
    #: Extra entities a camera-equipped model of this category adds. Empty for
    #: categories with no camera model (the Pura Air spray is BLE-only).
    camera_entities: tuple[EntityDef, ...] = ()
    #: Extra MQTT event topics a camera-equipped model of this category emits.
    camera_state_topics: tuple[str, ...] = ()
    #: `(codename, entities)` for a model that publishes more than its category.
    #: Appended last, so the shared list keeps its positions.
    #:
    #: A category is a BEHAVIOUR family, not a hardware one. The W7H drinks,
    #: detects pets and pours water like every other fountain, so it belongs
    #: here — but it reports a sewage tank, a lift valve and ten hall switches
    #: that no other fountain has. Pairs of tuples rather than a dict so the
    #: dataclass stays frozen and hashable, and so the order is written down.
    model_entities: tuple[tuple[str, tuple[EntityDef, ...]], ...] = ()
    #: `(codename, topic suffixes)` a model emits that its category does not.
    #: The topic-side twin of `model_entities`, and needed for the same reason:
    #: the W7H runs water-treatment jobs no other fountain has, so it reports
    #: events they never send.
    model_state_topics: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: `(codename, entity keys)` a model must NOT publish, for a field its
    #: hardware or firmware never reports.
    #:
    #: The alternative is worse than it looks: an entity nothing can fill is
    #: published, reads unknown forever, and is indistinguishable from a device
    #: that has not reported yet — which is the failure
    #: `tests/test_entity_backing.py` exists to catch. Excluding is not the same
    #: as deleting: the entity stays real for every other model in the family.
    model_excludes: tuple[tuple[str, frozenset[str]], ...] = ()

    def entities_for(self, has_camera: bool, device_type: str = "") -> list[EntityDef]:
        """HA entity definitions for one device, in discovery order.

        Returns a fresh list per call because callers treat it as their own.

        `device_type` is optional so a caller asking what a CATEGORY publishes
        still gets the shared answer; pass it to get one model's real list.
        """
        codename = device_type.lower()
        excluded: set[str] = set()
        for model, keys in self.model_excludes:
            if model == codename:
                excluded |= set(keys)

        entities = [e for e in self.entities if e.key not in excluded]
        if has_camera:
            entities.extend(e for e in self.camera_entities if e.key not in excluded)
        for model, extra in self.model_entities:
            if model == codename:
                entities.extend(extra)
        return entities

    def state_topics_for(self, has_camera: bool, device_type: str = "") -> list[str]:
        """MQTT event topic suffixes this device reports state on.

        `device_type` is optional for the same reason it is on `entities_for`:
        a caller asking what a CATEGORY reports still gets the shared answer.
        """
        codename = device_type.lower()
        topics = list(self.state_topics)
        if has_camera:
            topics.extend(self.camera_state_topics)
        for model, extra in self.model_state_topics:
            if model == codename:
                topics.extend(extra)
        return topics


#: Category name -> spec. Iteration order is the resolution order used by
#: `spec_for_device`, so a codename listed in two categories resolves to the
#: first -- litter, feeder, fountain, spray -- exactly as the if-chain this
#: table replaced did.
CATEGORY_SPECS: dict[str, CategorySpec] = {
    "litter": CategorySpec(
        device_types=frozenset(DEVICE_TYPES_LITTER),
        entities=(
            *LITTER_SENSORS,
            *LITTER_BINARY_SENSORS,
            *LITTER_SWITCHES,
            *LITTER_BUTTONS,
            *LITTER_NUMBERS,
            *LITTER_SELECTS,
            *LITTER_EVENTS,
            *LITTER_SCHEDULE_TEXT,
        ),
        # The halls go LAST, after the common bundle: appending is the only
        # change to this tuple that cannot move an existing entity.
        camera_entities=(*LITTER_CAMERA_SENSORS, *LITTER_CAMERA_SWITCHES,
                         *LITTER_CAMERA_NUMBERS, *_COMMON_CAMERA_ENTITIES,
                         *LITTER_CAMERA_HALL_SENSORS),
        state_topics=(
            "work_start", "work_continue", "work_suspend",
            "clean_over", "dump_over", "reset_over",
            "pet_in", "pet_out",
            "error_start", "error_over",
            "property/post", "data_get/post",
            "ble_response/post",
        ),
        camera_state_topics=("move_detect", "pet_detect"),
        # T6 has no deodorant/spray cartridge system at all, unlike T5 — no
        # N50, no N60, nothing for either to report or control.
        model_excludes=(
            ("t6", frozenset({
                "n50_durability", "n60_spray_days", "waste_bin_present",
                "deodorization_running", "auto_spray", "fixed_time_spray",
                "deep_spray", "deodorize", "reset_n50",
            })),
        ),
    ),
    "feeder": CategorySpec(
        device_types=frozenset(DEVICE_TYPES_FEEDER),
        entities=(
            *FEEDER_SENSORS,
            *FEEDER_BINARY_SENSORS,
            *FEEDER_SWITCHES,
            *FEEDER_BUTTONS,
            *FEEDER_NUMBERS,
            *FEEDER_SELECTS,
            *FEEDER_EVENTS,
            *FEEDER_SCHEDULE_TEXT,
        ),
        camera_entities=(*FEEDER_CAMERA_SWITCHES, *FEEDER_CAMERA_NUMBERS,
                         *_COMMON_CAMERA_ENTITIES),
        state_topics=(
            "feed_start", "feed_stop", "feed_over",
            "property/post", "data_get/post",
            "ble_response/post",
        ),
        camera_state_topics=(
            "eat_start", "eat_over",
            "move_detect", "pet_detect",
        ),
        model_entities=(
            # The embedded-Linux feeders report a dozen fields the ESP32 ones
            # do not; `state_parsers.FEEDER_NEXT_GEN_FIELDS` is what fills them.
            ("d4h", (*FEEDER_NEXT_GEN_SENSORS, *FEEDER_NEXT_GEN_HALL_SENSORS)),
            ("d4sh", (*FEEDER_NEXT_GEN_SENSORS, *FEEDER_NEXT_GEN_HALL_SENSORS,
                      *FEEDER_DUAL_BUTTONS, *FEEDER_DUAL_NUMBERS)),
        ),
        # Four controls a D4SH cannot fill, and the reason is the same for each:
        # the field behind it is not in either of the two real reports in issue
        # #2, nor a key in that firmware's state builder. They come from the
        # reference integration's CLOUD model, which is PetKit's account-side
        # view — a different vocabulary, not a different spelling.
        #
        # `food_low` and `food_in_bowl` are replaced by real entities above:
        # this model says `food1`/`food2`, and its bowl reading is a surplus
        # measurement with no unit anyone can name, not grams and not a
        # percentage. `battery_installed` reads `batteryPower`, where the device
        # sends `batV`/`ubat`.
        model_excludes=(
            ("d4sh", frozenset({
                "food_low", "food_in_bowl", "food_bowl_pct", "battery_installed",
            })),
            # A D4H reports one hopper, so its second-hopper sensor never fills.
            # Everything else on the exclude list applies to it too — the two
            # models share a `ctrl` and a state builder.
            ("d4h", frozenset({
                "food_low", "food_in_bowl", "food_bowl_pct", "battery_installed",
                "hopper2_level",
            })),
        ),
    ),
    # Of the five fountain codenames, only `w7h` can ever reach this table: the
    # other four have no WiFi (`utils/const.py::DEVICE_TYPES_BLE_ONLY`) and
    # therefore never become a `Device` at all. Their shared entity lists are
    # kept rather than deleted — they are what a fountain with WiFi would
    # publish, and PetKit could ship one — but nothing has filled them and
    # nothing will until such a fountain turns up. Treat their field names as
    # cloud-API guesses, not as anything a device has been seen to send.
    "fountain": CategorySpec(
        device_types=frozenset(DEVICE_TYPES_WATER_FOUNTAIN),
        entities=(
            *FOUNTAIN_SENSORS,
            *FOUNTAIN_BINARY_SENSORS,
            *FOUNTAIN_SWITCHES,
            *FOUNTAIN_BUTTONS,
            *FOUNTAIN_NUMBERS,
            *FOUNTAIN_SELECTS,
        ),
        # No fountain-specific camera switches exist; the W7H publishes only
        # the common bundle.
        camera_entities=_COMMON_CAMERA_ENTITIES,
        state_topics=("drink_start", "drink_over",
                      "property/post", "data_get/post"),
        # A real W7H sends all three (capture 2026-07-31), and it is the only
        # fountain with a camera, so they belong on the camera bundle rather
        # than the shared list.
        camera_state_topics=("pet_detect", "pet_discern"),
        model_entities=(
            ("w7h", (*FOUNTAIN_W7H_SENSORS, *FOUNTAIN_W7H_BINARY_SENSORS,
                     *FOUNTAIN_W7H_HALL_SENSORS, *FOUNTAIN_W7H_SWITCHES,
                     *FOUNTAIN_W7H_BUTTONS, *FOUNTAIN_W7H_EVENTS)),
        ),
        # The water-treatment jobs. A live W7H sent `work_start` (2026-08-01)
        # and `add_water_over` a second after a `drink_start`; neither is a
        # thing any other EverSweet does, so neither belongs on the shared list.
        model_state_topics=(
            ("w7h", ("work_start", "add_water_over")),
        ),
        # Everything the W7H cannot fill. The list above is what it reports
        # instead; between the two, the fountain family covers both hardware
        # generations without either pretending to be the other.
        #
        # Evidence for each exclusion is one real `property/post` (2026-07-31)
        # plus the reverse-engineered field map for the same firmware: the
        # payload carries 42 keys and not one of these is among them. Their
        # names come from the reference integration's CLOUD model, which is
        # PetKit's account-side view of the Bluetooth fountains — a different
        # device generation, not a different spelling.
        model_excludes=(
            ("w7h", frozenset({
                # No `workState` in the payload at all. Publishing it meant a
                # Device Status of 0, which the device never sent and which
                # decodes as a real mode.
                "device_status",
                # No filter and no battery in this hardware.
                "filter_percent", "filter_days", "battery", "low_battery",
                "replace_filter", "reset_filter",
                # Not reported. The W7H says the same things with `cwtState`,
                # the clean-tank halls and its `err{}` bits.
                "water_lack",
                # `detectStatus` is absent; presence arrives as `pet_detect`
                # events and the `lastPetDetect` timestamp instead.
                "pet_detected",
                # `drinkTime` here is a TIMESTAMP under `device{}`, not the
                # count this sensor renders. Replaced by `last_drink`.
                "drink_times",
                # Both wrote `power` through `property.set`, and `power` is not
                # among that firmware's set handlers — so they were writing a
                # field nothing reads.
                #
                # It is a SERVICE, though: `parse_service_invoke_msg` accepts
                # `type: "power"` with a `power_action` of 0 or 1. That is the
                # device off and on, not a running job paused, so these two
                # stay excluded; what replaced them are the three job buttons
                # in `FOUNTAIN_W7H_BUTTONS`, which use the `start` service.
                "pause_fountain", "resume_fountain",
            })),
        ),
    ),
    "purifier": CategorySpec(
        device_types=frozenset(DEVICE_TYPES_PURIFIER),
        entities=(
            *PURIFIER_SENSORS,
            *PURIFIER_BINARY_SENSORS,
            *PURIFIER_SWITCHES,
        ),
        # K2/K3 are BLE-only accessories with no camera, so the camera bundle
        # stays empty rather than the signature differing from its siblings.
        state_topics=("property/post",),
    ),
}


def spec_for_device(device: Device) -> CategorySpec | None:
    """Category spec for a device, or None for a codename we don't classify.

    Unknown codenames are supported everywhere else (they register, connect
    and heartbeat); they simply publish no entities, so returning None here
    rather than raising keeps an unrecognised device usable.
    """
    for spec in CATEGORY_SPECS.values():
        if device.device_type in spec.device_types:
            return spec
    return None
